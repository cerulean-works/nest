#!/usr/bin/env python3
"""
No-job D03 egress smoke: can the Controller reach an approved login node's SSH port?

Run it **in the Controller's own network namespace** (for a Docker deployment, a
disposable container sharing the Controller container's network; for a native
Controller, on its host) and only after the Platform operator approved the check.
Output from anywhere else is never acceptance evidence. It:

1. resolves the login node once and requires every address to be public, not on the
   Tailnet, and one the operator recorded (`--approved-address`, required live);
2. for **each** validated address separately: opens one TCP connection to the
   literal, fails if the chosen source address is on the Tailnet, and reads the
   server's SSH identification line (RFC 4253 section 4.2), then closes. It **sends
   no bytes at all**: no client banner, no username, no authentication;
3. with `--keyscan`, runs `ssh-keyscan` (key exchange only, no authentication)
   against each of those literals and requires a `--pin` to match a key observed
   at *that* address. `--keyscan-file` reads operator-supplied output instead; it
   is reported as a historical observation and never makes a run acceptance-eligible.

It never logs in, never runs a command, never submits a Slurm job and reads no
credential. Output is one JSON object per check plus a summary that records where
it ran (`--context`, hostname, network namespace, probe checksum) and whether the
run is `acceptance_eligible`. `evidence` is `synthetic` with `--fixture` (loopback
fixtures allowed) and `live` otherwise. Exits 1 if any check fails. A key or banner
proves only what answered at an address, never anyone's HPC identity.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from egress_policy import (
    TAILSCALE_CGNAT,
    TAILSCALE_ULA,
    DestinationRejected,
    IPAddress,
    ScannedKey,
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
    """
    Read up to the server's `SSH-` line without sending anything.

    Bounded by `deadline` and by `MAX_BANNER_BYTES` in total, so a peer that
    floods short lines or trickles bytes fails instead of hanging the smoke.
    """
    buffer = b""
    total = 0
    while True:
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.startswith(b"SSH-"):
                return line.rstrip(b"\r").decode("ascii", "replace")
        if total > MAX_BANNER_BYTES:
            raise ValueError("no identification line within 8 KiB")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("no identification line before --timeout")
        sock.settimeout(remaining)
        chunk = sock.recv(1024)
        if not chunk:
            raise ValueError("connection closed before an identification line")
        total += len(chunk)
        buffer += chunk


def keyscan_hosts(entry: str, port: int) -> list[tuple[str, int]]:
    """The `(address text, port)` pairs in a keyscan host column (`a,b` or `[a]:p`)."""
    pairs = []
    for item in entry.split(","):
        if item.startswith("|"):
            raise ValueError("hashed keyscan hosts cannot be checked; run without -H")
        if item.startswith("["):
            address, _, rest = item[1:].partition("]")
            pairs.append((address, int(rest.removeprefix(":")) if rest else port))
        else:
            pairs.append((item, 22))
    return pairs


def check_observed_at(
    keys: Sequence[ScannedKey], addresses: Sequence[IPAddress], port: int
) -> list[str]:
    """
    Require every scanned key to have been observed at a validated address and port.

    A key scanned by name was looked up outside the probe's policy (it could have
    gone over the Tailnet), and a stale or foreign file proves nothing, so both fail.
    """
    allowed = set(addresses)
    observed = set()
    for key in keys:
        for text, key_port in keyscan_hosts(key.host, port):
            try:
                address = ipaddress.ip_address(text)
            except ValueError:
                raise ValueError(f"keyscan_host_mismatch: {text} is not an address literal")
            if address not in allowed or key_port != port:
                raise ValueError(f"keyscan_host_mismatch: {text}:{key_port} was not validated")
            shown = f"[{address}]" if address.version == 6 else str(address)
            observed.add(f"{shown}:{key_port}")
    return sorted(observed)


def run_keyscan(command: str, address: IPAddress, port: int, timeout: float) -> str:
    """Run `ssh-keyscan` against the validated literal: no shell, no name lookup."""
    seconds = str(max(1, int(timeout)))
    argv = [command, "-p", str(port), "-T", seconds, "-t", "ed25519,ecdsa,rsa", str(address)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout + 5, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(f"ssh-keyscan returned no keys (exit {result.returncode})")
    return result.stdout


def keys_at(keys: Sequence[ScannedKey], address: IPAddress, port: int) -> list[ScannedKey]:
    """The scanned keys whose host column names exactly `address` on `port`."""
    found = []
    for key in keys:
        for text, key_port in keyscan_hosts(key.host, port):
            try:
                if ipaddress.ip_address(text) == address and key_port == port:
                    found.append(key)
                    break
            except ValueError:
                continue
    return found


def network_context() -> dict[str, Any]:
    """Where the probe ran, so a reviewer can match it to the Controller's namespace."""
    context: dict[str, Any] = {"hostname": socket.gethostname()}
    try:
        context["net_namespace"] = Path("/proc/self/ns/net").readlink().as_posix()
    except OSError:
        context["net_namespace"] = None
    context["probe_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return context


ACCEPTANCE_CONTEXTS = frozenset({"controller-namespace", "native-host"})
"""Contexts whose output can count as Controller egress evidence (see the runbook)."""


def run(argv: Sequence[str] | None = None, *, out: TextIO = sys.stdout, resolver=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--host", required=True, help="approved login-node name or address")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--allowed-port", type=int, action="append", help="default: 22 only")
    parser.add_argument("--approved-address", action="append", help="operator-approved IP")
    parser.add_argument("--pin", action="append", default=[], help="independent SHA256: pin")
    parser.add_argument(
        "--context",
        choices=["controller-namespace", "native-host", "other"],
        help="where this runs; required live (see the runbook)",
    )
    scan = parser.add_mutually_exclusive_group()
    scan.add_argument(
        "--keyscan", action="store_true", help="run ssh-keyscan against each validated address"
    )
    scan.add_argument(
        "--keyscan-file",
        type=Path,
        help="operator-supplied ssh-keyscan output (historical; never acceptance evidence)",
    )
    parser.add_argument("--keyscan-command", default="ssh-keyscan", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--fixture", action="store_true", help="allow loopback fixtures")
    args = parser.parse_args(argv)
    resolver = resolver or system_resolver
    evidence = "synthetic" if args.fixture else "live"
    context = "fixture" if args.fixture else args.context
    failures = 0
    tested: list[str] = []

    def report(check: str, ok: bool, **fields: Any) -> None:
        nonlocal failures
        failures += not ok
        record = {"check": check, "ok": ok, "evidence": evidence, **fields}
        print(json.dumps(record, sort_keys=True), file=out, flush=True)

    def summary(*, scanned: bool) -> int:
        blockers = []
        if evidence != "live":
            blockers.append("synthetic run")
        if context not in ACCEPTANCE_CONTEXTS:
            blockers.append("not run in the Controller's network namespace")
        if not scanned:
            blockers.append("no built-in --keyscan against every tested address")
        if failures:
            blockers.append("a check failed")
        record = {
            "summary": True,
            "ok": failures == 0,
            "failures": failures,
            "evidence": evidence,
            "context": context,
            "network": network_context(),
            "addresses_tested": tested,
            "acceptance_eligible": not blockers,
            "acceptance_blockers": blockers,
            "started_at": started,
            "slurm_jobs_submitted": 0,
            "logins_attempted": 0,
        }
        print(json.dumps(record, sort_keys=True), file=out, flush=True)
        return 0 if failures == 0 else 1

    started = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        pins = [normalize_pin(p) for p in args.pin]
        if not args.fixture and not args.approved_address:
            raise ValueError("a live run needs the operator-approved --approved-address set")
        if not args.fixture and context is None:
            raise ValueError("a live run needs --context (where the probe runs)")
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
        return summary(scanned=False)
    except ValueError as error:
        report("arguments", False, detail=str(error))
        return summary(scanned=False)
    report("destination", True, addresses=[str(a) for a in addresses], port=port)

    file_keys: list[ScannedKey] | None = None
    if args.keyscan_file is not None:
        try:
            file_keys = parse_keyscan(args.keyscan_file.read_text())
            check_observed_at(file_keys, addresses, port)
        except (OSError, ValueError) as error:
            report("host_key", False, source="operator_file", detail=str(error))
            file_keys = []

    # Every validated address is tested on its own; nothing is aggregated across them.
    for target in addresses:
        tested.append(str(target))
        probe_address(report, target, port, args.timeout)
        if args.keyscan:
            try:
                text = run_keyscan(args.keyscan_command, target, port, args.timeout)
                keys = parse_keyscan(text)
                check_observed_at(keys, [target], port)
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                report(
                    "host_key",
                    False,
                    address=str(target),
                    source="probe_keyscan",
                    detail=str(error) or type(error).__name__,
                )
                continue
            report_host_key(report, keys, target, port, pins, source="probe_keyscan")
        elif file_keys:
            report_host_key(
                report, keys_at(file_keys, target, port), target, port, pins, source="operator_file"
            )
    return summary(scanned=args.keyscan)


def probe_address(report, target: IPAddress, port: int, timeout: float) -> None:
    begin = time.monotonic()
    deadline = begin + timeout
    try:
        with socket.create_connection((str(target), port), timeout=timeout) as sock:
            local = ipaddress.ip_address(sock.getsockname()[0].split("%", 1)[0])
            on_tailnet = local in TAILSCALE_CGNAT or local in TAILSCALE_ULA
            report(
                "tcp_connect",
                not on_tailnet,
                address=str(target),
                local_address=str(local),
                connect_ms=round((time.monotonic() - begin) * 1000, 1),
                **({"reason": "tailnet_source"} if on_tailnet else {}),
            )
            try:
                ident = read_identification(sock, deadline)
            except (OSError, ValueError) as error:
                report(
                    "ssh_identification",
                    False,
                    address=str(target),
                    detail=str(error) or type(error).__name__,
                )
            else:
                report(
                    "ssh_identification",
                    ident.startswith(("SSH-2.0-", "SSH-1.99-")),
                    address=str(target),
                    identification=ident[:255],
                    bytes_sent=0,
                )
    except OSError as error:
        report("tcp_connect", False, address=str(target), detail=type(error).__name__)


def report_host_key(
    report,
    keys: Sequence[ScannedKey],
    target: IPAddress,
    port: int,
    pins: Sequence[str],
    *,
    source: str,
) -> None:
    """One `host_key` record for one address: a pin must match a key seen *there*."""
    fields: dict[str, Any] = {"address": str(target), "source": source}
    if source == "operator_file":
        fields["note"] = "historical observation supplied by the operator, not current evidence"
    else:
        fields["note"] = "observed by this run at this address; trust comes only from the pin"
    if not pins:
        report("host_key", False, detail="no independent --pin to compare with", **fields)
        return
    if not keys:
        report("host_key", False, detail="no key observed at this address", **fields)
        return
    matched, unpinned = match_pins(keys, pins)
    shown = f"[{target}]" if target.version == 6 else str(target)
    report(
        "host_key",
        bool(matched),
        matched=matched,
        unpinned_observed=unpinned,
        observed_at=[f"{shown}:{port}"],
        **fields,
    )


if __name__ == "__main__":
    sys.exit(run())
