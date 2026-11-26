"""
Destination and host-key checks shared by the D03 egress probe and relay contract.

Synthetic reference code for the feasibility spike: standard library only, no
network access of its own. A destination is acceptable only when every address its
name resolves to is a globally routable unicast address on an allowlisted port and,
when the operator recorded approved addresses, one of those. Tailscale's CGNAT range
(100.64.0.0/10) is never acceptable: Mode B must not ride the Mode A Tailnet.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

DEFAULT_ALLOWED_PORTS = frozenset({22})
"""Login-node SSH ports a relay may open unless the operator approves another."""

TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_ULA = ipaddress.ip_network("fd7a:115c:a1e0::/48")

SSH_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)


class DestinationRejected(Exception):
    """The destination is not an approved public SSH endpoint. `reason` is a stable code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def address_problem(address: IPAddress, *, allow_loopback: bool = False) -> str | None:
    """Why `address` may not be a Mode B destination, or `None` if it may."""
    if address in TAILSCALE_CGNAT or address in TAILSCALE_ULA:
        return "tailnet_address"
    if allow_loopback and address.is_loopback:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address_problem(address.ipv4_mapped, allow_loopback=allow_loopback)
    if address.is_multicast or address.is_unspecified:
        return "not_unicast"
    if not address.is_global:
        return "not_public"
    return None


def validate_port(port: int, allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        raise DestinationRejected("invalid_port")
    if port not in set(allowed_ports):
        raise DestinationRejected("port_not_allowlisted", str(port))
    return port


def validate_addresses(
    addresses: Sequence[str],
    *,
    approved: Iterable[str] | None = None,
    allow_loopback: bool = False,
) -> list[IPAddress]:
    """
    Check every resolved address, not just the first.

    A name that resolves to a mix of public and private addresses is rejected
    outright: a partial answer is how a rebinding attack looks. The caller must then
    connect to one of the returned address literals and must not resolve the name
    again between this check and `connect()`.

    Raises:
        `DestinationRejected`: `no_addresses`, `tailnet_address`, `not_public`,
            `not_unicast` or `address_not_approved`.
    """
    if not addresses:
        raise DestinationRejected("no_addresses")
    parsed: list[IPAddress] = []
    for text in dict.fromkeys(addresses):
        address = ipaddress.ip_address(text.split("%", 1)[0])
        problem = address_problem(address, allow_loopback=allow_loopback)
        if problem:
            raise DestinationRejected(problem, str(address))
        parsed.append(address)
    if approved is not None:
        allowed = {ipaddress.ip_address(a) for a in approved}
        stray = [str(a) for a in parsed if a not in allowed]
        if stray:
            raise DestinationRejected("address_not_approved", ",".join(stray))
    return parsed


type Resolver = Callable[[str, int], Sequence[str]]


def resolve_once(
    host: str,
    port: int,
    resolver: Resolver,
    *,
    approved: Iterable[str] | None = None,
    allow_loopback: bool = False,
) -> list[IPAddress]:
    """Resolve `host` exactly once and validate every answer (see `validate_addresses`)."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return validate_addresses([str(literal)], approved=approved, allow_loopback=allow_loopback)
    try:
        answers = resolver(host, port)
    except OSError as error:
        raise DestinationRejected("dns_failed", type(error).__name__) from None
    return validate_addresses(answers, approved=approved, allow_loopback=allow_loopback)


@dataclass(frozen=True)
class ScannedKey:
    """One public host key as `ssh-keyscan` prints it, and its OpenSSH SHA256 fingerprint."""

    host: str
    key_type: str
    fingerprint: str


def fingerprint(blob_b64: str) -> str:
    """OpenSSH's `SHA256:` fingerprint of a base64 public-key blob, as `ssh-keygen -l` prints."""
    blob = base64.b64decode(blob_b64, validate=True)
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def parse_keyscan(text: str) -> list[ScannedKey]:
    """
    Parse `ssh-keyscan` output (`host keytype base64` lines; `#` lines are comments).

    `ssh-keyscan` performs key exchange only and never authenticates, so it fits a
    no-login smoke. Its result is an *observation* from the same network path: it
    must be compared with a fingerprint obtained independently, never trusted alone.
    """
    keys: list[ScannedKey] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3 or parts[1] not in SSH_KEY_TYPES:
            raise ValueError("Not ssh-keyscan output")
        try:
            keys.append(ScannedKey(parts[0], parts[1], fingerprint(parts[2])))
        except binascii.Error, ValueError:
            raise ValueError("Not ssh-keyscan output") from None
    return keys


def normalize_pin(pin: str) -> str:
    pin = pin.strip()
    if not pin.startswith("SHA256:") or len(pin) != 50:
        raise ValueError("A host-key pin is an OpenSSH SHA256 fingerprint: SHA256:<43 characters>")
    return pin


def match_pins(keys: Sequence[ScannedKey], pins: Iterable[str]) -> tuple[list[str], list[str]]:
    """Return (matched pins, observed fingerprints that no pin covers)."""
    wanted = {normalize_pin(p) for p in pins}
    seen = {k.fingerprint for k in keys}
    return sorted(wanted & seen), sorted(seen - wanted)
