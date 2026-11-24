#!/usr/bin/env python3
"""
Staging-only gRPC origin for the public transport feasibility PoC (H06).

It serves two services on one plaintext h2c listener, the same shape as the
Controller behind Tailscale Serve:

- `nest.ExecutionService`, the real Controller handler, with a synthetic access
  check instead of the database. It has no Slurm backend and the probe sends only
  the empty-environment request that `scripts/verify-grpc.py` uses, so nothing is
  ever unpickled, executed, or submitted. A request that got past validation would
  stop at `FAILED_PRECONDITION` (no Slurm backend).
- `nest.poc.TransportProbe`, a synthetic service that exercises the transport
  features a gateway must preserve: metadata, trailers, status codes, streaming,
  deadlines, cancellation, message sizes and long-lived calls. It echoes bytes and
  sleeps; it never runs code.

Every call needs `authorization: Bearer <token>`, compared in constant time with
the token in `NEST_POC_TOKEN_FILE`. Neither the token nor any payload is logged.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import os
import signal
import threading
import time
from concurrent import futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import UUID

import grpc

from nest.authz.execution import ExecutionAccess, UnauthenticatedError
from nest.authz.policy import Principal
from nest.controller import ExecutionService
from nest.proto import execution_pb2_grpc

logger = logging.getLogger("nest.poc.origin")

SERVICE = "nest.poc.TransportProbe"
SYNTHETIC_USER = UUID("00000000-0000-4000-8000-000000000006")
# Request metadata the probe sends and the origin reports by name (never by value).
FORWARDED_NAMES = ("authorization", "x-nest-project-id", "x-nest-request-id", "x-poc-echo")


def _metadata(context) -> dict[str, str | bytes]:
    return {key: value for key, value in context.invocation_metadata()}


@dataclass
class TokenCheck:
    """Accepts exactly one synthetic bearer token, held only as a SHA-256 digest."""

    digest: bytes

    @classmethod
    def from_token(cls, token: str) -> TokenCheck:
        if len(token) < 16:
            raise ValueError("The synthetic token must have at least 16 characters")
        return cls(hashlib.sha256(token.encode()).digest())

    def accepts(self, metadata) -> bool:
        values = [value for key, value in metadata if key == "authorization"]
        if len(values) != 1 or not isinstance(values[0], str):
            return False
        scheme, _, token = values[0].partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        return hmac.compare_digest(hashlib.sha256(token.encode()).digest(), self.digest)


class SyntheticAccess:
    """
    Stands in for `authz.ExecutionAccess`: authenticates the synthetic token and
    nothing else. The Controller calls only `authenticate` before it rejects the
    probe's empty environment.
    """

    def __init__(self, check: TokenCheck):
        self.check = check

    def authenticate(self, metadata, rpc: str) -> Principal:
        if not self.check.accepts(metadata):
            raise UnauthenticatedError("invalid_token")
        return Principal(user_id=SYNTHETIC_USER, session_kind="cli", scopes=frozenset())

    def __getattr__(self, name):
        # Anything past authentication would need the real policy; refuse loudly.
        raise RuntimeError(f"The PoC origin does not authorize {name}")


@dataclass
class CallLedger:
    """How each `Wait` call ended, so the probe can see server-side cancellation."""

    outcomes: dict[str, str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, call_id: str, outcome: str) -> None:
        with self.lock:
            self.outcomes[call_id[:64]] = outcome
            while len(self.outcomes) > 1000:
                self.outcomes.pop(next(iter(self.outcomes)))

    def get(self, call_id: str) -> str:
        with self.lock:
            return self.outcomes.get(call_id[:64], "unknown")


class TransportProbe:
    """Synthetic handlers. Control parameters travel in `x-poc-*` request metadata."""

    def __init__(self, check: TokenCheck, ledger: CallLedger | None = None):
        self.check = check
        self.ledger = ledger or CallLedger()

    def _require_token(self, context) -> None:
        if not self.check.accepts(context.invocation_metadata()):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Synthetic bearer token required")

    @staticmethod
    def _seconds(metadata, name: str, default: float, limit: float = 3600) -> float:
        try:
            value = float(metadata.get(name, default))
        except TypeError, ValueError:
            return default
        return min(max(value, 0.0), limit)

    def echo(self, request: bytes, context) -> bytes:
        """Returns the request; reports which forwarded headers arrived, and trailers."""
        self._require_token(context)
        metadata = _metadata(context)
        seen = ",".join(name for name in FORWARDED_NAMES if name in metadata)
        context.send_initial_metadata((("x-poc-echo", str(metadata.get("x-poc-echo", ""))),))
        context.set_trailing_metadata(
            (
                ("x-poc-seen", seen),
                ("x-poc-received-bytes", str(len(request))),
                ("x-poc-trailer-bin", hashlib.sha256(request).digest()),
            )
        )
        return request

    def fail(self, request: bytes, context) -> bytes:
        """Ends with the status named in `x-poc-status`, its details and a trailer."""
        self._require_token(context)
        name = str(_metadata(context).get("x-poc-status", "UNKNOWN"))
        code = getattr(grpc.StatusCode, name, grpc.StatusCode.UNKNOWN)
        context.set_trailing_metadata((("nest-error-kind", f"poc_{name.lower()}"),))
        context.abort(code, f"PoC status {name}")
        raise AssertionError("context.abort raises")

    def wait(self, request: bytes, context) -> bytes:
        """Sends nothing for `x-poc-seconds`, like a blocking `Execute`, then replies."""
        self._require_token(context)
        metadata = _metadata(context)
        seconds = self._seconds(metadata, "x-poc-seconds", 1)
        call_id = str(metadata.get("x-poc-call-id", ""))
        self.ledger.set(call_id, "running")
        cancelled = threading.Event()
        context.add_callback(cancelled.set)
        if cancelled.wait(seconds) or not context.is_active():
            self.ledger.set(call_id, "cancelled")
            return b""
        self.ledger.set(call_id, "completed")
        return f"waited {seconds:g}".encode()

    def outcome(self, request: bytes, context) -> bytes:
        """The recorded outcome of the `Wait` call whose ID is the request."""
        self._require_token(context)
        return self.ledger.get(request.decode(errors="replace")).encode()

    def blob(self, request: bytes, context) -> bytes:
        """Replies with `x-poc-response-bytes` zero bytes; reports the request size."""
        self._require_token(context)
        metadata = _metadata(context)
        size = int(self._seconds(metadata, "x-poc-response-bytes", 0, limit=64 * 1024 * 1024))
        context.set_trailing_metadata((("x-poc-received-bytes", str(len(request))),))
        return bytes(size)

    def ticks(self, request: bytes, context):
        """Streams a tick every `x-poc-interval` seconds for `x-poc-seconds`."""
        self._require_token(context)
        metadata = _metadata(context)
        seconds = self._seconds(metadata, "x-poc-seconds", 3)
        interval = max(self._seconds(metadata, "x-poc-interval", 1), 0.05)
        start = time.monotonic()
        count = 0
        while context.is_active():
            yield f"{count}".encode()
            count += 1
            if time.monotonic() - start + interval > seconds:
                return
            time.sleep(interval)

    def chat(self, requests, context):
        """Bidirectional: answers each message as it arrives, before half-close."""
        self._require_token(context)
        for index, message in enumerate(requests):
            yield f"{index}:".encode() + message

    def handler(self) -> grpc.GenericRpcHandler:
        unary = grpc.unary_unary_rpc_method_handler
        return grpc.method_handlers_generic_handler(
            SERVICE,
            {
                "Echo": unary(self.echo),
                "Fail": unary(self.fail),
                "Wait": unary(self.wait),
                "Outcome": unary(self.outcome),
                "Blob": unary(self.blob),
                "Ticks": grpc.unary_stream_rpc_method_handler(self.ticks),
                "Chat": grpc.stream_stream_rpc_method_handler(self.chat),
            },
        )


def build_server(
    check: TokenCheck, bind: str, *, credentials: grpc.ServerCredentials | None = None
) -> tuple[grpc.Server, int]:
    """
    The origin server with the Controller's own server options. Returns it unstarted,
    with its bound port.
    """
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=16),
        options=[("grpc.so_reuseport", 0)],
    )
    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
        ExecutionService(
            None, executor="slurm", access=cast(ExecutionAccess, SyntheticAccess(check))
        ),
        server,
    )
    server.add_generic_rpc_handlers((TransportProbe(check).handler(),))
    if credentials is None:
        port = server.add_insecure_port(bind)
    else:
        port = server.add_secure_port(bind, credentials)
    if not port:
        raise RuntimeError(f"Cannot bind the PoC origin to {bind}")
    return server, port


def read_token(path: str | None) -> str:
    if not path:
        raise SystemExit("Set NEST_POC_TOKEN_FILE to a file holding the synthetic token")
    return Path(path).read_text().strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bind", default=os.environ.get("NEST_POC_ORIGIN_BIND", "0.0.0.0:50061"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    check = TokenCheck.from_token(read_token(os.environ.get("NEST_POC_TOKEN_FILE")))
    server, port = build_server(check, args.bind)
    server.start()
    logger.info("PoC origin listening bind=%s port=%s transport=h2c", args.bind, port)

    def stop(signum, frame):
        server.stop(grace=5)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.wait_for_termination()


if __name__ == "__main__":
    main()
