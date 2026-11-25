#!/usr/bin/env python3
"""
Checks a Nest public gRPC gateway from outside, for H08 staging and pilot acceptance.

Usage:

    uv run python scripts/verify-public-grpc.py grpc.staging.example.org:443 \\
        [--ca-file staging-ca.pem] [--origin HOST:PORT ...] [--signed-in] [--execute]

Without options it sends no credential and runs nothing:

- `tls`: the certificate verifies for the name (system roots, or `--ca-file` for a
  staging CA), TLS is 1.2 or later, ALPN selects `h2`; prints days left.
- `tls_wrong_name_refused`: a client asking for another name gets no certificate
  for it.
- `tls_http1_only_not_h2`: a client offering only `http/1.1` is not given `h2`.
- `unauthenticated_refused`: `ListEligibleProjects` without a session is
  `UNAUTHENTICATED`, with the Controller's `nest login` message intact.
- `non_nest_path_refused`: the gRPC health service is not served (`UNIMPLEMENTED`).
- `origin_unreachable_<addr>` (each `--origin`): the Controller or the gateway's
  listener cannot be reached directly, bypassing the edge.

`--signed-in` also lists your eligible Projects with your stored Nest session (the
SDK's settings decide which session: `NEST_PROFILE=hosted`, or `NEST_AUTH_URL`).
`--execute` also runs one trivial remote function through the SDK, which submits
one real Slurm job: use it only in the approved pilot. Neither prints a token.

Both first run `sdk_target`: the SDK's settings must name the gateway being
verified, over TLS, trusting the same roots (`--ca-file`, or system roots). If
they name another Controller, plaintext or other roots, the check fails and no
session is read and no job is submitted. The script never redirects a session to
the address given here; point the SDK at it instead (`NEST_PROFILE=hosted`, or
`NEST_CONTROLLER_ADDRESS` with `NEST_CONTROLLER_TLS_CA_FILE`).

Exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import datetime
import json
import socket
import ssl
import sys
from typing import cast

import grpc

from nest.proto import execution_pb2, execution_pb2_grpc


def _split(address: str) -> tuple[str, int]:
    host, _, port = address.rpartition(":")
    return host.strip("[]"), int(port)


def _context(ca_file: str | None, alpn: list[str], *, verify_name: bool = True) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = verify_name
    context.set_alpn_protocols(alpn)
    return context


def _connect(address: str, timeout: float) -> socket.socket:
    return socket.create_connection(_split(address), timeout=timeout)


def check_tls(address: str, ca_file: str | None, timeout: float) -> dict:
    host, _ = _split(address)
    with (
        _connect(address, timeout) as raw,
        _context(ca_file, ["h2"]).wrap_socket(raw, server_hostname=host) as tls,
    ):
        certificate = tls.getpeercert() or {}
        expires = datetime.datetime.strptime(
            str(certificate.get("notAfter")), "%b %d %H:%M:%S %Y %Z"
        ).replace(tzinfo=datetime.UTC)
        days_left = (expires - datetime.datetime.now(datetime.UTC)).days
        alpn = tls.selected_alpn_protocol()
        if alpn != "h2":
            raise RuntimeError(f"ALPN selected {alpn!r}, not h2")
        return {"version": tls.version(), "alpn": alpn, "days_left": days_left}


def check_wrong_name(address: str, ca_file: str | None, timeout: float) -> dict:
    with _connect(address, timeout) as raw:
        try:
            with _context(ca_file, ["h2"]).wrap_socket(
                raw, server_hostname="wrong-name.nest.invalid"
            ):
                pass
        except (ssl.SSLError, OSError) as exc:
            return {"refused": type(exc).__name__}
    raise RuntimeError("a certificate was accepted for another name")


def check_http1(address: str, ca_file: str | None, timeout: float) -> dict:
    host, _ = _split(address)
    with _connect(address, timeout) as raw:
        try:
            with _context(ca_file, ["http/1.1"]).wrap_socket(raw, server_hostname=host) as tls:
                alpn = tls.selected_alpn_protocol()
        except (ssl.SSLError, OSError) as exc:
            return {"refused": type(exc).__name__}
    if alpn == "h2":
        raise RuntimeError("h2 was selected for a client that offered only http/1.1")
    return {"alpn": alpn}


def _channel(address: str, ca_file: str | None) -> grpc.Channel:
    roots = None
    if ca_file:
        with open(ca_file, "rb") as handle:
            roots = handle.read()
    return grpc.secure_channel(address, grpc.ssl_channel_credentials(roots))


def check_unauthenticated(address: str, ca_file: str | None, timeout: float) -> dict:
    with _channel(address, ca_file) as channel:
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        try:
            stub.ListEligibleProjects(execution_pb2.ListEligibleProjectsRequest(), timeout=timeout)
        except grpc.RpcError as exc:
            call = cast(grpc.Call, exc)
            if call.code() == grpc.StatusCode.UNAUTHENTICATED and "nest login" in (
                call.details() or ""
            ):
                return {"status": call.code().name}
            raise RuntimeError(f"expected UNAUTHENTICATED, got {call.code().name}") from None
    raise RuntimeError("an unauthenticated call was answered")


def check_non_nest_path(address: str, ca_file: str | None, timeout: float) -> dict:
    with _channel(address, ca_file) as channel:
        check = channel.unary_unary("/grpc.health.v1.Health/Check")
        try:
            check(b"", timeout=timeout)
        except grpc.RpcError as exc:
            code = cast(grpc.Call, exc).code()
            if code == grpc.StatusCode.UNIMPLEMENTED:
                return {"status": code.name}
            raise RuntimeError(f"expected UNIMPLEMENTED, got {code.name}") from None
    raise RuntimeError("the health service is served publicly")


def check_origin(origin: str, timeout: float) -> dict:
    try:
        with _connect(origin, timeout):
            pass
    except OSError as exc:
        return {"refused": type(exc).__name__}
    raise RuntimeError(f"{origin} accepts TCP connections from this network")


def _read(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _same_address(a: str, b: str) -> bool:
    (host_a, port_a), (host_b, port_b) = _split(a), _split(b)
    return host_a.lower().rstrip(".") == host_b.lower().rstrip(".") and port_a == port_b


def require_sdk_target(config, address: str, ca_file: str | None) -> None:
    """
    Raises unless the SDK settings `config` connect to `address` over TLS with the
    same trust roots as the unauthenticated checks, so the signed-in and execution
    checks verify the same gateway and a session never goes anywhere else.
    """
    if not config.tls:
        raise RuntimeError(
            f"the SDK connects to {config.address} in plaintext; refusing to send a session"
        )
    if not _same_address(config.address, address):
        raise RuntimeError(
            f"the SDK is configured for {config.address}, not {address}: point it at the "
            "gateway being verified (NEST_PROFILE=hosted, or NEST_CONTROLLER_ADDRESS); "
            "no session was read and nothing was submitted"
        )
    sdk_roots = _read(config.tls_ca_file) if config.tls_ca_file else config.tls_root_certificates
    expected = _read(ca_file) if ca_file else None
    if sdk_roots != expected:
        raise RuntimeError(
            "the SDK trusts different TLS roots than this run ("
            + ("--ca-file" if ca_file else "system roots")
            + "); set NEST_CONTROLLER_TLS_CA_FILE to match"
        )


def check_sdk_target(address: str, ca_file: str | None):
    from nest.config import load_client_config

    config = load_client_config()
    require_sdk_target(config, address, ca_file)
    return config


def check_signed_in(config, timeout: float) -> dict:
    from nest.client import _channel as sdk_channel
    from nest.client import _session
    from nest.project_selection import list_eligible

    session = _session(config, interactive=False)
    if session is None:
        raise RuntimeError("not signed in to the Nest these settings name; run nest login")
    with sdk_channel(config) as channel:
        projects = list_eligible(channel, session.access_token, timeout=timeout)
    return {"controller": config.address, "eligible_projects": len(projects or [])}


def check_execute(address: str, ca_file: str | None) -> dict:
    from nest import remote
    from nest.config import load_client_config

    # The SDK reads its settings again when it executes: confirm they still name
    # the verified gateway immediately before submitting anything.
    require_sdk_target(load_client_config(), address, ca_file)

    @remote(cpu=1)
    def hello() -> str:
        return "nest-public-grpc-ok"

    result = hello.remote()
    if result != "nest-public-grpc-ok":
        raise RuntimeError(f"unexpected result {result!r}")
    return {"result": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("address", help="the gateway, such as grpc.staging.example.org:443")
    parser.add_argument("--ca-file", help="PEM roots of a staging CA (default: system roots)")
    parser.add_argument(
        "--origin", action="append", default=[], help="HOST:PORT that must be unreachable"
    )
    parser.add_argument("--signed-in", action="store_true", help="list your eligible Projects")
    parser.add_argument("--execute", action="store_true", help="run one remote function")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args(argv)

    checks = [
        ("tls", lambda: check_tls(args.address, args.ca_file, args.timeout)),
        (
            "tls_wrong_name_refused",
            lambda: check_wrong_name(args.address, args.ca_file, args.timeout),
        ),
        ("tls_http1_only_not_h2", lambda: check_http1(args.address, args.ca_file, args.timeout)),
        (
            "unauthenticated_refused",
            lambda: check_unauthenticated(args.address, args.ca_file, args.timeout),
        ),
        (
            "non_nest_path_refused",
            lambda: check_non_nest_path(args.address, args.ca_file, args.timeout),
        ),
    ]
    for origin in args.origin:
        checks.append(
            (f"origin_unreachable_{origin}", lambda o=origin: check_origin(o, args.timeout))
        )
    verified: dict = {}

    def sdk_target() -> dict:
        verified["config"] = check_sdk_target(args.address, args.ca_file)
        return {"controller": verified["config"].address}

    def needs_target(check):
        def run() -> dict:
            if "config" not in verified:
                raise RuntimeError("not run: the SDK does not target the verified gateway")
            return check(verified["config"])

        return run

    if args.signed_in or args.execute:
        checks.append(("sdk_target", sdk_target))
    if args.signed_in:
        checks.append(
            ("signed_in_projects", needs_target(lambda c: check_signed_in(c, args.timeout)))
        )
    if args.execute:
        checks.append(
            ("remote_execution", needs_target(lambda c: check_execute(args.address, args.ca_file)))
        )

    failed = 0
    for name, check in checks:
        try:
            result = {"check": name, "passed": True, **check()}
        except Exception as exc:  # noqa: BLE001 - every failure is reported, not raised
            failed += 1
            result = {"check": name, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result, sort_keys=True), flush=True)
    print(json.dumps({"summary": "fail" if failed else "pass", "failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
