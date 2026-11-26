#!/usr/bin/env python3
"""
No-job D03 egress smoke: can this host reach an approved login node's SSH port?

Run it **on the Controller host** (so it uses the Controller's egress path and source
IP) and only after the Platform operator approved the check. It:

1. resolves the login node once and requires every address to be public, not on the
   Tailnet, and (with `--approved-address`) one the operator recorded;
2. opens one TCP connection to a validated address literal, with Tailscale not used;
3. reads the server's SSH identification line (RFC 4253 section 4.2), then closes.
   It **sends no bytes at all**: no client banner, no username, no authentication;
4. with `--keyscan-file`, compares `ssh-keyscan` output with the independently
   obtained `--pin` fingerprints.

It never logs in, never runs a command, never submits a Slurm job and reads no
credential. Output is one JSON object per check plus a summary. `evidence` is
`synthetic` with `--fixture` (loopback fixtures allowed) and `live` otherwise.
Exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from egress_policy import (
    DestinationRejected,
    match_pins,
    normalize_pin,
    parse_keyscan,
    resolve_once,
    validate_port,
)

MAX_BANNER_BYTES = 8192
"""RFC 4253 lets a server send other lines before its identification; cap them."""


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


def read_identification(sock: socket.socket, deadline: float) -> str:
    """Read up to the server's `SSH-` line without sending anything."""
    buffer = b""
    while True:
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.startswith(b"SSH-"):
                return line.rstrip(b"\r").decode("ascii", "replace")
        if len(buffer) > MAX_BANNER_BYTES:
            raise ValueError("no identification line within 8 KiB")
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        chunk = sock.recv(1024)
        if not chunk:
            raise ValueError("connection closed before an identification line")
        buffer += chunk


def run(argv: Sequence[str] | None = None, *, out: TextIO = sys.stdout, resolver=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--host", required=True, help="approved login-node name or address")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--allowed-port", type=int, action="append", help="default: 22 only")
    parser.add_argument("--approved-address", action="append", help="operator-approved IP")
    parser.add_argument("--pin", action="append", default=[], help="independent SHA256: pin")
    parser.add_argument("--keyscan-file", type=Path, help="output of ssh-keyscan for --host")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--fixture", action="store_true", help="allow loopback fixtures")
    args = parser.parse_args(argv)
    resolver = resolver or system_resolver
    evidence = "synthetic" if args.fixture else "live"
    failures = 0

    def report(check: str, ok: bool, **fields: Any) -> None:
        nonlocal failures
        failures += not ok
        record = {"check": check, "ok": ok, "evidence": evidence, **fields}
        print(json.dumps(record, sort_keys=True), file=out, flush=True)

    started = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        pins = [normalize_pin(p) for p in args.pin]
        port = validate_port(args.port, args.allowed_port or {22})
        addresses = resolve_once(
            args.host,
            port,
            resolver,
            approved=args.approved_address,
            allow_loopback=args.fixture,
        )
    except DestinationRejected as error:
        report("destination", False, reason=error.reason, detail=str(error))
        return _summary(out, started, evidence, failures)
    except ValueError as error:
        report("arguments", False, detail=str(error))
        return _summary(out, started, evidence, failures)
    report("destination", True, addresses=[str(a) for a in addresses], port=port)

    target = addresses[0]
    begin = time.monotonic()
    deadline = begin + args.timeout
    try:
        with socket.create_connection((str(target), port), timeout=args.timeout) as sock:
            local = sock.getsockname()[0]
            report(
                "tcp_connect",
                True,
                address=str(target),
                local_address=local,
                connect_ms=round((time.monotonic() - begin) * 1000, 1),
            )
            try:
                ident = read_identification(sock, deadline)
            except (OSError, ValueError) as error:
                report("ssh_identification", False, detail=str(error) or type(error).__name__)
            else:
                report(
                    "ssh_identification",
                    ident.startswith(("SSH-2.0-", "SSH-1.99-")),
                    identification=ident[:255],
                    bytes_sent=0,
                )
    except OSError as error:
        report("tcp_connect", False, address=str(target), detail=type(error).__name__)

    if args.keyscan_file is not None:
        try:
            keys = parse_keyscan(args.keyscan_file.read_text())
        except (OSError, ValueError) as error:
            report("host_key", False, detail=str(error))
        else:
            if not pins:
                report("host_key", False, detail="no independent --pin to compare with")
            else:
                matched, unpinned = match_pins(keys, pins)
                report(
                    "host_key",
                    bool(matched),
                    matched=matched,
                    unpinned_observed=unpinned,
                    note="observation from this path; trust comes only from the pin",
                )
    return _summary(out, started, evidence, failures)


def _summary(out: TextIO, started: str, evidence: str, failures: int) -> int:
    record = {
        "summary": True,
        "ok": failures == 0,
        "failures": failures,
        "evidence": evidence,
        "started_at": started,
        "slurm_jobs_submitted": 0,
        "logins_attempted": 0,
    }
    print(json.dumps(record, sort_keys=True), file=out, flush=True)
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
