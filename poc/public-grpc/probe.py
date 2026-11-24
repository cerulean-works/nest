#!/usr/bin/env python3
"""
Runs the H06 public gRPC transport matrix against a gateway in front of the PoC origin.

Checks TLS (certificate, SNI, ALPN h2), unary calls, metadata and trailers, status
codes, bidirectional and server streaming, deadline propagation, client
cancellation, message limits, silent long-lived unary calls (the shape of today's
blocking `Execute`), the real Controller's authentication contract, and that the
origin cannot be reached directly.

It prints one JSON object per check and a summary; it never prints the token or a
payload, only sizes, durations and status codes. It talks only to the synthetic
PoC origin (`origin.py`): nothing is executed and no Slurm job is submitted.
Exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import socket
import ssl
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import cast

import grpc

from nest.proto import execution_pb2, execution_pb2_grpc

SERVICE = "/nest.poc.TransportProbe/"
PROJECT_ID = "00000000-0000-4000-8000-0000000000a6"
STATUS_CODES = (
    "INVALID_ARGUMENT",
    "NOT_FOUND",
    "PERMISSION_DENIED",
    "UNAUTHENTICATED",
    "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION",
    "UNAVAILABLE",
    "INTERNAL",
)
MiB = 1024 * 1024


@dataclass
class Result:
    check: str
    passed: bool | None  # None: not run (pending)
    detail: str = ""
    seconds: float = 0.0
    data: dict = field(default_factory=dict)


@dataclass
class Target:
    address: str
    server_name: str | None
    ca_file: str | None
    token: str
    plaintext: bool = False

    @property
    def host_port(self) -> tuple[str, int]:
        host, _, port = self.address.rpartition(":")
        return host.strip("[]"), int(port)

    def channel(self, options: list[tuple[str, int]] | None = None) -> grpc.Channel:
        # Connect directly: a test must not silently pass through an HTTP proxy.
        options = [("grpc.enable_http_proxy", 0), *(options or [])]
        names: list[tuple[str, str]] = []
        if self.plaintext:
            return grpc.insecure_channel(self.address, options=options)
        roots = None
        if self.ca_file:
            with open(self.ca_file, "rb") as handle:
                roots = handle.read()
        if self.server_name:
            names.append(("grpc.ssl_target_name_override", self.server_name))
            names.append(("grpc.default_authority", self.server_name))
        return grpc.secure_channel(
            self.address, grpc.ssl_channel_credentials(roots), options=[*options, *names]
        )

    def metadata(self, *extra: tuple[str, str], token: bool = True) -> tuple:
        values = [("x-nest-request-id", str(uuid.uuid4())), ("x-nest-project-id", PROJECT_ID)]
        if token:
            values.append(("authorization", f"Bearer {self.token}"))
        return tuple(values) + extra


def _code(error: grpc.RpcError) -> grpc.StatusCode:
    return cast(grpc.Call, error).code()


def _call(error: grpc.RpcError) -> grpc.Call:
    return cast(grpc.Call, error)


def _unary(channel: grpc.Channel, method: str):
    return channel.unary_unary(SERVICE + method)


def run(name: str, check: Callable[[], tuple[bool, str, dict]]) -> Result:
    start = time.monotonic()
    try:
        passed, detail, data = check()
    except grpc.RpcError as error:
        passed, detail, data = False, f"{_code(error).name}: {_call(error).details()}", {}
    except Exception as error:  # noqa: BLE001 - every failure is reported, not raised
        passed, detail, data = False, f"{type(error).__name__}: {error}", {}
    return Result(name, passed, detail, round(time.monotonic() - start, 3), data)


# --- TLS ------------------------------------------------------------------------


def check_tls(target: Target) -> tuple[bool, str, dict]:
    """Verifies the chain and name for the SNI sent, and that ALPN selects h2."""
    host, port = target.host_port
    server_name = target.server_name or host
    context = ssl.create_default_context(cafile=target.ca_file)
    context.set_alpn_protocols(["h2", "http/1.1"])
    with (
        socket.create_connection((host, port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname=server_name) as tls,
    ):
        alpn = tls.selected_alpn_protocol()
        cert = tls.getpeercert() or {}
        version = tls.version()
    not_after = datetime.strptime(str(cert.get("notAfter")), "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=UTC
    )
    issuer = dict(item[0] for item in cast(tuple, cert.get("issuer", ())))
    data = {
        "sni": server_name,
        "alpn": alpn,
        "tls_version": version,
        "issuer_cn": issuer.get("commonName"),
        "issuer_o": issuer.get("organizationName"),
        "not_after": not_after.isoformat(),
        "days_left": (not_after - datetime.now(UTC)).days,
    }
    return alpn == "h2", f"ALPN {alpn}, {version}, verified for {server_name}", data


def check_tls_wrong_name(target: Target) -> tuple[bool, str, dict]:
    """A client verifying another name must refuse the certificate."""
    host, port = target.host_port
    context = ssl.create_default_context(cafile=target.ca_file)
    context.set_alpn_protocols(["h2"])
    wrong = "wrong-name.invalid"
    try:
        with (
            socket.create_connection((host, port), timeout=10) as raw,
            context.wrap_socket(raw, server_hostname=wrong),
        ):
            pass
    except ssl.SSLCertVerificationError as error:
        # Envoy serves its one certificate (name mismatch); Traefik serves its default
        # self-signed one for an unknown SNI. Either way the client refuses it, and
        # check_tls has already verified the chain for the right name.
        return True, f"refused for {wrong}: {error.verify_message}", {}
    return False, f"certificate accepted for {wrong}", {}


def check_tls_no_h2(target: Target) -> tuple[bool, str, dict]:
    """With ALPN offering only http/1.1, a gRPC-only listener must not pretend h2."""
    host, port = target.host_port
    context = ssl.create_default_context(cafile=target.ca_file)
    context.set_alpn_protocols(["http/1.1"])
    try:
        with (
            socket.create_connection((host, port), timeout=10) as raw,
            context.wrap_socket(raw, server_hostname=target.server_name or host) as tls,
        ):
            alpn = tls.selected_alpn_protocol()
    except ssl.SSLCertVerificationError as error:
        return False, f"certificate not verified: {error.verify_message}", {}
    except ssl.SSLError as error:
        return True, f"refused without h2: {error.reason}", {"alpn": None}
    # Serving HTTP/1.1 is fine (a shared edge serves HTTPS too); it must not claim h2.
    return alpn != "h2", f"ALPN {alpn}", {"alpn": alpn}


# --- gRPC semantics -----------------------------------------------------------------


def check_unauthenticated(channel: grpc.Channel, target: Target) -> tuple[bool, str, dict]:
    try:
        _unary(channel, "Echo")(b"x", metadata=target.metadata(token=False), timeout=10)
    except grpc.RpcError as error:
        return _code(error) == grpc.StatusCode.UNAUTHENTICATED, _code(error).name, {}
    return False, "accepted without a token", {}


def check_controller_unauthenticated(
    channel: grpc.Channel, target: Target
) -> tuple[bool, str, dict]:
    stub = execution_pb2_grpc.ExecutionServiceStub(channel)
    request = execution_pb2.ExecuteRequest(environment=execution_pb2.EnvironmentFiles())
    try:
        stub.Execute(request, metadata=target.metadata(token=False), timeout=10)
    except grpc.RpcError as error:
        ok = _code(error) == grpc.StatusCode.UNAUTHENTICATED and "nest login" in str(
            _call(error).details()
        )
        return ok, f"{_code(error).name}: Controller message preserved={ok}", {}
    return False, "Controller accepted a call without a session", {}


def check_controller_authenticated(channel: grpc.Channel, target: Target) -> tuple[bool, str, dict]:
    """The `verify-grpc.py` contract, with a bearer token: reaches the real handler only."""
    stub = execution_pb2_grpc.ExecutionServiceStub(channel)
    request = execution_pb2.ExecuteRequest(environment=execution_pb2.EnvironmentFiles())
    try:
        stub.Execute(request, metadata=target.metadata(), timeout=10)
    except grpc.RpcError as error:
        ok = (
            _code(error) == grpc.StatusCode.INVALID_ARGUMENT
            and _call(error).details() == "Environment requires pyproject.toml and uv.lock"
        )
        return ok, f"{_code(error).name} from the Controller handler", {}
    return False, "Expected the empty environment to be rejected", {}


def check_unary(channel: grpc.Channel, target: Target) -> tuple[bool, str, dict]:
    payload = os.urandom(64 * 1024)
    response, call = _unary(channel, "Echo").with_call(
        payload, metadata=target.metadata(("x-poc-echo", "h06")), timeout=10
    )
    initial = dict(call.initial_metadata() or ())
    trailing = dict(call.trailing_metadata() or ())
    seen = set(str(trailing.get("x-poc-seen", "")).split(","))
    expected = {"authorization", "x-nest-project-id", "x-nest-request-id", "x-poc-echo"}
    checks = {
        "payload_round_trip": response == payload,
        "initial_metadata": initial.get("x-poc-echo") == "h06",
        "text_trailer": trailing.get("x-poc-received-bytes") == str(len(payload)),
        "binary_trailer": trailing.get("x-poc-trailer-bin") == hashlib.sha256(payload).digest(),
        "request_headers_forwarded": expected <= seen,
    }
    missing = sorted(expected - seen)
    return all(checks.values()), f"missing headers: {missing}" if missing else "ok", checks


def check_status_codes(channel: grpc.Channel, target: Target) -> tuple[bool, str, dict]:
    observed: dict[str, str] = {}
    for name in STATUS_CODES:
        try:
            _unary(channel, "Fail")(
                b"", metadata=target.metadata(("x-poc-status", name)), timeout=10
            )
            observed[name] = "OK"
        except grpc.RpcError as error:
            call = _call(error)
            kind = dict(call.trailing_metadata() or ()).get("nest-error-kind")
            good = (
                call.code().name == name
                and call.details() == f"PoC status {name}"
                and kind == f"poc_{name.lower()}"
            )
            observed[name] = "preserved" if good else f"{call.code().name}/{kind}"
    passed = all(value == "preserved" for value in observed.values())
    return passed, "codes, details and nest-error-kind trailer", observed


def check_bidi(channel: grpc.Channel, target: Target) -> tuple[bool, str, dict]:
    """Sends the next message only after the reply to the previous one arrived."""
    count = 20
    replies: list[bytes] = []
    lock = threading.Condition()
    sent = [0]

    def requests() -> Iterator[bytes]:
        for index in range(count):
            with lock:
                if not lock.wait_for(lambda i=index: len(replies) >= i, timeout=10):
                    return
                sent[0] += 1
            yield f"m{index}".encode()

    chat = channel.stream_stream(SERVICE + "Chat")
    latencies = []
    last = time.monotonic()
    for reply in chat(requests(), metadata=target.metadata(), timeout=60):
        now = time.monotonic()
        latencies.append(now - last)
        last = now
        with lock:
            replies.append(reply)
            lock.notify_all()
    ordered = replies == [f"{i}:m{i}".encode() for i in range(count)]
    data = {"messages": len(replies), "max_gap_s": round(max(latencies or [0]), 3)}
    return ordered and len(replies) == count, "interleaved request/response", data


def check_server_stream(
    channel: grpc.Channel, target: Target, seconds: float, interval: float
) -> tuple[bool, str, dict]:
    ticks = channel.unary_stream(SERVICE + "Ticks")
    times = []
    start = time.monotonic()
    for _ in ticks(
        b"",
        metadata=target.metadata(
            ("x-poc-seconds", str(seconds)), ("x-poc-interval", str(interval))
        ),
        timeout=seconds + 30,
    ):
        times.append(time.monotonic() - start)
    gaps = [b - a for a, b in itertools.pairwise(times)]
    expected = max(int(seconds / interval), 2)
    # Messages must arrive spread out, not buffered until the end.
    unbuffered = bool(gaps) and max(gaps) < interval * 3 + 1
    data = {"messages": len(times), "duration_s": round(times[-1] if times else 0, 2)}
    data["max_gap_s"] = round(max(gaps or [0]), 3)
    ok = unbuffered and len(times) >= expected
    return ok, f"{len(times)} ticks over {data['duration_s']}s", data


def check_long_chat(
    channel: grpc.Channel, target: Target, seconds: float, interval: float = 30.0
) -> tuple[bool, str, dict]:
    """A bidirectional stream that exchanges one message every `interval` seconds."""
    count = max(int(seconds / interval), 1) + 1

    def requests() -> Iterator[bytes]:
        for index in range(count):
            if index:
                time.sleep(interval)
            yield f"m{index}".encode()

    start = time.monotonic()
    chat = channel.stream_stream(SERVICE + "Chat")
    replies = list(chat(requests(), metadata=target.metadata(), timeout=seconds + 60))
    elapsed = round(time.monotonic() - start, 1)
    data = {"messages": len(replies), "expected": count, "duration_s": elapsed}
    return len(replies) == count, f"{len(replies)}/{count} replies over {elapsed}s", data


def check_silent_unary(
    channel: grpc.Channel, target: Target, seconds: float
) -> tuple[bool, str, dict]:
    """A unary call that sends nothing for `seconds`, like a blocking `Execute`."""
    response = _unary(channel, "Wait")(
        b"",
        metadata=target.metadata(("x-poc-seconds", str(seconds)), ("x-poc-call-id", "silent")),
        timeout=seconds + 30,
    )
    return response == f"waited {seconds:g}".encode(), f"silent for {seconds:g}s", {}


def _outcome(channel: grpc.Channel, target: Target, call_id: str) -> str:
    deadline = time.monotonic() + 10
    outcome = "unknown"
    while time.monotonic() < deadline:
        outcome = _unary(channel, "Outcome")(
            call_id.encode(), metadata=target.metadata(), timeout=10
        ).decode()
        if outcome != "running":
            break
        time.sleep(0.1)
    return outcome


def check_deadline(channel: grpc.Channel, target: Target) -> tuple[bool, str, dict]:
    call_id = f"deadline-{uuid.uuid4()}"
    start = time.monotonic()
    try:
        _unary(channel, "Wait")(
            b"",
            metadata=target.metadata(("x-poc-seconds", "20"), ("x-poc-call-id", call_id)),
            timeout=1.5,
        )
        return False, "no deadline", {}
    except grpc.RpcError as error:
        elapsed = time.monotonic() - start
        if _code(error) != grpc.StatusCode.DEADLINE_EXCEEDED:
            return False, _code(error).name, {}
    outcome = _outcome(channel, target, call_id)
    data = {"client_elapsed_s": round(elapsed, 3), "origin_outcome": outcome}
    return outcome == "cancelled" and elapsed < 5, "origin saw the deadline", data


def check_cancel(channel: grpc.Channel, target: Target) -> tuple[bool, str, dict]:
    call_id = f"cancel-{uuid.uuid4()}"
    future = _unary(channel, "Wait").future(
        b"",
        metadata=target.metadata(("x-poc-seconds", "20"), ("x-poc-call-id", call_id)),
        timeout=60,
    )
    time.sleep(1)
    future.cancel()
    outcome = _outcome(channel, target, call_id)
    return outcome == "cancelled", f"origin outcome {outcome}", {"origin_outcome": outcome}


def check_limits(channel: grpc.Channel, target: Target) -> tuple[bool, str, dict]:
    """
    Requests and responses just under and over gRPC's 4 MiB default receive limit,
    which the Controller and the SDK both use today.
    """
    blob = _unary(channel, "Blob")
    sizes = {"1MiB": MiB, "4MiB-1KiB": 4 * MiB - 1024, "5MiB": 5 * MiB}
    observed: dict[str, str] = {}
    for label, size in sizes.items():
        try:
            blob(bytes(size), metadata=target.metadata(), timeout=60)
            observed[f"request_{label}"] = "OK"
        except grpc.RpcError as error:
            observed[f"request_{label}"] = _code(error).name
        try:
            blob(b"", metadata=target.metadata(("x-poc-response-bytes", str(size))), timeout=60)
            observed[f"response_{label}"] = "OK"
        except grpc.RpcError as error:
            observed[f"response_{label}"] = _code(error).name
    expected = {
        "request_1MiB": "OK",
        "response_1MiB": "OK",
        "request_4MiB-1KiB": "OK",
        "response_4MiB-1KiB": "OK",
        "request_5MiB": "RESOURCE_EXHAUSTED",
        "response_5MiB": "RESOURCE_EXHAUSTED",
    }
    return observed == expected, "4 MiB default limit enforced end to end", observed


def check_origin_unreachable(address: str, token: str) -> tuple[bool, str, dict]:
    """The origin must refuse a direct connection, or at least a direct gRPC call."""
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host.strip("[]"), int(port)), timeout=3):
            pass
    except OSError as error:
        return True, f"TCP refused: {type(error).__name__}", {"tcp": "refused"}
    # TCP connects (for example, a host firewall allows it): plaintext gRPC must fail.
    plain = Target(address, None, None, token, plaintext=True)
    with plain.channel() as channel:
        try:
            _unary(channel, "Echo")(b"x", metadata=plain.metadata(), timeout=5)
        except grpc.RpcError as error:
            return False, f"TCP open; gRPC {_code(error).name}", {"tcp": "open"}
    return False, "direct plaintext gRPC accepted", {"tcp": "open", "grpc": "accepted"}


def matrix(target: Target, args) -> list[Result]:
    results: list[Result] = []
    if not target.plaintext:
        results.append(run("tls_certificate_sni_alpn_h2", lambda: check_tls(target)))
        results.append(run("tls_wrong_name_refused", lambda: check_tls_wrong_name(target)))
        results.append(run("tls_http1_only_not_h2", lambda: check_tls_no_h2(target)))
    options = [("grpc.keepalive_time_ms", int(args.keepalive * 1000))] if args.keepalive else []
    with target.channel(options) as channel:
        try:
            grpc.channel_ready_future(channel).result(timeout=15)
        except grpc.FutureTimeoutError:
            results.append(Result("channel_ready", False, "no gRPC connection in 15 s"))
            return results
        checks: list[tuple[str, Callable[[], tuple[bool, str, dict]]]] = [
            ("unauthenticated_refused", lambda: check_unauthenticated(channel, target)),
            (
                "controller_unauthenticated",
                lambda: check_controller_unauthenticated(channel, target),
            ),
            (
                "controller_authenticated_probe",
                lambda: check_controller_authenticated(channel, target),
            ),
            ("unary_metadata_trailers", lambda: check_unary(channel, target)),
            ("status_codes", lambda: check_status_codes(channel, target)),
            ("bidirectional_stream", lambda: check_bidi(channel, target)),
            (
                "server_stream",
                lambda: check_server_stream(channel, target, args.stream_seconds, 1.0),
            ),
            ("deadline_propagation", lambda: check_deadline(channel, target)),
            ("client_cancellation", lambda: check_cancel(channel, target)),
            ("message_limits", lambda: check_limits(channel, target)),
        ]
        for seconds in args.silent:
            checks.append(
                (
                    f"silent_unary_{seconds:g}s",
                    lambda s=seconds: check_silent_unary(channel, target, s),
                )
            )
        if args.long_chat:
            checks.append(
                (
                    f"long_bidi_{args.long_chat:g}s",
                    lambda: check_long_chat(channel, target, args.long_chat),
                )
            )
        if args.long_stream:
            checks.append(
                (
                    f"long_stream_{args.long_stream:g}s",
                    lambda: check_server_stream(channel, target, args.long_stream, 30.0),
                )
            )
        for name, check in checks:
            results.append(run(name, check))
    for address in args.origin:
        results.append(
            run(
                f"origin_unreachable_{address}",
                lambda a=address: check_origin_unreachable(a, target.token),
            )
        )
    return results


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("address", help="Gateway host:port, for example grpc.example.com:443")
    parser.add_argument("--server-name", help="SNI and :authority, when unlike the address")
    parser.add_argument("--ca-file", help="PEM roots; the system roots when omitted")
    parser.add_argument("--plaintext", action="store_true", help="h2c, for the origin only")
    parser.add_argument(
        "--silent",
        type=float,
        action="append",
        default=[],
        help="Seconds a silent unary call lasts; repeat for several (e.g. 30, 110, 650)",
    )
    parser.add_argument("--stream-seconds", type=float, default=5, help="At least 2")
    parser.add_argument(
        "--long-stream", type=float, default=0, help="Seconds of a stream with 30 s ticks"
    )
    parser.add_argument(
        "--long-chat", type=float, default=0, help="Seconds of a bidi stream with 30 s messages"
    )
    parser.add_argument("--keepalive", type=float, default=0, help="Client keepalive seconds")
    parser.add_argument(
        "--origin",
        action="append",
        default=[],
        help="Origin host:port that must NOT be reachable from here; repeatable",
    )
    return parser


def main() -> None:
    args = parser().parse_args()
    token_file = os.environ.get("NEST_POC_TOKEN_FILE")
    if not token_file:
        raise SystemExit("Set NEST_POC_TOKEN_FILE to the synthetic token file")
    with open(token_file) as handle:
        token = handle.read().strip()
    target = Target(args.address, args.server_name, args.ca_file, token, args.plaintext)
    results = matrix(target, args)
    for result in results:
        print(json.dumps(asdict(result), sort_keys=True))
    failed = [result.check for result in results if result.passed is False]
    summary = {"address": args.address, "checks": len(results), "failed": failed}
    print(json.dumps({"summary": summary}, sort_keys=True))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
