"""
Synthetic reference model of the future D03 restricted opaque SSH relay.

**Not a production relay and not wired into the Controller.** It pins down, as
executable code that the fixture tests exercise, the boundaries a real
implementation must keep (see `relay.proto` and docs/source/ssh-relay-feasibility.md):

- The client names only a Platform (plus Project and attempt IDs). There is no
  host, port or address field; the destination comes from the server-side login-node
  allowlist. Unknown fields are rejected, so no generic TCP/SOCKS forwarding exists.
- Authorization (D01 policy, B07 RBAC) runs before any DNS lookup or socket. A
  denial is final: there is no fallback to another mode or credential.
- The login node is resolved once; every answer must be public and not on the
  Tailnet; the socket connects to the validated address literal (rebinding defense).
- Concurrency (global, per user, per user and Platform) and open rate are bounded;
  frames, total bytes, idle time and total duration are capped.
- Bytes are opaque: the relay never parses, decrypts or logs them. Each direction
  holds at most one frame in flight, so memory per stream is bounded and a slow
  side applies backpressure to the other. Half-close is forwarded both ways.
- Every decision is audited with counts and reason codes, never payload.

TCP reachability proves nothing about who the user is on the HPC system.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from egress_policy import (
    DEFAULT_ALLOWED_PORTS,
    DestinationRejected,
    IPAddress,
    Resolver,
    resolve_once,
    validate_port,
)

EXECUTION_AUTHORITY = "user_owned"
NETWORK_TRANSPORT = "direct_public_tcp"


class Outcome(StrEnum):
    """Stable reason codes; a real service maps them onto gRPC status codes."""

    CLOSED = "closed"  # both directions reached EOF
    INVALID_REQUEST = "invalid_request"  # INVALID_ARGUMENT
    PERMISSION_DENIED = "permission_denied"  # PERMISSION_DENIED
    PLATFORM_NOT_APPROVED = "platform_not_approved"  # FAILED_PRECONDITION
    DESTINATION_REJECTED = "destination_rejected"  # FAILED_PRECONDITION
    RESOURCE_EXHAUSTED = "resource_exhausted"  # RESOURCE_EXHAUSTED
    CONNECT_FAILED = "connect_failed"  # UNAVAILABLE
    FRAME_TOO_LARGE = "frame_too_large"  # RESOURCE_EXHAUSTED
    BYTE_LIMIT = "byte_limit"  # RESOURCE_EXHAUSTED
    IDLE_TIMEOUT = "idle_timeout"  # DEADLINE_EXCEEDED
    DEADLINE_EXCEEDED = "deadline_exceeded"  # DEADLINE_EXCEEDED
    CANCELLED = "cancelled"  # CANCELLED
    UPSTREAM_ERROR = "upstream_error"  # UNAVAILABLE


class RelayError(Exception):
    def __init__(self, outcome: Outcome, detail: str = ""):
        super().__init__(f"{outcome}: {detail}" if detail else str(outcome))
        self.outcome = outcome


@dataclass(frozen=True)
class Principal:
    """The authenticated Nest caller, as B07 would resolve it. Never taken from the stream."""

    user_id: str
    principal_type: str = "human"


@dataclass(frozen=True)
class OpenRequest:
    """
    The first frame of a relay stream. Deliberately has no destination field.

    Attributes:
        platform_id (`str`): Immutable Platform ID.
        project_id (`str`): The Project the session acts for.
        attempt_id (`str | None`): Execution attempt registered before any `sbatch`.
    """

    platform_id: str
    project_id: str
    attempt_id: str | None = None

    FIELDS = frozenset({"platform_id", "project_id", "attempt_id"})

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> OpenRequest:
        unknown = set(payload) - cls.FIELDS
        if unknown:
            # host, port, address, target, socks... are never accepted.
            raise RelayError(Outcome.INVALID_REQUEST, "unknown field " + min(unknown))
        try:
            platform = str(uuid.UUID(str(payload["platform_id"])))
            project = str(uuid.UUID(str(payload["project_id"])))
            attempt = payload.get("attempt_id")
            attempt = str(uuid.UUID(str(attempt))) if attempt is not None else None
        except KeyError, ValueError:
            raise RelayError(Outcome.INVALID_REQUEST, "platform_id and project_id are UUIDs")
        return cls(platform, project, attempt)


@dataclass(frozen=True)
class ApprovedEndpoint:
    """
    A server-side allowlisted login node (from D07), approved for user-owned relay.

    `approved_addresses` is the operator-recorded address set, if any; a resolution
    outside it is refused. `host_key_pin_ref` is distributed to OpenSSH separately,
    never learned through the relay.
    """

    node_id: int
    host: str
    port: int
    host_key_pin_ref: str
    approved_addresses: frozenset[str] | None = None


@dataclass(frozen=True)
class RelayLimits:
    max_streams_global: int = 64
    max_streams_per_user: int = 4
    max_streams_per_user_platform: int = 2
    max_opens_per_minute_per_user: int = 10
    max_frame_bytes: int = 32 * 1024
    max_total_bytes: int = 1 << 30
    connect_timeout_s: float = 10.0  # resolution and connect together
    close_timeout_s: float = 5.0  # graceful close, then abort
    idle_timeout_s: float = 900.0
    max_duration_s: float = 8 * 3600.0


@dataclass
class AuditEvent:
    """What the relay records. Counts and codes only: no payload, prompt or secret."""

    relay_id: str
    event: str
    user_id: str
    platform_id: str | None = None
    project_id: str | None = None
    attempt_id: str | None = None
    node_id: int | None = None
    address: str | None = None
    port: int | None = None
    outcome: str | None = None
    detail: str | None = None
    bytes_up: int = 0
    bytes_down: int = 0
    duration_s: float = 0.0
    authority: str = EXECUTION_AUTHORITY
    transport: str = NETWORK_TRANSPORT


type Authorizer = Callable[[Principal, OpenRequest], bool]
"""D01/B07 stand-in: may this principal open a user-owned relay to this Platform?"""

type Connector = Callable[
    [IPAddress, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
]


async def tcp_connector(
    address: IPAddress, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to an address literal; never a name, so nothing re-resolves."""
    return await asyncio.open_connection(str(address), port)


class Admission:
    """Bounded concurrency and a sliding-window open rate, released on every exit path."""

    def __init__(self, limits: RelayLimits, clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.clock = clock
        self.total = 0
        self.per_user: dict[str, int] = defaultdict(int)
        self.per_pair: dict[tuple[str, str], int] = defaultdict(int)
        self.opens: dict[str, deque[float]] = defaultdict(deque)

    def acquire(self, user: str, platform: str) -> None:
        now = self.clock()
        window = self.opens[user]
        while window and now - window[0] >= 60.0:
            window.popleft()
        limits = self.limits
        if len(window) >= limits.max_opens_per_minute_per_user:
            raise RelayError(Outcome.RESOURCE_EXHAUSTED, "open rate")
        window.append(now)
        if self.total >= limits.max_streams_global:
            raise RelayError(Outcome.RESOURCE_EXHAUSTED, "global streams")
        if self.per_user[user] >= limits.max_streams_per_user:
            raise RelayError(Outcome.RESOURCE_EXHAUSTED, "user streams")
        if self.per_pair[user, platform] >= limits.max_streams_per_user_platform:
            raise RelayError(Outcome.RESOURCE_EXHAUSTED, "user platform streams")
        self.total += 1
        self.per_user[user] += 1
        self.per_pair[user, platform] += 1

    def release(self, user: str, platform: str) -> None:
        self.total -= 1
        self.per_user[user] -= 1
        self.per_pair[user, platform] -= 1


@dataclass
class _Counters:
    up: int = 0
    down: int = 0
    last_activity: float = field(default_factory=time.monotonic)


class RelayModel:
    """
    The relay's decision order and byte pump, against injected policy and network.

    Args:
        endpoints: Server-side allowlist, `platform_id -> [ApprovedEndpoint]` in
            priority order. Only these are ever reachable.
        authorize: D01/B07 decision for `(principal, request)`.
        resolver: DNS lookup, called at most once per stream.
        connector: Opens the TCP socket to a validated address literal.
        audit: Receives every `AuditEvent`.
        allowed_ports: Ports a login node may use; default 22 only.
        allow_loopback: Fixture-only: let loopback addresses through.
    """

    def __init__(
        self,
        endpoints: Mapping[str, list[ApprovedEndpoint]],
        authorize: Authorizer,
        resolver: Resolver,
        *,
        connector: Connector = tcp_connector,
        audit: Callable[[AuditEvent], None],
        limits: RelayLimits | None = None,
        allowed_ports: frozenset[int] = DEFAULT_ALLOWED_PORTS,
        allow_loopback: bool = False,
    ):
        self.endpoints = endpoints
        self.authorize = authorize
        self.resolver = resolver
        self.connector = connector
        self.audit = audit
        self.limits = limits or RelayLimits()
        self.admission = Admission(self.limits)
        self.allowed_ports = allowed_ports
        self.allow_loopback = allow_loopback

    async def open(
        self,
        principal: Principal,
        first_frame: Mapping[str, Any],
        client_frames: AsyncIterator[bytes],
        send: Callable[[bytes | None], Awaitable[None]],
    ) -> AuditEvent:
        """
        Run one relay stream to completion and return its final audit event.

        `client_frames` yields the client's opaque bytes and ends at the client's
        half-close. `send(chunk)` delivers server bytes; `send(None)` is the
        server-side half-close. Errors end the stream with `RelayError`.
        """
        relay_id = str(uuid.uuid4())
        event = AuditEvent(relay_id, "relay.open", principal.user_id)
        started = time.monotonic()
        admitted: tuple[str, str] | None = None
        writer: asyncio.StreamWriter | None = None
        try:
            request = OpenRequest.parse(first_frame)
            event.platform_id = request.platform_id
            event.project_id = request.project_id
            event.attempt_id = request.attempt_id
            if not self.authorize(principal, request):
                raise RelayError(Outcome.PERMISSION_DENIED)
            nodes = self.endpoints.get(request.platform_id) or []
            if not nodes:
                raise RelayError(Outcome.PLATFORM_NOT_APPROVED)
            self.admission.acquire(principal.user_id, request.platform_id)
            admitted = (principal.user_id, request.platform_id)
            node = nodes[0]
            event.node_id = node.node_id
            try:
                port = validate_port(node.port, self.allowed_ports)
            except DestinationRejected as error:
                raise RelayError(Outcome.DESTINATION_REJECTED, error.reason) from None
            # One bound covers resolution and connect. The resolver runs off the event
            # loop so a stalled lookup cannot block other streams; a production relay
            # should use a resolver with its own enforced timeout as well, because the
            # abandoned lookup thread is not interrupted.
            try:
                async with asyncio.timeout(self.limits.connect_timeout_s):
                    try:
                        addresses = await asyncio.to_thread(
                            resolve_once,
                            node.host,
                            port,
                            self.resolver,
                            approved=node.approved_addresses,
                            allow_loopback=self.allow_loopback,
                        )
                    except DestinationRejected as error:
                        raise RelayError(Outcome.DESTINATION_REJECTED, error.reason) from None
                    event.address, event.port = str(addresses[0]), port
                    reader, writer = await self.connector(addresses[0], port)
            except TimeoutError:
                phase = "connect_timeout" if event.address else "resolve_timeout"
                raise RelayError(Outcome.CONNECT_FAILED, phase) from None
            except OSError as error:
                raise RelayError(Outcome.CONNECT_FAILED, type(error).__name__) from None
            self._emit(event, "relay.connected", started)
            counters = _Counters()
            try:
                async with asyncio.timeout(self.limits.max_duration_s):
                    await self._pump(client_frames, send, reader, writer, counters)
            except TimeoutError:
                raise RelayError(Outcome.DEADLINE_EXCEEDED) from None
            finally:
                event.bytes_up, event.bytes_down = counters.up, counters.down
            event.outcome = Outcome.CLOSED
        except RelayError as error:
            event.outcome = error.outcome
            event.detail = str(error)
            raise
        except asyncio.CancelledError:
            event.outcome = Outcome.CANCELLED
            raise
        finally:
            # Release and the final audit run even if closing fails, stalls or is
            # itself cancelled.
            try:
                if writer is not None:
                    try:
                        await self._close(writer)
                    except asyncio.CancelledError:
                        event.outcome = Outcome.CANCELLED
                        event.detail = "cancelled during cleanup"
                        raise
            finally:
                if admitted is not None:
                    self.admission.release(*admitted)
                self._emit(event, "relay.closed", started)
        return event

    async def _close(self, writer: asyncio.StreamWriter) -> None:
        """Close gracefully within `close_timeout_s`, otherwise abort the socket."""
        writer.close()
        try:
            async with asyncio.timeout(self.limits.close_timeout_s):
                await writer.wait_closed()
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        except TimeoutError, OSError:  # stalled or reset: drop the connection
            writer.transport.abort()

    def _emit(self, event: AuditEvent, name: str, started: float) -> None:
        event.event = name
        event.duration_s = round(time.monotonic() - started, 3)
        self.audit(AuditEvent(**vars(event)))

    async def _pump(
        self,
        client_frames: AsyncIterator[bytes],
        send: Callable[[bytes | None], Awaitable[None]],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        counters: _Counters,
    ) -> None:
        limits = self.limits

        def account(size: int, up: bool) -> None:
            if up:
                counters.up += size
            else:
                counters.down += size
            if counters.up + counters.down > limits.max_total_bytes:
                raise RelayError(Outcome.BYTE_LIMIT)
            counters.last_activity = time.monotonic()

        async def upstream() -> None:
            try:
                async for frame in client_frames:
                    if len(frame) > limits.max_frame_bytes:
                        raise RelayError(Outcome.FRAME_TOO_LARGE)
                    account(len(frame), True)
                    writer.write(frame)
                    await writer.drain()  # backpressure: next frame waits for the socket
                if writer.can_write_eof():
                    writer.write_eof()  # client half-close -> TCP FIN
            except OSError as error:  # includes ConnectionError from write or drain
                raise RelayError(Outcome.UPSTREAM_ERROR, type(error).__name__) from None

        async def downstream() -> None:
            try:
                while chunk := await reader.read(limits.max_frame_bytes):
                    account(len(chunk), False)
                    await send(chunk)  # backpressure: next read waits for the client
            except OSError as error:
                raise RelayError(Outcome.UPSTREAM_ERROR, type(error).__name__) from None
            await send(None)  # TCP FIN -> server half-close

        async def idle_watch() -> None:
            while True:
                remaining = counters.last_activity + limits.idle_timeout_s - time.monotonic()
                if remaining <= 0:
                    raise RelayError(Outcome.IDLE_TIMEOUT)
                await asyncio.sleep(min(remaining, 1.0))

        directions = {asyncio.ensure_future(upstream()), asyncio.ensure_future(downstream())}
        watcher = asyncio.ensure_future(idle_watch())
        try:
            pending = set(directions)
            while pending:
                done, _ = await asyncio.wait(
                    pending | {watcher}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()  # re-raises the first failure
                pending -= done
        finally:
            for task in (*directions, watcher):
                task.cancel()
            await asyncio.gather(*directions, watcher, return_exceptions=True)
