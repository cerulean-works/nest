"""Tests for the hosted-pilot monitoring of #114 (R10): scripts/nest-monitor.py and
deploy/monitoring/nest_alerts.py.

Everything here is synthetic: recorded `docker` answers, log lines in the formats
the gateway template and nest._logging produce, and a fake verifier. The alert rules
are checked and evaluated with `promtool` against synthetic series when it is
available (`NEST_TEST_PROMTOOL`, or `promtool` on PATH; required when
`NEST_TEST_PROMTOOL_REQUIRED=1`, as in CI). tests/test_public_gateway.py parses the
access log of the real Envoy with the same parser.
"""

import importlib.util
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MONITOR = ROOT / "scripts" / "nest-monitor.py"
ALERTS = ROOT / "deploy" / "monitoring" / "nest_alerts.py"
TEMPLATE = ROOT / "deploy" / "public-grpc" / "envoy.yaml.template"

if not MONITOR.is_file() or not ALERTS.is_file():
    raise unittest.SkipTest("requires the repository's scripts/ and deploy/")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


monitor = _load("nest_monitor", MONITOR)
alerts = _load("nest_alerts", ALERTS)
doctor = monitor.doctor

NEVER = "0001-01-01T00:00:00Z"
NOW = 1_795_000_000.0  # 2026-11-17T07:46:40Z
SECRET = "nest-session-" + "s" * 40
CLIENT = "203.0.113.77"
REQUEST_ID = uuid.uuid4().hex
PROJECT_ID = str(uuid.uuid4())


def iso(seconds, fraction=""):
    import datetime

    moment = datetime.datetime.fromtimestamp(seconds, datetime.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


def container(
    service,
    *,
    status="running",
    created=NOW - 3600,
    exit_code=0,
    running=None,
    started=None,
    finished=NEVER,
    pid=1,
    name=None,
):
    running = status == "running" if running is None else running
    return {
        "Id": uuid.uuid4().hex + uuid.uuid4().hex,
        "Name": "/" + (name or f"app-{service}-1"),
        "Created": iso(created),
        "State": {
            "Status": status,
            "Running": running,
            "Pid": pid if running else 0,
            "ExitCode": exit_code,
            "StartedAt": started if started is not None else iso(created + 1),
            "FinishedAt": finished,
        },
    }


class FakeDocker:
    """Answers `docker ps` / `docker container inspect` only: the collectors may run
    nothing else, so any other command fails the test."""

    def __init__(self, containers=None, broken=False):
        self.by_service = containers or {}
        self.broken = broken
        self.calls = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        R = doctor.Result
        if self.broken:
            return R(1, "", "Cannot connect to the Docker daemon")
        if argv[:2] == ["docker", "ps"]:
            labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--filter"]
            wanted = dict(label.removeprefix("label=").split("=", 1) for label in labels)
            found = self.by_service.get(wanted.get("com.docker.compose.service"), [])
            return R(0, "\n".join(c["Id"] for c in found))
        if argv[:3] == ["docker", "container", "inspect"]:
            every = {c["Id"]: c for cs in self.by_service.values() for c in cs}
            return R(0, json.dumps([every[i] for i in argv[3:]]))
        raise AssertionError(f"the monitor ran a command it must not: {argv}")


class FakeLogs:
    def __init__(self, lines=None):
        self.lines = {} if lines is None else lines  # container id -> lines
        self.calls = []

    def __call__(self, container_id, since):
        self.calls.append((container_id, since))
        return list(self.lines.get(container_id, []))


def access(path="/nest.ExecutionService/Execute", grpc="OK", http=200, flags="-"):
    return (
        f"[2026-11-27T20:26:28.123Z] {path} grpc={grpc} http={http} flags={flags} rx=10 tx=20 ms=4"
    )


def warning(message, logger="nest.rpc_guard", request_id="-"):
    return f"2026-11-27T20:26:28.123Z WARNING {logger} pid=7 request_id={request_id} {message}"


def stamped(seconds, message, fraction=".000000001"):
    return f"{iso(seconds, fraction)} {message}"


def parse_textfile(text):
    """{(name, frozenset(labels)): value} of a Prometheus text file."""
    samples = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([a-z_:]+)(?:\{(.*)\})? (\S+)", line)
        assert match, line
        labels = frozenset(re.findall(r'(\w+)="([^"]*)"', match.group(2) or ""))
        samples[(match.group(1), labels)] = float(match.group(3))
    return samples


def value(samples, name, **labels):
    return samples[(name, frozenset(labels.items()))]


class _Dirs(unittest.TestCase):
    def setUp(self):
        self.textfiles = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def run_monitor(self, argv, docker=None, reader=None, runner=None, now=NOW, environ=None):
        out = io.StringIO()
        with redirect_stdout(out):
            code = monitor.main(
                [*argv, "--textfile-dir", str(self.textfiles), "--state-dir", str(self.state)],
                docker=doctor.Docker(run=docker) if docker else None,
                reader=reader or FakeLogs(),
                runner=runner or (lambda *a: (0, "")),
                clock=lambda: now,
                environ=environ or {},
            )
        return code, out.getvalue()

    def samples(self, collector):
        return parse_textfile((self.textfiles / f"nest_{collector}.prom").read_text())

    def everything_written(self):
        texts = [p.read_text() for p in (*self.textfiles.iterdir(), *self.state.iterdir())]
        return "\n".join(texts)


class MetricsTests(unittest.TestCase):
    def test_label_values_outside_the_fixed_lists_are_refused(self):
        metrics = monitor.Metrics()
        for bad in (CLIENT + " x", 'a"b', "x" * 65, "a\nb", "{}"):
            with self.assertRaises(ValueError):
                metrics.add("nest_x_total", "counter", "x", 1, code=bad)
        with self.assertRaises(ValueError):
            metrics.add("nest-x", "gauge", "x", 1)

    def test_render_is_prometheus_text(self):
        metrics = monitor.Metrics()
        metrics.add("nest_a_total", "counter", "A.", 3, code="OK")
        metrics.add("nest_b", "gauge", "B.", 0.5)
        self.assertEqual(
            metrics.render(),
            '# HELP nest_a_total A.\n# TYPE nest_a_total counter\nnest_a_total{code="OK"} 3\n'
            "# HELP nest_b B.\n# TYPE nest_b gauge\nnest_b 0.5\n",
        )

    def test_textfile_is_replaced_atomically_and_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            metrics = monitor.Metrics()
            metrics.add("nest_b", "gauge", "B.", 1)
            path = monitor.write_textfile(Path(directory), "gateway", metrics)
            self.assertEqual(path.name, "nest_gateway.prom")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
            self.assertEqual(os.listdir(directory), ["nest_gateway.prom"])


class DockerLogCursorTests(unittest.TestCase):
    def test_timestamps_keep_nanoseconds(self):
        ns, text, message = monitor.split_docker_line("2026-11-27T20:26:28.123456789Z hello world")
        self.assertEqual(text, "2026-11-27T20:26:28.123456789Z")
        self.assertEqual(message, "hello world")
        self.assertEqual(ns % 1_000_000_000, 123456789)
        trimmed, _, _ = monitor.split_docker_line("2026-11-27T20:26:28.1234Z x")
        self.assertEqual(trimmed % 1_000_000_000, 123400000)
        self.assertIsNone(monitor.split_docker_line("no timestamp"))

    def test_first_run_starts_now_and_later_runs_count_each_line_once(self):
        gateway = container("grpc-gateway")
        logs = FakeLogs()
        result, cursor = monitor.read_new(logs, gateway, monitor.Cursor(), 100, NOW)
        self.assertEqual(result.messages, [])  # history is not replayed as a burst
        self.assertEqual(logs.calls, [])
        logs.lines[gateway["Id"]] = [
            stamped(NOW - 10, "old, before the collector existed"),
            stamped(NOW + 5, "a", ".5"),
            stamped(NOW + 5, "b", ".5"),
            stamped(NOW + 6, "c"),
        ]
        result, cursor = monitor.read_new(logs, gateway, cursor, 100, NOW + 60)
        self.assertEqual(result.messages, ["a", "b", "c"])
        self.assertEqual(logs.calls[-1], (gateway["Id"], iso(NOW)))
        # Docker's --since includes the cursor's own timestamp: those lines are skipped
        # exactly as many times as they were counted, and a third line there is new.
        logs.lines[gateway["Id"]] = [
            stamped(NOW + 6, "c"),
            stamped(NOW + 6, "d"),
            stamped(NOW + 7, "e"),
        ]
        result, cursor = monitor.read_new(logs, gateway, cursor, 100, NOW + 120)
        self.assertEqual(result.messages, ["d", "e"])
        result, cursor = monitor.read_new(logs, gateway, cursor, 100, NOW + 180)
        self.assertEqual(result.messages, [])

    def test_a_recreated_container_is_read_from_its_start(self):
        old, new = container("grpc-gateway"), container("grpc-gateway")
        cursor = monitor.Cursor(old["Id"], int(NOW) * 10**9, iso(NOW), 1)
        logs = FakeLogs({new["Id"]: [stamped(NOW - 100, "first line of the new container")]})
        result, cursor = monitor.read_new(logs, new, cursor, 100, NOW)
        self.assertEqual(result.messages, ["first line of the new container"])
        self.assertEqual(logs.calls, [(new["Id"], None)])
        self.assertEqual(cursor.container, new["Id"])

    def test_reading_is_bounded_and_resumes(self):
        gateway = container("grpc-gateway")
        cursor = monitor.Cursor(gateway["Id"], int(NOW) * 10**9, iso(NOW), 0)
        lines = [stamped(NOW + i, f"line {i}") for i in range(1, 8)]
        logs = FakeLogs({gateway["Id"]: lines})
        result, cursor = monitor.read_new(logs, gateway, cursor, 3, NOW)
        self.assertEqual(result.messages, ["line 1", "line 2", "line 3"])
        self.assertTrue(result.capped)
        result, cursor = monitor.read_new(logs, gateway, cursor, 3, NOW)
        self.assertEqual(result.messages, ["line 4", "line 5", "line 6"])


class AccessLogTests(unittest.TestCase):
    def test_the_template_format_is_the_one_parsed(self):
        template = TEMPLATE.read_text()
        found = re.search(r'inline_string: "(.*)\\n"', template)
        assert found is not None
        fmt = found.group(1)
        rendered = (
            fmt.replace("%START_TIME%", "2026-11-27T20:26:28.123Z")
            .replace("%REQ(:PATH)%", "/nest.ExecutionService/Execute")
            .replace("%GRPC_STATUS%", "ResourceExhausted")
            .replace("%RESPONSE_CODE%", "200")
            .replace("%RESPONSE_FLAGS%", "RL")
            .replace("%BYTES_RECEIVED%", "12")
            .replace("%BYTES_SENT%", "0")
            .replace("%DURATION%", "1")
        )
        self.assertNotIn("%", rendered, "the access-log format gained a field: update the parser")
        entry = monitor.parse_access_line(rendered)
        self.assertEqual(
            (entry.route, entry.code, entry.flags), ("service", "RESOURCE_EXHAUSTED", ["RL"])
        )

    def test_codes_in_every_spelling_map_to_fixed_names(self):
        for raw, code in (
            ("OK", "OK"),
            ("ResourceExhausted", "RESOURCE_EXHAUSTED"),
            ("RESOURCE_EXHAUSTED", "RESOURCE_EXHAUSTED"),
            ("8", "RESOURCE_EXHAUSTED"),
            ("Unauthenticated", "UNAUTHENTICATED"),
            ("Canceled", "CANCELLED"),
            ("Unavailable", "UNAVAILABLE"),
            ("-", "UNSET"),
            ("99", "OTHER"),
            ("Bogus" + CLIENT, "OTHER"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(monitor.parse_access_line(access(grpc=raw)).code, code)

    def test_flags_and_routes_are_bounded(self):
        entry = monitor.parse_access_line(
            access(path="/grpc.health.v1.Health/Check", flags="UF,URX,ZZ,QQ")
        )
        self.assertEqual(entry.route, "other")
        self.assertEqual(entry.flags, ["UF", "URX", "other"])
        attacker = "/nest.ExecutionService/" + "X" * 500
        self.assertEqual(monitor.parse_access_line(access(path=attacker)).route, "service")

    def test_gateway_and_controller_rejections_are_told_apart(self):
        counters = {}
        monitor.count_gateway(
            [
                access(grpc="ResourceExhausted", flags="RL"),
                access(grpc="ResourceExhausted"),
                access(grpc="ResourceExhausted"),
                access(),
                access(grpc="Unavailable", flags="UH"),
                "[2026-11-27 20:26:28.123][1][warning][config] an Envoy message, not access",
                "[2026-11-27T20:26:28.123Z] /x grpc=OK flags=- truncated",
            ],
            counters,
        )
        self.assertEqual(counters["resource_exhausted"], {"gateway": 1, "controller": 2})
        self.assertEqual(counters["requests"]["service/RESOURCE_EXHAUSTED"], 3)
        self.assertEqual(counters["requests"]["service/OK"], 1)
        self.assertEqual(counters["flags"], {"RL": 1, "UH": 1})
        self.assertEqual(counters["unparsed"], 1)


class ControllerLogTests(unittest.TestCase):
    def real_line(self, message, *args):
        """A line exactly as the Controller's configured handler writes it."""
        from nest import _logging

        _logging.configure_logging()
        handler = next(
            h for h in logging.getLogger("nest").handlers if getattr(h, "_nest_console", False)
        )
        record = logging.getLogger("nest.rpc_guard").makeRecord(
            "nest.rpc_guard",
            logging.WARNING,
            __file__,
            1,
            message,
            args,
            None,
            extra={"request_id": REQUEST_ID},
        )
        return handler.format(record)

    def test_the_real_warnings_are_counted_by_kind_and_scope(self):
        lines = [
            self.real_line("RPC refused: connection from outside NEST_GRPC_ALLOWED_PEERS"),
            self.real_line("RPC rate-limited scope=user client=%s", CLIENT),
            self.real_line("RPC rate-limited scope=address client=%s", CLIENT),
            self.real_line("RPC rate-limited scope=address client=%s", "-"),
        ]
        counters = {}
        monitor.count_controller(lines, counters)
        self.assertEqual(
            counters, {"refused/origin": 1, "rate_limited/user": 1, "rate_limited/address": 2}
        )

    def test_text_inside_another_message_cannot_pose_as_a_warning(self):
        forged = "RPC refused: connection from outside NEST_GRPC_ALLOWED_PEERS"
        counters = {}
        monitor.count_controller(
            [
                warning(f"Project name {warning(forged)}", logger="nest.projects"),
                warning(forged, logger="nest.other"),
                warning("RPC rate-limited scope=project client=x"),
                "  " + warning(forged),
                f"2026-11-27T20:26:28.123Z INFO nest.rpc_guard pid=7 request_id=- {forged}",
                warning(forged, request_id="not-a-request-id"),
            ],
            counters,
        )
        self.assertEqual(counters, {})


class LogCollectorTests(_Dirs):
    def setUp(self):
        super().setUp()
        self.gateway = container("grpc-gateway")
        self.controller = container("controller")
        self.docker = FakeDocker({"grpc-gateway": [self.gateway], "controller": [self.controller]})
        self.logs = FakeLogs()

    def collect(self, kind, now):
        return self.run_monitor([kind, "--project", "app"], self.docker, self.logs, now=now)

    def test_counters_accumulate_and_hold_nothing_identifying(self):
        code, out = self.collect("gateway", NOW)
        self.assertEqual(code, 0, out)
        self.assertEqual(
            value(
                self.samples("gateway"), "nest_gateway_requests_total", route="service", code="OK"
            ),
            0,
        )
        self.logs.lines[self.gateway["Id"]] = [
            stamped(NOW + 1, access(path=f"/nest.ExecutionService/Execute?project={PROJECT_ID}")),
            stamped(NOW + 2, access(grpc="ResourceExhausted")),
            stamped(NOW + 3, f"authorization: Bearer {SECRET} x-forwarded-for: {CLIENT}"),
        ]
        self.logs.lines[self.controller["Id"]] = [
            stamped(
                NOW + 1,
                warning(f"RPC rate-limited scope=address client={CLIENT}", request_id=REQUEST_ID),
            ),
            stamped(
                NOW + 2, warning(f"Session {SECRET} for project {PROJECT_ID}", logger="nest.auth")
            ),
        ]
        self.collect("controller", NOW)
        code, out = self.run_monitor(
            ["collect", "--project", "app"], self.docker, self.logs, now=NOW + 60
        )
        self.assertEqual(code, 0, out)  # migration is `absent`: a status, not a collector failure
        gateway = self.samples("gateway")
        self.assertEqual(
            value(gateway, "nest_gateway_requests_total", route="service", code="OK"), 1
        )
        self.assertEqual(
            value(gateway, "nest_gateway_resource_exhausted_total", source="controller"), 1
        )
        self.assertEqual(
            value(gateway, "nest_gateway_resource_exhausted_total", source="gateway"), 0
        )
        controller = self.samples("controller")
        self.assertEqual(
            value(controller, "nest_controller_rpc_rate_limited_total", scope="address"), 1
        )
        self.assertEqual(value(controller, "nest_controller_rpc_refused_total", reason="origin"), 0)
        written = self.everything_written() + out
        for leaked in (SECRET, CLIENT, REQUEST_ID, PROJECT_ID, "Bearer"):
            self.assertNotIn(leaked, written)
        # A later run with nothing new keeps the counters (they only grow).
        self.collect("gateway", NOW + 120)
        self.assertEqual(
            value(
                self.samples("gateway"), "nest_gateway_requests_total", route="service", code="OK"
            ),
            1,
        )

    def test_a_missing_container_fails_the_collector_but_keeps_its_counters(self):
        self.collect("gateway", NOW)
        self.logs.lines[self.gateway["Id"]] = [stamped(NOW + 1, access())]
        self.collect("gateway", NOW + 60)
        self.docker.by_service["grpc-gateway"] = []
        code, out = self.collect("gateway", NOW + 120)
        self.assertEqual(code, 1)
        self.assertIn("expected one running grpc-gateway container", out)
        samples = self.samples("gateway")
        self.assertEqual(value(samples, "nest_monitor_collector_success", collector="gateway"), 0)
        self.assertEqual(
            value(samples, "nest_monitor_last_success_timestamp_seconds", collector="gateway"),
            NOW + 60,
        )
        self.assertEqual(
            value(samples, "nest_monitor_last_run_timestamp_seconds", collector="gateway"),
            NOW + 120,
        )
        self.assertEqual(
            value(samples, "nest_gateway_requests_total", route="service", code="OK"), 1
        )

    def test_a_failing_docker_logs_counts_nothing_twice(self):
        self.collect("gateway", NOW)

        def broken(container_id, since):
            yield stamped(NOW + 1, access())
            raise doctor.DockerError("docker logs exited 1")

        code, _ = self.run_monitor(
            ["gateway", "--project", "app"], self.docker, broken, now=NOW + 60
        )
        self.assertEqual(code, 1)
        self.logs.lines[self.gateway["Id"]] = [stamped(NOW + 1, access())]
        self.collect("gateway", NOW + 120)
        self.assertEqual(
            value(
                self.samples("gateway"), "nest_gateway_requests_total", route="service", code="OK"
            ),
            1,
        )

    def test_only_read_only_docker_commands_run(self):
        self.run_monitor(["collect", "--project", "app"], self.docker, self.logs)
        for argv in self.docker.calls:
            self.assertIn(argv[:2], (["docker", "ps"], ["docker", "container"]))
            if argv[1] == "container":
                self.assertEqual(argv[2], "inspect")


class MigrationTests(_Dirs):
    def run_migration(self, docker, now=NOW):
        code, out = self.run_monitor(["migration", "--project", "app"], docker, now=now)
        samples = self.samples("migration")
        status = [
            s
            for s in monitor.MIGRATION_STATUSES
            if value(samples, "nest_migration_status", status=s)
        ]
        self.assertEqual(len(status), 1, samples)
        return code, status[0], samples, out

    def deployment(self, migrate, controller_created=NOW - 3600):
        return {
            "controller": [container("controller", created=controller_created)],
            "migrate": migrate,
            "postgres": [],
        }

    def completed(self, created=NOW - 3610):
        return container(
            "migrate",
            status="exited",
            created=created,
            finished=iso(created + 5),
            name="app-migrate-1",
        )

    def test_each_status(self):
        cases = {
            "completed": [self.completed()],
            "failed": [
                container("migrate", status="exited", exit_code=1, finished=iso(NOW - 3500))
            ],
            "stuck": [
                container(
                    "migrate", status="created", started=NEVER, created=NOW - 3600, running=False
                )
            ],
            "in_progress": [container("migrate", status="running", created=NOW - 60)],
            "stale": [self.completed(created=NOW - 86400)],
            "absent": [],
        }
        for expected, migrate in cases.items():
            with self.subTest(expected):
                for path in self.state.iterdir():
                    path.unlink()
                code, status, samples, out = self.run_migration(
                    FakeDocker(self.deployment(migrate))
                )
                self.assertEqual(status, expected, out)
                self.assertEqual(code, 0)  # the collector worked; the status is the signal
                self.assertEqual(
                    value(samples, "nest_migration_deploy_timestamp_seconds"), NOW - 3600
                )

    def test_unknown_when_docker_or_the_controller_is_missing(self):
        code, status, samples, _ = self.run_migration(FakeDocker(broken=True))
        self.assertEqual((code, status), (1, "unknown"))
        self.assertEqual(value(samples, "nest_migration_deploy_timestamp_seconds"), 0)
        code, status, _, _ = self.run_migration(FakeDocker({"controller": [], "migrate": []}))
        self.assertEqual((code, status), (1, "unknown"))

    def test_recorded_evidence_survives_removal_until_the_controller_is_recreated(self):
        deployment = self.deployment([self.completed()])
        self.run_migration(FakeDocker(deployment))
        deployment["migrate"] = []  # Dokploy or a prune removed the finished container
        _, status, samples, out = self.run_migration(FakeDocker(deployment), now=NOW + 60)
        self.assertEqual(status, "completed", out)
        self.assertIn("recorded", out)
        self.assertEqual(value(samples, "nest_migration_evidence_recorded"), 1)
        # A new deploy recreates the Controller: the old evidence no longer counts.
        deployment["controller"] = [container("controller", created=NOW + 30)]
        _, status, _, _ = self.run_migration(FakeDocker(deployment), now=NOW + 120)
        self.assertEqual(status, "absent")

    def test_a_newer_unsuccessful_attempt_supersedes_recorded_evidence(self):
        # completed -> a newer attempt that did not complete -> its container removed,
        # all with the same Controller container: never `completed` again.
        later = {
            "failed": container(
                "migrate", status="exited", exit_code=1, created=NOW - 60, finished=iso(NOW - 30)
            ),
            "stuck": container(
                "migrate", status="created", started=NEVER, created=NOW - 3000, running=False
            ),
            "in_progress": container("migrate", status="running", created=NOW - 60),
            "stale": self.completed(created=NOW - 86400),
        }
        for status, attempt in later.items():
            with self.subTest(status):
                for path in self.state.iterdir():
                    path.unlink()
                deployment = self.deployment([self.completed()])
                controller = deployment["controller"]
                _, found, _, out = self.run_migration(FakeDocker(deployment))
                self.assertEqual(found, "completed", out)
                deployment = {"controller": controller, "migrate": [attempt], "postgres": []}
                _, found, _, out = self.run_migration(FakeDocker(deployment), now=NOW + 60)
                self.assertEqual(found, status, out)
                deployment["migrate"] = []  # the attempt's container is then removed
                _, found, samples, out = self.run_migration(FakeDocker(deployment), now=NOW + 120)
                self.assertEqual(found, "absent", out)
                self.assertEqual(value(samples, "nest_migration_evidence_recorded"), 0)

    def test_unreadable_docker_keeps_recorded_evidence(self):
        deployment = self.deployment([self.completed()])
        self.run_migration(FakeDocker(deployment))
        self.run_migration(FakeDocker(broken=True), now=NOW + 60)  # nothing observed
        deployment["migrate"] = []
        _, found, _, out = self.run_migration(FakeDocker(deployment), now=NOW + 120)
        self.assertEqual(found, "completed", out)

    def test_a_restarted_migrate_container_is_judged_by_when_it_ran(self):
        # A deploy that changes only Controller settings recreates the Controller and
        # starts the existing migrate container again: its creation time is old.
        restarted = container(
            "migrate",
            status="exited",
            created=NOW - 86400,
            started=iso(NOW - 1795),
            finished=iso(NOW - 1780),
            name="app-migrate-1",
        )
        deployment = self.deployment([restarted], controller_created=NOW - 1800)
        _, status, samples, out = self.run_migration(FakeDocker(deployment))
        self.assertEqual(status, "completed", out)
        self.assertEqual(value(samples, "nest_migration_finished_timestamp_seconds"), NOW - 1780)
        # The same old container, not run again since the earlier deploy, is stale.
        old = container(
            "migrate",
            status="exited",
            created=NOW - 86400,
            started=iso(NOW - 86395),
            finished=iso(NOW - 86380),
            name="app-migrate-1",
        )
        for path in self.state.iterdir():
            path.unlink()
        deployment = self.deployment([old], controller_created=NOW - 1800)
        _, status, _, out = self.run_migration(FakeDocker(deployment))
        self.assertEqual(status, "stale", out)

    def test_evidence_is_not_recorded_from_a_stale_container(self):
        deployment = self.deployment([self.completed(created=NOW - 86400)])
        self.run_migration(FakeDocker(deployment))
        deployment["migrate"] = []
        _, status, _, _ = self.run_migration(FakeDocker(deployment))
        self.assertEqual(status, "absent")


def verifier_output(*checks, summary=None):
    lines = []
    failed = 0
    for name, passed, extra in checks:
        failed += not passed
        lines.append(json.dumps({"check": name, "passed": passed, **extra}))
    lines.append(json.dumps(summary or {"summary": "fail" if failed else "pass", "failed": failed}))
    return "\n".join(lines) + "\n"


PASSING = (
    ("tls", True, {"alpn": "h2", "version": "TLSv1.3", "days_left": 62}),
    ("tls_wrong_name_refused", True, {}),
    ("tls_http1_only_not_h2", True, {}),
    ("unauthenticated_refused", True, {}),
    ("non_nest_path_refused", True, {}),
    ("origin_unreachable_10.0.0.5:50051", True, {}),
)


class ProbeTests(_Dirs):
    def setUp(self):
        super().setUp()
        self.ca = self.state / "ca.pem"
        self.ca.write_text("-----BEGIN CERTIFICATE-----\n")
        self.calls = []

    def runner(self, code, stdout):
        def run(argv, timeout, env):
            self.calls.append((list(argv), env))
            return code, stdout

        return run

    def probe(self, runner, *extra, now=NOW, environ=None):
        argv = ["probe", "grpc.staging.example.org:443", "--ca-file", str(self.ca), *extra]
        return self.run_monitor(argv, runner=runner, now=now, environ=environ)

    def test_a_passing_probe(self):
        environ = {"PATH": "/usr/bin", "NEST_PROFILE": "hosted", "NEST_SESSION_TOKEN": SECRET}
        code, out = self.probe(
            self.runner(0, verifier_output(*PASSING)), "--origin", "10.0.0.5:50051", environ=environ
        )
        self.assertEqual(code, 0, out)
        argv, env = self.calls[0]
        self.assertEqual(
            argv[-6:],
            ["--ca-file", str(self.ca), "--origin", "10.0.0.5:50051", "--timeout", "15.0"],
        )
        self.assertNotIn("--signed-in", argv)
        self.assertNotIn("--execute", argv)
        self.assertEqual(env, {"PATH": "/usr/bin"})  # no NEST_ setting reaches the verifier
        samples = self.samples("probe")
        self.assertEqual(value(samples, "nest_public_probe_success"), 1)
        self.assertEqual(value(samples, "nest_public_tls_certificate_days_left"), 62)
        self.assertEqual(value(samples, "nest_public_probe_last_success_timestamp_seconds"), NOW)
        self.assertEqual(
            value(samples, "nest_public_probe_check_success", check="origin_unreachable"), 1
        )
        self.assertNotIn("10.0.0.5", self.everything_written())

    def test_a_failing_probe_keeps_the_last_success(self):
        self.probe(self.runner(0, verifier_output(*PASSING)))
        failing = (("tls", False, {"error": "SSLCertVerificationError: expired"}), *PASSING[1:])
        code, out = self.probe(self.runner(1, verifier_output(*failing)), now=NOW + 86400)
        self.assertEqual(code, 0, out)  # the probe ran; its result is the signal
        self.assertIn("FAIL tls", out)
        samples = self.samples("probe")
        self.assertEqual(value(samples, "nest_public_probe_success"), 0)
        self.assertEqual(value(samples, "nest_public_probe_check_success", check="tls"), 0)
        self.assertEqual(value(samples, "nest_public_probe_last_success_timestamp_seconds"), NOW)
        self.assertNotIn(("nest_public_tls_certificate_days_left", frozenset()), samples)
        self.assertNotIn("expired", self.everything_written())

    def test_a_verifier_that_cannot_run_fails_the_collector(self):
        def broken(argv, timeout, env):
            raise doctor.DockerError("the verifier did not finish in 120 s")

        for runner in (broken, self.runner(1, "Traceback (most recent call last):\n")):
            with self.subTest(runner=runner):
                code, out = self.probe(runner)
                self.assertEqual(code, 1, out)
                samples = self.samples("probe")
                self.assertEqual(value(samples, "nest_public_probe_success"), 0)
                self.assertEqual(
                    value(samples, "nest_monitor_collector_success", collector="probe"), 0
                )
                self.assertEqual(
                    value(samples, "nest_public_probe_last_success_timestamp_seconds"), 0
                )

    def test_credentials_and_execution_are_refused(self):
        for extra in (["--signed-in"], ["--execute"], ["--execute=1"]):
            with self.subTest(extra=extra):
                code, _ = self.probe(self.runner(0, ""), *extra)
                self.assertEqual(code, 2)
        self.assertEqual(self.calls, [])

    def test_roots_must_be_chosen_explicitly(self):
        with self.assertRaises(SystemExit):
            self.run_monitor(["probe", "grpc.staging.example.org:443"])
        code, _ = self.run_monitor(
            ["probe", "grpc.staging.example.org:443", "--ca-file", str(self.state / "missing.pem")]
        )
        self.assertEqual(code, 2)
        code, _ = self.run_monitor(
            ["probe", "grpc.staging.example.org:443", "--system-roots"],
            runner=self.runner(0, verifier_output(*PASSING)),
        )
        self.assertEqual(code, 0)
        self.assertNotIn("--ca-file", self.calls[-1][0])

    def test_the_address_must_be_host_and_port(self):
        for bad in ("grpc.example.org", "grpc.example.org:443 --execute", "a/b:1", "h:"):
            code, _ = self.run_monitor(["probe", bad, "--system-roots"], runner=self.runner(0, ""))
            self.assertEqual(code, 2, bad)
        self.assertEqual(self.calls, [])


def _all_metric_names():
    """Every series name the collectors write, from one synthetic run of each."""
    names = set()
    gateway, controller = container("grpc-gateway"), container("controller")
    docker = doctor.Docker(
        run=FakeDocker({"grpc-gateway": [gateway], "controller": [controller], "migrate": []})
    )
    with tempfile.TemporaryDirectory() as directory:
        args = SimpleNamespace(
            textfile_dir=directory,
            state_dir=directory,
            project="app",
            max_lines=10,
            min_age=300,
            stale_tolerance=600,
            runner="python",
            verifier="v",
            address="h:1",
            ca_file=None,
            origin=[],
            check_timeout=1,
            probe_timeout=1,
        )
        outcomes = [
            monitor.collect_probe(args, {}, lambda *a: (0, verifier_output(*PASSING)), NOW, {}),
            monitor.collect_logs("gateway", args, docker, {}, FakeLogs(), NOW),
            monitor.collect_logs("controller", args, docker, {}, FakeLogs(), NOW),
            monitor.collect_migration(args, docker, {}),
        ]
        with redirect_stdout(io.StringIO()):
            for name, outcome in zip(("probe", "gateway", "controller", "migration"), outcomes):
                monitor.finish(name, outcome, args, {}, NOW)
                names |= outcome.metrics.names()
    return names


class AlertRuleTests(unittest.TestCase):
    def test_committed_files_are_rendered_from_the_definitions(self):
        committed = (ROOT / "deploy/monitoring/prometheus/nest-alerts.rules.yml").read_text()
        self.assertEqual(committed, alerts.prometheus())
        example = json.loads(
            (ROOT / "deploy/monitoring/grafana/nest-alerts.example.json").read_text()
        )
        self.assertEqual(example, alerts.grafana("PROMETHEUS_DATASOURCE_UID", "nest-pilot"))

    def test_rules_read_only_series_the_collectors_write(self):
        written = _all_metric_names()
        for rule in alerts.RULES:
            used = set(re.findall(r"\b(nest_[a-z_]+)\b", rule.expr))
            self.assertTrue(used, rule.name)
            self.assertLessEqual(used, written, rule.name)
            self.assertNotIn("vllm", rule.expr)
            self.assertNotIn("{{", rule.summary + rule.description)

    def test_every_selector_is_limited_to_the_nest_job(self):
        grafana = alerts.grafana("u", "c")["groups"][0]["rules"]
        expressions = [alerts.scoped(r.expr) for r in alerts.RULES]
        expressions += [r["data"][0]["model"]["expr"] for r in grafana]
        for expr in expressions:
            selectors = re.findall(r"nest_[a-z_]+(\{[^}]*\}|)", expr)
            self.assertTrue(selectors, expr)
            for found in selectors:
                self.assertTrue(found.startswith('{job="nest"'), expr)
        self.assertIn(alerts.scoped(alerts.RULES[0].expr), alerts.prometheus().replace('\\"', '"'))
        self.assertIn('job=\\"monitoring-nest\\"', alerts.prometheus("monitoring-nest"))
        for bad in ("", 'a"b', "a}b", "a b"):
            with self.assertRaises(ValueError):
                alerts.scoped("nest_x", bad)

    def test_grafana_rules_route_only_to_the_named_contact_point(self):
        provisioned = alerts.grafana("prom-uid", "nest-pilot")
        text = json.dumps(provisioned)
        for forbidden in ("policies", "contactPoints", "receivers", "deleteRules", "$"):
            self.assertNotIn(forbidden, text)
        rules = provisioned["groups"][0]["rules"]
        self.assertEqual(len(rules), len(alerts.RULES))
        self.assertEqual(len({r["uid"] for r in rules}), len(rules))
        for rule in rules:
            self.assertLessEqual(len(rule["uid"]), 40)
            self.assertEqual(rule["notification_settings"], {"receiver": "nest-pilot"})
            self.assertEqual(rule["data"][0]["datasourceUid"], "prom-uid")
            self.assertTrue(rule["data"][0]["model"]["expr"].endswith(" or vector(0)"))
        for bad_uid, contact in (
            ("", "x"),
            ("a b", "x"),
            ("u", ""),
            ("u", "${GF_SECRET}"),
            ("u", "a\nb"),
        ):
            with self.assertRaises(ValueError):
                alerts.grafana(bad_uid, contact)


PROMTOOL = os.environ.get("NEST_TEST_PROMTOOL", "").strip() or shutil.which("promtool")
PROMTOOL_REQUIRED = os.environ.get("NEST_TEST_PROMTOOL_REQUIRED", "").strip() == "1"


def _series(name, values, job="nest", **labels):
    """One synthetic series, scraped by the `nest` job unless `job` says otherwise."""
    text = ",".join(f'{k}="{v}"' for k, v in {"job": job, **labels}.items())
    return {"series": f"{name}{{{text}}}", "values": values}


def _twice(series):
    """`series` plus the copy the existing node_exporter job scrapes from the same
    textfile directory."""
    copies = [
        {**s, "series": s["series"].replace('job="nest"', 'job="node_exporter"')} for s in series
    ]
    return [*series, *copies]


def _alert(name, eval_time, firing, **labels):
    rule = next(r for r in alerts.RULES if r.name == name)
    expected = []
    if firing:
        expected = [
            {
                "exp_labels": {**alerts._labels(rule), **labels},
                "exp_annotations": alerts._annotations(rule),
            }
        ]
    return {"eval_time": eval_time, "alertname": name, "exp_alerts": expected}


def _grafana_query(name, eval_time, expected):
    rule = next(r for r in alerts.RULES if r.name == name)
    return {
        "expr": f"count({alerts.scoped(rule.expr)}) or vector(0)",
        "eval_time": eval_time,
        "exp_samples": [{"labels": "{}", "value": expected}],
    }


def _test(series, alerts_=(), queries=()):
    return {
        "interval": "1m",
        "input_series": series,
        "alert_rule_test": list(alerts_),
        "promql_expr_test": list(queries),
    }


def _migration(status, deployed):
    return [
        _series("nest_migration_status", "1x60" if s == status else "0x60", status=s)
        for s in monitor.MIGRATION_STATUSES
    ] + [_series("nest_migration_deploy_timestamp_seconds", f"{deployed}x60")]


def promtool_cases():
    """Normal and failure transitions of every rule, on synthetic series. Times
    start at 0, so a timestamp series `0+60x...` is "now"."""
    probe_ok = [
        _series("nest_public_probe_success", "1x200"),
        _series("nest_public_probe_last_success_timestamp_seconds", "0+60x200"),
        _series("nest_public_tls_certificate_days_left", "62x200"),
    ]
    collectors = [
        _series("nest_monitor_last_run_timestamp_seconds", "0+60x60", collector=c)
        for c in ("probe", "gateway", "controller", "migration")
    ] + [
        _series("nest_monitor_last_success_timestamp_seconds", "0+60x60", collector=c)
        for c in ("gateway", "controller", "migration")
    ]
    req = "nest_gateway_requests_total"
    exhausted = "nest_gateway_resource_exhausted_total"
    flags = "nest_gateway_response_flags_total"
    return [
        _test(
            probe_ok,
            [
                _alert("NestPublicProbeFailed", "2h", False),
                _alert("NestPublicProbeStale", "2h", False),
                _alert("NestCertificateExpiringSoon", "2h", False),
            ],
            [_grafana_query("NestPublicProbeFailed", "2h", 0)],
        ),
        _test(
            [_series("nest_public_probe_success", "1x10 0x30")],
            [
                _alert("NestPublicProbeFailed", "5m", False),
                _alert("NestPublicProbeFailed", "20m", True),
            ],
            [_grafana_query("NestPublicProbeFailed", "20m", 1)],
        ),
        _test(
            [_series("nest_public_tls_certificate_days_left", "14x30")],
            [_alert("NestCertificateExpiringSoon", "20m", False)],
        ),
        _test(
            [_series("nest_public_tls_certificate_days_left", "13x30")],
            [_alert("NestCertificateExpiringSoon", "20m", True)],
        ),
        _test(
            [
                _series("nest_public_probe_success", "1x1700"),
                _series("nest_public_probe_last_success_timestamp_seconds", "0x1700"),
            ],
            [
                _alert("NestPublicProbeStale", "25h", False),
                _alert("NestPublicProbeStale", "27h", True),
            ],
        ),
        # 30% RESOURCE_EXHAUSTED at 10 requests a minute: fires after its 10 minutes.
        _test(
            [
                _series(req, "0+7x60", route="service", code="OK"),
                _series(req, "0+3x60", route="service", code="RESOURCE_EXHAUSTED"),
                _series(exhausted, "0+3x60", source="controller"),
                _series(exhausted, "0x60", source="gateway"),
            ],
            [
                _alert("NestGatewayResourceExhaustedHigh", "5m", False),
                _alert("NestGatewayResourceExhaustedHigh", "40m", True),
            ],
            [_grafana_query("NestGatewayResourceExhaustedHigh", "40m", 1)],
        ),
        # Every request rejected, but below the 20-request floor: no alert.
        _test(
            [
                _series(req, "0+1x60", route="service", code="RESOURCE_EXHAUSTED"),
                _series(exhausted, "0+1x60", source="gateway"),
            ],
            [_alert("NestGatewayResourceExhaustedHigh", "40m", False)],
        ),
        # One expected quota rejection in normal traffic: no alert.
        _test(
            [
                _series(req, "0+10x60", route="service", code="OK"),
                _series(exhausted, "0x20 1x40", source="controller"),
            ],
            [_alert("NestGatewayResourceExhaustedHigh", "30m", False)],
        ),
        # No traffic at all: the ratio is undefined, not an alert.
        _test(
            [
                _series(req, "0x60", route="service", code="OK"),
                _series(exhausted, "0x60", source="controller"),
            ],
            [_alert("NestGatewayResourceExhaustedHigh", "30m", False)],
            [_grafana_query("NestGatewayResourceExhaustedHigh", "30m", 0)],
        ),
        _test(
            [_series(flags, "0x20 5x40", flag="UH"), _series(flags, "0x60", flag="UF")],
            [
                _alert("NestGatewayUpstreamFailures", "15m", False),
                _alert("NestGatewayUpstreamFailures", "30m", True),
            ],
        ),
        _test(
            [_series(flags, "0x20 1x40", flag="UF"), _series(flags, "0x60", flag="UH")],
            [_alert("NestGatewayUpstreamFailures", "30m", False)],
        ),
        _test(
            [_series("nest_gateway_access_log_unparsed_total", "0x10 1x50")],
            [
                _alert("NestGatewayAccessLogUnparsed", "5m", False),
                _alert("NestGatewayAccessLogUnparsed", "20m", True),
            ],
        ),
        _test(
            [_series("nest_controller_rpc_refused_total", "0x10 1x50", reason="origin")],
            [
                _alert("NestControllerRpcRefused", "9m", False),
                _alert("NestControllerRpcRefused", "15m", True),
            ],
        ),
        _test(
            [_series("nest_controller_rpc_rate_limited_total", "0x10 1x50", scope="user")],
            [_alert("NestControllerRateLimitedSustained", "30m", False)],
        ),
        _test(
            [
                _series("nest_controller_rpc_rate_limited_total", "0+2x60", scope="address"),
                _series("nest_controller_rpc_rate_limited_total", "0x60", scope="user"),
            ],
            [_alert("NestControllerRateLimitedSustained", "40m", True)],
        ),
        _test(
            _migration("completed", 0),
            [_alert("NestMigrationNotCompleted", "50m", False)],
            [_grafana_query("NestMigrationNotCompleted", "50m", 0)],
        ),
        *(
            _test(_migration(status, 0), [_alert("NestMigrationNotCompleted", "30m", True)])
            for status in ("failed", "stuck", "in_progress", "absent", "stale", "unknown")
        ),
        # Deployed at 10 minutes: within the 15-minute grace at 20, alerting by 40.
        _test(
            _migration("absent", 600),
            [
                _alert("NestMigrationNotCompleted", "20m", False),
                _alert("NestMigrationNotCompleted", "40m", True),
            ],
        ),
        _test(
            collectors,
            [
                _alert("NestMonitorCollectorFailing", "50m", False),
                _alert("NestMonitorCollectorMissing", "50m", False),
            ],
            [_grafana_query("NestMonitorCollectorMissing", "50m", 0)],
        ),
        _test(
            [
                _series("nest_monitor_last_success_timestamp_seconds", "0x60", collector="gateway"),
                _series(
                    "nest_monitor_last_success_timestamp_seconds", "0+60x60", collector="controller"
                ),
            ],
            [
                _alert("NestMonitorCollectorFailing", "15m", False),
                _alert("NestMonitorCollectorFailing", "25m", True, collector="gateway"),
            ],
        ),
        _test(
            collectors[1:],
            [
                _alert("NestMonitorCollectorMissing", "20m", False),
                _alert("NestMonitorCollectorMissing", "40m", True),
            ],
        ),
        _test(
            [_series("nest_public_probe_success", "1x60")],
            [_alert("NestMonitorCollectorMissing", "40m", True, job="nest")],
            [_grafana_query("NestMonitorCollectorMissing", "40m", 1)],
        ),
        # The documented topology: node_exporter's own job scrapes the same files.
        # Counts and the missing-collector check still see one copy.
        _test(
            _twice(collectors),
            [
                _alert("NestMonitorCollectorMissing", "50m", False),
                _alert("NestMonitorCollectorFailing", "50m", False),
            ],
            [_grafana_query("NestMonitorCollectorMissing", "50m", 0)],
        ),
        _test(
            _twice(collectors[1:]),
            [_alert("NestMonitorCollectorMissing", "40m", True)],
            [_grafana_query("NestMonitorCollectorMissing", "40m", 1)],
        ),
        _test(
            _twice([_series(flags, "0x20 2x40", flag="UH"), _series(flags, "0x60", flag="UF")]),
            [_alert("NestGatewayUpstreamFailures", "30m", False)],
        ),
        _test(
            _twice([_series(flags, "0x20 3x40", flag="UF"), _series(flags, "0x60", flag="UH")]),
            [_alert("NestGatewayUpstreamFailures", "30m", True)],
        ),
        _test(
            _twice([_series("nest_controller_rpc_rate_limited_total", "0+0.4x60", scope="user")]),
            [_alert("NestControllerRateLimitedSustained", "40m", False)],
        ),
        _test(
            _twice(
                [
                    _series(req, "0+0.8x60", route="service", code="RESOURCE_EXHAUSTED"),
                    _series(exhausted, "0+0.8x60", source="controller"),
                ]
            ),
            [_alert("NestGatewayResourceExhaustedHigh", "40m", False)],
        ),
    ]


@unittest.skipUnless(
    PROMTOOL or PROMTOOL_REQUIRED,
    "set NEST_TEST_PROMTOOL to a promtool binary to evaluate the rules",
)
class PromtoolTests(unittest.TestCase):
    def setUp(self):
        if not PROMTOOL:
            self.fail("NEST_TEST_PROMTOOL_REQUIRED=1 but no promtool binary was found")
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        shutil.copy(ROOT / "deploy/monitoring/prometheus/nest-alerts.rules.yml", self.work)

    def promtool(self, *argv):
        return subprocess.run(
            [str(PROMTOOL), *argv],
            cwd=self.work,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def test_rules_are_valid(self):
        result = self.promtool("check", "rules", "nest-alerts.rules.yml")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_grafana_queries_are_valid_promql(self):
        records = [
            {"record": f"nest:grafana_query_{i}", "expr": f"count({r.expr}) or vector(0)"}
            for i, r in enumerate(alerts.RULES)
        ]
        (self.work / "grafana.rules.yml").write_text(
            json.dumps({"groups": [{"name": "g", "rules": records}]})
        )
        result = self.promtool("check", "rules", "grafana.rules.yml")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_alerts_fire_and_stay_quiet_as_designed(self):
        tests = {
            "rule_files": ["nest-alerts.rules.yml"],
            "evaluation_interval": "1m",
            "tests": promtool_cases(),
        }
        (self.work / "nest-alerts.test.yml").write_text(json.dumps(tests, indent=1))
        result = self.promtool("test", "rules", "nest-alerts.test.yml")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class DeploymentFileTests(unittest.TestCase):
    def test_host_scripts_parse_as_python_3_9(self):
        # They run with the Docker host's Python, not Nest's (pyproject.toml sets
        # ruff's per-file target accordingly).
        import ast

        for path in (MONITOR, ALERTS, ROOT / "scripts" / "nest-deploy-doctor.py"):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))

    def test_units_use_no_secret_and_least_privilege(self):
        units = ROOT / "deploy/monitoring/systemd"
        collector = (units / "nest-monitor.service").read_text()
        probe = (units / "nest-monitor-probe.service").read_text()
        self.assertIn("SupplementaryGroups=docker nest-textfile", collector)
        self.assertNotIn("docker", probe.split("[Service]")[1].replace("Docker access", ""))
        for unit in (collector, probe):
            self.assertIn("NoNewPrivileges=yes", unit)
            self.assertIn("ProtectSystem=strict", unit)
            self.assertNotIn("--signed-in", unit)
            self.assertNotIn("--execute", unit)
        self.assertIn("--ca-file", probe)
        for path in units.iterdir():
            self.assertNotRegex(path.read_text(), r"(?i)(token|password|secret)\s*=")

    def test_scrape_example_is_a_separate_job(self):
        example = (ROOT / "deploy/monitoring/prometheus/nest-scrape.yml.example").read_text()
        self.assertIn("- job_name: nest", example)
        self.assertNotIn("file_sd_configs", example)
        self.assertIn('regex: "nest_.*', example)

    def test_the_gateway_admin_stays_on_loopback(self):
        template = TEMPLATE.read_text()
        admin = re.search(
            r"admin:\s*\n\s*address:\s*\n\s*socket_address: \{ address: ([^,]+),", template
        )
        assert admin is not None
        self.assertEqual(admin.group(1), "127.0.0.1")
        compose = (ROOT / "compose.dokploy.yaml").read_text()
        gateway = compose.split("  grpc-gateway:")[1].split("\n  grpc-certificates:")[0]
        self.assertNotIn("ports:", gateway)


if __name__ == "__main__":
    unittest.main()
