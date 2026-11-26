"""
Abuse controls in front of the gRPC execution service (H07).

[`rpc_guard.RpcGuard`] is a server interceptor that runs before a request message is
even decoded. For every unary call it:

1. refuses a connection from outside `NEST_GRPC_ALLOWED_PEERS`, when set, with
   `PERMISSION_DENIED` (`origin_not_allowed`), so a client that reaches the
   Controller's port directly instead of through the gateway is served nothing;
2. works out the client address: the TCP peer, or, only when the peer is one of
   `NEST_GRPC_TRUSTED_PROXIES`, the nearest untrusted address in the gateway's
   `x-forwarded-for` (any other `x-forwarded-for` is ignored, so a client cannot
   choose its own address);
3. applies the per-address rate limit (`NEST_GRPC_RATE_LIMIT_PER_PEER`), answering
   `RESOURCE_EXHAUSTED` with `nest-error-kind: rate_limited`;
4. only then decodes the request, answering a malformed one `INVALID_ARGUMENT`.

Authentication, the per-User rate limit ([`rpc_guard.RpcGuard.allow_user`]) and
authorization follow in [`controller.ExecutionService`], before any file is staged
or any SSH or Slurm command runs. gRPC itself bounds each message (after
decompression), the request metadata, and the number of RPCs running or queued
(see [`rpc_guard.server_options`]).

Rate limits are token buckets kept in this process: each replica limits on its
own, and the number of addresses tracked is bounded, so a flood of addresses
cannot grow memory without limit. Nothing here logs a header value or a token.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from typing import Any
from urllib.parse import unquote

import grpc
from google.protobuf.message import DecodeError

from ._logging import client_context, current_client, get_logger
from .config import Network, RpcPolicy
from .errors import ERROR_KIND_METADATA

logger = get_logger("nest.rpc_guard")

__all__ = [
    "FORWARDED_FOR_METADATA",
    "RATE_LIMITED_KIND",
    "RpcGuard",
    "TokenBuckets",
    "canonical_address",
    "client_address",
    "current_client",
    "peer_address",
    "server_options",
]

FORWARDED_FOR_METADATA = "x-forwarded-for"
RATE_LIMITED_KIND = "rate_limited"
ORIGIN_KIND = "origin_not_allowed"
MAX_TRACKED_KEYS = 10_000
"""Most rate-limit buckets kept at once; the least recently used is dropped first."""

Address = IPv4Address | IPv6Address
_MAPPED = IPv6Network("::ffff:0:0/96")
"""The IPv4-mapped IPv6 addresses (RFC 4291, section 2.5.5.2)."""


def server_options(policy: RpcPolicy) -> list[tuple[str, Any]]:
    """
    The gRPC server options that bound one Controller.

    - Messages in both directions are limited to `max_message_bytes`, measured
      after decompression, so a small compressed request cannot expand past it.
    - Request metadata is limited to `max_metadata_bytes`.
    - The HTTP/2 PING policy is explicit (H06 contract): a PING without data is
      accepted every 20 seconds, which covers the SDK's planned 30 to 60 second
      keepalive, and no keepalive is accepted without a call.
    """
    return [
        ("grpc.so_reuseport", 0),
        ("grpc.max_receive_message_length", policy.max_message_bytes),
        ("grpc.max_send_message_length", policy.max_message_bytes),
        ("grpc.max_metadata_size", policy.max_metadata_bytes),
        ("grpc.http2.min_recv_ping_interval_without_data_ms", 20_000),
        ("grpc.http2.max_ping_strikes", 2),
        ("grpc.keepalive_permit_without_calls", 0),
    ]


def canonical_address(address: Address) -> Address:
    """
    `address` with an IPv4-mapped IPv6 address (`::ffff:10.0.0.3`) replaced by its
    IPv4 address (`10.0.0.3`).

    A Controller listening on a dual-stack socket sees an IPv4 client as the mapped
    IPv6 address, so without this an IPv4 allowlist or trusted proxy would never
    match it. Every other address, including ordinary IPv6, is returned unchanged.
    """
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def peer_address(peer: str | None) -> Address | None:
    """
    The IP address of a gRPC peer string, or `None` when it names none.

    gRPC renders the peer as a URI: `ipv4:1.2.3.4:5`, and `ipv6:%5B::1%5D:5`
    (gRPC 1.5x and later) or `ipv6:[::1]:5`. An IPv6 zone (`%25eth0` or `%eth0`) is
    dropped and an IPv4-mapped IPv6 address becomes its IPv4 address
    ([`rpc_guard.canonical_address`]). Anything else, such as `unix:` or a malformed
    string, gives `None`, which an allowlist refuses.
    """
    if not peer:
        return None
    scheme, _, rest = peer.partition(":")
    if scheme == "ipv4":
        host = rest.rpartition(":")[0]
    elif scheme == "ipv6":
        host = unquote(rest).rpartition(":")[0]
        if not (host.startswith("[") and host.endswith("]")):
            return None
        host = host[1:-1]
    else:
        return None
    try:
        return canonical_address(ip_address(host.split("%", 1)[0]))
    except ValueError:
        return None


def _within(address: Address, networks: Iterable[Network]) -> bool:
    """
    Whether `address` is in one of `networks`. An IPv4 address and its IPv4-mapped
    form are the same address, and a network written in mapped form
    (`::ffff:10.0.0.0/104`) is the IPv4 network it maps. No other IPv6 network
    matches an IPv4 address, so a wide IPv6 entry such as `::/0` admits no IPv4 peer.
    """
    address = canonical_address(address)
    for network in networks:
        if isinstance(network, IPv6Network) and network.subnet_of(_MAPPED):
            network = IPv4Network(
                (int(network.network_address) & 0xFFFFFFFF, network.prefixlen - 96)
            )
        if address.version == network.version and address in network:
            return True
    return False


def client_address(
    peer: Address | None,
    metadata: Iterable[tuple[str, str | bytes]],
    trusted_proxies: tuple[Network, ...],
) -> str:
    """
    The address a call came from.

    `x-forwarded-for` is read only when the TCP peer is a trusted proxy, and then
    from the right: the first address that is not itself a trusted proxy is the
    client. Several headers, or an unparsable entry before an untrusted one, fall
    back to the peer, so a client never picks the address it is limited by.
    """
    if peer is None:
        return "unknown"
    peer = canonical_address(peer)
    if not trusted_proxies or not _within(peer, trusted_proxies):
        return str(peer)
    values = [
        value.decode(errors="replace") if isinstance(value, bytes) else value
        for name, value in metadata
        if name.lower() == FORWARDED_FOR_METADATA
    ]
    if len(values) != 1:
        return str(peer)
    for item in reversed(values[0].split(",")):
        try:
            hop = canonical_address(ip_address(item.strip()))
        except ValueError:
            return str(peer)
        if not _within(hop, trusted_proxies):
            return str(hop)
    return str(peer)


class TokenBuckets:
    """
    Per-key token buckets: `rate` calls per minute, with a burst of a tenth of a
    minute's worth (at least 5). A rate of `0` allows everything.
    """

    def __init__(
        self,
        rate: int,
        *,
        max_keys: int = MAX_TRACKED_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.rate = rate
        self.capacity = float(max(5, rate // 10))
        self.per_second = rate / 60.0
        self.max_keys = max_keys
        self.clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> float | None:
        """
        Takes one token for `key`.

        Returns:
            `float | None`: `None` when allowed, else the seconds until a token is free.
        """
        if self.rate <= 0:
            return None
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.pop(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.per_second)
            allowed = tokens >= 1.0
            if allowed:
                tokens -= 1.0
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self.max_keys:
                self._buckets.popitem(last=False)
        return None if allowed else (1.0 - tokens) / self.per_second

    def __len__(self) -> int:
        return len(self._buckets)


def _abort_limited(context, message: str, retry_after: float) -> None:
    context.set_trailing_metadata(
        ((ERROR_KIND_METADATA, RATE_LIMITED_KIND), ("retry-after", str(max(1, round(retry_after)))))
    )
    context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, message)


class RpcGuard(grpc.ServerInterceptor):
    """
    Origin, address and rate checks before a request is decoded. See the module
    documentation.

    Args:
        policy ([`config.RpcPolicy`]): The listener's bounds.
        clock (`Callable[[], float]`, *optional*): Monotonic clock, for tests.
    """

    def __init__(self, policy: RpcPolicy, *, clock: Callable[[], float] = time.monotonic):
        self.policy = policy
        self.peers = TokenBuckets(policy.peer_rate, clock=clock)
        self.users = TokenBuckets(policy.user_rate, clock=clock)

    def allow_user(self, context, user_id: object) -> None:
        """Applies the per-User rate limit after authentication; aborts when exceeded."""
        retry = self.users.allow(str(user_id))
        if retry is not None:
            logger.warning("RPC rate-limited scope=user client=%s", current_client())
            _abort_limited(
                context,
                "Too many calls from your Nest account; nothing was run. Retry in "
                f"{max(1, round(retry))} seconds",
                retry,
            )

    def _admit(self, context) -> str:
        """Runs the pre-decoding checks; returns the client address or aborts."""
        peer = peer_address(context.peer())
        allowed = self.policy.allowed_peers
        if allowed and (peer is None or not _within(peer, allowed)):
            logger.warning("RPC refused: connection from outside NEST_GRPC_ALLOWED_PEERS")
            context.set_trailing_metadata(((ERROR_KIND_METADATA, ORIGIN_KIND),))
            context.abort(
                grpc.StatusCode.PERMISSION_DENIED,
                "This Controller accepts calls only through its gateway",
            )
        client = client_address(peer, context.invocation_metadata(), self.policy.trusted_proxies)
        retry = self.peers.allow(client)
        if retry is not None:
            logger.warning("RPC rate-limited scope=address client=%s", client)
            _abort_limited(
                context,
                f"Too many calls from this address; nothing was run. Retry in "
                f"{max(1, round(retry))} seconds",
                retry,
            )
        return client

    def intercept_service(self, continuation, handler_call_details):
        handler = continuation(handler_call_details)
        if handler is None or handler.unary_unary is None:
            return handler
        behavior = handler.unary_unary
        decode = handler.request_deserializer

        def guarded(raw: bytes, context):
            client = self._admit(context)
            try:
                request = decode(raw) if decode is not None else raw
            except DecodeError, ValueError:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Malformed request message")
                raise AssertionError("context.abort raises") from None
            with client_context(client):
                return behavior(request, context)

        return grpc.unary_unary_rpc_method_handler(
            guarded,
            request_deserializer=None,
            response_serializer=handler.response_serializer,
        )
