#!/usr/bin/env python3
"""
Read-only deployment checks and a bounded migrate recovery for `compose.dokploy.yaml`.

Run it on the Docker host as a user who may use the Docker CLI. It needs only Python
3.9+ and `docker` (and `ip` for the route check); it does not import Nest.

    python3 scripts/nest-deploy-doctor.py preflight      --project APP --env-file .env
    python3 scripts/nest-deploy-doctor.py migrate-status --project APP [--logs 40]
    python3 scripts/nest-deploy-doctor.py wait-migrate   --project APP --timeout 300
    python3 scripts/nest-deploy-doctor.py recover-migrate --project APP [--apply]

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
    `failed` (non-zero exit), `running`, or `stuck-created` (created by Compose but
    never started: PID 0, no start time, no logs). `--logs N` adds the last N log
    lines (at most 200) of failed or running containers.

wait-migrate
    Waits, bounded by `--timeout`, for the migration to finish. Exits 0 when it
    completed, 4 when it failed, 3 when a container stays `stuck-created` for
    `--stuck-after` seconds, 5 on timeout. It starts and stops nothing.

recover-migrate
    For `stuck-created` containers only. Prints the exact `docker rm` it would run;
    with `--apply` it runs it. `docker rm` without `-f` or `-v` removes only a
    container that is not running and never touches a volume. The migration is not
    skipped: the next deploy creates a fresh `migrate` container, runs
    `nest-db upgrade` again, and the Controller still starts only after it succeeds.
    A deploy that is still waiting on the removed container fails at once instead of
    waiting forever; deploy again afterwards.

Exit codes: 0 ok, 1 a check failed, 2 usage or Docker error, 3 stuck, 4 migration
failed, 5 timed out, 6 no migrate container found.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
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

    def remove(self, container_id: str) -> None:
        self._ok(["docker", "rm", container_id])


def read_env_file(path: str | None) -> dict[str, str]:
    """Reads KEY=VALUE lines literally: no expansion, one pair of quotes removed."""
    values: dict[str, str] = {}
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
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            if key in SETTINGS:
                values[key] = value
    return values


def settings(env_file: str | None, environ: dict[str, str]) -> dict[str, str]:
    values = {key: environ[key] for key in SETTINGS if environ.get(key, "").strip()}
    values.update({k: v for k, v in read_env_file(env_file).items() if v.strip()})
    return values


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
    values = settings(args.env_file, environ)
    profiles = {p.strip() for p in values.get("COMPOSE_PROFILES", "").split(",") if p.strip()}
    networks = docker.networks()
    checks = check_origin(values, args.project, networks, host_routes(docker.run))
    if "public-grpc" in profiles:
        checks += check_gateway(values, args.project, networks, docker, args.traefik_container)
    else:
        checks.append(
            Check("public_grpc", "SKIP", "profile public-grpc is off; gateway settings not checked")
        )
    emit(checks, args.json)
    return 1 if any(c.status == "FAIL" for c in checks) else 0


@dataclass
class MigrateState:
    id: str
    name: str
    state: str  # completed, failed, running, stuck-created, other
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


def classify(container: dict) -> MigrateState:
    state = container.get("State") or {}
    status = state.get("Status", "")
    name = container.get("Name", "").lstrip("/")
    started = state.get("StartedAt", "")
    finished = state.get("FinishedAt", "")
    prefix = name.split("_", 1)[0]
    replacement = "_" in name and len(prefix) == 12 and all(c in "0123456789abcdef" for c in prefix)
    exit_code = int(state.get("ExitCode", 0) or 0)
    if status == "created" and not state.get("Pid") and started in ("", NEVER_STARTED):
        kind, detail = (
            "stuck-created",
            "created but never started (PID 0); Compose waits on it forever",
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


def migrate_states(docker: Docker, project: str) -> list[MigrateState]:
    states = [classify(c) for c in docker.containers(project, "migrate")]
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
    if newest.state == "running":
        return 5
    return 0 if newest.state == "completed" else 1


def migrate_status(args, docker: Docker) -> int:
    states = migrate_states(docker, args.project)
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
            print(f"{state.state:<13} {state.name} ({state.id[:12]}): {state.detail}")
            for line in state.logs:
                print(f"    | {line}")
    code = verdict(states)
    if code == 3 and not args.json:
        print(
            f"Recover with: python3 scripts/nest-deploy-doctor.py recover-migrate --project {args.project}"
        )
    return code


def wait_migrate(args, docker: Docker, clock=time.monotonic, sleep=time.sleep) -> int:
    start = clock()
    stuck_since: dict[str, float] = {}
    while True:
        states = migrate_states(docker, args.project)
        now = clock()
        stuck = [s for s in states if s.state == "stuck-created"]
        for state in stuck:
            stuck_since.setdefault(state.id, now)
            if now - stuck_since[state.id] >= args.stuck_after:
                print(f"stuck: {state.name} ({state.id[:12]}) {state.detail}")
                return 3
        if states and not stuck:
            newest = states[-1]
            if newest.state == "completed":
                print(f"completed: {newest.name}")
                return 0
            if newest.state == "failed":
                print(
                    f"failed: {newest.name} exited {newest.exit_code}; see migrate-status --logs 40"
                )
                return 4
        if now - start >= args.timeout:
            print(f"timed out after {args.timeout:.0f} s; see migrate-status")
            return 5
        sleep(args.interval)


def recover_migrate(args, docker: Docker) -> int:
    states = migrate_states(docker, args.project)
    stuck = [s for s in states if s.state == "stuck-created"]
    if not stuck:
        print("nothing to recover: no migrate container is stuck in Created")
        return verdict(states) if states else 6
    for state in stuck:
        # Re-read just before acting: the container must still be the same,
        # never-started migrate container of this project.
        fresh = docker.container(state.id)
        labels = ((fresh or {}).get("Config") or {}).get("Labels") or {}
        if (
            fresh is None
            or labels.get("com.docker.compose.project") != args.project
            or labels.get("com.docker.compose.service") != "migrate"
            or classify(fresh).state != "stuck-created"
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


def emit(checks: list[Check], as_json: bool) -> None:
    for check in checks:
        print(json.dumps(check.__dict__) if as_json else check.line())


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", default="nest", help="Compose project name (Dokploy appName)")
    common.add_argument("--json", action="store_true", help="one JSON object per line")
    commands = top.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "preflight", parents=[common], help="check networks and gateway settings"
    )
    check.add_argument("--env-file", help="the deployment's .env (read literally)")
    check.add_argument("--traefik-container", default=DEFAULT_TRAEFIK_CONTAINER)
    status = commands.add_parser(
        "migrate-status", parents=[common], help="classify migrate containers"
    )
    status.add_argument(
        "--logs", type=int, default=0, metavar="N", help=f"last N log lines (max {MAX_LOG_LINES})"
    )
    wait = commands.add_parser(
        "wait-migrate", parents=[common], help="bounded wait for the migration"
    )
    wait.add_argument("--timeout", type=float, default=300.0)
    wait.add_argument("--stuck-after", type=float, default=60.0)
    wait.add_argument("--interval", type=float, default=2.0)
    recover = commands.add_parser(
        "recover-migrate", parents=[common], help="remove a never-started migrate container"
    )
    recover.add_argument("--apply", action="store_true", help="run the removal (default: print it)")
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
        return recover_migrate(args, docker)
    except (DockerError, OSError, ValueError) as error:
        print(f"nest-deploy-doctor: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
