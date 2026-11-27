#!/usr/bin/env python3
"""
Collects bounded, sanitized monitoring signals of a hosted Nest deployment (R10).

Run it on the Docker host, from a timer, as a user who may use the Docker CLI (see
docs/source/monitoring.md for the exact scope and the alternatives). It needs only
Python 3.9+ and `docker`; `probe` also runs scripts/verify-public-grpc.py in a Nest
checkout. It reuses scripts/nest-deploy-doctor.py, which must sit next to it.

    python3 scripts/nest-monitor.py collect --project APP --textfile-dir DIR --state-dir DIR
    python3 scripts/nest-monitor.py probe   grpc.staging.example.org:443 --ca-file CA.pem \\
        --textfile-dir DIR --state-dir DIR

Each collector writes one Prometheus text file, `nest_<collector>.prom`, atomically
into `--textfile-dir` (node_exporter's textfile collector reads it; nothing listens
on a port), and keeps its cursor and counters in `--state-dir`. With several
deployments on one host (#118), `--environment NAME` (such as `main` or `release`)
adds `environment="NAME"` to every series and names the files
`nest_NAME_<collector>.prom`, so each deployment's files stand side by side; without
it nothing changes.

probe
    Runs the credential-free public verifier against the gateway: no session is read
    and nothing is executed (`--signed-in` and `--execute` are refused). It requires
    `--ca-file` (a staging CA), or `--system-roots` to say the certificate chains to a
    public root. It exports whether the run passed, each check's result under a fixed
    name (`origin_unreachable_<address>` becomes `origin_unreachable`), the
    certificate's days left, and when it last ran and last passed.

gateway
    Reads new lines of the gateway container's log (`docker logs --since`) and counts
    Envoy's access-log lines: requests by route (`service` or `other`) and gRPC code,
    RESOURCE_EXHAUSTED by who answered (`gateway`: Envoy's own rate limit, flag `RL`;
    `controller`: the Controller's answer, passed through), and the response flags
    in a fixed list (`UH`, `UF`, ...; anything else is `other`). A line that looks
    like an access-log line but does not parse is counted, so a format change is
    visible. The path's method, the client address and everything else in the line
    are dropped once the line is counted.

controller
    Reads new lines of the Controller's log and counts its `RPC refused` and
    `RPC rate-limited scope=user|address` warnings. Only the kind and scope are
    kept: the client address, request ID and the rest of the line are dropped.

migration
    Classifies the last migration of this deployment, read-only, as one of
    `completed`, `failed`, `stuck`, `in_progress`, `absent`, `stale` or `unknown`
    (see docs/source/monitoring.md#migration-status). It records completion evidence
    against the Controller container it was observed with, so a one-shot container
    removed after it finished still counts, until the Controller is recreated. It
    never starts, removes, recovers or upgrades anything, and it does not replace
    the read-only schema check (`nest-db verify`).

collect
    Runs gateway, controller and migration, each independently.

Nothing it writes holds a log line, a header, a token, an address, a request, User,
Project or execution ID: label values come only from fixed lists. Output on the
terminal is a one-line summary per collector.

Exit codes: 0 ok, 1 a collector failed (its text file says so), 2 usage error.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_doctor():
    loaded = sys.modules.get("nest_deploy_doctor")
    if loaded is not None:
        return loaded
    path = HERE / "nest-deploy-doctor.py"
    spec = importlib.util.spec_from_file_location("nest_deploy_doctor", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} not found; keep nest-monitor.py next to it")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


doctor = _load_doctor()
DockerError = doctor.DockerError

COLLECTORS = ("probe", "gateway", "controller", "migration")
GATEWAY_SERVICE = "grpc-gateway"
CONTROLLER_SERVICE = "controller"
DEFAULT_MAX_LINES = 200_000
DEFAULT_PROBE_TIMEOUT = 120.0
# A migrate container that last finished this long before the Controller container
# was created belongs to an earlier deploy: one `up` creates the Controller and runs
# the migration within minutes of each other.
DEFAULT_STALE_TOLERANCE = 600.0

# --- Prometheus text output -------------------------------------------------------

_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
# Label values are only ever taken from the fixed lists below; this is a second fence.
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{0,64}$")
# `--environment`: one deployment's short name when several share a host (#118), the
# same rule as deploy/monitoring/nest_alerts.py.
ENVIRONMENT = re.compile(r"^[a-z][a-z0-9]{0,7}$")


class Metrics:
    """A small Prometheus text-format writer whose label values are checked."""

    def __init__(self) -> None:
        self._families: dict[str, tuple[str, str, list[tuple[tuple, float]]]] = {}

    def add(self, name: str, kind: str, help_text: str, value: float, **labels: str) -> None:
        if not _NAME.match(name) or kind not in ("gauge", "counter"):
            raise ValueError(f"bad metric {name!r} ({kind})")
        for key, label in labels.items():
            if not _NAME.match(key) or not _LABEL_VALUE.match(str(label)):
                raise ValueError(f"label {key} of {name} has a value outside the fixed lists")
        family = self._families.setdefault(name, (kind, help_text, []))
        if family[0] != kind:
            raise ValueError(f"{name} declared as {family[0]} and {kind}")
        family[2].append((tuple(sorted(labels.items())), float(value)))

    def names(self) -> set[str]:
        return set(self._families)

    def render(self, environment: str | None = None) -> str:
        """The text format; with `environment`, every sample also carries
        `environment="<it>"` (one deployment of several on a host, #118)."""
        if environment is not None and not ENVIRONMENT.match(environment):
            raise ValueError("bad environment")
        extra = (("environment", environment),) if environment else ()
        lines = []
        for name in sorted(self._families):
            kind, help_text, samples = self._families[name]
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for own, value in sorted(samples):
                labels = tuple(sorted(own + extra))
                text = ",".join(f'{k}="{v}"' for k, v in labels)
                number = repr(value) if value != int(value) else str(int(value))
                lines.append(f"{name}{{{text}}} {number}" if text else f"{name} {number}")
        return "\n".join(lines) + "\n"


def _atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as out:
            out.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _stem(collector: str, environment: str | None) -> str:
    return f"{environment}_{collector}" if environment else collector


def write_textfile(
    directory: Path, collector: str, metrics: Metrics, environment: str | None = None
) -> Path:
    # node_exporter reads only *.prom and skips the dot-prefixed temporary file. Each
    # deployment's files have their own names, so two never overwrite each other.
    path = directory / f"nest_{_stem(collector, environment)}.prom"
    _atomic_write(path, metrics.render(environment), 0o644)
    return path


def load_state(directory: Path, collector: str, environment: str | None = None) -> dict:
    stem = _stem(collector, environment).replace("_", "-")
    try:
        data = json.loads((directory / f"nest-monitor-{stem}.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(
    directory: Path, collector: str, state: dict, environment: str | None = None
) -> None:
    text = json.dumps(state, sort_keys=True)
    stem = _stem(collector, environment).replace("_", "-")
    _atomic_write(directory / f"nest-monitor-{stem}.json", text, 0o600)


# --- docker log reading -------------------------------------------------------------

_DOCKER_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z (.*)$")


def split_docker_line(line: str) -> tuple[int, str, str] | None:
    """`(nanoseconds since the epoch, timestamp text, message)` of a
    `docker logs --timestamps` line, or `None`."""
    match = _DOCKER_TS.match(line)
    if not match:
        return None
    seconds = doctor.parse_time(match.group(1) + "Z")
    if seconds is None:
        return None
    nanoseconds = int(seconds) * 1_000_000_000 + int((match.group(2) or "").ljust(9, "0"))
    return nanoseconds, line[: match.start(3) - 1], match.group(3)


LogReader = Callable[[str, "str | None"], Iterable[str]]


def docker_log_lines(container_id: str, since: str | None) -> Iterator[str]:
    """Streams `docker logs --timestamps` (both streams) without holding it in memory."""
    argv = ["docker", "logs", "--timestamps"]
    if since:
        argv += ["--since", since]
    argv.append(container_id)
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
        )
    except FileNotFoundError as error:
        raise DockerError("docker is not installed") from error
    assert process.stdout is not None
    finished = False
    try:
        for line in process.stdout:
            yield line.rstrip("\n")
        finished = True
    finally:
        if process.poll() is None:
            process.kill()
        code = process.wait()
    if finished and code != 0:
        raise DockerError(f"docker logs exited {code}")


@dataclass
class Cursor:
    """Where the previous run stopped, so a line is counted once."""

    container: str = ""
    ns: int = 0
    text: str = ""
    at: int = 0  # lines already counted with exactly this timestamp

    @classmethod
    def load(cls, data: object) -> Cursor:
        if not isinstance(data, dict):
            return cls()
        try:
            return cls(str(data["container"]), int(data["ns"]), str(data["text"]), int(data["at"]))
        except (KeyError, TypeError, ValueError):
            return cls()

    def dump(self) -> dict:
        return {"container": self.container, "ns": self.ns, "text": self.text, "at": self.at}


@dataclass
class ReadResult:
    messages: list[str] = field(default_factory=list)
    capped: bool = False


def read_new(
    reader: LogReader,
    container: dict,
    cursor: Cursor,
    max_lines: int,
    now: float,
) -> tuple[ReadResult, Cursor]:
    """New log messages of `container` after `cursor`. The first run starts at `now`
    (history before the collector existed is not counted, so it cannot look like a
    sudden burst); a recreated container is read from its start."""
    container_id = container.get("Id", "")
    result = ReadResult()
    if cursor.container != container_id:
        if not cursor.container:
            seconds = int(now)
            text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
            return result, Cursor(container_id, seconds * 1_000_000_000, text, 0)
        cursor = Cursor(container_id)  # recreated: everything it logged is new
    since = cursor.text or None
    new = Cursor(container_id, cursor.ns, cursor.text, cursor.at)
    skip_at = cursor.at
    for line in reader(container_id, since):
        parsed = split_docker_line(line)
        if parsed is None:
            continue
        ns, text, message = parsed
        if ns < cursor.ns:
            continue
        if ns == cursor.ns and skip_at > 0:
            skip_at -= 1
            continue
        if len(result.messages) >= max_lines:
            result.capped = True
            break
        result.messages.append(message)
        if ns == new.ns:
            new.at += 1
        elif ns > new.ns:
            new.ns, new.text, new.at = ns, text, 1
    return result, new


# --- gateway (Envoy access log) -------------------------------------------------------

# The access-log format of deploy/public-grpc/envoy.yaml.template.
ACCESS_LINE = re.compile(
    r"^\[(?P<start>[^\]\s]+)\] (?P<path>\S+) grpc=(?P<grpc>\S+) http=(?P<http>\d{1,3}) "
    r"flags=(?P<flags>\S+) rx=(?P<rx>\d+) tx=(?P<tx>\d+) ms=(?P<ms>\S+)$"
)
SERVICE_PREFIX = "/nest.ExecutionService/"
GRPC_CODES = (
    "OK",
    "CANCELLED",
    "UNKNOWN",
    "INVALID_ARGUMENT",
    "DEADLINE_EXCEEDED",
    "NOT_FOUND",
    "ALREADY_EXISTS",
    "PERMISSION_DENIED",
    "RESOURCE_EXHAUSTED",
    "FAILED_PRECONDITION",
    "ABORTED",
    "OUT_OF_RANGE",
    "UNIMPLEMENTED",
    "INTERNAL",
    "UNAVAILABLE",
    "DATA_LOSS",
    "UNAUTHENTICATED",
)
_CAMEL = {code.title().replace("_", ""): code for code in GRPC_CODES}
_CAMEL["Canceled"] = "CANCELLED"  # Envoy's spelling
# Envoy's response flags (short form) worth telling apart; any other is `other`.
RESPONSE_FLAGS = (
    "UH",  # no healthy upstream
    "UF",  # upstream connection failure
    "UO",  # upstream overflow (circuit breaker)
    "URX",  # upstream retry limit
    "UT",  # upstream request timeout
    "UC",  # upstream connection termination
    "UR",  # upstream remote reset
    "LR",  # local reset
    "DC",  # downstream connection termination
    "DT",  # request or connection exceeded max duration
    "SI",  # stream idle timeout
    "RL",  # rate-limited locally
    "NR",  # no route
    "DPE",  # downstream protocol error
    "UPE",  # upstream protocol error
    "OM",  # overload manager
)


def grpc_code(value: str) -> str:
    if value in ("-", ""):
        return "UNSET"
    if value.isdigit():
        number = int(value)
        return GRPC_CODES[number] if number < len(GRPC_CODES) else "OTHER"
    if value in GRPC_CODES:
        return value
    return _CAMEL.get(value, "OTHER")


def response_flags(value: str) -> list[str]:
    if value in ("-", ""):
        return []
    found = []
    for flag in value.split(","):
        flag = flag.strip()
        name = flag if flag in RESPONSE_FLAGS else "other"
        if name not in found:
            found.append(name)
    return found


@dataclass
class AccessEntry:
    route: str
    code: str
    flags: list[str]


def parse_access_line(message: str) -> AccessEntry | None:
    """The bounded fields of one access-log line, or `None` if it is not one."""
    match = ACCESS_LINE.match(message)
    if not match:
        return None
    route = "service" if match.group("path").startswith(SERVICE_PREFIX) else "other"
    return AccessEntry(route, grpc_code(match.group("grpc")), response_flags(match.group("flags")))


def looks_like_access_line(message: str) -> bool:
    return message.startswith("[") and " grpc=" in message and " flags=" in message


def count_gateway(messages: Iterable[str], counters: dict) -> None:
    requests = counters.setdefault("requests", {})
    exhausted = counters.setdefault("resource_exhausted", {})
    flags = counters.setdefault("flags", {})
    for message in messages:
        entry = parse_access_line(message)
        if entry is None:
            if looks_like_access_line(message):
                counters["unparsed"] = counters.get("unparsed", 0) + 1
            continue
        key = f"{entry.route}/{entry.code}"
        requests[key] = requests.get(key, 0) + 1
        if entry.code == "RESOURCE_EXHAUSTED":
            source = "gateway" if "RL" in entry.flags else "controller"
            exhausted[source] = exhausted.get(source, 0) + 1
        for flag in entry.flags:
            flags[flag] = flags.get(flag, 0) + 1


def gateway_metrics(counters: dict, metrics: Metrics) -> None:
    requests = dict(counters.get("requests", {}))
    requests.setdefault("service/OK", 0)  # so a sum over no traffic is 0, not empty
    for key, value in requests.items():
        route, _, code = key.partition("/")
        metrics.add(
            "nest_gateway_requests_total",
            "counter",
            "Requests in the gateway's access log, by route (service, other) and gRPC code.",
            value,
            route=route,
            code=code,
        )
    for source in ("gateway", "controller"):
        metrics.add(
            "nest_gateway_resource_exhausted_total",
            "counter",
            "RESOURCE_EXHAUSTED answers, by who answered: gateway (Envoy rate limit) or "
            "controller.",
            counters.get("resource_exhausted", {}).get(source, 0),
            source=source,
        )
    flags = dict(counters.get("flags", {}))
    for flag in ("UH", "UF", "UO", "RL"):
        flags.setdefault(flag, 0)
    for flag, value in flags.items():
        metrics.add(
            "nest_gateway_response_flags_total",
            "counter",
            "Access-log lines carrying an Envoy response flag (fixed list; other).",
            value,
            flag=flag,
        )
    metrics.add(
        "nest_gateway_access_log_unparsed_total",
        "counter",
        "Lines that look like access-log lines but do not match the expected format.",
        counters.get("unparsed", 0),
    )


# --- controller warnings --------------------------------------------------------------

# nest._logging's format; anchored so text inside a message cannot pose as a warning.
CONTROLLER_WARNING = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z WARNING nest\.rpc_guard pid=\d+ "
    r"request_id=(?:-|[0-9a-f]{32}) RPC (?:(?P<refused>refused: )|rate-limited "
    r"scope=(?P<scope>user|address) client=)"
)


def parse_controller_line(message: str) -> tuple[str, str] | None:
    """`("refused", "origin")` or `("rate_limited", "user"|"address")`, or `None`."""
    match = CONTROLLER_WARNING.match(message)
    if not match:
        return None
    if match.group("refused"):
        return "refused", "origin"
    return "rate_limited", match.group("scope")


def count_controller(messages: Iterable[str], counters: dict) -> None:
    for message in messages:
        found = parse_controller_line(message)
        if found:
            key = "/".join(found)
            counters[key] = counters.get(key, 0) + 1


def controller_metrics(counters: dict, metrics: Metrics) -> None:
    metrics.add(
        "nest_controller_rpc_refused_total",
        "counter",
        "Controller 'RPC refused' warnings: a call from outside NEST_GRPC_ALLOWED_PEERS.",
        counters.get("refused/origin", 0),
        reason="origin",
    )
    for scope in ("user", "address"):
        metrics.add(
            "nest_controller_rpc_rate_limited_total",
            "counter",
            "Controller 'RPC rate-limited' warnings, by scope.",
            counters.get(f"rate_limited/{scope}", 0),
            scope=scope,
        )


# --- collectors -----------------------------------------------------------------------


@dataclass
class Outcome:
    ok: bool
    summary: str
    metrics: Metrics = field(default_factory=Metrics)


def _one_container(docker, project: str, service: str) -> dict:
    found = [
        c for c in docker.containers(project, service) if (c.get("State") or {}).get("Running")
    ]
    if len(found) != 1:
        raise DockerError(
            f"expected one running {service} container in project {project}, found {len(found)}"
        )
    return found[0]


def collect_logs(
    kind: str,
    args,
    docker,
    state: dict,
    reader: LogReader,
    now: float,
) -> Outcome:
    service = GATEWAY_SERVICE if kind == "gateway" else CONTROLLER_SERVICE
    counters = state.setdefault("counters", {})
    outcome = Outcome(False, "")
    try:
        container = _one_container(docker, args.project, service)
        result, cursor = read_new(
            reader, container, Cursor.load(state.get("cursor")), args.max_lines, now
        )
    except DockerError as error:
        outcome.summary = f"{kind}: {error}"
        result = None
    else:
        (count_gateway if kind == "gateway" else count_controller)(result.messages, counters)
        state["cursor"] = cursor.dump()
        outcome.ok = True
        outcome.summary = f"{kind}: {len(result.messages)} new lines" + (
            f" (capped at {args.max_lines}; the rest next run)" if result.capped else ""
        )
    (gateway_metrics if kind == "gateway" else controller_metrics)(counters, outcome.metrics)
    if kind == "gateway":
        outcome.metrics.add(
            "nest_gateway_log_backlog",
            "gauge",
            "1 when the last run stopped at --max-lines and lines are still unread.",
            1 if result is not None and result.capped else 0,
        )
    return outcome


MIGRATION_STATUSES = ("completed", "failed", "stuck", "in_progress", "absent", "stale", "unknown")


@dataclass
class Migration:
    status: str
    detail: str
    deployed: float = 0.0  # when the current Controller container was created
    finished: float = 0.0
    recorded: bool = False


def classify_migration(
    docker, project: str, evidence: dict, min_age: float, tolerance: float
) -> tuple[Migration, dict]:
    """The migration status and the evidence to keep (see the module documentation)."""
    try:
        controllers = sorted(
            docker.containers(project, CONTROLLER_SERVICE), key=lambda c: c.get("Created", "")
        )
        states = doctor.migrate_states(docker, project, min_age)
    except DockerError as error:
        return Migration("unknown", f"docker: {error}"), evidence
    if not controllers:
        return Migration("unknown", "no Controller container in this project"), evidence
    controller = controllers[-1]
    controller_id = controller.get("Id", "")
    deployed = doctor.parse_time(controller.get("Created", "")) or 0.0
    if not states:
        if evidence.get("controller") == controller_id and evidence.get("finished"):
            return (
                Migration(
                    "completed",
                    "recorded: a migration completed for this Controller container; its "
                    "container has since been removed",
                    deployed,
                    float(evidence["finished"]),
                    True,
                ),
                evidence,
            )
        return (
            Migration(
                "absent",
                "no migrate container and no completion recorded for this Controller container",
                deployed,
            ),
            evidence,
        )
    # A migrate container is observed: it is the latest attempt, and only its own
    # outcome counts. Evidence recorded for an earlier attempt is dropped unless this
    # one completed, so a later failed (or stuck, running, stale) attempt that is then
    # removed can never fall back to the earlier success.
    if any(s.state == "stuck-created" for s in states):
        return Migration("stuck", "a migrate container never started", deployed), {}
    newest = states[-1]
    finished = doctor.parse_time(newest.finished) or 0.0
    if newest.state == "failed":
        return Migration("failed", f"exited {newest.exit_code}", deployed, finished), {}
    if newest.state in ("running", "pending", "waiting-dependency"):
        return Migration("in_progress", newest.state, deployed), {}
    if newest.state != "completed":
        return Migration("unknown", f"migrate container state {newest.state}", deployed), {}
    # When it last ran, not when it was created: a deploy that recreates only the
    # Controller starts the existing migrate container again, keeping its old
    # creation time (the doctor's `wait-migrate --since` judges by finish time too).
    ran = finished or doctor.parse_time(newest.started) or doctor.parse_time(newest.created) or 0.0
    if ran < deployed - tolerance:
        return (
            Migration(
                "stale",
                "the newest migrate container last finished before the Controller "
                "container was created: it belongs to an earlier deploy",
                deployed,
                finished,
            ),
            {},
        )
    kept = {"controller": controller_id, "migrate": newest.id, "finished": finished}
    return Migration("completed", "nest-db upgrade finished", deployed, finished), kept


def collect_migration(args, docker, state: dict) -> Outcome:
    found, evidence = classify_migration(
        docker, args.project, state.get("evidence") or {}, args.min_age, args.stale_tolerance
    )
    state["evidence"] = evidence
    outcome = Outcome(found.status != "unknown", f"migration: {found.status} ({found.detail})")
    for status in MIGRATION_STATUSES:
        outcome.metrics.add(
            "nest_migration_status",
            "gauge",
            "1 for the current migration status of this deployment, 0 for the others.",
            1 if status == found.status else 0,
            status=status,
        )
    outcome.metrics.add(
        "nest_migration_deploy_timestamp_seconds",
        "gauge",
        "When the current Controller container was created (0 if unknown).",
        found.deployed,
    )
    outcome.metrics.add(
        "nest_migration_finished_timestamp_seconds",
        "gauge",
        "When the migration counted for this deployment finished (0 if none).",
        found.finished,
    )
    outcome.metrics.add(
        "nest_migration_evidence_recorded",
        "gauge",
        "1 when completion comes from recorded evidence, its container since removed.",
        1 if found.recorded else 0,
    )
    return outcome


PROBE_CHECKS = (
    "tls",
    "tls_wrong_name_refused",
    "tls_http1_only_not_h2",
    "unauthenticated_refused",
    "non_nest_path_refused",
    "origin_unreachable",
)
_ADDRESS = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):\d{1,5}$")

ProbeRunner = Callable[[Sequence[str], float, dict], "tuple[int, str]"]


def run_verifier(argv: Sequence[str], timeout: float, env: dict) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError as error:
        raise DockerError(f"{argv[0]} is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise DockerError(f"the verifier did not finish in {timeout:.0f} s") from error
    return done.returncode, done.stdout


def verifier_argv(args) -> list[str]:
    argv = [*shlex.split(args.runner), args.verifier, args.address]
    if args.ca_file:
        argv += ["--ca-file", args.ca_file]
    for origin in args.origin:
        argv += ["--origin", origin]
    return [*argv, "--timeout", str(args.check_timeout)]


def probe_environment(environ: dict[str, str]) -> dict[str, str]:
    """The verifier's environment without any NEST_ setting: credential-free runs
    read no session, and no setting can redirect it."""
    return {k: v for k, v in environ.items() if not k.startswith("NEST_")}


def collect_probe(args, state: dict, runner: ProbeRunner, now: float, environ) -> Outcome:
    outcome = Outcome(False, "")
    checks: dict[str, int] = {}
    days_left = None
    passed = False
    try:
        code, stdout = runner(verifier_argv(args), args.probe_timeout, probe_environment(environ))
    except DockerError as error:
        outcome.summary = f"probe: could not run: {error}"
    else:
        summary = None
        for line in stdout.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if "summary" in record:
                summary = record
                continue
            name = str(record.get("check", ""))
            name = "origin_unreachable" if name.startswith("origin_unreachable_") else name
            if name not in PROBE_CHECKS:
                continue
            ok = 1 if record.get("passed") is True else 0
            checks[name] = min(checks.get(name, 1), ok)
            if name == "tls" and ok and isinstance(record.get("days_left"), int):
                days_left = record["days_left"]
        if summary is None or "tls" not in checks:
            outcome.summary = f"probe: the verifier exited {code} without a result"
        else:
            outcome.ok = True
            passed = code == 0 and summary.get("summary") == "pass" and all(checks.values())
            failed = sorted(k for k, v in checks.items() if not v)
            outcome.summary = "probe: pass" if passed else f"probe: FAIL {', '.join(failed)}"
    if passed:
        state["last_pass"] = now
    metrics = outcome.metrics
    metrics.add(
        "nest_public_probe_success",
        "gauge",
        "1 if the last credential-free public probe passed every check.",
        1 if passed else 0,
    )
    metrics.add(
        "nest_public_probe_last_run_timestamp_seconds",
        "gauge",
        "When the public probe last ran.",
        now,
    )
    metrics.add(
        "nest_public_probe_last_success_timestamp_seconds",
        "gauge",
        "When the public probe last passed (0 if never).",
        float(state.get("last_pass") or 0),
    )
    for name in PROBE_CHECKS:
        if name in checks:
            metrics.add(
                "nest_public_probe_check_success",
                "gauge",
                "1 if this check of the last public probe passed.",
                checks[name],
                check=name,
            )
    if days_left is not None:
        metrics.add(
            "nest_public_tls_certificate_days_left",
            "gauge",
            "Whole days until the gateway certificate expires, from the last probe.",
            days_left,
        )
    return outcome


def finish(name: str, outcome: Outcome, args, state: dict, now: float) -> int:
    if outcome.ok:
        state["last_success"] = now
    metrics = outcome.metrics
    metrics.add(
        "nest_monitor_last_run_timestamp_seconds",
        "gauge",
        "When this collector last ran.",
        now,
        collector=name,
    )
    metrics.add(
        "nest_monitor_last_success_timestamp_seconds",
        "gauge",
        "When this collector last ran successfully (0 if never).",
        float(state.get("last_success") or 0),
        collector=name,
    )
    metrics.add(
        "nest_monitor_collector_success",
        "gauge",
        "1 if this collector's last run succeeded.",
        1 if outcome.ok else 0,
        collector=name,
    )
    environment = getattr(args, "environment", None)
    write_textfile(Path(args.textfile_dir), name, metrics, environment)
    save_state(Path(args.state_dir), name, state, environment)
    print(outcome.summary, flush=True)
    return 0 if outcome.ok else 1


# --- command line -----------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__.split("\n\n")[1].strip())
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--textfile-dir", required=True, help="node_exporter textfile directory")
    common.add_argument("--state-dir", required=True, help="this collector's private state")
    common.add_argument(
        "--environment",
        help="this deployment's short name when several share the host (#118), such as main",
    )
    docker_args = argparse.ArgumentParser(add_help=False)
    docker_args.add_argument("--project", required=True, help="the Compose project (Dokploy app)")
    docker_args.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES)
    docker_args.add_argument("--min-age", type=float, default=doctor.DEFAULT_MIN_AGE)
    docker_args.add_argument("--stale-tolerance", type=float, default=DEFAULT_STALE_TOLERANCE)
    commands = top.add_subparsers(dest="command", required=True)
    for name in ("gateway", "controller", "migration", "collect"):
        commands.add_parser(name, parents=[common, docker_args])
    probe = commands.add_parser("probe", parents=[common])
    probe.add_argument("address", help="the gateway, such as grpc.staging.example.org:443")
    roots = probe.add_mutually_exclusive_group(required=True)
    roots.add_argument("--ca-file", help="PEM roots of the staging CA")
    roots.add_argument(
        "--system-roots", action="store_true", help="the certificate chains to a public root"
    )
    probe.add_argument("--origin", action="append", default=[], help="HOST:PORT, unreachable")
    probe.add_argument("--verifier", default=str(HERE / "verify-public-grpc.py"))
    probe.add_argument(
        "--runner",
        default="uv run --frozen python",
        help="how to run the verifier with Nest installed (default: %(default)s)",
    )
    probe.add_argument("--probe-timeout", type=float, default=DEFAULT_PROBE_TIMEOUT)
    probe.add_argument("--check-timeout", type=float, default=15.0)
    return top


def main(
    argv: Sequence[str] | None = None,
    docker=None,
    reader: LogReader = docker_log_lines,
    runner: ProbeRunner = run_verifier,
    clock: Callable[[], float] = time.time,
    environ: dict[str, str] | None = None,
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if any(a.split("=", 1)[0] in ("--signed-in", "--execute") for a in raw):
        print("refused: the monitor runs only the credential-free checks", file=sys.stderr)
        return 2
    args = parser().parse_args(raw)
    if args.environment is not None and not ENVIRONMENT.match(args.environment):
        print("refused: --environment is 1-8 lower-case letters or digits", file=sys.stderr)
        return 2
    docker = docker or doctor.Docker()
    now = clock()
    state_dir = Path(args.state_dir)
    if args.command == "probe":
        if not _ADDRESS.match(args.address):
            print("refused: the address must be HOST:PORT", file=sys.stderr)
            return 2
        if args.ca_file and not os.path.isfile(args.ca_file):
            print(f"refused: --ca-file {args.ca_file} is not a file", file=sys.stderr)
            return 2
        state = load_state(state_dir, "probe", args.environment)
        outcome = collect_probe(
            args, state, runner, now, os.environ if environ is None else environ
        )
        return finish("probe", outcome, args, state, now)
    if args.max_lines < 1:
        print("refused: --max-lines must be positive", file=sys.stderr)
        return 2
    names = ("gateway", "controller", "migration") if args.command == "collect" else (args.command,)
    code = 0
    for name in names:
        state = load_state(state_dir, name, args.environment)
        if name == "migration":
            outcome = collect_migration(args, docker, state)
        else:
            outcome = collect_logs(name, args, docker, state, reader, now)
        code = max(code, finish(name, outcome, args, state, now))
    return code


if __name__ == "__main__":
    sys.exit(main())
