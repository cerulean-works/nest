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
    python3 scripts/nest-deploy-doctor.py gateway-runtime --project APP

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

    Several deployments on one host (#118): it also compares this deployment with
    every other Nest deployment there, running or stopped. One Compose project name
    must not come from two checkouts. With `public-grpc`, `NEST_GRPC_EDGE_ALIAS`
    must be a valid alias that no other container on the edge network answers to,
    and `NEST_GRPC_PUBLIC_HOST` one that no other gateway serves. A short database
    host name must not name another project's container on the edge network.
    `NEST_TAILSCALE_HOSTNAME` and `NEST_CONTROLLER_IMAGE` (an image tag is
    daemon-wide) must differ from other deployments, and so must each HPC workdir,
    which must not be nested in another either, whatever login hosts each names
    (one cluster has several); a shared Slurm account is a warning, because each
    deployment's admission limits apply only to itself. A running conflict
    fails, a stopped one warns. It then reads Traefik's dynamic directory
    (`--traefik-dynamic-dir`, default /etc/dokploy/traefik/dynamic), read-only:
    a TCP router, service or transport name defined in two files, or one SNI name
    in two routers, fails, and this deployment's route must dial exactly
    `NEST_GRPC_EDGE_ALIAS`:8443. `route_change` fails when the settings would change
    the running gateway's alias or name while an installed route file still depends
    on the current one, unless `--retire-route FILE` records that it is being
    retired. HPC values and the database URL are never printed; a platforms file
    (`NEST_PLATFORMS_ENV_FILE`, default ./platforms.env next to `--env-file`) and the
    Platform variables an `env_file` gives other Controllers are compared too.

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

gateway-runtime
    After a deploy with the `public-grpc` profile, and again after any redeploy,
    restart or Dokploy network change. Preflight reasons about settings and subnets;
    this checks what the running containers actually do (#109). It requires
    exactly one running gateway and one running Controller, and checks:
    `NEST_GRPC_UPSTREAM` names the grpc-origin-only alias, not the generic
    `controller` name. The gateway answers to its `NEST_GRPC_EDGE_ALIAS` on the
    edge network, and no other container there does (#118). That name is a DNS name of the Controller on grpc-origin and on
    no other network. Inside the gateway it resolves (getent) to exactly the
    Controller's grpc-origin address. The Controller's `NEST_GRPC_TRUSTED_PROXIES` is
    the gateway's grpc-origin /32, and neither it nor `NEST_GRPC_ALLOWED_PEERS` admits
    any of the gateway's other addresses. Finally it opens one TCP connection from
    the gateway to the upstream, without sending a byte, and reads the socket tables:
    the gateway's end must be its grpc-origin address. Every connection the Controller
    holds from any gateway address must come from that address, which is the peer
    `context.peer()` reports, mapped IPv4 included. A check that cannot run fails.
    Only addresses and network names are printed, never other settings. It changes
    nothing; the probe connection is closed without data.

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
import uuid
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

DEFAULT_EDGE_ALIAS = "nest-grpc-gateway"
DEFAULT_CONTROLLER_IMAGE = "nest-controller:local"
DEFAULT_PUBLIC_HOST = "grpc.nest.cerulean.works"
DEFAULT_TAILSCALE_HOSTNAME = "nest-controller"
DEFAULT_TRAEFIK_DYNAMIC_DIR = "/etc/dokploy/traefik/dynamic"
# Compose service names of compose.dokploy.yaml: Docker gives each one as a DNS name
# on every network the service joins, so on a shared edge network they resolve to
# every deployment's container and can never identify one of them (#109, #118).
SERVICE_NAMES = (
    "controller",
    "migrate",
    "postgres",
    "tailscale",
    "grpc-gateway",
    "grpc-certificates",
    "controller-grpc-origin",
)
EDGE_ALIAS_PATTERN = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
HPC_SETTINGS = (
    "NEST_SLURM_HOST",
    "NEST_SLURM_LOGIN_NODES",
    "NEST_SLURM_ACCOUNT",
    "NEST_SLURM_WORKDIR",
    "NEST_PLATFORMS_ENV",
    "NEST_PLATFORMS_ENV_FILE",
    "NEST_LIMIT_ACTIVE_PER_PLATFORM",
    # Only its host name is used; the URL itself may hold a password.
    "NEST_DATABASE_URL",
)

# Settings read from the environment file. Only the network ones are printed.
SETTINGS = (
    "COMPOSE_PROFILES",
    "NEST_GRPC_ORIGIN_SUBNET",
    "NEST_GRPC_GATEWAY_ADDRESS",
    "NEST_GRPC_TRUSTED_PROXIES",
    "NEST_GRPC_ALLOWED_PEERS",
    "NEST_GRPC_EDGE_PROXY",
    "NEST_GRPC_EDGE_NETWORK",
    "NEST_GRPC_EDGE_ALIAS",
    "NEST_GRPC_PUBLIC_HOST",
    "NEST_TAILSCALE_HOSTNAME",
    "NEST_CONTROLLER_IMAGE",
    "NEST_PUBLIC_PILOT",
    # HPC targets, compared with other deployments on this host and never printed.
    *HPC_SETTINGS,
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

    def service_containers(self, service: str) -> list[dict]:
        """Every container, of any Compose project and in any state, of `service`."""
        ids = self._ok(
            [
                "docker",
                "ps",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.service={service}",
            ]
        ).split()
        if not ids:
            return []
        return json.loads(self._ok(["docker", "container", "inspect", *ids]))

    def inspect(self, ids: Sequence[str]) -> list[dict]:
        if not ids:
            return []
        return json.loads(self._ok(["docker", "container", "inspect", *ids]))

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

    def exec(self, container_id: str, argv: Sequence[str], *, detach: bool = False) -> Result:
        """Runs `argv` in a running container; the result is returned, not checked."""
        return self.run(["docker", "exec", *(["--detach"] if detach else []), container_id, *argv])


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


IPV4_MAPPED_NETWORK = ipaddress.IPv6Network("::ffff:0:0/96")


def _cidrs(value: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """The networks of a comma-separated setting. Like the Controller, refuses an
    IPv4 range written in IPv4-mapped IPv6 form, so every IPv4 range is checked as
    IPv4 (#109)."""
    networks = []
    for part in value.split(","):
        if not part.strip():
            continue
        network = ipaddress.ip_network(part.strip(), strict=False)
        if isinstance(network, ipaddress.IPv6Network) and network.subnet_of(IPV4_MAPPED_NETWORK):
            raise ValueError(
                f"{part.strip()} is an IPv4-mapped IPv6 network, which the Controller refuses; "
                "write the IPv4 network"
            )
        networks.append(network)
    return networks


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
            elif name.endswith("_grpc-origin"):
                collisions.append(
                    f"{name} ({existing}), the origin network of deployment "
                    f"{name[: -len('_grpc-origin')]}; every deployment needs its own subnet"
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


# --- Several deployments on one host (#118) -----------------------------------------


def _project(container: dict) -> str:
    return ((container.get("Config") or {}).get("Labels") or {}).get(
        "com.docker.compose.project", ""
    )


def _running(container: dict) -> bool:
    return (container.get("State") or {}).get("Status") == "running"


def _edge_alias_of(container: dict, edge_network: str | None) -> str:
    """The edge alias a gateway container uses: its `NEST_GRPC_EDGE_ALIAS`, or for a
    gateway from before that setting, the alias it has on the edge network, or the
    one compose.dokploy.yaml always gave it."""
    alias = container_env(container, ("NEST_GRPC_EDGE_ALIAS",)).get("NEST_GRPC_EDGE_ALIAS", "")
    if alias:
        return alias
    for name, attachment in _attachments(container).items():
        if edge_network is None or name == edge_network:
            for candidate in attachment.get("Aliases") or []:
                # Docker also lists the service name and the short container ID.
                if candidate not in SERVICE_NAMES and not container.get("Id", "").startswith(
                    candidate
                ):
                    return candidate
    return DEFAULT_EDGE_ALIAS


def edge_alias_problem(alias: str) -> str | None:
    """Why `alias` cannot name one gateway on a shared edge network, or None."""
    if not EDGE_ALIAS_PATTERN.fullmatch(alias):
        return f"NEST_GRPC_EDGE_ALIAS {alias!r} must be a lower-case DNS label"
    if alias in SERVICE_NAMES:
        return (
            f"NEST_GRPC_EDGE_ALIAS {alias!r} is a Compose service name, which resolves to "
            "every deployment's container on the edge network"
        )
    return None


def _platform_targets(text: str) -> list[tuple[str, frozenset, str, str]]:
    """(platform, login hosts, account, workdir) of each HPC Platform in the lines of a
    platforms.env value (real or `\\n`-escaped newlines)."""
    fields: dict[str, dict[str, str]] = {}
    for line in text.replace("\\n", "\n").splitlines():
        key, _, value = line.strip().partition("=")
        match = re.fullmatch(r"NEST_PLATFORM_([A-Z0-9_]+?)_(LOGIN_NODES|ACCOUNT|WORKDIR)", key)
        if match:
            fields.setdefault(match.group(1), {})[match.group(2)] = value.strip().strip("'\"")
    targets = []
    for platform, item in sorted(fields.items()):
        hosts = frozenset(
            h.strip().lower() for h in item.get("LOGIN_NODES", "").split(",") if h.strip()
        )
        targets.append((platform, hosts, item.get("ACCOUNT", ""), item.get("WORKDIR", "")))
    return targets


PLATFORM_KEY = re.compile(r"NEST_PLATFORM_[A-Z0-9_]+?_(LOGIN_NODES|ACCOUNT|WORKDIR)")


def platform_lines(container: dict) -> str:
    """The `NEST_PLATFORM_<ID>_LOGIN_NODES/ACCOUNT/WORKDIR` variables in a container's
    environment, as platforms.env lines. Compose's `env_file` (`./platforms.env`, or
    `NEST_PLATFORMS_ENV_FILE`) puts a file's Platforms there as plain variables."""
    lines = []
    for item in (container.get("Config") or {}).get("Env") or []:
        key, _, value = item.partition("=")
        if PLATFORM_KEY.fullmatch(key):
            lines.append(f"{key}={value}")
    return "\n".join(lines)


def platforms_file_lines(env_file: str | None, values: dict[str, str]) -> str | None:
    """This deployment's platforms file, which Compose loads with `env_file`: the
    matching lines only, never printed. `None` when there is none to read (the file
    is optional), and `""` when it names none."""
    import os

    if not env_file:
        return None
    name = values.get("NEST_PLATFORMS_ENV_FILE") or "./platforms.env"
    path = name if os.path.isabs(name) else os.path.join(os.path.dirname(env_file) or ".", name)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    lines = []
    for raw in text.splitlines():
        key, _, value = raw.strip().removeprefix("export ").partition("=")
        if PLATFORM_KEY.fullmatch(key.strip()):
            lines.append(f"{key.strip()}={value.strip()}")
    return "\n".join(lines)


def hpc_targets(values: dict[str, str]) -> list[tuple[str, frozenset, str, str]]:
    """Every HPC target a Controller's settings name: the legacy single cluster and
    each Platform in `NEST_PLATFORMS_ENV` (inline, or the lines of a platforms file
    joined into it by the caller)."""
    targets = []
    host_text = values.get("NEST_SLURM_LOGIN_NODES") or values.get("NEST_SLURM_HOST", "")
    if values.get("NEST_SLURM_WORKDIR") or values.get("NEST_SLURM_ACCOUNT"):
        hosts = frozenset(h.strip().lower() for h in host_text.split(",") if h.strip())
        targets.append(
            (
                "NEST_SLURM",
                hosts,
                values.get("NEST_SLURM_ACCOUNT", ""),
                values.get("NEST_SLURM_WORKDIR", ""),
            )
        )
    targets += _platform_targets(values.get("NEST_PLATFORMS_ENV", ""))
    return targets


def _overlapping_paths(a: str, b: str) -> bool:
    """Whether two workdirs are the same directory or one lies inside the other, by
    path text: `/w/nest` overlaps `/w/nest/` and `/w/nest/release`."""
    a, b = a.rstrip("/") or "/", b.rstrip("/") or "/"
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def database_host_check(
    values: dict[str, str], project: str, docker: Docker, networks: list[dict], edge_network: str
) -> Check:
    """The Controller joins the shared edge network too (Dokploy attaches it for the
    Web Domain), so a short database host name such as `postgres` must not name any
    other project's container there: the Controller could reach another deployment's
    database. Only the host name is printed, never the URL."""
    import urllib.parse

    url = values.get("NEST_DATABASE_URL", "")
    if not url:
        return Check("database_host", "SKIP", "NEST_DATABASE_URL is unset")
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return Check("database_host", "FAIL", "NEST_DATABASE_URL does not parse (not printed)")
    if not host or "." in host or ":" in host:
        return Check(
            "database_host", "PASS", "NEST_DATABASE_URL names a host outside this host's Docker DNS"
        )
    edge = next((n for n in networks if n.get("Name") == edge_network), None)
    ids = [i for i in ((edge or {}).get("Containers") or {}) if not i.startswith("ep-")]
    holders = [
        f"{c.get('Name', '?').lstrip('/')} ({_project(c) or 'no project'})"
        for c in docker.inspect(ids)
        if _project(c) != project and host in _dns_names(_attachments(c).get(edge_network) or {})
    ]
    if holders:
        return Check(
            "database_host",
            "FAIL",
            f"the database host {host} also names {', '.join(sorted(holders))} on "
            f"{edge_network}, which the Controller joins; that container could answer for this "
            "deployment's database. Take it off the edge network or rename it",
        )
    return Check(
        "database_host", "PASS", f"{host} names no other project's container on {edge_network}"
    )


def controller_image_checks(values, project, docker, others, verdict, projects) -> list[Check]:
    """The Controller and migrate image of this deployment is its revision. Both
    services build `NEST_CONTROLLER_IMAGE` from the deployment's own checkout, but an
    image tag is daemon-wide, not per Compose project: another deployment building
    the same tag replaces it, and this deployment's next recreate or migration would
    run the other revision (review of #119). A digest-pinned reference cannot change.
    Also reports when this deployment's Controller and its last migration ran
    different builds."""
    checks = []
    image = values.get("NEST_CONTROLLER_IMAGE", DEFAULT_CONTROLLER_IMAGE)
    if "@sha256:" in image:
        checks.append(Check("controller_image", "PASS", f"{image} is pinned by digest"))
    else:
        same = [
            c
            for service in ("controller", "migrate")
            for c in others(service)
            if (c.get("Config") or {}).get("Image") == image
        ]
        if same:
            checks.append(
                Check(
                    "controller_image",
                    verdict(same),
                    f"NEST_CONTROLLER_IMAGE {image} is also the image of "
                    f"{projects(same)}: a build of either deployment retags it, and the other's "
                    "next recreate or migration runs that revision. Give each deployment its "
                    "own tag, such as nest-controller:release (docs/source/multi-environment.md)",
                )
            )
        else:
            checks.append(
                Check("controller_image", "PASS", f"no other deployment uses the image {image}")
            )

    def newest(service: str) -> dict | None:
        found = docker.containers(project, service)
        if not found:
            return None
        return max(found, key=lambda c: str(c.get("Created", "")))

    controller, migrate = newest("controller"), newest("migrate")
    if controller is not None and migrate is not None:
        ours, theirs = controller.get("Image", ""), migrate.get("Image", "")
        short = lambda image_id: image_id.removeprefix("sha256:")[:12] or "?"
        if ours == theirs:
            checks.append(
                Check(
                    "controller_revision",
                    "PASS",
                    f"the Controller and its last migration ran image {short(ours)}",
                )
            )
        else:
            checks.append(
                Check(
                    "controller_revision",
                    "WARN",
                    f"the Controller runs image {short(ours)} but the last migration ran "
                    f"{short(theirs)}; redeploy so both run this deployment's build",
                )
            )
    return checks


def check_isolation(
    values: dict[str, str],
    project: str,
    docker: Docker,
    networks: list[dict],
    public_grpc: bool,
    env_file: str | None = None,
) -> list[Check]:
    """Compares this deployment's identity with every other Nest deployment on the
    host, running or stopped: the Compose project, the gateway's edge alias and public
    name, the Tailnet hostname and the HPC workdirs and accounts. Read-only."""
    checks: list[Check] = []
    own = docker.project_containers(project)
    directories = sorted(
        {
            ((c.get("Config") or {}).get("Labels") or {}).get(
                "com.docker.compose.project.working_dir", ""
            )
            for c in own
        }
        - {""}
    )
    if len(directories) > 1:
        checks.append(
            Check(
                "project_identity",
                "FAIL",
                f"Compose project {project} has containers from {len(directories)} checkouts: "
                "two deployments share one project name, and with it every network, volume "
                "and database. Give each its own Dokploy application (appName)",
            )
        )
    elif project == "nest":
        checks.append(
            Check(
                "project_identity",
                "WARN",
                "project `nest` is compose.dokploy.yaml's default name; a second deployment "
                "started without -p would share it. Dokploy uses each application's appName",
            )
        )
    else:
        checks.append(Check("project_identity", "PASS", f"project {project} has one checkout"))

    def others(service: str) -> list[dict]:
        return [c for c in docker.service_containers(service) if _project(c) not in ("", project)]

    def verdict(found: list[dict]) -> str:
        return "FAIL" if any(_running(c) for c in found) else "WARN"

    def projects(found: list[dict]) -> str:
        return ", ".join(
            sorted(f"{_project(c)}{'' if _running(c) else ' (stopped)'}" for c in found)
        )

    edge_network = values.get("NEST_GRPC_EDGE_NETWORK", DEFAULT_EDGE_NETWORK)
    gateways = others("grpc-gateway")
    if public_grpc:
        alias = values.get("NEST_GRPC_EDGE_ALIAS", DEFAULT_EDGE_ALIAS)
        problem = edge_alias_problem(alias)
        same = [c for c in gateways if _edge_alias_of(c, edge_network) == alias]
        # Any other container that answers to the name on the edge network.
        edge = next((n for n in networks if n.get("Name") == edge_network), None)
        strangers = []
        if edge is not None:
            ids = [i for i in (edge.get("Containers") or {}) if not i.startswith("ep-")]
            known = {c.get("Id") for c in same}
            for container in docker.inspect(ids):
                if _project(container) == project or container.get("Id") in known:
                    continue
                attachment = _attachments(container).get(edge_network) or {}
                if alias in _dns_names(attachment):
                    strangers.append(container.get("Name", "?").lstrip("/"))
        if problem:
            checks.append(Check("edge_alias", "FAIL", problem))
        elif same or strangers:
            holders = ", ".join(filter(None, [projects(same), ", ".join(strangers)]))
            checks.append(
                Check(
                    "edge_alias",
                    "FAIL" if strangers else verdict(same),
                    f"{alias} on {edge_network} is also the gateway alias of {holders}: Traefik "
                    "would reach either gateway. Set a unique NEST_GRPC_EDGE_ALIAS for this "
                    "deployment and render its route with --instance (docs/source/multi-environment.md)",
                )
            )
        else:
            checks.append(
                Check(
                    "edge_alias", "PASS", f"{alias} is used by no other container on {edge_network}"
                )
            )

        host = values.get("NEST_GRPC_PUBLIC_HOST", DEFAULT_PUBLIC_HOST).lower()
        same = [
            c
            for c in gateways
            if container_env(c, ("NEST_GRPC_PUBLIC_HOST",))
            .get("NEST_GRPC_PUBLIC_HOST", DEFAULT_PUBLIC_HOST)
            .lower()
            == host
        ]
        if same:
            checks.append(
                Check(
                    "public_host",
                    verdict(same),
                    f"{host} is also served by the gateway of {projects(same)}; each deployment "
                    "needs its own NEST_GRPC_PUBLIC_HOST, route and certificate",
                )
            )
        else:
            checks.append(Check("public_host", "PASS", f"no other gateway serves {host}"))

    checks.append(database_host_check(values, project, docker, networks, edge_network))
    checks += controller_image_checks(values, project, docker, others, verdict, projects)

    hostname = values.get("NEST_TAILSCALE_HOSTNAME", DEFAULT_TAILSCALE_HOSTNAME).lower()
    same = [
        c
        for c in others("tailscale")
        if container_env(c, ("TS_HOSTNAME",)).get("TS_HOSTNAME", "").lower() == hostname
    ]
    if same:
        checks.append(
            Check(
                "tailnet_hostname",
                verdict(same),
                f"Tailscale hostname {hostname} is also used by {projects(same)}; the second "
                "node gets a suffixed MagicDNS name, so set a distinct NEST_TAILSCALE_HOSTNAME",
            )
        )
    else:
        checks.append(Check("tailnet_hostname", "PASS", f"no other deployment uses {hostname}"))

    from_file = platforms_file_lines(env_file, values)
    if from_file is None and values.get("NEST_PLATFORMS_ENV_FILE"):
        checks.append(
            Check(
                "hpc_workdir",
                "FAIL",
                "NEST_PLATFORMS_ENV_FILE names a file this tool cannot read; run preflight "
                "from the checkout, next to the .env, so its Platforms are compared",
            )
        )
        return checks
    own = dict(values)
    own["NEST_PLATFORMS_ENV"] = "\n".join(
        filter(None, [values.get("NEST_PLATFORMS_ENV", ""), from_file or ""])
    )
    ours = hpc_targets(own)
    if not ours:
        checks.append(
            Check(
                "hpc_workdir",
                "SKIP",
                "no NEST_SLURM_*, NEST_PLATFORMS_ENV or platforms file target to compare",
            )
        )
        return checks
    shared_dirs, shared_accounts, unknown = [], [], []
    for controller in others("controller"):
        settings_of = container_env(controller, HPC_SETTINGS)
        settings_of["NEST_PLATFORMS_ENV"] = "\n".join(
            filter(None, [settings_of.get("NEST_PLATFORMS_ENV", ""), platform_lines(controller)])
        )
        theirs = hpc_targets(settings_of)
        label = f"{_project(controller)}{'' if _running(controller) else ' (stopped)'}"
        if not theirs:
            # A Controller whose targets cannot be read is never assumed isolated.
            unknown.append(label)
            continue
        # Login hosts never prove separate clusters: one Slurm cluster often has
        # several login nodes on one shared filesystem and one accounting database,
        # so every pair is compared, whatever hosts it names (review of #119).
        for name, hosts, account, workdir in ours:
            for other_name, other_hosts, other_account, other_workdir in theirs:
                pair = f"{name} = {label} {other_name}"
                if hosts and other_hosts and not hosts & other_hosts:
                    pair += " (other login hosts, not proof of another cluster)"
                if workdir and other_workdir and _overlapping_paths(workdir, other_workdir):
                    shared_dirs.append((controller, pair))
                if account and account == other_account:
                    shared_accounts.append(pair)
    if shared_dirs:
        checks.append(
            Check(
                "hpc_workdir",
                verdict([c for c, _ in shared_dirs]),
                "the HPC workdir (not printed) is the same as, or nested in, another "
                "deployment's: "
                + "; ".join(sorted({d for _, d in shared_dirs}))
                + ". Jobs, results and worker state would mix; give each deployment its own "
                "workdir path, even on login hosts you believe are separate clusters",
            )
        )
    elif unknown:
        checks.append(
            Check(
                "hpc_workdir",
                "WARN",
                "cannot read the HPC targets of "
                + ", ".join(sorted(set(unknown)))
                + "; compare their workdirs and accounts with this deployment's by hand",
            )
        )
    else:
        checks.append(
            Check(
                "hpc_workdir", "PASS", "no other deployment on this host uses the same HPC workdir"
            )
        )
    if shared_accounts:
        limit = values.get("NEST_LIMIT_ACTIVE_PER_PLATFORM", "")
        checks.append(
            Check(
                "hpc_account",
                "WARN",
                "the Slurm account (not printed) is shared with another deployment: "
                + "; ".join(sorted(set(shared_accounts)))
                + ". Each deployment admits work on its own ledger, so their limits add up; "
                + ("NEST_LIMIT_ACTIVE_PER_PLATFORM is unset here. " if not limit else "")
                + "Split the operator-approved allocation between them, or record that "
                "the accounts belong to separate clusters (docs/source/multi-environment.md, "
                "HPC capacity)",
            )
        )
    elif unknown:
        checks.append(
            Check("hpc_account", "WARN", "not compared with " + ", ".join(sorted(set(unknown))))
        )
    else:
        checks.append(Check("hpc_account", "PASS", "no other deployment shares the Slurm account"))
    return checks


# The few lines of a Traefik dynamic file that matter here, at the indentation
# render.sh writes (and Traefik's documentation uses): two spaces per level.
ROUTE_SECTION = re.compile(r"^  (routers|services|serversTransports):\s*$")
ROUTE_NAME = re.compile(r"^    ([A-Za-z0-9_.@-]+):\s*$")
ROUTE_RULE = re.compile(r"HostSNI\(([^)]*)\)")
ROUTE_ADDRESS = re.compile(r"^\s*-\s*address:\s*[\"']?([^\"'\s]+)[\"']?\s*$")


def parse_tcp_routes(text: str) -> dict[str, dict]:
    """Names, HostSNI names and server addresses of the `tcp:` section of a Traefik
    YAML dynamic file. Other sections (http, tls) are ignored."""
    found: dict[str, dict] = {"routers": {}, "services": {}, "serversTransports": {}}
    in_tcp, section, name = False, None, None
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].rstrip() if "`" not in line else line.rstrip()
        if not stripped.strip():
            continue
        if not line.startswith(" "):
            in_tcp, section, name = stripped == "tcp:", None, None
            continue
        if not in_tcp:
            continue
        match = ROUTE_SECTION.match(stripped)
        if match:
            section, name = match.group(1), None
            continue
        if section is None:
            continue
        match = ROUTE_NAME.match(stripped)
        if match:
            name = match.group(1)
            found[section][name] = {"hosts": [], "addresses": []}
            continue
        if name is None:
            continue
        rule = ROUTE_RULE.search(stripped)
        if rule and "rule:" in stripped:
            hosts = re.findall(r"`([^`]*)`", rule.group(1))
            found[section][name]["hosts"] += [h.lower() for h in hosts]
        address = ROUTE_ADDRESS.match(stripped)
        if address:
            found[section][name]["addresses"].append(address.group(1))
    return found


def check_routes(values: dict[str, str], directory: str, public_grpc: bool) -> list[Check]:
    """Reads Traefik's dynamic configuration directory (read-only) for the collisions
    that break a second deployment's gRPC route: a router, service or transport name
    defined in two files (Traefik keeps one, so a name goes unrouted), one SNI name in two
    routers, and this deployment's route dialling anything but its own edge alias."""
    import os

    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith((".yml", ".yaml")))
    except OSError as error:
        return [
            Check(
                "traefik_routes",
                "SKIP",
                f"cannot read {directory} ({error.strerror}); pass --traefik-dynamic-dir, or "
                "check the route files by hand (docs/source/multi-environment.md)",
            )
        ]
    files: dict[str, dict] = {}
    for name in names:
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                files[name] = parse_tcp_routes(handle.read())
        except (OSError, UnicodeDecodeError) as error:
            return [Check("traefik_routes", "FAIL", f"cannot read {name}: {error}")]
    checks: list[Check] = []
    duplicates = []
    for section in ("routers", "services", "serversTransports"):
        defined: dict[str, list[str]] = {}
        for file, routes in files.items():
            for name in routes[section]:
                defined.setdefault(name, []).append(file)
        duplicates += [
            f"{section[:-1]} {n} in {', '.join(f)}"
            for n, f in sorted(defined.items())
            if len(f) > 1
        ]
    if duplicates:
        checks.append(
            Check(
                "route_names",
                "FAIL",
                "defined in more than one file, so Traefik's file provider keeps only one "
                "definition and the other deployment's name is not routed: "
                + "; ".join(duplicates)
                + ". Render each deployment's file with its own --instance",
            )
        )
    else:
        checks.append(
            Check("route_names", "PASS", f"no TCP name repeats across {len(files)} files")
        )
    owners: dict[str, list[str]] = {}
    for file, routes in files.items():
        for router, item in routes["routers"].items():
            for host in item["hosts"]:
                owners.setdefault(host, []).append(f"{file}:{router}")
    repeated = [f"{h} in {', '.join(o)}" for h, o in sorted(owners.items()) if len(o) > 1]
    if repeated:
        checks.append(
            Check(
                "route_sni",
                "FAIL",
                "one SNI name in several routers: "
                + "; ".join(repeated)
                + ". Keep one route per name",
            )
        )
    else:
        checks.append(Check("route_sni", "PASS", "every HostSNI name has one router"))
    if not public_grpc:
        return checks

    host = values.get("NEST_GRPC_PUBLIC_HOST", DEFAULT_PUBLIC_HOST).lower()
    alias = values.get("NEST_GRPC_EDGE_ALIAS", DEFAULT_EDGE_ALIAS)
    mine = [
        (file, router, item)
        for file, routes in files.items()
        for router, item in routes["routers"].items()
        if host in item["hosts"]
    ]
    if not mine:
        checks.append(
            Check(
                "route_target",
                "WARN",
                f"no route for {host} in {directory} yet; install the one render.sh prints",
            )
        )
        return checks
    problems = []
    for file, _router, _item in mine:
        addresses = [a for item in files[file]["services"].values() for a in item["addresses"]]
        for address in addresses or ["(none)"]:
            target, _, port = address.rpartition(":")
            if target != alias or port != "8443":
                problems.append(f"{file} dials {address}, not {alias}:8443")
    if problems:
        checks.append(
            Check(
                "route_target",
                "FAIL",
                "; ".join(problems)
                + ". Render this deployment's route with --edge-alias set to its NEST_GRPC_EDGE_ALIAS",
            )
        )
    else:
        checks.append(
            Check("route_target", "PASS", f"{mine[0][0]} routes {host} to {alias}:8443 only")
        )
    return checks


def check_route_change(
    values: dict[str, str],
    project: str,
    docker: Docker,
    directory: str,
    retire: Sequence[str] = (),
) -> Check | None:
    """Whether deploying these settings would orphan an installed route: this project's
    gateway as it runs now answers to an alias and serves a name, and a route file
    that dials that alias or routes that name stops working when either changes
    (Phase 2 of docs/source/multi-environment.md). Files named in `retire` are the
    operator's recorded decision to retire them (#118, decision 5) and only warn."""
    import os

    running = [c for c in docker.containers(project, "grpc-gateway") if _running(c)]
    if not running:
        return None
    edge_network = values.get("NEST_GRPC_EDGE_NETWORK", DEFAULT_EDGE_NETWORK)
    current_alias = _edge_alias_of(running[0], edge_network)
    current_host = (
        container_env(running[0], ("NEST_GRPC_PUBLIC_HOST",))
        .get("NEST_GRPC_PUBLIC_HOST", DEFAULT_PUBLIC_HOST)
        .lower()
    )
    alias = values.get("NEST_GRPC_EDGE_ALIAS", DEFAULT_EDGE_ALIAS)
    host = values.get("NEST_GRPC_PUBLIC_HOST", DEFAULT_PUBLIC_HOST).lower()
    if alias == current_alias and host == current_host:
        return Check("route_change", "PASS", "the gateway keeps its alias and name")
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith((".yml", ".yaml")))
    except OSError:
        return Check(
            "route_change",
            "FAIL",
            f"these settings change the gateway's alias ({current_alias} -> {alias}) or name "
            f"({current_host} -> {host}), and {directory} cannot be read to see which "
            "route files that breaks",
        )
    broken = []
    for name in names:
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                routes = parse_tcp_routes(handle.read())
        except OSError:
            continue
        except UnicodeDecodeError:
            continue
        targets = {
            a.rpartition(":")[0] for item in routes["services"].values() for a in item["addresses"]
        }
        hosts = {h for item in routes["routers"].values() for h in item["hosts"]}
        if (alias != current_alias and current_alias in targets) or (
            host != current_host and current_host in hosts
        ):
            broken.append(name)
    if not broken:
        return Check(
            "route_change",
            "PASS",
            f"no installed route depends on the current alias {current_alias} or name {current_host}",
        )
    blocking = [n for n in broken if n not in retire]
    detail = (
        f"deploying changes the gateway's alias ({current_alias} -> {alias}) or name "
        f"({current_host} -> {host}), which stops {', '.join(broken)} from routing"
    )
    if blocking:
        return Check(
            "route_change",
            "FAIL",
            detail + ". Keep the current values (Phase 1), or, once retiring that route is "
            "the recorded decision, pass --retire-route for it (docs/source/multi-environment.md)",
        )
    return Check("route_change", "WARN", detail + "; retiring it as recorded")


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
    public_grpc = "public-grpc" in profiles
    if public_grpc:
        checks += check_gateway(values, args.project, networks, docker, args.traefik_container)
    else:
        checks.append(
            Check("public_grpc", "SKIP", "profile public-grpc is off; gateway settings not checked")
        )
    checks += check_isolation(values, args.project, docker, networks, public_grpc, args.env_file)
    checks += check_routes(values, args.traefik_dynamic_dir, public_grpc)
    if public_grpc:
        change = check_route_change(
            values, args.project, docker, args.traefik_dynamic_dir, args.retire_route or ()
        )
        if change is not None:
            checks.append(change)
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
        moment = moment.replace(tzinfo=datetime.timezone.utc)
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


# --- gateway-runtime (#109) -------------------------------------------------------

GENERIC_UPSTREAM_NAMES = ("controller",)
RUNTIME_ENV = ("NEST_GRPC_UPSTREAM", "NEST_GRPC_TRUSTED_PROXIES", "NEST_GRPC_ALLOWED_PEERS")
PROBE_SECONDS = 4


def container_env(container: dict, keys: Sequence[str]) -> dict[str, str]:
    """Only the named variables of a container's environment; nothing else is kept."""
    found = {}
    for item in (container.get("Config") or {}).get("Env") or []:
        key, _, value = item.partition("=")
        if key in keys:
            found[key] = value
    return found


def _attachments(container: dict) -> dict[str, dict]:
    return (container.get("NetworkSettings") or {}).get("Networks") or {}


def _addresses(container: dict) -> dict[str, str]:
    """Network name -> the container's IPv4 address on it."""
    return {
        name: a.get("IPAddress", "")
        for name, a in _attachments(container).items()
        if a.get("IPAddress")
    }


def _all_addresses(container: dict) -> dict[str, list]:
    """Network name -> every address of the container on it, both families
    (`IPAddress`, `GlobalIPv6Address`, `LinkLocalIPv6Address`), IPv4-mapped ones as IPv4."""
    found: dict[str, list] = {}
    for name, attachment in _attachments(container).items():
        for key in ("IPAddress", "GlobalIPv6Address", "LinkLocalIPv6Address"):
            text = attachment.get(key) or ""
            if not text:
                continue
            try:
                found.setdefault(name, []).append(_canonical(ipaddress.ip_address(text)))
            except ValueError:
                continue
    return found


def _dns_names(attachment: dict) -> set[str]:
    return set(attachment.get("DNSNames") or []) | set(attachment.get("Aliases") or [])


def _canonical(address: ipaddress.IPv4Address | ipaddress.IPv6Address):
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def parse_proc_net(text: str, byteorder: str = sys.byteorder) -> list[tuple]:
    """
    Established TCP sockets from /proc/net/tcp and /proc/net/tcp6 text, as
    `(local address, local port, remote address, remote port, inode)`. The kernel
    prints each 32-bit word of an address in host byte order; IPv4-mapped IPv6
    addresses are returned as IPv4, as the Controller's guard sees them.
    """
    sockets = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or fields[3] != "01" or ":" not in fields[1]:
            continue
        try:
            ends = []
            for end in fields[1:3]:
                hex_address, hex_port = end.split(":")
                raw = bytes.fromhex(hex_address)
                if byteorder == "little":
                    raw = b"".join(raw[i : i + 4][::-1] for i in range(0, len(raw), 4))
                ends.append((_canonical(ipaddress.ip_address(raw)), int(hex_port, 16)))
            inode = int(fields[9])
        except ValueError:
            continue
        sockets.append((ends[0][0], ends[0][1], ends[1][0], ends[1][1], inode))
    return sockets


# Both tables are required where the kernel has them: /proc/net/tcp6 may be absent
# (no IPv6), but a table that exists and cannot be read fails the check.
SOCKET_TABLES = (
    "cat /proc/net/tcp || exit 3; if [ -e /proc/net/tcp6 ]; then cat /proc/net/tcp6 || exit 4; fi"
)
# Prints `probe socket:[INODE]` for the probe holding fd 3 whose command line carries
# the token, then the socket tables.
PROBE_SOCKET = (
    "for p in /proc/[0-9]*; do "
    'if tr "\\0" " " < "$p/cmdline" 2>/dev/null | grep -q -- "$1"; then '
    'link=$(readlink "$p/fd/3" 2>/dev/null) '
    '&& case "$link" in socket:*) echo "probe $link";; esac; fi; done; ' + SOCKET_TABLES
)


def _read_sockets(
    docker: Docker, container_id: str, token: str | None = None
) -> tuple[list[tuple], set[int]] | None:
    """The container's established sockets and, with `token`, the inodes the probe
    holds; `None` when a table cannot be read."""
    if token is None:
        result = docker.exec(container_id, ["sh", "-c", SOCKET_TABLES])
    else:
        result = docker.exec(container_id, ["sh", "-c", PROBE_SOCKET, "sockets", token])
    if result.returncode != 0 or "local_address" not in result.stdout:
        return None
    inodes = set()
    for match in re.finditer(r"^probe socket:\[(\d+)\]$", result.stdout, re.MULTILINE):
        inodes.add(int(match.group(1)))
    return parse_proc_net(result.stdout), inodes


def _one_running(docker: Docker, project: str, service: str) -> tuple[dict | None, Check | None]:
    found = docker.containers(project, service)
    running = [c for c in found if (c.get("State") or {}).get("Status") == "running"]
    if len(running) == 1:
        return running[0], None
    if not running:
        states = ", ".join((c.get("State") or {}).get("Status", "?") for c in found) or "none"
        detail = f"no running {service} container in project {project} (found: {states})"
    else:
        detail = (
            f"{len(running)} running {service} containers in project {project}; a stale "
            "container from an earlier deploy is still up. Remove it, then rerun"
        )
    return None, Check(f"{service}_container", "FAIL", detail)


def private_path(
    docker: Docker,
    gateway_id: str,
    controller_id: str,
    host: str,
    port: int,
    *,
    origin: tuple,
    gateway_ips: set,
    controller_ips: set,
    origin_name: str,
    sleep: Callable[[float], None],
) -> Check:
    """
    Positive, correlated evidence of the private path. A silent TCP probe from the
    gateway to the upstream is identified by the socket it holds (its command line
    carries a fresh token). Its 4-tuple must run from the gateway's grpc-origin
    address to the Controller's grpc-origin address and port, and the Controller
    must hold the mirror of that tuple. Every other established connection between
    any gateway address and any Controller address on that port, in either table,
    must use the same two origin addresses. Anything that cannot be read or found fails.
    """
    gateway_origin, controller_origin = origin
    token = f"nest-doctor-probe-{uuid.uuid4().hex[:12]}"
    launched = docker.exec(
        gateway_id,
        [
            "bash",
            "-c",
            # `; :` keeps this bash, whose command line carries the token, holding fd 3;
            # otherwise bash replaces itself with the final `sleep`.
            f'exec 3<>"/dev/tcp/$1/$2" && sleep {PROBE_SECONDS}; :',
            token,
            host,
            str(port),
        ],
        detach=True,
    )
    if launched.returncode != 0:
        return Check(
            "private_path", "FAIL", "could not start the probe in the gateway (needs bash)"
        )
    probe = mirror = None
    outgoing: list[tuple] = []
    incoming: list[tuple] = []
    for _ in range(PROBE_SECONDS * 2):
        sleep(0.5)
        read_gateway = _read_sockets(docker, gateway_id, token)
        read_controller = _read_sockets(docker, controller_id)
        if read_gateway is None or read_controller is None:
            return Check(
                "private_path",
                "FAIL",
                "could not read a socket table (/proc/net/tcp or an existing /proc/net/tcp6) "
                "in the " + ("gateway" if read_gateway is None else "Controller"),
            )
        (outgoing, inodes), (incoming, _) = read_gateway, read_controller
        probe = next((s for s in outgoing if s[4] in inodes), None)
        if probe is not None:
            mirror = next(
                (
                    s
                    for s in incoming
                    if (s[0], s[1], s[2], s[3]) == (probe[2], probe[3], probe[0], probe[1])
                ),
                None,
            )
            if mirror is not None:
                break
    if probe is None:
        return Check(
            "private_path",
            "FAIL",
            f"the probe never held a connection to {host}:{port}; the gateway cannot reach the "
            "Controller there",
        )
    expected = (gateway_origin, controller_origin, port)
    if (probe[0], probe[2], probe[3]) != expected:
        return Check(
            "private_path",
            "FAIL",
            f"the probe connected {probe[0]} -> {probe[2]}:{probe[3]}, not {gateway_origin} -> "
            f"{controller_origin}:{port} on {origin_name}",
        )
    if mirror is None:
        return Check(
            "private_path",
            "FAIL",
            f"the Controller holds no connection from {probe[0]}:{probe[1]}; it is not the "
            "endpoint the gateway reached",
        )
    wrong = set()
    expected_pair = (gateway_origin, controller_origin)
    for local, _, remote, rport, _ in outgoing:
        between = rport == port and local in gateway_ips and remote in controller_ips
        if between and (local, remote) != expected_pair:
            wrong.add(f"{local} -> {remote}")
    for local, lport, remote, _, _ in incoming:
        between = lport == port and remote in gateway_ips and local in controller_ips
        if between and (remote, local) != expected_pair:
            wrong.add(f"{remote} -> {local}")
    if wrong:
        return Check(
            "private_path",
            "FAIL",
            f"the gateway also reaches the Controller as {', '.join(sorted(wrong))}, not only "
            f"{gateway_origin} -> {controller_origin} on {origin_name}; the Controller would "
            "refuse it",
        )
    return Check(
        "private_path",
        "PASS",
        f"probe {probe[0]}:{probe[1]} -> {probe[2]}:{probe[3]} seen from both ends; every "
        f"gateway connection to the Controller is {gateway_origin} -> {controller_origin}",
    )


def edge_alias_runtime(docker: Docker, gateway: dict, origin_name: str) -> Check:
    """The running gateway answers to its NEST_GRPC_EDGE_ALIAS on the edge network,
    and no other container there does, so Traefik's route reaches this gateway only
    (#118)."""
    alias = container_env(gateway, ("NEST_GRPC_EDGE_ALIAS",)).get(
        "NEST_GRPC_EDGE_ALIAS", DEFAULT_EDGE_ALIAS
    )
    problem = edge_alias_problem(alias)
    if problem:
        return Check("edge_alias", "FAIL", problem)
    edges = [n for n in _attachments(gateway) if n != origin_name]
    if len(edges) != 1:
        return Check(
            "edge_alias",
            "FAIL",
            f"the gateway joins {', '.join(edges) or 'no network'} besides {origin_name}; "
            "expected exactly the edge network",
        )
    edge = edges[0]
    if alias not in _dns_names(_attachments(gateway)[edge]):
        return Check(
            "edge_alias",
            "FAIL",
            f"the gateway does not answer to NEST_GRPC_EDGE_ALIAS {alias} on {edge}; redeploy "
            "it from the current compose.dokploy.yaml",
        )
    network = next((n for n in docker.networks() if n.get("Name") == edge), None)
    ids = [i for i in ((network or {}).get("Containers") or {}) if not i.startswith("ep-")]
    others = []
    for container in docker.inspect(ids):
        if container.get("Id") == gateway.get("Id"):
            continue
        if alias in _dns_names(_attachments(container).get(edge) or {}):
            others.append(container.get("Name", "?").lstrip("/"))
    if others:
        return Check(
            "edge_alias",
            "FAIL",
            f"{alias} on {edge} also names {', '.join(sorted(others))}, so Traefik's route "
            "resolves to more than one gateway; give each deployment a unique "
            "NEST_GRPC_EDGE_ALIAS (docs/source/multi-environment.md)",
        )
    return Check("edge_alias", "PASS", f"{alias} names only this gateway on {edge}")


def gateway_runtime(args, docker: Docker, sleep: Callable[[float], None] | None = None) -> int:
    sleep = sleep or time.sleep
    checks: list[Check] = []
    gateway, problem = _one_running(docker, args.project, "grpc-gateway")
    controller, problem2 = _one_running(docker, args.project, "controller")
    checks += [c for c in (problem, problem2) if c]
    if gateway is None or controller is None:
        emit(checks, args.json)
        return 1
    started = parse_time((gateway.get("State") or {}).get("StartedAt", ""))
    controller_started = parse_time((controller.get("State") or {}).get("StartedAt", ""))
    if started and controller_started and controller_started > started:
        checks.append(
            Check(
                "containers",
                "WARN",
                "the Controller was (re)started after the gateway; the gateway's start-up "
                "resolution check predates it, so the checks below are what count",
            )
        )
    else:
        checks.append(Check("containers", "PASS", "one running gateway and one running Controller"))

    origin_name = f"{args.project}_grpc-origin"
    gateway_networks = _addresses(gateway)
    controller_networks = _addresses(controller)
    gateway_origin = gateway_networks.get(origin_name)
    controller_origin = controller_networks.get(origin_name)
    if not gateway_origin or not controller_origin:
        missing = [
            n for n, a in (("gateway", gateway_origin), ("Controller", controller_origin)) if not a
        ]
        checks.append(
            Check(
                "origin_network", "FAIL", f"{' and '.join(missing)} not attached to {origin_name}"
            )
        )
        emit(checks, args.json)
        return 1
    checks.append(
        Check(
            "origin_network",
            "PASS",
            f"{origin_name}: gateway {gateway_origin}, Controller {controller_origin}",
        )
    )

    upstream = container_env(gateway, RUNTIME_ENV).get("NEST_GRPC_UPSTREAM", "")
    host, _, port_text = upstream.rpartition(":")
    if not host or not port_text.isdigit():
        checks.append(
            Check(
                "upstream_setting", "FAIL", f"gateway NEST_GRPC_UPSTREAM is {upstream or 'unset'}"
            )
        )
        emit(checks, args.json)
        return 1
    if host in GENERIC_UPSTREAM_NAMES or host == f"{args.project}-controller-1":
        checks.append(
            Check(
                "upstream_setting",
                "FAIL",
                f"NEST_GRPC_UPSTREAM is {upstream}: a name that resolves on every network the "
                "Controller joins, including Dokploy's edge network; use "
                "controller-grpc-origin:50051 from the current compose.dokploy.yaml",
            )
        )
    else:
        checks.append(Check("upstream_setting", "PASS", upstream))

    named_on = sorted(n for n, a in _attachments(controller).items() if host in _dns_names(a))
    if named_on == [origin_name]:
        checks.append(
            Check("upstream_alias", "PASS", f"{host} is a Controller name only on {origin_name}")
        )
    else:
        checks.append(
            Check(
                "upstream_alias",
                "FAIL",
                f"{host} is a Controller name on {', '.join(named_on) or 'no network'}; it must "
                f"be on {origin_name} only",
            )
        )

    resolved = docker.exec(gateway["Id"], ["getent", "ahosts", host])
    addresses = sorted({line.split()[0] for line in resolved.stdout.splitlines() if line.split()})
    if resolved.returncode != 0 or not addresses:
        checks.append(Check("upstream_dns", "FAIL", f"{host} does not resolve inside the gateway"))
    elif addresses == [controller_origin]:
        checks.append(Check("upstream_dns", "PASS", f"{host} -> {controller_origin} only"))
    else:
        checks.append(
            Check(
                "upstream_dns",
                "FAIL",
                f"{host} resolves inside the gateway to {', '.join(addresses)}; expected only "
                f"{controller_origin} on {origin_name}",
            )
        )

    checks.append(edge_alias_runtime(docker, gateway, origin_name))

    controller_env = container_env(controller, RUNTIME_ENV)
    gateway_cidr = ipaddress.IPv4Network(f"{gateway_origin}/32")
    origin_ip = ipaddress.IPv4Address(gateway_origin)
    controller_ip = ipaddress.IPv4Address(controller_origin)
    gateway_all = _all_addresses(gateway)
    controller_all = _all_addresses(controller)
    gateway_ips = {a for addresses in gateway_all.values() for a in addresses}
    controller_ips = {a for addresses in controller_all.values() for a in addresses}
    # Every gateway address but its grpc-origin IPv4 one, in both families: an IPv6
    # allowlist entry admits a native IPv6 peer, so an IPv6 edge address counts too.
    other_gateway = sorted(gateway_ips - {origin_ip}, key=lambda a: (a.version, a))
    try:
        trusted = _cidrs(controller_env.get("NEST_GRPC_TRUSTED_PROXIES", ""))
        peers = _cidrs(controller_env.get("NEST_GRPC_ALLOWED_PEERS", ""))
    except ValueError as error:
        checks.append(
            Check("controller_trust", "FAIL", f"Controller setting does not parse: {error}")
        )
    else:
        problems = []
        if trusted != [gateway_cidr]:
            shown = ",".join(str(n) for n in trusted) or "unset"
            problems.append(f"NEST_GRPC_TRUSTED_PROXIES is {shown}, not {gateway_cidr}")
        if not any(gateway_cidr.subnet_of(p) for p in _ipv4(peers)):
            problems.append(f"NEST_GRPC_ALLOWED_PEERS does not admit {gateway_cidr}")
        for address in other_gateway:
            for name, networks in (
                ("NEST_GRPC_TRUSTED_PROXIES", trusted),
                ("NEST_GRPC_ALLOWED_PEERS", peers),
            ):
                if any(address.version == n.version and address in n for n in networks):
                    problems.append(
                        f"{name} admits the gateway's non-origin address {address}; never widen it "
                        "to the edge network"
                    )
        if problems:
            checks.append(Check("controller_trust", "FAIL", "; ".join(problems)))
        else:
            checks.append(Check("controller_trust", "PASS", f"trusts exactly {gateway_cidr}"))
    checks.append(
        private_path(
            docker,
            gateway["Id"],
            controller["Id"],
            host,
            int(port_text),
            origin=(origin_ip, controller_ip),
            gateway_ips=gateway_ips,
            controller_ips=controller_ips,
            origin_name=origin_name,
            sleep=sleep,
        )
    )
    emit(checks, args.json)
    return 1 if any(c.status == "FAIL" for c in checks) else 0


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
    check.add_argument(
        "--traefik-dynamic-dir",
        default=DEFAULT_TRAEFIK_DYNAMIC_DIR,
        help="Traefik's file-provider directory, read for route collisions",
    )
    check.add_argument(
        "--retire-route",
        action="append",
        metavar="FILE",
        help="a route file in that directory whose retirement is a recorded decision",
    )
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
    commands.add_parser(
        "gateway-runtime",
        parents=[common],
        help="check the running gateway's upstream DNS, trust and private path (#109)",
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
        if args.command == "gateway-runtime":
            return gateway_runtime(args, docker)
        return recover_migrate(args, docker)
    except (DockerError, OSError, ValueError) as error:
        print(f"nest-deploy-doctor: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
