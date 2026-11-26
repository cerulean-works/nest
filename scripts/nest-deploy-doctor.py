#!/usr/bin/env python3
"""
Read-only deployment checks and a bounded migrate recovery for `compose.dokploy.yaml`.

Run it on the Docker host as a user who may use the Docker CLI. It needs only Python
3.9+ and `docker` (and `ip` for the route check); it does not import Nest.

    python3 scripts/nest-deploy-doctor.py preflight      --project APP --env-file .env
    python3 scripts/nest-deploy-doctor.py migrate-status --project APP [--logs 40]
    python3 scripts/nest-deploy-doctor.py wait-migrate   --project APP --timeout 300 [--since T]
    python3 scripts/nest-deploy-doctor.py recover-migrate --project APP [--apply]
    python3 scripts/nest-deploy-doctor.py log-leak-check --project APP < token-file

`--project` is the Compose project name: Dokploy's application name (its `appName`,
shown by `docker compose ls`), or `nest` for a plain `docker compose` run of the
file. `--env-file` is the `.env` Dokploy writes next to the Compose file; values are
read literally and only the network settings below are ever printed.

preflight
    Before a deploy that creates or changes `grpc-origin`. Checks that
    `NEST_GRPC_ORIGIN_SUBNET` is a valid IPv4 network that no other Docker network and
    no host route overlaps, that `NEST_GRPC_GATEWAY_ADDRESS` is a usable host address
    in it, and, with the `public-grpc` profile, that `NEST_GRPC_TRUSTED_PROXIES` is
    exactly the gateway's /32, `NEST_GRPC_ALLOWED_PEERS` admits the gateway and the
    Tailnet bridge, `NEST_GRPC_EDGE_PROXY` is the running edge proxy's /32, and which
    Traefik passthrough file matches the installed Traefik. It never creates, changes
    or deletes anything.

migrate-status
    Lists this project's `migrate` containers and classifies each: `completed`,
    `failed` (non-zero exit), `running`, `waiting-dependency` (not started because
    this project's PostgreSQL is not healthy yet: normal while it starts),
    `pending` (created less than `--min-age` seconds ago), or `stuck-created`
    (never started, PID 0, no start time, no logs, although PostgreSQL is healthy or
    absent and the container is older than `--min-age`). `--logs N` adds the last N
    log lines (at most 200) of failed or running containers.

wait-migrate
    Waits, bounded by `--timeout`, for a migration that finishes after `--since`
    (default: when the command starts, so start it as you press Deploy; a previous
    deploy's finished container never counts). Exits 0 when it completed, 4 when it
    failed, 3 when a container stays `stuck-created` for `--stuck-after` seconds,
    5 on timeout. It starts and stops nothing.

recover-migrate
    For `stuck-created` containers only, never one waiting for PostgreSQL or younger
    than `--min-age`. It checks the state again after `--settle` seconds, prints the
    exact `docker rm` it would run, and with `--apply` runs it. `docker rm` without `-f` or `-v` removes only a
    container that is not running and never touches a volume. The migration is not
    skipped: the next deploy creates a fresh `migrate` container, runs
    `nest-db upgrade` again, and the Controller still starts only after it succeeds.
    A deploy that is still waiting on the removed container fails at once instead of
    waiting forever; deploy again afterwards.

log-leak-check
    Counts how often secret values occur in the logs of this project's containers (all
    of them, or each `--service`), in memory. A value is typed without echo, or read one
    per line from standard input, never from the command line, and is never printed.
    `--canary` searches for a synthetic non-secret marker instead. Exits 1 if it
    occurs anywhere, and 2 if any log cannot be read or a service has no container,
    so a read failure never passes.

Exit codes: 0 ok, 1 a check failed, 2 usage or Docker error, 3 stuck, 4 migration
failed, 5 still in progress or timed out, 6 no migrate container found.
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

DEFAULT_ORIGIN_SUBNET = "172.31.250.0/28"
DEFAULT_GATEWAY_ADDRESS = "172.31.250.2"
DEFAULT_EDGE_NETWORK = "dokploy-network"
DEFAULT_TRAEFIK_CONTAINER = "dokploy-traefik"
NEVER_STARTED = "0001-01-01T00:00:00Z"
MAX_LOG_LINES = 200
# Longer than compose.dokploy.yaml's PostgreSQL health window (start_period 30 s,
# then up to 30 retries 5 s apart): a never-started migrate container younger than
# this may still be about to start.
DEFAULT_MIN_AGE = 300.0
DEFAULT_SETTLE = 10.0

# Settings read from the environment file. Only the network ones are printed.
SETTINGS = (
    "COMPOSE_PROFILES",
    "NEST_GRPC_ORIGIN_SUBNET",
    "NEST_GRPC_GATEWAY_ADDRESS",
    "NEST_GRPC_TRUSTED_PROXIES",
    "NEST_GRPC_ALLOWED_PEERS",
    "NEST_GRPC_EDGE_PROXY",
    "NEST_GRPC_EDGE_NETWORK",
    "NEST_PUBLIC_PILOT",
)

Runner = Callable[[Sequence[str]], "Result"]


class DockerError(RuntimeError):
    """A Docker command failed or could not run."""


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str = ""


def run_command(argv: Sequence[str], timeout: float = 30.0) -> Result:
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as error:
        raise DockerError(f"{argv[0]} is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise DockerError(f"{' '.join(argv[:3])} did not answer in {timeout:.0f} s") from error
    return Result(done.returncode, done.stdout, done.stderr)


@dataclass
class Check:
    name: str
    status: str  # PASS, FAIL, WARN or SKIP
    detail: str

    def line(self) -> str:
        return f"{self.status:<4} {self.name}: {self.detail}"


@dataclass
class Docker:
    run: Runner = run_command

    def _ok(self, argv: Sequence[str]) -> str:
        result = self.run(argv)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip().splitlines()
            raise DockerError(f"{' '.join(argv[:3])} failed: {message[-1] if message else ''}")
        return result.stdout

    def networks(self) -> list[dict]:
        ids = self._ok(["docker", "network", "ls", "--quiet", "--no-trunc"]).split()
        if not ids:
            return []
        return json.loads(self._ok(["docker", "network", "inspect", *ids]))

    def containers(self, project: str, service: str) -> list[dict]:
        ids = self._ok(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--filter",
                f"label=com.docker.compose.service={service}",
            ]
        ).split()
        if not ids:
            return []
        return json.loads(self._ok(["docker", "container", "inspect", *ids]))

    def container(self, name: str) -> dict | None:
        result = self.run(["docker", "container", "inspect", name])
        if result.returncode != 0:
            return None
        found = json.loads(result.stdout)
        return found[0] if found else None

    def logs(self, container_id: str, lines: int) -> str:
        result = self.run(["docker", "logs", "--tail", str(lines), container_id])
        return (result.stdout + result.stderr).rstrip()

    def project_containers(self, project: str) -> list[dict]:
        ids = self._ok(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ]
        ).split()
        if not ids:
            return []
        return json.loads(self._ok(["docker", "container", "inspect", *ids]))

    def full_logs(self, container_id: str, since: str | None) -> str:
        """Every log line of a container, both streams; raises when Docker cannot
        read them (so a read failure never looks like a clean result)."""
        argv = ["docker", "logs"]
        if since:
            argv += ["--since", since]
        result = self.run([*argv, container_id])
        if result.returncode != 0:
            raise DockerError(f"could not read the logs of {container_id[:12]}")
        return result.stdout + result.stderr

    def remove(self, container_id: str) -> None:
        self._ok(["docker", "rm", container_id])


def read_env_file(path: str | None) -> dict[str, tuple[str, bool]]:
    """Reads the KEY=VALUE lines of the settings this tool checks, without expanding
    anything: one pair of quotes removed. Each value comes with whether Compose would
    interpolate it (a `$` outside single quotes), which this tool does not evaluate."""
    values: dict[str, tuple[str, bool]] = {}
    if not path:
        return values
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export ") :].strip()
            value = value.strip()
            literal = False
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                literal = value[0] == "'"
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            if key in SETTINGS:
                values[key] = (value, "$" in value and not literal)
    return values


def settings(env_file: str | None, environ: dict[str, str]) -> tuple[dict[str, str], list[Check]]:
    """The values Compose would use for the checked settings, with Compose's
    precedence: a variable set in the invoking shell's environment wins over the
    `.env` file, even when it is empty. An empty or unset value then takes the
    file's default, as `${NAME:-default}` does. A `.env` value that Compose would
    interpolate is refused, not guessed."""
    from_file = read_env_file(env_file)
    values: dict[str, str] = {}
    checks: list[Check] = []
    overridden = []
    for key in SETTINGS:
        if key in environ:
            value = environ[key]
            if key in from_file and from_file[key][0] != value:
                overridden.append(key)
        elif key in from_file:
            value, interpolated = from_file[key]
            if interpolated:
                checks.append(
                    Check(
                        "settings",
                        "FAIL",
                        f"{key} in {env_file} contains `$`, which Compose interpolates and "
                        "this tool does not; write the literal value in single quotes",
                    )
                )
                continue
        else:
            continue
        if value.strip():
            values[key] = value.strip()
    if overridden:
        checks.append(
            Check(
                "settings",
                "WARN",
                f"the shell environment overrides {env_file or '.env'} for "
                f"{', '.join(overridden)}, as it does for `docker compose`; checked the "
                "shell's values. Dokploy deploys from .env alone: unset them to check that.",
            )
        )
    return values, checks


def _cidrs(value: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [
        ipaddress.ip_network(part.strip(), strict=False)
        for part in value.split(",")
        if part.strip()
    ]


def _ipv4(networks: list) -> list[ipaddress.IPv4Network]:
    return [n for n in networks if isinstance(n, ipaddress.IPv4Network)]


def _subnets(network: dict) -> list[ipaddress.IPv4Network]:
    """The IPv4 subnets of a Docker network (grpc-origin and the checks are IPv4)."""
    found = []
    for config in (network.get("IPAM") or {}).get("Config") or []:
        subnet = config.get("Subnet")
        if subnet:
            try:
                found.append(ipaddress.ip_network(subnet, strict=False))
            except ValueError:
                continue
    return _ipv4(found)


def _bridge_name(network: dict) -> str:
    options = network.get("Options") or {}
    return options.get("com.docker.network.bridge.name") or "br-" + network.get("Id", "")[:12]


def check_origin(
    values: dict[str, str], project: str, networks: list[dict], routes: list[dict] | None
) -> list[Check]:
    checks: list[Check] = []
    subnet_text = values.get("NEST_GRPC_ORIGIN_SUBNET", DEFAULT_ORIGIN_SUBNET)
    gateway_text = values.get("NEST_GRPC_GATEWAY_ADDRESS", DEFAULT_GATEWAY_ADDRESS)
    try:
        subnet = ipaddress.IPv4Network(subnet_text, strict=True)
    except ValueError as error:
        return [Check("origin_subnet", "FAIL", f"NEST_GRPC_ORIGIN_SUBNET={subnet_text}: {error}")]
    if not 16 <= subnet.prefixlen <= 29:
        checks.append(
            Check("origin_subnet", "FAIL", f"{subnet} must be a /16 to /29 (it needs a few hosts)")
        )
    else:
        checks.append(Check("origin_subnet", "PASS", f"{subnet}"))
    try:
        gateway = ipaddress.IPv4Address(gateway_text)
    except ValueError as error:
        checks.append(Check("gateway_address", "FAIL", f"NEST_GRPC_GATEWAY_ADDRESS: {error}"))
        gateway = None
    if gateway is not None:
        hosts = list(subnet.hosts()) if subnet.prefixlen <= 30 else []
        if gateway not in subnet:
            checks.append(Check("gateway_address", "FAIL", f"{gateway} is not in {subnet}"))
        elif gateway in (subnet.network_address, subnet.broadcast_address):
            checks.append(
                Check(
                    "gateway_address",
                    "FAIL",
                    f"{gateway} is {subnet}'s network or broadcast address",
                )
            )
        elif hosts and gateway == hosts[0]:
            checks.append(
                Check(
                    "gateway_address",
                    "FAIL",
                    f"{gateway} is the bridge's own gateway address in {subnet}; use the second host",
                )
            )
        else:
            checks.append(Check("gateway_address", "PASS", f"{gateway} in {subnet}"))

    own_name = f"{project}_grpc-origin"
    own_bridges = set()
    collisions = []
    for network in networks:
        name = network.get("Name", "?")
        for existing in _subnets(network):
            if not existing.overlaps(subnet):
                continue
            if name == own_name:
                own_bridges.add(_bridge_name(network))
                if existing == subnet:
                    checks.append(
                        Check("origin_network", "PASS", f"{name} already exists with {subnet}")
                    )
                else:
                    checks.append(
                        Check(
                            "origin_network",
                            "WARN",
                            f"{name} exists with {existing}; Compose will recreate it with {subnet}, restarting the Controller",
                        )
                    )
            else:
                collisions.append(f"{name} ({existing})")
    if collisions:
        checks.append(
            Check(
                "origin_subnet_free",
                "FAIL",
                f"{subnet} overlaps Docker network {', '.join(collisions)}; choose another NEST_GRPC_ORIGIN_SUBNET (and NEST_GRPC_GATEWAY_ADDRESS) that no network or route uses",
            )
        )
    else:
        checks.append(
            Check("origin_subnet_free", "PASS", f"no other Docker network overlaps {subnet}")
        )

    if routes is None:
        checks.append(
            Check(
                "origin_routes",
                "SKIP",
                "`ip -json route` unavailable; check `ip route show table all` by hand",
            )
        )
    else:
        overlapping = []
        for route in routes:
            dst = route.get("dst", "")
            if dst in ("", "default") or route.get("dev") in own_bridges:
                continue
            try:
                destination = ipaddress.ip_network(dst, strict=False)
            except ValueError:
                continue
            if (
                destination.version == 4
                and destination.prefixlen > 0
                and destination.overlaps(subnet)
            ):
                overlapping.append(f"{destination} dev {route.get('dev', '?')}")
        if overlapping:
            checks.append(
                Check(
                    "origin_routes",
                    "FAIL",
                    f"{subnet} overlaps host route {', '.join(overlapping)}",
                )
            )
        else:
            checks.append(Check("origin_routes", "PASS", f"no host route overlaps {subnet}"))
    return checks


def check_gateway(
    values: dict[str, str],
    project: str,
    networks: list[dict],
    docker: Docker,
    traefik_container: str,
) -> list[Check]:
    checks: list[Check] = []
    gateway_text = values.get("NEST_GRPC_GATEWAY_ADDRESS", DEFAULT_GATEWAY_ADDRESS)
    subnet_text = values.get("NEST_GRPC_ORIGIN_SUBNET", DEFAULT_ORIGIN_SUBNET)
    try:
        gateway = ipaddress.IPv4Network(f"{gateway_text}/32")
        subnet = ipaddress.IPv4Network(subnet_text)
    except ValueError:
        return [
            Check("gateway_settings", "FAIL", "fix the origin subnet and gateway address first")
        ]

    if values.get("NEST_PUBLIC_PILOT", "").lower() in ("1", "true", "yes", "on"):
        checks.append(Check("public_pilot", "PASS", "NEST_PUBLIC_PILOT is on"))
    else:
        checks.append(
            Check("public_pilot", "FAIL", "the public-grpc profile needs NEST_PUBLIC_PILOT=true")
        )

    try:
        trusted = _cidrs(values.get("NEST_GRPC_TRUSTED_PROXIES", ""))
    except ValueError as error:
        trusted = []
        checks.append(Check("trusted_proxies", "FAIL", f"NEST_GRPC_TRUSTED_PROXIES: {error}"))
    else:
        if trusted == [gateway]:
            checks.append(Check("trusted_proxies", "PASS", f"exactly {gateway}"))
        else:
            shown = ",".join(str(n) for n in trusted) or "unset"
            checks.append(
                Check(
                    "trusted_proxies",
                    "FAIL",
                    f"NEST_GRPC_TRUSTED_PROXIES is {shown}; it must be exactly {gateway}",
                )
            )

    tailnet = next((n for n in networks if n.get("Name") == f"{project}_tailnet"), None)
    tailnet_subnets = _subnets(tailnet) if tailnet else []
    try:
        peers = _cidrs(values.get("NEST_GRPC_ALLOWED_PEERS", ""))
    except ValueError as error:
        checks.append(Check("allowed_peers", "FAIL", f"NEST_GRPC_ALLOWED_PEERS: {error}"))
    else:
        problems = []
        peers4 = _ipv4(peers)
        if not any(gateway.subnet_of(p) for p in peers4):
            problems.append(f"does not admit the gateway {gateway}")
        for peer in peers4:
            if peer.overlaps(subnet) and not peer.subnet_of(subnet):
                problems.append(f"{peer} is wider than the origin subnet {subnet}")
        if tailnet is None:
            note = f"{project}_tailnet does not exist yet; rerun after the first deploy"
            checks.append(Check("allowed_peers_tailnet", "WARN", note))
        else:
            for tailnet_subnet in tailnet_subnets:
                if not any(tailnet_subnet.subnet_of(p) for p in peers4):
                    problems.append(
                        f"does not admit the Tailnet bridge {tailnet_subnet}, so Tailnet users would be refused"
                    )
        extra = [
            str(p)
            for p in peers4
            if not p.subnet_of(subnet) and not any(p.subnet_of(t) for t in tailnet_subnets)
        ] + [str(p) for p in peers if not isinstance(p, ipaddress.IPv4Network)]
        if problems:
            checks.append(
                Check("allowed_peers", "FAIL", "NEST_GRPC_ALLOWED_PEERS " + "; ".join(problems))
            )
        elif extra:
            checks.append(
                Check(
                    "allowed_peers",
                    "WARN",
                    f"also admits {', '.join(extra)}, outside the origin and Tailnet networks",
                )
            )
        else:
            checks.append(Check("allowed_peers", "PASS", ",".join(str(p) for p in peers)))

    edge_network = values.get("NEST_GRPC_EDGE_NETWORK", DEFAULT_EDGE_NETWORK)
    traefik = docker.container(traefik_container)
    edge_text = values.get("NEST_GRPC_EDGE_PROXY", "")
    if traefik is None:
        checks.append(
            Check(
                "edge_proxy", "FAIL", f"no container {traefik_container}; pass --traefik-container"
            )
        )
    else:
        attached = ((traefik.get("NetworkSettings") or {}).get("Networks") or {}).get(
            edge_network
        ) or {}
        actual = attached.get("IPAddress", "")
        if not actual:
            checks.append(
                Check("edge_proxy", "FAIL", f"{traefik_container} is not on {edge_network}")
            )
        else:
            expected = ipaddress.ip_network(f"{actual}/32")
            try:
                configured = ipaddress.ip_network(edge_text, strict=True) if edge_text else None
            except ValueError as error:
                configured = None
                edge_text = f"invalid ({error})"
            if configured == expected:
                checks.append(
                    Check(
                        "edge_proxy", "PASS", f"{expected} is {traefik_container} on {edge_network}"
                    )
                )
            else:
                checks.append(
                    Check(
                        "edge_proxy",
                        "FAIL",
                        f"NEST_GRPC_EDGE_PROXY is {edge_text or 'unset'}; {traefik_container} is {expected} on {edge_network}",
                    )
                )
        image = (traefik.get("Config") or {}).get("Image", "")
        checks.append(traefik_file_check(image))
    return checks


def traefik_file_check(image: str) -> Check:
    """Which passthrough file suits the Traefik image tag (see deploy/public-grpc/traefik)."""
    tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else ""
    parts = tag.lstrip("v").split(".")
    try:
        major, minor = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return Check(
            "traefik_version",
            "WARN",
            f"cannot tell the Traefik version from {image!r}; run `docker exec <traefik> traefik version` and pick the file as in docs/source/public-grpc-gateway.md (Route it)",
        )
    if (major, minor) >= (3, 5):
        chosen = "nest-grpc.servers-transport.yml"
    elif (major, minor) >= (2, 0):
        chosen = "nest-grpc.yml"
    else:
        return Check(
            "traefik_version", "FAIL", f"Traefik {tag} predates TCP PROXY protocol support"
        )
    return Check(
        "traefik_version", "PASS", f"Traefik {tag}: use deploy/public-grpc/traefik/{chosen}"
    )


def host_routes(run: Runner) -> list[dict] | None:
    if not shutil.which("ip") and run is run_command:
        return None
    try:
        result = run(["ip", "-json", "-4", "route", "show", "table", "all"])
    except DockerError:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "[]")
    except ValueError:
        return None


def preflight(args, docker: Docker, environ: dict[str, str]) -> int:
    values, checks = settings(args.env_file, environ)
    profiles = {p.strip() for p in values.get("COMPOSE_PROFILES", "").split(",") if p.strip()}
    networks = docker.networks()
    checks += check_origin(values, args.project, networks, host_routes(docker.run))
    if "public-grpc" in profiles:
        checks += check_gateway(values, args.project, networks, docker, args.traefik_container)
    else:
        checks.append(
            Check("public_grpc", "SKIP", "profile public-grpc is off; gateway settings not checked")
        )
    emit(checks, args.json)
    return 1 if any(c.status == "FAIL" for c in checks) else 0


def parse_time(value: str) -> float | None:
    """Seconds since the epoch for a Docker timestamp (RFC 3339, nanoseconds), or
    `None` for an empty or never-set one."""
    if not value or value.startswith("0001-01-01"):
        return None
    text = value.strip().replace("Z", "+00:00")
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", text)
    if match:
        # Python before 3.11 accepts only 3 or 6 fractional digits.
        digits = (match.group(2) or ".")[1:]
        fraction = "." + (digits + "000000")[:6] if digits else ""
        text = match.group(1) + fraction + match.group(3)
    moment = datetime.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)  # noqa: UP017 (Python 3.9+)
    return moment.timestamp()


@dataclass
class MigrateState:
    id: str
    name: str
    # completed, failed, running, waiting-dependency, pending, stuck-created, other
    state: str
    exit_code: int
    created: str
    started: str
    finished: str
    replacement: bool = False
    detail: str = ""
    logs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = dict(self.__dict__)
        data["id"] = self.id[:12]
        if not self.logs:
            data.pop("logs")
        return data

    def finished_after(self, moment: float) -> bool:
        finished = parse_time(self.finished)
        return finished is not None and finished >= moment


def dependency_health(docker: Docker, project: str) -> str | None:
    """Health of this project's PostgreSQL (`migrate` waits for it to be healthy), or
    `None` when the project has none (an external database)."""
    found = sorted(docker.containers(project, "postgres"), key=lambda c: c.get("Created", ""))
    if not found:
        return None
    state = found[-1].get("State") or {}
    health = (state.get("Health") or {}).get("Status")
    return health or state.get("Status") or "unknown"


def classify(
    container: dict,
    *,
    now: float | None = None,
    dependency: str | None = None,
    min_age: float = DEFAULT_MIN_AGE,
) -> MigrateState:
    state = container.get("State") or {}
    status = state.get("Status", "")
    name = container.get("Name", "").lstrip("/")
    started = state.get("StartedAt", "")
    finished = state.get("FinishedAt", "")
    prefix = name.split("_", 1)[0]
    replacement = "_" in name and len(prefix) == 12 and all(c in "0123456789abcdef" for c in prefix)
    exit_code = int(state.get("ExitCode", 0) or 0)
    created_at = parse_time(container.get("Created", ""))
    age = None if created_at is None else (time.time() if now is None else now) - created_at
    if status == "created" and not state.get("Pid") and started in ("", NEVER_STARTED):
        if dependency is not None and dependency != "healthy":
            kind, detail = (
                "waiting-dependency",
                (
                    f"not started yet: PostgreSQL is {dependency}, and Compose starts migrate "
                    "once it is healthy; check the postgres container if this lasts"
                ),
            )
        elif age is None or age < min_age:
            kind, detail = (
                "pending",
                "created recently and not started yet; Compose normally starts it within seconds",
            )
        else:
            kind, detail = (
                "stuck-created",
                (
                    f"created {age:.0f} s ago but never started (PID 0) although "
                    f"{'PostgreSQL is healthy' if dependency else 'it waits on nothing'}; "
                    "Compose waits on it forever"
                ),
            )
    elif status == "exited" and exit_code == 0:
        kind, detail = "completed", "nest-db upgrade finished"
    elif status in ("exited", "dead"):
        kind, detail = "failed", f"exited {exit_code}; the Controller will not start"
    elif status in ("running", "restarting"):
        kind, detail = "running", "nest-db upgrade is running"
    else:
        kind, detail = "other", f"state {status}"
    if replacement:
        detail += "; its name carries Compose's replacement prefix (an interrupted recreate)"
    return MigrateState(
        id=container.get("Id", ""),
        name=name,
        state=kind,
        exit_code=exit_code,
        created=container.get("Created", ""),
        started=started,
        finished=finished,
        replacement=replacement,
        detail=detail,
    )


def migrate_states(
    docker: Docker, project: str, min_age: float = DEFAULT_MIN_AGE, now: float | None = None
) -> list[MigrateState]:
    containers = docker.containers(project, "migrate")
    dependency = dependency_health(docker, project) if containers else None
    states = [classify(c, now=now, dependency=dependency, min_age=min_age) for c in containers]
    return sorted(states, key=lambda s: s.created)


def verdict(states: list[MigrateState]) -> int:
    if not states:
        return 6
    kinds = {s.state for s in states}
    if "stuck-created" in kinds:
        return 3
    newest = states[-1]
    if newest.state == "failed":
        return 4
    if newest.state in ("running", "waiting-dependency", "pending"):
        return 5
    return 0 if newest.state == "completed" else 1


def migrate_status(args, docker: Docker) -> int:
    states = migrate_states(docker, args.project, args.min_age)
    lines = min(max(args.logs, 0), MAX_LOG_LINES)
    for state in states:
        if lines and state.state in ("failed", "running"):
            state.logs = docker.logs(state.id, lines).splitlines()[-lines:]
    if args.json:
        for state in states:
            print(json.dumps(state.as_dict(), sort_keys=True))
    else:
        if not states:
            print(f"no migrate container in project {args.project} (is the migrate profile on?)")
        for state in states:
            print(f"{state.state:<18} {state.name} ({state.id[:12]}): {state.detail}")
            for line in state.logs:
                print(f"    | {line}")
    code = verdict(states)
    if code == 3 and not args.json:
        print(
            f"Recover with: python3 scripts/nest-deploy-doctor.py recover-migrate --project {args.project}"
        )
    return code


def wait_migrate(
    args, docker: Docker, clock=time.monotonic, sleep=time.sleep, wall=time.time
) -> int:
    start = clock()
    since = parse_time(args.since) if args.since else wall()
    if since is None:
        raise ValueError(f"--since must be an RFC 3339 time, got {args.since!r}")
    stuck_since: dict[str, float] = {}
    while True:
        states = migrate_states(docker, args.project, args.min_age, now=wall())
        now = clock()
        stuck = [s for s in states if s.state == "stuck-created"]
        for state in stuck:
            stuck_since.setdefault(state.id, now)
            if now - stuck_since[state.id] >= args.stuck_after:
                print(f"stuck: {state.name} ({state.id[:12]}) {state.detail}")
                return 3
        if states and not stuck:
            newest = states[-1]
            # A previous deploy's container does not count: only one that finished
            # after `since` is this deploy's migration.
            if newest.finished_after(since) and newest.state == "completed":
                print(f"completed: {newest.name}")
                return 0
            if newest.finished_after(since) and newest.state == "failed":
                print(
                    f"failed: {newest.name} exited {newest.exit_code}; see migrate-status --logs 40"
                )
                return 4
        if now - start >= args.timeout:
            print(
                f"timed out after {args.timeout:.0f} s without a migration that finished "
                "after --since; see migrate-status"
            )
            return 5
        sleep(args.interval)


def recover_migrate(args, docker: Docker, sleep=time.sleep) -> int:
    states = migrate_states(docker, args.project, args.min_age)
    stuck = [s for s in states if s.state == "stuck-created"]
    if not stuck:
        waiting = [s for s in states if s.state in ("waiting-dependency", "pending")]
        if waiting:
            print(
                f"nothing to recover: {waiting[0].name} is {waiting[0].state}: {waiting[0].detail}"
            )
        else:
            print("nothing to recover: no migrate container is stuck in Created")
        return verdict(states) if states else 6
    # Give Compose time to start it if PostgreSQL has only just become healthy.
    sleep(args.settle)
    dependency = dependency_health(docker, args.project)
    for state in stuck:
        # Re-read just before acting: the container must still be the same,
        # never-started migrate container of this project, still stuck.
        fresh = docker.container(state.id)
        labels = ((fresh or {}).get("Config") or {}).get("Labels") or {}
        if (
            fresh is None
            or labels.get("com.docker.compose.project") != args.project
            or labels.get("com.docker.compose.service") != "migrate"
            or classify(fresh, dependency=dependency, min_age=args.min_age).state != "stuck-created"
        ):
            print(f"skipped {state.name}: it changed since it was listed")
            continue
        command = f"docker rm {state.id[:12]}"
        if not args.apply:
            print(f"would run: {command}   # {state.name}, never started")
            continue
        docker.remove(state.id)
        print(f"removed {state.name} ({state.id[:12]}), which never started")
    if args.apply:
        print(
            "Deploy again (Dokploy: Deploy). Compose creates a new migrate container and the "
            "Controller starts only after nest-db upgrade succeeds."
        )
    else:
        print("Nothing changed. Rerun with --apply to remove it.")
    return 0


MIN_SECRET_LENGTH = 16


def read_secret(stdin=None, prompt=None) -> list[str]:
    """The values to look for: one typed without echo on a terminal, or one per line
    of standard input (for example piped over SSH from the client's credential store).
    Never a command-line argument."""
    import getpass

    stream = stdin or sys.stdin
    if stream.isatty():
        values = [(prompt or getpass.getpass)("Value to look for (not echoed): ")]
    else:
        values = stream.read().splitlines()
    return [v.strip() for v in values if v.strip()]


def log_leak_check(args, docker: Docker, stdin=None, prompt=None) -> int:
    if args.canary:
        needles = [args.canary]
    else:
        needles = read_secret(stdin, prompt)
        if not needles or any(len(n) < MIN_SECRET_LENGTH for n in needles):
            print(
                f"nest-deploy-doctor: give each full value (at least {MIN_SECRET_LENGTH} "
                "characters) on its own line of standard input; nothing was searched",
                file=sys.stderr,
            )
            return 2
    containers = docker.project_containers(args.project)
    wanted = set(args.service or [])
    selected = [
        c
        for c in containers
        if not wanted
        or ((c.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.service") in wanted
    ]
    found_services = {
        ((c.get("Config") or {}).get("Labels") or {}).get("com.docker.compose.service")
        for c in selected
    }
    if not selected or (wanted - found_services):
        missing = ", ".join(sorted(wanted - found_services)) or "any service"
        print(
            f"nest-deploy-doctor: no container for {missing} in project {args.project}",
            file=sys.stderr,
        )
        return 2
    leaked = 0
    for container in sorted(selected, key=lambda c: c.get("Name", "")):
        name = container.get("Name", "").lstrip("/")
        # Read failures raise DockerError (exit 2): an unreadable log is never "clean".
        text = docker.full_logs(container["Id"], args.since)
        count = sum(text.count(needle) for needle in needles)
        leaked += count
        print(f"{'LEAK' if count else 'PASS'} {name}: {count} occurrence(s)")
    if leaked:
        print(
            "The value appears in these logs. Do not copy or share them. Revoke the session "
            "(docs/source/hosted-rollout.md, R5), then follow the leak steps there."
        )
        return 1
    return 0


def emit(checks: list[Check], as_json: bool) -> None:
    for check in checks:
        print(json.dumps(check.__dict__) if as_json else check.line())


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default="nest", help="Compose project name (Dokploy appName)")
    common.add_argument("--json", action="store_true", help="one JSON object per line")
    age = argparse.ArgumentParser(add_help=False)
    age.add_argument(
        "--min-age",
        type=float,
        default=DEFAULT_MIN_AGE,
        help=f"seconds before a never-started container may be called stuck ({DEFAULT_MIN_AGE:.0f})",
    )
    commands = top.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "preflight", parents=[common], help="check networks and gateway settings"
    )
    check.add_argument("--env-file", help="the deployment's .env (read literally)")
    check.add_argument("--traefik-container", default=DEFAULT_TRAEFIK_CONTAINER)
    status = commands.add_parser(
        "migrate-status", parents=[common, age], help="classify migrate containers"
    )
    status.add_argument(
        "--logs", type=int, default=0, metavar="N", help=f"last N log lines (max {MAX_LOG_LINES})"
    )
    wait = commands.add_parser(
        "wait-migrate", parents=[common, age], help="bounded wait for the migration"
    )
    wait.add_argument("--timeout", type=float, default=300.0)
    wait.add_argument(
        "--since",
        help="RFC 3339 time the deploy started (default: now); older results are ignored",
    )
    wait.add_argument("--stuck-after", type=float, default=60.0)
    wait.add_argument("--interval", type=float, default=2.0)
    recover = commands.add_parser(
        "recover-migrate", parents=[common, age], help="remove a never-started migrate container"
    )
    recover.add_argument(
        "--settle", type=float, default=DEFAULT_SETTLE, help="seconds to re-check before acting"
    )
    recover.add_argument("--apply", action="store_true", help="run the removal (default: print it)")
    leak = commands.add_parser(
        "log-leak-check",
        parents=[common],
        help="count a token (read from stdin, never argv) in this project's container logs",
    )
    leak.add_argument(
        "--service",
        action="append",
        help="only this Compose service (repeatable); default: every container of the project",
    )
    leak.add_argument("--since", help="only logs since this time (docker logs --since)")
    leak.add_argument(
        "--canary", help="a synthetic, non-secret marker to look for instead of a real token"
    )
    return top


def main(argv: Sequence[str] | None = None, docker: Docker | None = None, environ=None) -> int:
    import os

    args = parser().parse_args(argv)
    docker = docker or Docker()
    try:
        if args.command == "preflight":
            return preflight(args, docker, dict(os.environ if environ is None else environ))
        if args.command == "migrate-status":
            return migrate_status(args, docker)
        if args.command == "wait-migrate":
            return wait_migrate(args, docker)
        if args.command == "log-leak-check":
            return log_leak_check(args, docker)
        return recover_migrate(args, docker)
    except (DockerError, OSError, ValueError) as error:
        print(f"nest-deploy-doctor: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
