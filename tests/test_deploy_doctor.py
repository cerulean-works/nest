"""Tests for scripts/nest-deploy-doctor.py and the Traefik passthrough renderer.

The doctor is checked against recorded `docker` answers (the states seen in the
2026-11-26 Dokploy incident and in tests/repro_migrate_stall.sh), and, when a Docker
daemon is available, against a real never-started container of a throwaway project.
"""

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "scripts" / "nest-deploy-doctor.py"
RENDER = ROOT / "deploy" / "public-grpc" / "traefik" / "render.sh"

# Inside the Controller image only tests/ is present (and sometimes scripts/).
if not DOCTOR.is_file():
    raise unittest.SkipTest("requires the repository's scripts/")
CHECKOUT = (ROOT / "compose.dokploy.yaml").is_file() and RENDER.is_file()

spec = importlib.util.spec_from_file_location("nest_deploy_doctor", DOCTOR)
assert spec is not None and spec.loader is not None
doctor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = doctor  # dataclasses look their module up
spec.loader.exec_module(doctor)

NEVER = "0001-01-01T00:00:00Z"


def container(
    name,
    status,
    *,
    project="nest",
    service="migrate",
    pid=0,
    exit_code=0,
    started=NEVER,
    finished=NEVER,
    created="2026-11-26T08:00:00Z",
    health=None,
):
    state = {
        "Status": status,
        "Pid": pid,
        "ExitCode": exit_code,
        "StartedAt": started,
        "FinishedAt": finished,
    }
    if health is not None:
        state["Health"] = {"Status": health}
    return {
        "Id": uuid.uuid4().hex + uuid.uuid4().hex,
        "Name": "/" + name,
        "Created": created,
        "State": state,
        "Config": {
            "Image": "nest-controller:local",
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
            },
        },
    }


def doctor_now_iso(offset):
    """A Docker timestamp `offset` seconds from now."""
    import datetime

    moment = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=offset)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def network(name, subnet, bridge=None):
    return {
        "Name": name,
        "Id": uuid.uuid4().hex,
        "IPAM": {"Config": [{"Subnet": subnet}] if subnet else []},
        "Options": {"com.docker.network.bridge.name": bridge} if bridge else {},
    }


class FakeDocker:
    """Answers the docker commands the doctor runs, and records them."""

    def __init__(self, containers=(), networks=(), routes=None, traefik=None, logs=None):
        self.containers = {c["Id"]: c for c in containers}
        # Container ID -> its full log text, or None when Docker cannot read it.
        self.logs = logs or {}
        self.networks = list(networks)
        self.routes = routes
        self.traefik = traefik
        self.calls = []

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        R = doctor.Result
        if argv[:3] == ["docker", "network", "ls"]:
            return R(0, "\n".join(n["Id"] for n in self.networks))
        if argv[:3] == ["docker", "network", "inspect"]:
            return R(0, json.dumps(self.networks))
        if argv[:2] == ["docker", "ps"]:
            labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--filter"]
            wanted = dict(label.removeprefix("label=").split("=", 1) for label in labels)
            found = [
                c["Id"]
                for c in self.containers.values()
                if all(c["Config"]["Labels"].get(k) == v for k, v in wanted.items())
            ]
            return R(0, "\n".join(found))
        if argv[:3] == ["docker", "container", "inspect"]:
            found = []
            for key in argv[3:]:
                if key == "dokploy-traefik" and self.traefik is not None:
                    found.append(self.traefik)
                elif key in self.containers:
                    found.append(self.containers[key])
            if not found:
                return R(1, "", "Error: No such container")
            return R(0, json.dumps(found))
        if argv[:2] == ["docker", "logs"]:
            target = argv[-1]
            if target in self.logs:
                text = self.logs[target]
                if text is None:
                    return R(1, "", "Error response from daemon: can not get logs")
                return R(0, text, "")
            return R(0, "line 1\nline 2\nline 3\n")
        if argv[:2] == ["docker", "rm"]:
            target = self.containers.get(argv[-1])
            if target is None or target["State"]["Status"] == "running":
                return R(1, "", "cannot remove")
            del self.containers[argv[-1]]
            return R(0, argv[-1])
        if argv[:1] == ["ip"]:
            if self.routes is None:
                return R(1, "", "no ip")
            return R(0, json.dumps(self.routes))
        raise AssertionError(f"unexpected command {argv}")


def run(argv, fake, environ=None):
    out = io.StringIO()
    with redirect_stdout(out):
        code = doctor.main(argv, docker=doctor.Docker(run=fake), environ=environ or {})
    return code, out.getvalue()


def incident_containers():
    """The 2026-11-26 state: the previous migrate run completed, and an interrupted
    recreate left its replacement Created under Compose's temporary name."""
    done = container(
        "nest-migrate-1",
        "exited",
        started="2026-11-25T10:00:01Z",
        created="2026-11-25T10:00:00Z",
    )
    stuck = container(done["Id"][:12] + "_nest-migrate-1", "created")
    return done, stuck


class MigrateStatusTests(unittest.TestCase):
    def test_incident_state_is_stuck_and_names_the_recovery(self):
        done, stuck = incident_containers()
        code, out = run(["migrate-status"], FakeDocker([done, stuck]))
        self.assertEqual(code, 3)
        self.assertIn("stuck-created", out)
        self.assertIn("replacement prefix", out)
        self.assertIn("recover-migrate --project nest", out)

    def test_completed_failed_running_and_missing(self):
        completed = container("nest-migrate-1", "exited", started="2026-11-26T08:00:01Z")
        self.assertEqual(run(["migrate-status"], FakeDocker([completed]))[0], 0)
        failed = container("nest-migrate-1", "exited", exit_code=1, started="2026-11-26T08:00:01Z")
        code, out = run(["migrate-status", "--logs", "2"], FakeDocker([failed]))
        self.assertEqual(code, 4)
        self.assertIn("the Controller will not start", out)
        self.assertIn("| line 3", out)
        self.assertNotIn("| line 1", out)
        running = container("nest-migrate-1", "running", pid=42, started="2026-11-26T08:00:01Z")
        self.assertEqual(run(["migrate-status"], FakeDocker([running]))[0], 5)
        self.assertEqual(run(["migrate-status"], FakeDocker([]))[0], 6)

    def test_logs_are_bounded(self):
        failed = container("nest-migrate-1", "exited", exit_code=1, started="2026-11-26T08:00:01Z")
        fake = FakeDocker([failed])
        run(["migrate-status", "--logs", "100000"], fake)
        logs = [c for c in fake.calls if c[:2] == ["docker", "logs"]]
        self.assertEqual(logs[0][3], str(doctor.MAX_LOG_LINES))

    def test_only_this_projects_migrate_containers_are_listed(self):
        other = container("other-migrate-1", "created", project="other")
        controller = container("nest-controller-1", "created", service="controller")
        code, out = run(["migrate-status", "--project", "nest"], FakeDocker([other, controller]))
        self.assertEqual(code, 6)
        self.assertNotIn("other-migrate-1", out)

    def test_json_output(self):
        done, stuck = incident_containers()
        code, out = run(["migrate-status", "--json"], FakeDocker([done, stuck]))
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["state"] for r in rows], ["completed", "stuck-created"])
        self.assertTrue(rows[1]["replacement"])
        self.assertEqual(code, 3)


class RecoverMigrateTests(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        done, stuck = incident_containers()
        fake = FakeDocker([done, stuck])
        code, out = run(["recover-migrate", "--settle", "0"], fake)
        self.assertEqual(code, 0)
        self.assertIn(f"would run: docker rm {stuck['Id'][:12]}", out)
        self.assertFalse([c for c in fake.calls if c[:2] == ["docker", "rm"]])
        self.assertEqual(len(fake.containers), 2)

    def test_apply_removes_only_the_never_started_container(self):
        done, stuck = incident_containers()
        running = container("nest-controller-1", "running", service="controller", pid=7)
        fake = FakeDocker([done, stuck, running])
        code, out = run(["recover-migrate", "--apply", "--settle", "0"], fake)
        self.assertEqual(code, 0)
        removals = [c for c in fake.calls if c[:2] == ["docker", "rm"]]
        # Never forced and never with volumes.
        self.assertEqual(removals, [["docker", "rm", stuck["Id"]]])
        self.assertEqual(set(fake.containers), {done["Id"], running["Id"]})
        self.assertIn("Deploy again", out)

    def test_apply_refuses_completed_failed_and_running_containers(self):
        for state in (
            container("nest-migrate-1", "exited", started="2026-11-26T08:00:01Z"),
            container("nest-migrate-1", "exited", exit_code=3, started="2026-11-26T08:00:01Z"),
            container("nest-migrate-1", "running", pid=9, started="2026-11-26T08:00:01Z"),
        ):
            fake = FakeDocker([state])
            _, out = run(["recover-migrate", "--apply", "--settle", "0"], fake)
            self.assertIn("nothing to recover", out)
            self.assertFalse([c for c in fake.calls if c[:2] == ["docker", "rm"]])

    def test_apply_skips_a_container_that_started_since_it_was_listed(self):
        _, stuck = incident_containers()
        fake = FakeDocker([stuck])
        original = fake.__call__
        inspections = []

        def started_meanwhile(argv):
            if list(argv[:3]) == ["docker", "container", "inspect"]:
                inspections.append(argv)
                if len(inspections) == 2:  # the re-read just before acting
                    stuck["State"].update(
                        Status="running", Pid=11, StartedAt="2026-11-26T08:00:05Z"
                    )
            return original(argv)

        _, out = run(["recover-migrate", "--apply", "--settle", "0"], started_meanwhile)
        self.assertIn("changed since it was listed", out)
        self.assertIn(stuck["Id"], fake.containers)

    def test_never_removes_a_container_waiting_for_postgres(self):
        # A healthy deploy: Compose created migrate and waits for PostgreSQL's health
        # check before starting it, however long that takes.
        for health in ("starting", "unhealthy"):
            with self.subTest(health):
                waiting = container("nest-migrate-1", "created")
                postgres = container(
                    "nest-postgres-1", "running", service="postgres", pid=5, health=health
                )
                fake = FakeDocker([waiting, postgres])
                code, out = run(["migrate-status"], fake)
                self.assertEqual(code, 5, out)
                self.assertIn("waiting-dependency", out)
                self.assertIn(f"PostgreSQL is {health}", out)
                self.assertNotIn("Recover with", out)
                _, out = run(["recover-migrate", "--apply", "--settle", "0"], fake)
                self.assertIn("nothing to recover", out)
                self.assertFalse([c for c in fake.calls if c[:2] == ["docker", "rm"]])

    def test_never_removes_a_recently_created_container(self):
        young = container("nest-migrate-1", "created", created=doctor_now_iso(-30))
        fake = FakeDocker([young])
        code, out = run(["migrate-status"], fake)
        self.assertEqual(code, 5)
        self.assertIn("pending", out)
        _, out = run(["recover-migrate", "--apply", "--settle", "0"], fake)
        self.assertIn("nothing to recover", out)
        self.assertFalse([c for c in fake.calls if c[:2] == ["docker", "rm"]])

    def test_stuck_once_postgres_is_healthy_and_the_container_is_old(self):
        _, stuck = incident_containers()
        postgres = container(
            "nest-postgres-1", "running", service="postgres", pid=5, health="healthy"
        )
        fake = FakeDocker([stuck, postgres])
        code, out = run(["migrate-status"], fake)
        self.assertEqual(code, 3, out)
        self.assertIn("PostgreSQL is healthy", out)
        code, _ = run(["recover-migrate", "--apply", "--settle", "0"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(
            [c for c in fake.calls if c[:2] == ["docker", "rm"]], [["docker", "rm", stuck["Id"]]]
        )
        self.assertIn(postgres["Id"], fake.containers)

    def test_rechecks_after_settling_and_skips_a_container_postgres_released(self):
        _, stuck = incident_containers()
        postgres = container(
            "nest-postgres-1", "running", service="postgres", pid=5, health="healthy"
        )
        fake = FakeDocker([stuck, postgres])
        slept = []

        def settle(seconds):
            slept.append(seconds)
            stuck["State"].update(Status="running", Pid=12, StartedAt="2026-11-26T09:00:00Z")

        parsed = doctor.parser().parse_args(["recover-migrate", "--apply"])
        out = io.StringIO()
        with redirect_stdout(out):
            doctor.recover_migrate(parsed, doctor.Docker(run=fake), sleep=settle)
        self.assertEqual(slept, [doctor.DEFAULT_SETTLE])
        self.assertIn("changed since it was listed", out.getvalue())
        self.assertFalse([c for c in fake.calls if c[:2] == ["docker", "rm"]])


# The wall clock the wait tests run at: the deploy (and the wait) starts here.
DEPLOY_START = "2026-11-26T09:00:00Z"


class WaitMigrateTests(unittest.TestCase):
    def wait(self, fake, timeout=30.0, stuck_after=10.0, extra=()):
        now = [0.0]
        start = doctor.parse_time(DEPLOY_START)

        def sleep(seconds):
            now[0] += seconds

        parsed = doctor.parser().parse_args(
            ["wait-migrate", "--timeout", str(timeout), "--stuck-after", str(stuck_after), *extra]
        )
        out = io.StringIO()
        with redirect_stdout(out):
            code = doctor.wait_migrate(
                parsed,
                doctor.Docker(run=fake),
                clock=lambda: now[0],
                sleep=sleep,
                wall=lambda: start + now[0],
            )
        return code, now[0], out.getvalue()

    def test_stuck_container_is_reported_after_the_bound(self):
        code, elapsed, out = self.wait(FakeDocker(incident_containers()))
        self.assertEqual(code, 3)
        self.assertGreaterEqual(elapsed, 10.0)
        self.assertLess(elapsed, 30.0)
        self.assertIn("stuck:", out)

    def test_completed_failed_and_timeout(self):
        after = {"started": "2026-11-26T09:00:01Z", "finished": "2026-11-26T09:00:02.5Z"}
        done = container("nest-migrate-1", "exited", **after)
        self.assertEqual(self.wait(FakeDocker([done]))[0], 0)
        failed = container("nest-migrate-1", "exited", exit_code=1, **after)
        self.assertEqual(self.wait(FakeDocker([failed]))[0], 4)
        running = container("nest-migrate-1", "running", pid=3, started="2026-11-26T08:00:01Z")
        code, elapsed, _ = self.wait(FakeDocker([running]), timeout=12)
        self.assertEqual(code, 5)
        self.assertGreaterEqual(elapsed, 12)
        code, _, _ = self.wait(FakeDocker([]), timeout=4)
        self.assertEqual(code, 5)

    def test_a_previous_deploys_result_does_not_count(self):
        before = {"started": "2026-11-25T10:00:01Z", "finished": "2026-11-25T10:00:02Z"}
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                old = container("nest-migrate-1", "exited", exit_code=exit_code, **before)
                code, elapsed, out = self.wait(FakeDocker([old]), timeout=8)
                self.assertEqual(code, 5, out)
                self.assertGreaterEqual(elapsed, 8)
        # The same container started again by this deploy (Compose reuses an unchanged
        # one) counts once it finishes after the deploy started.
        old = container("nest-migrate-1", "exited", **before)
        fake = FakeDocker([old])
        original = fake.__call__
        polls = []

        def rerun(argv):
            if (
                list(argv[:2]) == ["docker", "ps"]
                and "com.docker.compose.service=migrate" in argv[-1]
            ):
                polls.append(argv)
                if len(polls) == 3:
                    old["State"].update(
                        StartedAt="2026-11-26T09:00:04Z", FinishedAt="2026-11-26T09:00:05Z"
                    )
            return original(argv)

        code, _, out = self.wait(rerun, timeout=60)
        self.assertEqual(code, 0, out)

    def test_since_names_the_deploy_start(self):
        result = {"started": "2026-11-26T08:59:30Z", "finished": "2026-11-26T08:59:40Z"}
        done = container("nest-migrate-1", "exited", **result)
        code, _, _ = self.wait(FakeDocker([done]), extra=("--since", "2026-11-26T08:59:00Z"))
        self.assertEqual(code, 0)
        code, _, _ = self.wait(FakeDocker([done]), timeout=4)
        self.assertEqual(code, 5)
        with self.assertRaises(ValueError):
            self.wait(FakeDocker([done]), extra=("--since", "yesterday"))

    def test_waiting_for_postgres_is_not_stuck(self):
        waiting = container("nest-migrate-1", "created")
        postgres = container(
            "nest-postgres-1", "running", service="postgres", pid=5, health="starting"
        )
        code, _, out = self.wait(FakeDocker([waiting, postgres]), timeout=120, stuck_after=10)
        self.assertEqual(code, 5, out)
        self.assertNotIn("stuck", out)

    def test_docker_timestamps(self):
        self.assertIsNone(doctor.parse_time(NEVER))
        self.assertIsNone(doctor.parse_time(""))
        nanos = doctor.parse_time("2026-11-26T09:30:08.027740823Z")
        self.assertAlmostEqual(
            nanos, doctor.parse_time("2026-11-26T09:30:08Z") + 0.0277408, places=5
        )
        self.assertEqual(
            doctor.parse_time("2026-11-26T10:30:08+01:00"),
            doctor.parse_time("2026-11-26T09:30:08Z"),
        )


class PreflightTests(unittest.TestCase):
    def preflight(self, environ, networks=(), routes=None, traefik=None, project="nest"):
        fake = FakeDocker(networks=networks, routes=routes, traefik=traefik)
        code, out = run(["preflight", "--project", project], fake, environ)
        self.assertFalse(
            [
                c
                for c in fake.calls
                if c[:2] in (["docker", "rm"], ["docker", "network"])
                and c[2] not in ("ls", "inspect")
            ],
            "preflight must not change anything",
        )
        return code, {line.split()[1].rstrip(":"): line for line in out.splitlines()}

    def test_incident_overlap_with_the_default_subnet_fails(self):
        code, checks = self.preflight(
            {}, networks=[network("bridge", "172.17.0.0/16"), network("app_x", "172.31.0.0/16")]
        )
        self.assertEqual(code, 1)
        self.assertTrue(checks["origin_subnet_free"].startswith("FAIL"))
        self.assertIn("app_x (172.31.0.0/16)", checks["origin_subnet_free"])

    def test_operator_override_passes_and_own_network_is_not_a_collision(self):
        env = {
            "NEST_GRPC_ORIGIN_SUBNET": "10.253.250.0/28",
            "NEST_GRPC_GATEWAY_ADDRESS": "10.253.250.2",
        }
        own = network("nest_grpc-origin", "10.253.250.0/28", bridge="br-own")
        routes = [
            {"dst": "default", "dev": "eth0"},
            {"dst": "10.253.250.0/28", "dev": "br-own"},
            {"dst": "172.31.0.0/16", "dev": "br-other"},
        ]
        code, checks = self.preflight(
            env, networks=[own, network("app_x", "172.31.0.0/16")], routes=routes
        )
        self.assertEqual(code, 0, checks)
        self.assertTrue(checks["origin_network"].startswith("PASS"))
        self.assertTrue(checks["origin_routes"].startswith("PASS"))
        self.assertTrue(checks["public_grpc"].startswith("SKIP"))

    def test_existing_origin_network_with_another_subnet_is_a_warning(self):
        env = {
            "NEST_GRPC_ORIGIN_SUBNET": "10.253.250.0/28",
            "NEST_GRPC_GATEWAY_ADDRESS": "10.253.250.2",
        }
        own = network("nest_grpc-origin", "10.253.0.0/16")
        code, checks = self.preflight(env, networks=[own])
        self.assertEqual(code, 0)
        self.assertIn("recreate", checks["origin_network"])

    def test_host_route_overlap_fails(self):
        env = {
            "NEST_GRPC_ORIGIN_SUBNET": "10.253.250.0/28",
            "NEST_GRPC_GATEWAY_ADDRESS": "10.253.250.2",
        }
        code, checks = self.preflight(env, routes=[{"dst": "10.253.0.0/16", "dev": "wg0"}])
        self.assertEqual(code, 1)
        self.assertIn("10.253.0.0/16 dev wg0", checks["origin_routes"])

    def test_missing_route_tool_is_skipped_not_passed(self):
        _, checks = self.preflight({})
        self.assertTrue(checks["origin_routes"].startswith("SKIP"))

    def test_invalid_subnets_and_gateway_addresses(self):
        cases = {
            "not a network": ({"NEST_GRPC_ORIGIN_SUBNET": "10.0.0.0/33"}, "origin_subnet"),
            "host bits set": ({"NEST_GRPC_ORIGIN_SUBNET": "10.9.9.9/28"}, "origin_subnet"),
            "too small": (
                {"NEST_GRPC_ORIGIN_SUBNET": "10.9.9.0/30", "NEST_GRPC_GATEWAY_ADDRESS": "10.9.9.2"},
                "origin_subnet",
            ),
            "outside": ({"NEST_GRPC_ORIGIN_SUBNET": "10.9.9.0/28"}, "gateway_address"),
            "bridge gateway": (
                {"NEST_GRPC_ORIGIN_SUBNET": "10.9.9.0/28", "NEST_GRPC_GATEWAY_ADDRESS": "10.9.9.1"},
                "gateway_address",
            ),
            "broadcast": (
                {
                    "NEST_GRPC_ORIGIN_SUBNET": "10.9.9.0/28",
                    "NEST_GRPC_GATEWAY_ADDRESS": "10.9.9.15",
                },
                "gateway_address",
            ),
        }
        for label, (env, check) in cases.items():
            with self.subTest(label):
                code, checks = self.preflight(env)
                self.assertEqual(code, 1)
                self.assertTrue(checks[check].startswith("FAIL"), checks)

    def gateway_env(self, **changes):
        env = {
            "COMPOSE_PROFILES": "local-db,migrate,public-grpc",
            "NEST_PUBLIC_PILOT": "true",
            "NEST_GRPC_ORIGIN_SUBNET": "10.253.250.0/28",
            "NEST_GRPC_GATEWAY_ADDRESS": "10.253.250.2",
            "NEST_GRPC_TRUSTED_PROXIES": "10.253.250.2/32",
            "NEST_GRPC_ALLOWED_PEERS": "10.253.250.0/28,172.18.0.0/16",
            "NEST_GRPC_EDGE_PROXY": "10.0.1.7/32",
        }
        env.update(changes)
        return env

    def traefik(self, image="traefik:v3.3.7", address="10.0.1.7"):
        return {
            "Config": {"Image": image},
            "NetworkSettings": {"Networks": {"dokploy-network": {"IPAddress": address}}},
        }

    def gateway_networks(self):
        return [network("nest_tailnet", "172.18.0.0/16"), network("dokploy-network", "10.0.1.0/24")]

    def test_consistent_gateway_settings_pass(self):
        code, checks = self.preflight(
            self.gateway_env(), networks=self.gateway_networks(), traefik=self.traefik()
        )
        self.assertEqual(code, 0, checks)
        for name in (
            "public_pilot",
            "trusted_proxies",
            "allowed_peers",
            "edge_proxy",
            "traefik_version",
        ):
            self.assertTrue(checks[name].startswith("PASS"), checks[name])
        self.assertIn("nest-grpc.yml", checks["traefik_version"])

    def test_gateway_mismatches_fail_closed(self):
        cases = {
            "stale default trusted proxy": (
                {"NEST_GRPC_TRUSTED_PROXIES": "172.31.250.2/32"},
                "trusted_proxies",
            ),
            "trusted proxy wider than the gateway": (
                {"NEST_GRPC_TRUSTED_PROXIES": "10.253.250.0/28"},
                "trusted_proxies",
            ),
            "no Tailnet bridge": ({"NEST_GRPC_ALLOWED_PEERS": "10.253.250.0/28"}, "allowed_peers"),
            "gateway not admitted": ({"NEST_GRPC_ALLOWED_PEERS": "172.18.0.0/16"}, "allowed_peers"),
            "wider than the origin": (
                {"NEST_GRPC_ALLOWED_PEERS": "10.253.0.0/16,172.18.0.0/16"},
                "allowed_peers",
            ),
            "edge moved": ({"NEST_GRPC_EDGE_PROXY": "10.0.1.9/32"}, "edge_proxy"),
            "edge unset": ({"NEST_GRPC_EDGE_PROXY": ""}, "edge_proxy"),
            "edge too wide": ({"NEST_GRPC_EDGE_PROXY": "10.0.1.0/24"}, "edge_proxy"),
            "pilot off": ({"NEST_PUBLIC_PILOT": ""}, "public_pilot"),
        }
        for label, (changes, check) in cases.items():
            with self.subTest(label):
                code, checks = self.preflight(
                    self.gateway_env(**changes),
                    networks=self.gateway_networks(),
                    traefik=self.traefik(),
                )
                self.assertEqual(code, 1)
                self.assertTrue(checks[check].startswith("FAIL"), checks[check])

    def test_missing_traefik_fails(self):
        code, checks = self.preflight(self.gateway_env(), networks=self.gateway_networks())
        self.assertEqual(code, 1)
        self.assertTrue(checks["edge_proxy"].startswith("FAIL"))

    def test_traefik_file_follows_the_version(self):
        expected = {
            "traefik:v2.11": "nest-grpc.yml",
            "traefik:v3.1.2": "nest-grpc.yml",
            "traefik:v3.4.5": "nest-grpc.yml",
            "traefik:v3.5.0": "nest-grpc.servers-transport.yml",
            "registry.example.org:5000/traefik:v3.6.2": "nest-grpc.servers-transport.yml",
        }
        for image, chosen in expected.items():
            with self.subTest(image):
                check = doctor.traefik_file_check(image)
                self.assertEqual(check.status, "PASS")
                self.assertTrue(check.detail.endswith("/" + chosen))
        self.assertEqual(doctor.traefik_file_check("traefik:latest").status, "WARN")
        self.assertEqual(doctor.traefik_file_check("traefik").status, "WARN")

    def test_env_file_is_read_literally_and_secrets_never_printed(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write(
                "# comment\n"
                "COMPOSE_PROFILES='local-db,migrate'\n"
                'NEST_GRPC_ORIGIN_SUBNET="10.253.250.0/28"\n'
                "export NEST_GRPC_GATEWAY_ADDRESS=10.253.250.2  # fixed\n"
                "NEST_DATABASE_PASSWORD='s3cret-$HOME'\n"
                "NEST_TAILSCALE_AUTHKEY=tskey-should-not-print\n"
            )
        values = doctor.read_env_file(handle.name)
        self.assertEqual(values["NEST_GRPC_ORIGIN_SUBNET"], ("10.253.250.0/28", False))
        self.assertEqual(values["NEST_GRPC_GATEWAY_ADDRESS"], ("10.253.250.2", False))
        self.assertNotIn("NEST_DATABASE_PASSWORD", values)
        fake = FakeDocker()
        code, out = run(["preflight", "--env-file", handle.name], fake)
        Path(handle.name).unlink()
        self.assertEqual(code, 0, out)
        self.assertNotIn("s3cret", out)
        self.assertNotIn("tskey", out)

    @unittest.skipUnless(CHECKOUT, "requires the repository checkout")
    def test_defaults_match_compose_dokploy(self):
        compose = (ROOT / "compose.dokploy.yaml").read_text()
        self.assertIn(f"${{NEST_GRPC_ORIGIN_SUBNET:-{doctor.DEFAULT_ORIGIN_SUBNET}}}", compose)
        self.assertIn(f"${{NEST_GRPC_GATEWAY_ADDRESS:-{doctor.DEFAULT_GATEWAY_ADDRESS}}}", compose)
        self.assertIn(f"${{NEST_GRPC_EDGE_NETWORK:-{doctor.DEFAULT_EDGE_NETWORK}}}", compose)


class SettingsPrecedenceTests(unittest.TestCase):
    """Preflight checks what `docker compose --env-file .env` would use."""

    def env_file(self, text):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write(text)
        self.addCleanup(Path(handle.name).unlink)
        return handle.name

    def preflight(self, environ, text, networks=(), traefik=None):
        path = self.env_file(text)
        fake = FakeDocker(networks=networks, traefik=traefik)
        code, out = run(["preflight", "--env-file", path], fake, environ)
        return code, out

    SAFE = "NEST_GRPC_ORIGIN_SUBNET='10.253.250.0/28'\nNEST_GRPC_GATEWAY_ADDRESS='10.253.250.2'\n"

    def test_the_shell_environment_wins_over_the_env_file(self):
        squatter = [network("app_x", "172.31.0.0/16")]
        code, out = self.preflight({}, self.SAFE, networks=squatter)
        self.assertEqual(code, 0, out)
        # The invoking shell exports the colliding default: Compose would use it.
        shell = {
            "NEST_GRPC_ORIGIN_SUBNET": "172.31.250.0/28",
            "NEST_GRPC_GATEWAY_ADDRESS": "172.31.250.2",
        }
        code, out = self.preflight(shell, self.SAFE, networks=squatter)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL origin_subnet_free: 172.31.250.0/28 overlaps", out)
        self.assertIn("WARN settings: the shell environment overrides", out)
        self.assertIn("NEST_GRPC_ORIGIN_SUBNET, NEST_GRPC_GATEWAY_ADDRESS", out)

    def test_an_empty_shell_value_overrides_the_file_and_takes_the_default(self):
        squatter = [network("app_x", "172.31.0.0/16")]
        shell = {"NEST_GRPC_ORIGIN_SUBNET": "", "NEST_GRPC_GATEWAY_ADDRESS": ""}
        code, out = self.preflight(shell, self.SAFE, networks=squatter)
        self.assertEqual(code, 1, out)
        self.assertIn("PASS origin_subnet: 172.31.250.0/28", out)

    def test_empty_and_unset_file_values_take_the_compose_default(self):
        code, out = self.preflight({}, "NEST_GRPC_ORIGIN_SUBNET=''\n")
        self.assertIn("PASS origin_subnet: 172.31.250.0/28", out)
        self.assertIn("PASS gateway_address: 172.31.250.2 in 172.31.250.0/28", out)
        self.assertEqual(code, 0, out)

    def test_interpolated_values_are_refused_not_guessed(self):
        for line in (
            "NEST_GRPC_ORIGIN_SUBNET=${SUBNET}\n",
            'NEST_GRPC_ORIGIN_SUBNET="$SUBNET"\n',
            "NEST_GRPC_TRUSTED_PROXIES=${GATEWAY:-10.0.0.2}/32\n",
        ):
            with self.subTest(line):
                code, out = self.preflight({}, line)
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL settings:", out)
                self.assertIn("interpolates", out)
        # Single quotes are literal for Compose too: checked as written (and invalid).
        code, out = self.preflight({}, "NEST_GRPC_ORIGIN_SUBNET='$SUBNET'\n")
        self.assertNotIn("FAIL settings:", out)
        self.assertIn("FAIL origin_subnet:", out)

    def test_the_shell_decides_the_profiles_and_the_gateway_checks(self):
        text = self.SAFE + (
            "COMPOSE_PROFILES='local-db,migrate'\n"
            "NEST_PUBLIC_PILOT='true'\n"
            "NEST_GRPC_TRUSTED_PROXIES='10.253.250.2/32'\n"
            "NEST_GRPC_ALLOWED_PEERS='10.253.250.0/28,172.18.0.0/16'\n"
            "NEST_GRPC_EDGE_PROXY='10.0.1.7/32'\n"
        )
        traefik = {
            "Config": {"Image": "traefik:v3.3.7"},
            "NetworkSettings": {"Networks": {"dokploy-network": {"IPAddress": "10.0.1.7"}}},
        }
        nets = [network("nest_tailnet", "172.18.0.0/16")]
        code, out = self.preflight({}, text, networks=nets, traefik=traefik)
        self.assertIn("SKIP public_grpc", out)
        shell = {
            "COMPOSE_PROFILES": "local-db,migrate,public-grpc",
            "NEST_GRPC_EDGE_PROXY": "10.0.1.9/32",
        }
        code, out = self.preflight(shell, text, networks=nets, traefik=traefik)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL edge_proxy: NEST_GRPC_EDGE_PROXY is 10.0.1.9/32", out)
        self.assertIn("PASS trusted_proxies", out)


SYNTHETIC_TOKEN = "nest-synthetic-" + "0123456789abcdef" * 2


class LogLeakCheckTests(unittest.TestCase):
    def services(self):
        gateway = container("nest-grpc-gateway-1", "running", service="grpc-gateway", pid=4)
        controller = container("nest-controller-1", "running", service="controller", pid=5)
        other = container(
            "other-controller-1", "running", project="other", service="controller", pid=6
        )
        return gateway, controller, other

    def check(self, fake, argv=(), secret=SYNTHETIC_TOKEN + "\n"):
        parsed = doctor.parser().parse_args(["log-leak-check", *argv])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), mock.patch.object(sys, "stderr", err):
            try:
                code = doctor.log_leak_check(
                    parsed, doctor.Docker(run=fake), stdin=io.StringIO(secret)
                )
            except doctor.DockerError as error:
                print(f"nest-deploy-doctor: {error}", file=sys.stderr)
                code = 2
        return code, out.getvalue() + err.getvalue()

    def test_a_leak_is_counted_and_the_value_never_printed(self):
        gateway, controller, other = self.services()
        logs = {
            gateway[
                "Id"
            ]: f"GET /nest.ExecutionService/Execute authorization: Bearer {SYNTHETIC_TOKEN}\n",
            controller["Id"]: "started\n",
            other["Id"]: SYNTHETIC_TOKEN,
        }
        fake = FakeDocker([gateway, controller, other], logs=logs)
        code, out = self.check(fake)
        self.assertEqual(code, 1)
        self.assertIn("LEAK nest-grpc-gateway-1: 1 occurrence(s)", out)
        self.assertIn("PASS nest-controller-1: 0 occurrence(s)", out)
        self.assertNotIn("other-controller-1", out)
        self.assertNotIn(SYNTHETIC_TOKEN, out)
        self.assertNotIn(SYNTHETIC_TOKEN[:20], out)
        # The value is never part of a Docker command line.
        self.assertFalse([c for c in fake.calls if any(SYNTHETIC_TOKEN in a for a in c)])

    def test_several_values_from_stdin_are_all_searched(self):
        gateway, controller, _ = self.services()
        refresh = "nest-synthetic-refresh-" + "f" * 32
        fake = FakeDocker(
            [gateway, controller], logs={gateway["Id"]: "ok", controller["Id"]: f"x {refresh} y"}
        )
        code, out = self.check(fake, secret=f"{SYNTHETIC_TOKEN}\n{refresh}\n")
        self.assertEqual(code, 1)
        self.assertIn("LEAK nest-controller-1: 1 occurrence(s)", out)
        self.assertNotIn(refresh, out)
        # One short value refuses the whole run, before any log is read.
        code, out = self.check(FakeDocker([gateway]), secret=f"{SYNTHETIC_TOKEN}\nshort\n")
        self.assertEqual(code, 2)

    def test_clean_logs_pass(self):
        gateway, controller, _ = self.services()
        fake = FakeDocker(
            [gateway, controller], logs={gateway["Id"]: "ok\n", controller["Id"]: "ok\n"}
        )
        code, out = self.check(fake, ["--since", "2026-11-26T09:00:00Z"])
        self.assertEqual(code, 0, out)
        logs = [c for c in fake.calls if c[:2] == ["docker", "logs"]]
        self.assertTrue(all(c[2:4] == ["--since", "2026-11-26T09:00:00Z"] for c in logs))

    def test_an_unreadable_log_never_passes(self):
        gateway, controller, _ = self.services()
        fake = FakeDocker(
            [gateway, controller], logs={gateway["Id"]: None, controller["Id"]: "ok\n"}
        )
        code, out = self.check(fake)
        self.assertEqual(code, 2, out)
        self.assertIn("could not read the logs", out)

    def test_a_missing_service_or_project_never_passes(self):
        _, controller, _ = self.services()
        code, out = self.check(FakeDocker([controller]), ["--service", "grpc-gateway"])
        self.assertEqual(code, 2)
        self.assertIn("no container for grpc-gateway", out)
        code, _ = self.check(FakeDocker([]))
        self.assertEqual(code, 2)

    def test_service_selection(self):
        gateway, controller, _ = self.services()
        fake = FakeDocker(
            [gateway, controller], logs={gateway["Id"]: "ok", controller["Id"]: SYNTHETIC_TOKEN}
        )
        code, out = self.check(fake, ["--service", "grpc-gateway"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("nest-controller-1", out)

    def test_short_or_missing_values_are_refused(self):
        gateway, controller, _ = self.services()
        fake = FakeDocker([gateway, controller])
        for secret in ("", "short\n"):
            code, out = self.check(fake, secret=secret)
            self.assertEqual(code, 2)
            self.assertIn("nothing was searched", out)
        self.assertFalse([c for c in fake.calls if c[:2] == ["docker", "logs"]])

    def test_a_synthetic_canary_needs_no_secret(self):
        gateway, controller, _ = self.services()
        fake = FakeDocker(
            [gateway, controller], logs={gateway["Id"]: "nest-canary-42", controller["Id"]: ""}
        )
        code, out = self.check(fake, ["--canary", "nest-canary-42"], secret="")
        self.assertEqual(code, 1)
        self.assertIn("LEAK nest-grpc-gateway-1", out)

    def test_the_value_is_typed_without_echo_on_a_terminal(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        prompts = []

        def prompt(text):
            prompts.append(text)
            return SYNTHETIC_TOKEN

        self.assertEqual(doctor.read_secret(Terminal(), prompt), [SYNTHETIC_TOKEN])
        self.assertEqual(len(prompts), 1)
        self.assertNotIn(SYNTHETIC_TOKEN, prompts[0])
        self.assertNotIn("--token", " ".join(doctor.parser().format_help().split()))


def _proc_word(address: str) -> str:
    """One IPv4 address as /proc/net/tcp prints it on a little-endian host."""
    return bytes(int(octet) for octet in address.split("."))[::-1].hex().upper()


def proc_tcp(*sockets) -> str:
    """/proc/net/tcp text for established `(local, lport, remote, rport)` sockets."""
    lines = ["  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"]
    lines.append("   0: 00000000:C383 00000000:0000 0A 00000000:00000000 00:00000000 00000000")
    for i, (local, lport, remote, rport) in enumerate(sockets, 1):
        lines.append(
            f"   {i}: {_proc_word(local)}:{lport:04X} {_proc_word(remote)}:{rport:04X} 01 "
            "00000000:00000000 00:00000000 00000000"
        )
    return "\n".join(lines) + "\n"


def proc_tcp6_mapped(*sockets) -> str:
    """/proc/net/tcp6 text for a dual-stack listener: IPv4 ends as ::ffff:a.b.c.d."""
    lines = ["  sl  local_address                         remote_address                        st"]
    for i, (local, lport, remote, rport) in enumerate(sockets, 1):
        ends = [
            f"0000000000000000FFFF0000{_proc_word(address)}:{port:04X}"
            for address, port in ((local, lport), (remote, rport))
        ]
        lines.append(f"   {i}: {ends[0]} {ends[1]} 01 00000000:00000000")
    return "\n".join(lines) + "\n"


ORIGIN = "nest_grpc-origin"
EDGE = "dokploy-network"


def runtime_container(service, networks, env, *, started="2026-11-26T15:00:00Z"):
    found = container(f"nest-{service}-1", "running", service=service, pid=42, started=started)
    found["Config"]["Env"] = [f"{k}={v}" for k, v in env.items()]
    found["NetworkSettings"] = {
        "Networks": {
            name: {"IPAddress": address, "DNSNames": names, "Aliases": names}
            for name, (address, names) in networks.items()
        }
    }
    return found


class RuntimeDocker(FakeDocker):
    """Also answers `docker exec`: getent in the gateway, the probe, and each
    container's socket tables."""

    def __init__(self, containers, *, resolves, sockets, probe=0):
        super().__init__(containers=containers)
        self.resolves = resolves  # gateway getent answer, or None when it fails
        self.sockets = sockets  # container ID -> socket table text, or None
        self.probe = probe

    def __call__(self, argv):
        argv = list(argv)
        if argv[:2] != ["docker", "exec"]:
            return super().__call__(argv)
        self.calls.append(argv)
        R = doctor.Result
        rest = argv[2:]
        if rest[0] == "--detach":
            assert rest[2] == "bash" and "/dev/tcp/" in rest[4], rest
            return R(self.probe, "")
        target, command = rest[0], rest[1:]
        if command[0] == "getent":
            if self.resolves is None:
                return R(2, "")
            return R(0, "".join(f"{a} STREAM {command[2]}\n{a} DGRAM\n" for a in self.resolves))
        if command[0] == "sh":
            text = self.sockets.get(target)
            return R(1, "", "exec failed") if text is None else R(0, text)
        raise AssertionError(f"unexpected exec {argv}")


class GatewayRuntimeTests(unittest.TestCase):
    """#109: what the running gateway and Controller actually do, from recorded answers
    shaped like the 2026-11-26 staging host (Controller .2 and Envoy .3 on grpc-origin,
    both also on dokploy-network)."""

    CONTROLLER_ENV: ClassVar[dict[str, str]] = {
        "NEST_GRPC_TRUSTED_PROXIES": "10.253.250.3/32",
        "NEST_GRPC_ALLOWED_PEERS": "10.253.250.3/32,172.20.0.0/16",
        "NEST_DATABASE_PASSWORD": "never-printed-secret",
        "NEST_GOOGLE_CLIENT_SECRET": "GOCSPX-never-printed",
    }

    def setUp(self):
        patcher = mock.patch.object(doctor.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def deployment(self, *, upstream="controller-grpc-origin:50051", generic_on_edge=False, **env):
        origin_names = ["nest-controller-1", "controller", "controller-grpc-origin"]
        edge_names = ["nest-controller-1", "controller"]
        if generic_on_edge:
            origin_names = ["nest-controller-1", "controller"]
        controller = runtime_container(
            "controller",
            {
                ORIGIN: ("10.253.250.2", origin_names),
                EDGE: ("10.0.1.12", edge_names),
                "nest_tailnet": ("172.20.0.3", ["nest-controller-1", "controller"]),
            },
            {**self.CONTROLLER_ENV, **env},
        )
        gateway = runtime_container(
            "grpc-gateway",
            {
                ORIGIN: ("10.253.250.3", ["nest-grpc-gateway-1"]),
                EDGE: ("10.0.1.13", ["nest-grpc-gateway-1", "nest-grpc-gateway"]),
            },
            {"NEST_GRPC_UPSTREAM": upstream, "NEST_GRPC_EDGE_PROXY": "10.0.1.7/32"},
            started="2026-11-26T15:00:05Z",
        )
        return controller, gateway

    def run_runtime(self, fake):
        return run(["gateway-runtime", "--project", "nest"], fake)

    def healthy(self, controller, gateway, **overrides):
        options = {
            "resolves": ["10.253.250.2"],
            "sockets": {
                gateway["Id"]: proc_tcp(("10.253.250.3", 41000, "10.253.250.2", 50051)),
                controller["Id"]: proc_tcp(("10.253.250.2", 50051, "10.253.250.3", 41000)),
            },
        }
        options.update(overrides)
        return RuntimeDocker([controller, gateway], **options)

    def test_current_configuration_passes_with_socket_evidence(self):
        controller, gateway = self.deployment()
        code, out = self.run_runtime(self.healthy(controller, gateway))
        self.assertEqual(code, 0, out)
        self.assertNotIn("FAIL", out)
        self.assertIn("PASS upstream_dns: controller-grpc-origin -> 10.253.250.2 only", out)
        self.assertIn(
            "PASS private_path: gateway 10.253.250.3 -> Controller 10.253.250.2:50051", out
        )
        for secret in ("never-printed-secret", "GOCSPX-never-printed"):
            self.assertNotIn(secret, out)

    def test_staging_topology_with_the_generic_name_fails(self):
        """The observed failure: `controller` resolves to the edge address and the
        Controller's socket shows the gateway's edge address."""
        controller, gateway = self.deployment(upstream="controller:50051", generic_on_edge=True)
        fake = self.healthy(
            controller,
            gateway,
            resolves=["10.0.1.12"],
            sockets={
                gateway["Id"]: proc_tcp(("10.0.1.13", 41000, "10.0.1.12", 50051)),
                controller["Id"]: proc_tcp6_mapped(("10.0.1.12", 50051, "10.0.1.13", 41000)),
            },
        )
        code, out = self.run_runtime(fake)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL upstream_setting: NEST_GRPC_UPSTREAM is controller:50051", out)
        self.assertIn(f"FAIL upstream_alias: controller is a Controller name on {EDGE}", out)
        self.assertIn("FAIL upstream_dns: controller resolves inside the gateway to 10.0.1.12", out)
        self.assertIn("FAIL private_path: the gateway reaches the Controller from 10.0.1.13", out)

    def test_ipv4_mapped_controller_sockets_are_recognised(self):
        """A dual-stack Controller lists the gateway as ::ffff:10.253.250.3."""
        controller, gateway = self.deployment()
        sockets = {
            gateway["Id"]: proc_tcp(("10.253.250.3", 41000, "10.253.250.2", 50051)),
            controller["Id"]: proc_tcp6_mapped(("10.253.250.2", 50051, "10.253.250.3", 41000)),
        }
        code, out = self.run_runtime(self.healthy(controller, gateway, sockets=sockets))
        self.assertEqual(code, 0, out)
        self.assertIn("peer for every gateway connection is 10.253.250.3", out)

    def test_any_gateway_connection_over_the_edge_fails(self):
        controller, gateway = self.deployment()
        sockets = {
            gateway["Id"]: proc_tcp(("10.253.250.3", 41000, "10.253.250.2", 50051)),
            controller["Id"]: proc_tcp(
                ("10.253.250.2", 50051, "10.253.250.3", 41000),
                ("10.0.1.12", 50051, "10.0.1.13", 41002),  # a pooled Envoy connection
                ("172.20.0.3", 50051, "172.20.0.2", 40000),  # Tailscale Serve: not the gateway
            ),
        }
        code, out = self.run_runtime(self.healthy(controller, gateway, sockets=sockets))
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL private_path: the gateway reaches the Controller from 10.0.1.13", out)

    def test_wrong_or_extra_addresses_fail(self):
        controller, gateway = self.deployment()
        for resolves in (["10.253.250.2", "10.0.1.12"], ["10.253.250.9"], []):
            with self.subTest(resolves=resolves):
                code, out = self.run_runtime(self.healthy(controller, gateway, resolves=resolves))
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL upstream_dns", out)

    def test_alias_on_another_network_fails(self):
        controller, gateway = self.deployment()
        controller["NetworkSettings"]["Networks"][EDGE]["DNSNames"].append("controller-grpc-origin")
        code, out = self.run_runtime(self.healthy(controller, gateway))
        self.assertEqual(code, 1, out)
        self.assertIn(
            f"FAIL upstream_alias: controller-grpc-origin is a Controller name on {EDGE}, {ORIGIN}",
            out,
        )

    def test_widened_trust_fails(self):
        for env, message in (
            ({"NEST_GRPC_TRUSTED_PROXIES": "10.0.1.0/24"}, "is 10.0.1.0/24, not 10.253.250.3/32"),
            (
                {"NEST_GRPC_ALLOWED_PEERS": "10.253.250.3/32,10.0.1.0/24"},
                "NEST_GRPC_ALLOWED_PEERS admits the gateway's non-origin address 10.0.1.13",
            ),
            ({"NEST_GRPC_ALLOWED_PEERS": "172.20.0.0/16"}, "does not admit 10.253.250.3/32"),
            ({"NEST_GRPC_TRUSTED_PROXIES": "not-a-network"}, "does not parse"),
        ):
            with self.subTest(env):
                controller, gateway = self.deployment(**env)
                code, out = self.run_runtime(self.healthy(controller, gateway))
                self.assertEqual(code, 1, out)
                self.assertIn("FAIL controller_trust", out)
                self.assertIn(message, out)

    def test_unavailable_checks_fail(self):
        controller, gateway = self.deployment()
        for label, overrides in (
            ("getent", {"resolves": None}),
            ("probe", {"probe": 1}),
            ("socket table", {"sockets": {gateway["Id"]: None, controller["Id"]: None}}),
            (
                "no connection",
                {"sockets": {gateway["Id"]: proc_tcp(), controller["Id"]: proc_tcp()}},
            ),
        ):
            with self.subTest(label):
                code, out = self.run_runtime(self.healthy(controller, gateway, **overrides))
                self.assertEqual(code, 1, out)
                self.assertRegex(out, r"FAIL (upstream_dns|private_path)")

    def test_stale_or_missing_containers_fail(self):
        controller, gateway = self.deployment()
        _, stale = self.deployment()
        fake = RuntimeDocker([controller, gateway, stale], resolves=["10.253.250.2"], sockets={})
        code, out = self.run_runtime(fake)
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL grpc-gateway_container: 2 running grpc-gateway containers", out)
        gateway["State"]["Status"] = "restarting"
        code, out = self.run_runtime(RuntimeDocker([controller, gateway], resolves=[], sockets={}))
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL grpc-gateway_container: no running grpc-gateway container", out)
        self.assertIn("found: restarting", out)

    def test_controller_restarted_after_the_gateway_is_reported(self):
        controller, gateway = self.deployment()
        controller["State"]["StartedAt"] = "2026-11-26T16:00:00Z"
        code, out = self.run_runtime(self.healthy(controller, gateway))
        self.assertEqual(code, 0, out)
        self.assertIn("WARN containers: the Controller was (re)started after the gateway", out)

    def test_parse_proc_net_byte_order(self):
        text = proc_tcp(("10.253.250.3", 41000, "10.253.250.2", 50051))
        ((local, lport, remote, rport),) = doctor.parse_proc_net(text, "little")
        self.assertEqual(
            (str(local), lport, str(remote), rport), ("10.253.250.3", 41000, "10.253.250.2", 50051)
        )
        big = "   1: 0AFDFA03:A028 0AFDFA02:C383 01 0\n"
        ((local, _, remote, _),) = doctor.parse_proc_net(big, "big")
        self.assertEqual((str(local), str(remote)), ("10.253.250.3", "10.253.250.2"))
        self.assertEqual(doctor.parse_proc_net("   1: zz:1 yy:2 01\n"), [])


@unittest.skipUnless(CHECKOUT, "requires the repository checkout")
class RunbookCommandTests(unittest.TestCase):
    """Every Compose command in the operator docs targets compose.dokploy.yaml, and
    every service they name exists there."""

    DOCS = ("hosted-rollout.md", "public-grpc-gateway.md", "deployment-recovery.md", "dokploy.md")

    def services(self):
        compose = (ROOT / "compose.dokploy.yaml").read_text()
        block = compose.split("\nservices:\n", 1)[1].split("\nnetworks:\n", 1)[0]
        return set(re.findall(r"^  ([a-z][a-z0-9-]*):$", block, re.MULTILINE))

    def commands(self):
        for name in self.DOCS:
            text = (ROOT / "docs" / "source" / name).read_text()
            for block in re.findall(r"```(?:bash|sh)\n(.*?)```", text, re.DOTALL):
                for line in block.replace("\\\n", " ").splitlines():
                    yield name, line.strip()

    def test_compose_commands_name_the_dokploy_file(self):
        for name, line in self.commands():
            if "docker compose" not in line or line.startswith("#"):
                continue
            command = line.split("#", 1)[0]
            if re.search(r"docker compose (ls|version)\b", command):
                continue
            with self.subTest(doc=name, line=line):
                self.assertRegex(command, r"-f \S*compose\.dokploy\.yaml\b")

    def test_named_services_exist(self):
        services = self.services()
        seen = 0
        for name, line in self.commands():
            match = re.search(
                r"(?:^|\s|\()dc\s+(?:logs|exec|restart|up -d --force-recreate)\s+((?:-[\w-]+\s+\S+\s+)*)([a-z][a-z0-9-]*)",
                line,
            )
            if match:
                seen += 1
                with self.subTest(doc=name, line=line):
                    self.assertIn(match.group(2), services)
        self.assertGreater(seen, 3)

    def test_the_helper_is_the_dokploy_invocation(self):
        text = (ROOT / "docs" / "source" / "hosted-rollout.md").read_text()
        self.assertIn(
            'dc() { docker compose -p "$APP" --project-directory "$CODE" -f "$CODE/compose.dokploy.yaml" --env-file "$CODE/.env" "$@"; }',
            text,
        )
        self.assertNotRegex(text, r"docker compose -p <appName> (logs|exec|restart)")


@unittest.skipUnless(CHECKOUT, "requires the repository checkout")
class TraefikRenderTests(unittest.TestCase):
    def render(self, *args):
        return subprocess.run(
            ["/bin/sh", str(RENDER), *args], capture_output=True, text=True, check=False, timeout=30
        )

    def test_version_selects_the_proxy_protocol_form(self):
        old = self.render("grpc.staging.example.org", "v3.4.5")
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertIn("rule: HostSNI(`grpc.staging.example.org`)", old.stdout)
        self.assertRegex(old.stdout, r"loadBalancer:\n(?:.*\n)*?\s+proxyProtocol:\n\s+version: 2")
        self.assertNotIn("serversTransports:", old.stdout)
        new = self.render("grpc.staging.example.org", "3.5.3")
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertIn("serversTransport: nest-grpc-proxy-v2", new.stdout)
        self.assertRegex(
            new.stdout,
            r"serversTransports:\n\s+nest-grpc-proxy-v2:\n\s+(?:#.*\n\s+)*proxyProtocol:\n\s+version: 2",
        )
        for text in (old.stdout, new.stdout):
            self.assertIn("passthrough: true", text)
            self.assertIn("- address: nest-grpc-gateway:8443", text)
            self.assertNotIn("grpc.nest.cerulean.works`", text)

    def test_bad_input_is_refused(self):
        for args in (
            ("grpc.example.org`) || HostSNI(`*", "v3.3"),
            ("localhost", "v3.3"),
            ("grpc.example.org", "latest"),
            ("grpc.example.org", "v1.7"),
            ("grpc.example.org",),
        ):
            with self.subTest(args):
                result = self.render(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_both_files_route_the_same_name_and_backend(self):
        directory = RENDER.parent
        texts = [
            (directory / n).read_text()
            for n in ("nest-grpc.yml", "nest-grpc.servers-transport.yml")
        ]
        for text in texts:
            self.assertIn("rule: HostSNI(`grpc.nest.cerulean.works`)", text)
            self.assertEqual(len(re.findall(r"version: 2", text)), 1)


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    result = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=30)
    return result.returncode == 0


@unittest.skipUnless(docker_available(), "needs a Docker daemon")
class RealDockerTests(unittest.TestCase):
    """A never-started container carrying Compose's labels, as an interrupted
    recreate leaves it; nothing else on the daemon is touched."""

    IMAGE = "busybox:1.37"

    def setUp(self):
        have = subprocess.run(
            ["docker", "image", "inspect", self.IMAGE], capture_output=True, check=False
        )
        if have.returncode != 0:
            pulled = subprocess.run(
                ["docker", "pull", "-q", self.IMAGE], capture_output=True, check=False, timeout=300
            )
            if pulled.returncode != 0:
                self.skipTest(f"cannot pull {self.IMAGE}")
        self.project = f"nest-doctor-{uuid.uuid4().hex[:8]}"
        self.addCleanup(self.cleanup)

    def cleanup(self):
        found = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.project}"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        if found:
            subprocess.run(["docker", "rm", "-f", *found], capture_output=True, check=False)

    def create(self, name, service="migrate"):
        return subprocess.run(
            [
                "docker",
                "create",
                "--name",
                name,
                "--label",
                f"com.docker.compose.project={self.project}",
                "--label",
                f"com.docker.compose.service={service}",
                self.IMAGE,
                "true",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def doctor(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = doctor.main([*args, "--project", self.project, "--min-age", "0"])
        return code, out.getvalue()

    def test_detects_and_removes_only_the_never_started_container(self):
        stuck = self.create(f"0123456789ab_{self.project}-migrate-1")
        controller = self.create(f"{self.project}-controller-1", service="controller")
        code, out = self.doctor("migrate-status")
        self.assertEqual(code, 3, out)
        self.assertIn("replacement prefix", out)
        code, _ = self.doctor("recover-migrate", "--settle", "0")
        self.assertEqual(code, 0)
        self.assertEqual(self.doctor("migrate-status")[0], 3)
        code, out = self.doctor("recover-migrate", "--apply", "--settle", "0")
        self.assertEqual(code, 0, out)
        remaining = subprocess.run(
            [
                "docker",
                "ps",
                "-aq",
                "--no-trunc",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        self.assertNotIn(stuck, remaining)
        self.assertIn(controller, remaining)
        self.assertEqual(self.doctor("migrate-status")[0], 6)


if __name__ == "__main__":
    unittest.main()
