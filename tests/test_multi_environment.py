"""Two Nest deployments on one Dokploy host (#118): Main and a tagged Release.

Synthetic, credential-free checks of the parts that keep them apart:

- the Traefik route renderer: the single-deployment output is unchanged, and
  `--instance` gives each deployment its own router, service, transport and target;
- the gateway entrypoint's validation of `NEST_GRPC_EDGE_ALIAS`;
- compose.dokploy.yaml's alias setting and its backward-compatible default;
- `nest-deploy-doctor.py`: alias, SNI, Tailnet, database host, HPC workdir and
  account collisions with other deployments, and Traefik route-file collisions;
- the two environment templates;
- the SDK settings a Main developer uses, which never send a Release session.

The live acceptance (real Dokploy, DNS, certificates, OAuth, HPC) is operator-only
and is not claimed here; tests/smoke_two_environments.sh is the real-container check.
"""

import importlib.util
import io
import ipaddress
import json
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
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "scripts" / "nest-deploy-doctor.py"
GATEWAY = ROOT / "deploy" / "public-grpc"
RENDER = GATEWAY / "traefik" / "render.sh"
ENVIRONMENTS = ROOT / "deploy" / "environments"

if not (DOCTOR.is_file() and RENDER.is_file() and (ROOT / "compose.dokploy.yaml").is_file()):
    raise unittest.SkipTest("requires the repository checkout")

if "nest_deploy_doctor" in sys.modules:
    doctor = sys.modules["nest_deploy_doctor"]
else:
    spec = importlib.util.spec_from_file_location("nest_deploy_doctor", DOCTOR)
    assert spec is not None and spec.loader is not None
    doctor = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = doctor  # dataclasses look their module up
    spec.loader.exec_module(doctor)

EDGE = "dokploy-network"


def render(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/sh", str(RENDER), *args], capture_output=True, text=True, check=False, timeout=30
    )


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'")
    return values


class RendererTests(unittest.TestCase):
    def test_single_deployment_output_is_unchanged(self):
        """Backward compatible: without options, both Traefik families print the
        reviewed file byte for byte, as before #118."""
        for version, file in (
            ("v3.4.5", "nest-grpc.yml"),
            ("v2.11.0", "nest-grpc.yml"),
            ("v3.5.0", "nest-grpc.servers-transport.yml"),
            ("v3.6.2", "nest-grpc.servers-transport.yml"),
        ):
            with self.subTest(version):
                result = render("grpc.nest.cerulean.works", version)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, (GATEWAY / "traefik" / file).read_text())

    def test_instances_get_their_own_names_and_target(self):
        for version in ("v3.3.7", "v3.6.2"):
            main = render("grpc.main.example.org", version, "--instance", "main")
            release = render("--instance", "release", "grpc.example.org", version)
            for result in (main, release):
                self.assertEqual(result.returncode, 0, result.stderr)
            routes = {
                "main": doctor.parse_tcp_routes(main.stdout),
                "release": doctor.parse_tcp_routes(release.stdout),
            }
            for name, parsed in routes.items():
                with self.subTest(version=version, instance=name):
                    self.assertEqual(list(parsed["routers"]), [f"nest-grpc-{name}"])
                    self.assertEqual(list(parsed["services"]), [f"nest-grpc-gateway-{name}"])
                    self.assertEqual(
                        parsed["services"][f"nest-grpc-gateway-{name}"]["addresses"],
                        [f"nest-grpc-gateway-{name}:8443"],
                    )
            self.assertEqual(
                routes["main"]["routers"]["nest-grpc-main"]["hosts"], ["grpc.main.example.org"]
            )
            for text in (main.stdout, release.stdout):
                # Security properties are untouched: passthrough, PROXY v2, websecure.
                self.assertIn("passthrough: true", text)
                self.assertRegex(text, r"proxyProtocol:\n\s+version: 2")
                self.assertIn("- websecure", text)
                self.assertNotIn("nest-grpc-gateway:8443", text)
                self.assertNotIn("grpc.nest.cerulean.works`", text)
            if version == "v3.6.2":
                self.assertIn("serversTransport: nest-grpc-proxy-v2-main", main.stdout)
                self.assertEqual(
                    list(routes["release"]["serversTransports"]), ["nest-grpc-proxy-v2-release"]
                )
            else:
                self.assertNotIn("serversTransport", main.stdout)

    def test_explicit_edge_alias(self):
        result = render(
            "grpc.main.example.org",
            "v3.6.2",
            "--instance",
            "main",
            "--edge-alias",
            "nest-grpc-gateway",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("- address: nest-grpc-gateway:8443", result.stdout)
        self.assertIn("    nest-grpc-main:", result.stdout)
        self.assertIn("# Nest instance: main; edge alias: nest-grpc-gateway", result.stdout)

    def test_unsafe_names_are_refused(self):
        cases = {
            "upper-case instance": ("--instance", "Main"),
            "instance with a dot": ("--instance", "a.b"),
            "instance with a slash": ("--instance", "a/b"),
            "instance too long": ("--instance", "a" * 25),
            "instance with a trailing hyphen": ("--instance", "main-"),
            "empty instance": ("--instance", ""),
            "service-name alias": ("--edge-alias", "grpc-gateway"),
            "controller alias": ("--edge-alias", "controller"),
            "origin alias": ("--edge-alias", "controller-grpc-origin"),
            "alias with a colon": ("--edge-alias", "a:8443"),
            "alias with a space": ("--edge-alias", "a b"),
            "empty alias": ("--edge-alias", ""),
            "unknown option": ("--name", "x"),
            "missing value": ("--instance",),
        }
        for label, extra in cases.items():
            with self.subTest(label):
                result = render("grpc.example.org", "v3.6.2", *extra)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertIn("render.sh:", result.stderr)
        for args in (("grpc.example.org", "v3.6.2", "extra"), ("grpc.example.org",)):
            with self.subTest(args):
                self.assertEqual(render(*args).returncode, 2)

    def test_two_default_files_collide_and_instances_do_not(self):
        """The #118 collision in route form: two default renders define the same
        router, service and transport in two files; Traefik keeps one of each."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "nest-grpc.yml").write_text(render("grpc.staging.example.org", "v3.6.2").stdout)
            (path / "nest-grpc-release.yml").write_text(render("grpc.example.org", "v3.6.2").stdout)
            (path / "dokploy.yml").write_text(
                "http:\n  routers:\n    nest-grpc:\n      rule: Host(`x`)\n"
            )
            checks = {c.name: c for c in doctor.check_routes({}, directory, public_grpc=False)}
            self.assertEqual(checks["route_names"].status, "FAIL")
            self.assertIn(
                "router nest-grpc in nest-grpc-release.yml, nest-grpc.yml",
                checks["route_names"].detail,
            )
            self.assertIn("serversTransport nest-grpc-proxy-v2", checks["route_names"].detail)
            # An http router of the same name is a different namespace.
            self.assertNotIn("dokploy.yml", checks["route_names"].detail)
            (path / "nest-grpc-release.yml").write_text(
                render("grpc.example.org", "v3.6.2", "--instance", "release").stdout
            )
            checks = {c.name: c for c in doctor.check_routes({}, directory, public_grpc=False)}
            self.assertEqual(checks["route_names"].status, "PASS", checks["route_names"].detail)
            self.assertEqual(checks["route_sni"].status, "PASS")


class EntrypointAliasTests(unittest.TestCase):
    def run_render(self, **env) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as out:
            return subprocess.run(
                ["/bin/sh", str(GATEWAY / "gateway-entrypoint.sh"), "--render", out],
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "NEST_GRPC_EDGE_PROXY": "10.0.1.7/32",
                    **env,
                },
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )

    def test_default_and_unique_aliases_render(self):
        for env in ({}, {"NEST_GRPC_EDGE_ALIAS": "nest-grpc-gateway-release"}):
            with self.subTest(env):
                result = self.run_render(**env)
                self.assertEqual(result.returncode, 0, result.stderr)
                alias = env.get("NEST_GRPC_EDGE_ALIAS", "nest-grpc-gateway")
                self.assertIn(f"edge alias {alias},", result.stderr)

    def test_unsafe_aliases_fail_closed(self):
        for alias in ("grpc-gateway", "controller", "Release", "a.b", "a;b", "-x", "x" * 64):
            with self.subTest(alias):
                result = self.run_render(NEST_GRPC_EDGE_ALIAS=alias)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("NEST_GRPC_EDGE_ALIAS", result.stderr)


class ComposeAliasTests(unittest.TestCase):
    COMPOSE = (ROOT / "compose.dokploy.yaml").read_text()

    def test_alias_is_a_setting_with_the_old_default(self):
        self.assertIn('aliases: ["${NEST_GRPC_EDGE_ALIAS:-nest-grpc-gateway}"]', self.COMPOSE)
        self.assertIn(
            "NEST_GRPC_EDGE_ALIAS: ${NEST_GRPC_EDGE_ALIAS:-nest-grpc-gateway}", self.COMPOSE
        )
        # The #109 origin-only upstream alias is unchanged.
        self.assertIn("aliases: [controller-grpc-origin]", self.COMPOSE)
        self.assertIn("NEST_GRPC_UPSTREAM: controller-grpc-origin:50051", self.COMPOSE)
        # No host port anywhere: the gateway is reached only through Traefik.
        self.assertNotRegex(self.COMPOSE, r"(?m)^\s+ports:")

    @unittest.skipUnless(shutil.which("docker"), "needs the docker compose CLI")
    def test_compose_resolves_each_deployment_separately(self):
        def config(env: dict[str, str]) -> dict:
            with tempfile.TemporaryDirectory() as directory:
                env_file = Path(directory) / ".env"
                env_file.write_text("".join(f"{k}='{v}'\n" for k, v in env.items()))
                result = subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(ROOT / "compose.dokploy.yaml"),
                        "--env-file",
                        str(env_file),
                        "--profile",
                        "public-grpc",
                        "--profile",
                        "local-db",
                        "--profile",
                        "public-grpc-acme",
                        "-p",
                        env.get("P", "nest"),
                        "config",
                        "--format",
                        "json",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                if result.returncode != 0 and "is not a docker command" in result.stderr:
                    self.skipTest("docker compose plugin missing")
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

        default = config({})
        gateway = default["services"]["grpc-gateway"]
        self.assertEqual(gateway["networks"]["edge"]["aliases"], ["nest-grpc-gateway"])
        self.assertEqual(gateway["environment"]["NEST_GRPC_EDGE_ALIAS"], "nest-grpc-gateway")
        main_env = {
            **read_env(ENVIRONMENTS / "main.env.example"),
            **read_phase2(ENVIRONMENTS / "main.env.example"),
        }
        main = config({"P": "nest-main", **main_env})
        release = config({"P": "nest-release", **read_env(ENVIRONMENTS / "release.env.example")})
        for resolved, alias, subnet in (
            (main, "nest-grpc-gateway-main", "10.253.250.0/28"),
            (release, "nest-grpc-gateway-release", "10.253.251.0/28"),
        ):
            gateway = resolved["services"]["grpc-gateway"]
            self.assertEqual(gateway["networks"]["edge"]["aliases"], [alias])
            self.assertEqual(
                resolved["networks"]["grpc-origin"]["ipam"]["config"][0]["subnet"], subnet
            )
            self.assertEqual(resolved["networks"]["edge"]["name"], EDGE)
        # Project-scoped names: nothing private is shared by the two deployments.
        for key in ("grpc-origin", "tailnet"):
            self.assertNotEqual(main["networks"][key]["name"], release["networks"][key]["name"])
        for key in ("postgres-data", "tailscale-state", "grpc-certificates", "grpc-acme-state"):
            self.assertNotEqual(main["volumes"][key]["name"], release["volumes"][key]["name"])


def read_phase2(path: Path) -> dict[str, str]:
    """The commented-out Phase 2 values of main.env.example."""
    text = path.read_text()
    block = text[text.index("PHASE 2 ONLY") :]
    values = {}
    for line in block.splitlines():
        match = re.match(r"^# (NEST_[A-Z0-9_]+)='([^']*)'$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


class TemplateTests(unittest.TestCase):
    MAIN = read_env(ENVIRONMENTS / "main.env.example")
    MAIN_PHASE2 = read_phase2(ENVIRONMENTS / "main.env.example")
    RELEASE = read_env(ENVIRONMENTS / "release.env.example")

    def test_identities_differ(self):
        for key in (
            "NEST_GRPC_ORIGIN_SUBNET",
            "NEST_GRPC_GATEWAY_ADDRESS",
            "NEST_GRPC_EDGE_ALIAS",
            "NEST_GRPC_PUBLIC_HOST",
            "NEST_TAILSCALE_HOSTNAME",
            "NEST_GOOGLE_REDIRECT_URL",
            "NEST_PUBLIC_URL",
            "NEST_SLURM_WORKDIR",
            "NEST_GRPC_CLOUDFLARE_DNS_API_TOKEN",
            "NEST_CONTROLLER_IMAGE",
        ):
            with self.subTest(key):
                self.assertIn(key, self.MAIN)
                self.assertIn(key, self.RELEASE)
                self.assertNotEqual(self.MAIN[key], self.RELEASE[key])
                if key in self.MAIN_PHASE2:
                    self.assertNotEqual(self.MAIN_PHASE2[key], self.RELEASE[key])
        main = ipaddress.IPv4Network(self.MAIN["NEST_GRPC_ORIGIN_SUBNET"])
        release = ipaddress.IPv4Network(self.RELEASE["NEST_GRPC_ORIGIN_SUBNET"])
        self.assertFalse(main.overlaps(release))

    def test_main_template_is_phase_1(self):
        """Review of #119: following main.env.example on today's staging application
        changes neither its alias nor its name nor its origin; Main's new values are
        only in the commented Phase 2 block."""
        self.assertEqual(self.MAIN["NEST_GRPC_EDGE_ALIAS"], doctor.DEFAULT_EDGE_ALIAS)
        for key in ("NEST_GRPC_PUBLIC_HOST", "NEST_GOOGLE_REDIRECT_URL", "NEST_PUBLIC_URL"):
            self.assertRegex(self.MAIN[key], r"^<existing [^>]+>$", key)
        self.assertEqual(
            self.MAIN_PHASE2,
            {
                "NEST_GRPC_PUBLIC_HOST": "grpc.main.nest.cerulean.works",
                "NEST_GRPC_EDGE_ALIAS": "nest-grpc-gateway-main",
                "NEST_GOOGLE_REDIRECT_URL": "https://main.nest.cerulean.works/auth/google/callback",
                "NEST_PUBLIC_URL": "https://main.nest.cerulean.works",
            },
        )

    def test_each_template_is_internally_consistent(self):
        main_target = {**self.MAIN, **self.MAIN_PHASE2}
        for name, env in (("main", main_target), ("release", self.RELEASE)):
            with self.subTest(name):
                subnet = ipaddress.IPv4Network(env["NEST_GRPC_ORIGIN_SUBNET"])
                gateway = ipaddress.IPv4Address(env["NEST_GRPC_GATEWAY_ADDRESS"])
                hosts = list(subnet.hosts())
                self.assertIn(gateway, hosts[1:])
                self.assertEqual(env["NEST_GRPC_TRUSTED_PROXIES"], f"{gateway}/32")
                self.assertTrue(env["NEST_GRPC_ALLOWED_PEERS"].startswith(f"{subnet},"))
                self.assertIsNone(doctor.edge_alias_problem(env["NEST_GRPC_EDGE_ALIAS"]))
                self.assertTrue(env["NEST_GRPC_EDGE_ALIAS"].endswith(f"-{name}"))
                # Let's Encrypt staging and no hosted-profile activation (H09 stays off).
                self.assertNotIn("NEST_GRPC_ACME_SERVER", env)
                self.assertEqual(env["NEST_CONSOLE_HOSTED_PROFILE"], "off")
                self.assertEqual(env["NEST_PUBLIC_PILOT"], "true")
                self.assertEqual(env["NEST_EXECUTION_REGISTRY"], "required")
                # Origins are the environment's own, over HTTPS.
                origin = env["NEST_PUBLIC_URL"]
                self.assertTrue(env["NEST_GOOGLE_REDIRECT_URL"].startswith(origin + "/"))
                self.assertTrue(origin.startswith("https://"))
        self.assertEqual(self.RELEASE["NEST_PUBLIC_URL"], "https://nest.cerulean.works")
        self.assertEqual(self.RELEASE["NEST_GRPC_PUBLIC_HOST"], "grpc.nest.cerulean.works")

    def test_no_credential_values(self):
        secret_keys = re.compile(r"(PASSWORD|SECRET|TOKEN|AUTHKEY|PRIVATE_KEY)$")
        for name, env in (("main", self.MAIN), ("release", self.RELEASE)):
            for key, value in env.items():
                if secret_keys.search(key):
                    with self.subTest(template=name, key=key):
                        self.assertRegex(value, r"^<[^>]+>$")

    def test_route_commands_match_the_aliases(self):
        for name, path, env in (
            ("main", "main.env.example", {**self.MAIN, **self.MAIN_PHASE2}),
            ("release", "release.env.example", self.RELEASE),
        ):
            text = (ENVIRONMENTS / path).read_text()
            with self.subTest(name):
                self.assertIn(f"--instance {name}", text)
                result = render(env["NEST_GRPC_PUBLIC_HOST"], "v3.6.2", "--instance", name)
                self.assertIn(f"- address: {env['NEST_GRPC_EDGE_ALIAS']}:8443", result.stdout)


# --- The doctor against recorded docker answers --------------------------------------


def container(
    name, project, service, *, running=True, env=None, edge_aliases=None, workdir="/srv/a"
):
    networks = {}
    if edge_aliases is not None:
        networks[EDGE] = {
            "IPAddress": "10.0.1.20",
            "Aliases": edge_aliases,
            "DNSNames": edge_aliases,
        }
    return {
        "Id": uuid.uuid4().hex + uuid.uuid4().hex,
        "Name": "/" + name,
        "State": {"Status": "running" if running else "exited"},
        "Config": {
            "Env": [f"{k}={v}" for k, v in (env or {}).items()],
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": service,
                "com.docker.compose.project.working_dir": workdir,
            },
        },
        "NetworkSettings": {"Networks": networks},
    }


class FakeDocker:
    def __init__(self, containers=(), networks=()):
        self.containers = list(containers)
        self.networks = list(networks)
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
                for c in self.containers
                if all(c["Config"]["Labels"].get(k) == v for k, v in wanted.items())
            ]
            return R(0, "\n".join(found))
        if argv[:3] == ["docker", "container", "inspect"]:
            found = [c for c in self.containers if c["Id"] in argv[3:] or c["Name"][1:] in argv[3:]]
            return R(0, json.dumps(found)) if found else R(1, "", "Error: No such container")
        if argv[:2] == ["ip", "-json"]:
            return R(0, "[]")
        raise AssertionError(f"unexpected command {argv}")


def edge_network(*containers):
    return {
        "Name": EDGE,
        "Id": uuid.uuid4().hex,
        "IPAM": {"Config": [{"Subnet": "10.0.1.0/24"}]},
        "Containers": {c["Id"]: {"Name": c["Name"][1:]} for c in containers},
    }


RELEASE_ENV = {
    "COMPOSE_PROFILES": "local-db,migrate,public-grpc",
    "NEST_GRPC_ORIGIN_SUBNET": "10.253.251.0/28",
    "NEST_GRPC_GATEWAY_ADDRESS": "10.253.251.3",
    "NEST_GRPC_TRUSTED_PROXIES": "10.253.251.3/32",
    "NEST_GRPC_ALLOWED_PEERS": "10.253.251.0/28",
    "NEST_PUBLIC_PILOT": "true",
    "NEST_GRPC_EDGE_ALIAS": "nest-grpc-gateway-release",
    "NEST_GRPC_PUBLIC_HOST": "grpc.nest.example.org",
    "NEST_TAILSCALE_HOSTNAME": "nest-controller-release",
    "NEST_DATABASE_URL": "postgresql://nest:hunter2-not-printed@postgres:5432/nest",
    "NEST_PLATFORMS_ENV": "NEST_PLATFORMS=lab\\nNEST_PLATFORM_LAB_LOGIN_NODES=login.hpc.example\\n"
    "NEST_PLATFORM_LAB_ACCOUNT=acct-not-printed\\nNEST_PLATFORM_LAB_WORKDIR=/home/svc/nest-release",
    "NEST_LIMIT_ACTIVE_PER_PLATFORM": "2",
}

MAIN_CONTROLLER_ENV = {
    "NEST_PLATFORMS_ENV": "NEST_PLATFORMS=lab\\nNEST_PLATFORM_LAB_LOGIN_NODES=login.hpc.example\\n"
    "NEST_PLATFORM_LAB_ACCOUNT=acct-not-printed\\nNEST_PLATFORM_LAB_WORKDIR=/home/svc/nest-main",
    "NEST_DATABASE_PASSWORD": "never-printed",
}


def main_deployment(
    *,
    running=True,
    alias="nest-grpc-gateway-main",
    host="grpc.main.example.org",
    tailnet="nest-controller",
    controller_env=None,
):
    gateway = container(
        "main-grpc-gateway-1",
        "main",
        "grpc-gateway",
        running=running,
        env={"NEST_GRPC_EDGE_ALIAS": alias, "NEST_GRPC_PUBLIC_HOST": host},
        edge_aliases=["main-grpc-gateway-1", "grpc-gateway", alias],
        workdir="/etc/dokploy/compose/main/code",
    )
    controller = container(
        "main-controller-1",
        "main",
        "controller",
        running=running,
        env=controller_env or MAIN_CONTROLLER_ENV,
        edge_aliases=["main-controller-1", "controller"],
        workdir="/etc/dokploy/compose/main/code",
    )
    tailscale = container(
        "main-tailscale-1",
        "main",
        "tailscale",
        running=running,
        env={"TS_HOSTNAME": tailnet},
        workdir="/etc/dokploy/compose/main/code",
    )
    postgres = container(
        "main-postgres-1",
        "main",
        "postgres",
        running=running,
        workdir="/etc/dokploy/compose/main/code",
    )
    return [gateway, controller, tailscale, postgres]


class IsolationPreflightTests(unittest.TestCase):
    def preflight(
        self,
        containers,
        env=None,
        *,
        extra_networks=(),
        routes_dir=None,
        project="release",
        extra=(),
    ):
        env = dict(RELEASE_ENV if env is None else env)
        edge = edge_network(*[c for c in containers if EDGE in c["NetworkSettings"]["Networks"]])
        fake = FakeDocker(containers, [edge, *extra_networks])
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as empty, redirect_stdout(out):
            code = doctor.main(
                [
                    "preflight",
                    "--project",
                    project,
                    "--traefik-dynamic-dir",
                    routes_dir or empty,
                    *extra,
                ],
                docker=doctor.Docker(run=fake),
                environ=env,
            )
        text = out.getvalue()
        for secret in ("hunter2-not-printed", "acct-not-printed", "never-printed", "/home/svc"):
            self.assertNotIn(secret, text)
        mutating = [c for c in fake.calls if c[:2] in (["docker", "rm"], ["docker", "exec"])]
        self.assertFalse(mutating, "preflight must not change anything")
        checks = {}
        for line in text.splitlines():
            status, name, *_ = line.split()
            checks[name.rstrip(":")] = (status, line)
        return code, checks

    def test_distinct_deployments_pass(self):
        code, checks = self.preflight(main_deployment())
        for name in (
            "project_identity",
            "edge_alias",
            "public_host",
            "tailnet_hostname",
            "database_host",
            "hpc_workdir",
        ):
            self.assertEqual(checks[name][0], "PASS", checks[name][1])
        # The shared account is a warning with the capacity guidance, never a pass.
        self.assertEqual(checks["hpc_account"][0], "WARN")
        self.assertIn("limits add up", checks["hpc_account"][1])
        self.assertIn("main LAB", checks["hpc_account"][1])
        self.assertEqual(checks["route_target"][0], "WARN")  # not installed yet
        self.assertEqual(code, 1 if any(s == "FAIL" for s, _ in checks.values()) else 0)

    def test_default_alias_twice_is_the_118_collision(self):
        env = {**RELEASE_ENV}
        del env["NEST_GRPC_EDGE_ALIAS"]
        code, checks = self.preflight(main_deployment(alias="nest-grpc-gateway"), env)
        self.assertEqual(code, 1)
        self.assertEqual(checks["edge_alias"][0], "FAIL")
        self.assertIn(
            "nest-grpc-gateway on dokploy-network is also the gateway alias of main",
            checks["edge_alias"][1],
        )

    def test_gateway_from_before_the_setting_counts_with_its_alias(self):
        """An older gateway has no NEST_GRPC_EDGE_ALIAS: its edge alias decides."""
        deployment = main_deployment(alias="nest-grpc-gateway")
        deployment[0]["Config"]["Env"] = []
        env = {**RELEASE_ENV, "NEST_GRPC_EDGE_ALIAS": "nest-grpc-gateway"}
        _, checks = self.preflight(deployment, env)
        self.assertEqual(checks["edge_alias"][0], "FAIL")

    def test_a_stopped_deployment_warns(self):
        env = {**RELEASE_ENV, "NEST_GRPC_EDGE_ALIAS": "nest-grpc-gateway-main"}
        deployment = main_deployment(running=False)
        for c in deployment:
            c["NetworkSettings"]["Networks"] = {}
        _, checks = self.preflight(deployment, env)
        self.assertEqual(checks["edge_alias"][0], "WARN")
        self.assertIn("main (stopped)", checks["edge_alias"][1])

    def test_a_non_nest_container_with_the_alias_fails(self):
        stranger = container(
            "other-app-1", "other", "web", edge_aliases=["nest-grpc-gateway-release"]
        )
        _, checks = self.preflight([*main_deployment(), stranger])
        self.assertEqual(checks["edge_alias"][0], "FAIL")
        self.assertIn("other-app-1", checks["edge_alias"][1])

    def test_invalid_alias_fails(self):
        for alias in ("grpc-gateway", "Nest", "a.b"):
            with self.subTest(alias):
                _, checks = self.preflight(
                    main_deployment(), {**RELEASE_ENV, "NEST_GRPC_EDGE_ALIAS": alias}
                )
                self.assertEqual(checks["edge_alias"][0], "FAIL")

    def test_same_public_name_and_tailnet_hostname_fail(self):
        _, checks = self.preflight(
            main_deployment(host="grpc.nest.example.org", tailnet="nest-controller-release")
        )
        self.assertEqual(checks["public_host"][0], "FAIL")
        self.assertEqual(checks["tailnet_hostname"][0], "FAIL")
        self.assertIn("NEST_TAILSCALE_HOSTNAME", checks["tailnet_hostname"][1])

    def test_shared_workdir_fails_without_printing_it(self):
        shared = {**MAIN_CONTROLLER_ENV, "NEST_PLATFORMS_ENV": RELEASE_ENV["NEST_PLATFORMS_ENV"]}
        _, checks = self.preflight(main_deployment(controller_env=shared))
        self.assertEqual(checks["hpc_workdir"][0], "FAIL")
        self.assertIn("LAB = main LAB", checks["hpc_workdir"][1])

    def test_other_login_nodes_do_not_prove_another_cluster(self):
        """Review of #119 (lkw50197): one Slurm cluster has several login nodes on one
        filesystem and one accounting database. Main on login01 and Release on login04
        with the same workdir and account must not PASS."""
        ours = RELEASE_ENV["NEST_PLATFORMS_ENV"].replace(
            "login.hpc.example", "login04.cluster.example"
        )
        theirs = ours.replace("login04.cluster.example", "login01.cluster.example")
        env = {**RELEASE_ENV, "NEST_PLATFORMS_ENV": ours}
        _, checks = self.preflight(
            main_deployment(controller_env={"NEST_PLATFORMS_ENV": theirs}), env
        )
        self.assertEqual(checks["hpc_workdir"][0], "FAIL", checks["hpc_workdir"][1])
        self.assertIn("other login hosts, not proof of another cluster", checks["hpc_workdir"][1])
        self.assertEqual(checks["hpc_account"][0], "WARN", checks["hpc_account"][1])
        self.assertNotIn("login0", checks["hpc_workdir"][1] + checks["hpc_account"][1])

    def test_workdir_paths_are_normalized(self):
        """Review of #119 (P3): the same directory written differently overlaps."""
        for same in (
            "/home/svc//nest-release",
            "/home/svc/./nest-release",
            "/home/svc/x/../nest-release/",
        ):
            with self.subTest(same):
                self.assertTrue(doctor._overlapping_paths(same, "/home/svc/nest-release"))
        self.assertFalse(
            doctor._overlapping_paths("/home/svc/nest-release-old", "/home/svc/nest-release")
        )
        self.assertTrue(doctor._overlapping_paths("/", "/home/svc"))

    def test_nested_workdirs_overlap(self):
        nested = RELEASE_ENV["NEST_PLATFORMS_ENV"].replace(
            "/home/svc/nest-release", "/home/svc/nest-release/main"
        )
        _, checks = self.preflight(main_deployment(controller_env={"NEST_PLATFORMS_ENV": nested}))
        self.assertEqual(checks["hpc_workdir"][0], "FAIL", checks["hpc_workdir"][1])
        sibling = RELEASE_ENV["NEST_PLATFORMS_ENV"].replace(
            "/home/svc/nest-release", "/home/svc/nest-release-old"
        )
        _, checks = self.preflight(main_deployment(controller_env={"NEST_PLATFORMS_ENV": sibling}))
        self.assertEqual(checks["hpc_workdir"][0], "PASS", checks["hpc_workdir"][1])

    def test_separate_workdirs_and_accounts_pass(self):
        separate = (
            RELEASE_ENV["NEST_PLATFORMS_ENV"]
            .replace("/home/svc/nest-release", "/home/svc/nest-main")
            .replace("acct-not-printed", "other-acct-not-printed")
        )
        _, checks = self.preflight(main_deployment(controller_env={"NEST_PLATFORMS_ENV": separate}))
        self.assertEqual(checks["hpc_workdir"][0], "PASS")
        self.assertEqual(checks["hpc_account"][0], "PASS")

    def test_legacy_single_cluster_settings_are_compared(self):
        legacy = {
            "NEST_SLURM_HOST": "login.hpc.example",
            "NEST_SLURM_ACCOUNT": "a",
            "NEST_SLURM_WORKDIR": "/w/nest/",
        }
        env = {
            **RELEASE_ENV,
            "NEST_PLATFORMS_ENV": "",
            **legacy,
            "NEST_SLURM_WORKDIR": "/w/nest",
        }
        del env["NEST_LIMIT_ACTIVE_PER_PLATFORM"]
        _, checks = self.preflight(main_deployment(controller_env=legacy), env)
        self.assertEqual(checks["hpc_workdir"][0], "FAIL")
        self.assertEqual(checks["hpc_account"][0], "WARN")
        self.assertIn("NEST_LIMIT_ACTIVE_PER_PLATFORM is unset", checks["hpc_account"][1])

    def test_platforms_file_of_another_deployment_is_compared(self):
        """Review of #119: Compose's env_file puts a platforms.env file's Platforms into
        the container as plain variables; they must be compared, not ignored."""
        file_style = {
            "NEST_PLATFORMS": "lab",
            "NEST_PLATFORM_LAB_LOGIN_NODES": "login.hpc.example",
            "NEST_PLATFORM_LAB_ACCOUNT": "acct-not-printed",
            "NEST_PLATFORM_LAB_WORKDIR": "/home/svc/nest-release",
        }
        _, checks = self.preflight(main_deployment(controller_env=file_style))
        self.assertEqual(checks["hpc_workdir"][0], "FAIL", checks["hpc_workdir"][1])
        self.assertIn("LAB = main LAB", checks["hpc_workdir"][1])
        self.assertEqual(checks["hpc_account"][0], "WARN")

    def test_unreadable_targets_of_another_deployment_never_pass(self):
        _, checks = self.preflight(main_deployment(controller_env={"NEST_DATABASE_PASSWORD": "x"}))
        self.assertEqual(checks["hpc_workdir"][0], "WARN")
        self.assertIn("cannot read the HPC targets of main", checks["hpc_workdir"][1])
        self.assertEqual(checks["hpc_account"][0], "WARN")

    def test_own_platforms_file_is_compared(self):
        env = {**RELEASE_ENV, "NEST_PLATFORMS_ENV": ""}
        with tempfile.TemporaryDirectory() as checkout:
            env_file = Path(checkout) / ".env"
            env_file.write_text("".join(f"{k}='{v}'\n" for k, v in env.items() if v))
            (Path(checkout) / "platforms.env").write_text(
                "NEST_PLATFORMS=lab\n"
                "NEST_PLATFORM_LAB_LOGIN_NODES=login.hpc.example\n"
                "NEST_PLATFORM_LAB_ACCOUNT=acct-not-printed\n"
                "NEST_PLATFORM_LAB_WORKDIR=/home/svc/nest-main\n"
            )
            _, checks = self.preflight_file(main_deployment(), env_file)
            self.assertEqual(checks["hpc_workdir"][0], "FAIL", checks["hpc_workdir"][1])
            env_file.write_text(env_file.read_text() + "NEST_PLATFORMS_ENV_FILE='./missing.env'\n")
            _, checks = self.preflight_file(main_deployment(), env_file)
            self.assertEqual(checks["hpc_workdir"][0], "FAIL")
            self.assertIn("cannot read", checks["hpc_workdir"][1])

    def preflight_file(self, containers, env_file):
        edge = edge_network(*[c for c in containers if EDGE in c["NetworkSettings"]["Networks"]])
        fake = FakeDocker(containers, [edge])
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as empty, redirect_stdout(out):
            code = doctor.main(
                [
                    "preflight",
                    "--project",
                    "release",
                    "--env-file",
                    str(env_file),
                    "--traefik-dynamic-dir",
                    empty,
                ],
                docker=doctor.Docker(run=fake),
                environ={},
            )
        text = out.getvalue()
        for secret in ("hunter2-not-printed", "acct-not-printed", "/home/svc"):
            self.assertNotIn(secret, text)
        return code, {
            line.split()[1].rstrip(":"): (line.split()[0], line) for line in text.splitlines()
        }

    def test_shared_controller_image_tag_fails(self):
        """Review of #119 (lkw50197): an image tag is daemon-wide, so two deployments
        building the same NEST_CONTROLLER_IMAGE replace each other's revision."""

        def with_image(deployment, image, *, migrate_running=True):
            for c in deployment:
                if c["Config"]["Labels"]["com.docker.compose.service"] == "controller":
                    c["Config"]["Image"] = image
            migrate = container("main-migrate-1", "main", "migrate", running=migrate_running)
            migrate["Config"]["Image"] = image
            return [*deployment, migrate]

        env = {**RELEASE_ENV, "NEST_CONTROLLER_IMAGE": "nest-controller:release"}
        _, checks = self.preflight(with_image(main_deployment(), "nest-controller:release"), env)
        self.assertEqual(checks["controller_image"][0], "FAIL", checks["controller_image"][1])
        self.assertIn("is also the image of main", checks["controller_image"][1])
        # Both on today's default: the same collision.
        _, checks = self.preflight(
            with_image(main_deployment(), "nest-controller:local"), dict(RELEASE_ENV)
        )
        self.assertEqual(checks["controller_image"][0], "FAIL")
        # Only a stopped deployment's migrate container has it: a warning.
        stopped = with_image(
            main_deployment(running=False), "nest-controller:release", migrate_running=False
        )
        _, checks = self.preflight(stopped, env)
        self.assertEqual(checks["controller_image"][0], "WARN")
        # Distinct tags pass.
        _, checks = self.preflight(with_image(main_deployment(), "nest-controller:main"), env)
        self.assertEqual(checks["controller_image"][0], "PASS")
        pinned = {**env, "NEST_CONTROLLER_IMAGE": "registry.example/nest@sha256:" + "a" * 64}
        _, checks = self.preflight(
            with_image(main_deployment(), pinned["NEST_CONTROLLER_IMAGE"]), pinned
        )
        # Review of #119 (P3): compose.dokploy.yaml builds the image, so a digest
        # reference cannot be its tag; refused rather than passed.
        self.assertEqual(checks["controller_image"][0], "FAIL")
        self.assertIn("cannot tag a build with a digest", checks["controller_image"][1])

    def test_controller_and_migration_revision_are_compared(self):
        controller = container("release-controller-1", "release", "controller")
        migrate = container("release-migrate-1", "release", "migrate", running=False)
        controller["Image"] = migrate["Image"] = "sha256:" + "1" * 64
        _, checks = self.preflight([controller, migrate, *main_deployment()])
        self.assertEqual(checks["controller_revision"][0], "PASS")
        migrate["Image"] = "sha256:" + "2" * 64
        _, checks = self.preflight([controller, migrate, *main_deployment()])
        self.assertEqual(checks["controller_revision"][0], "WARN")
        self.assertIn("111111111111", checks["controller_revision"][1])

    def test_shared_project_name_fails(self):
        """Two checkouts deploying under one Compose project share everything."""
        mine = container("release-controller-1", "release", "controller", workdir="/a")
        twin = container("release-postgres-1", "release", "postgres", workdir="/b")
        _, checks = self.preflight([mine, twin, *main_deployment()])
        self.assertEqual(checks["project_identity"][0], "FAIL")
        _, checks = self.preflight(main_deployment(), project="nest")
        self.assertEqual(checks["project_identity"][0], "WARN")

    def test_database_host_on_the_edge_network_fails(self):
        exposed = container("other-postgres-1", "other", "db", edge_aliases=["postgres"])
        _, checks = self.preflight([*main_deployment(), exposed])
        self.assertEqual(checks["database_host"][0], "FAIL")
        self.assertIn("other-postgres-1", checks["database_host"][1])
        env = {
            **RELEASE_ENV,
            "NEST_DATABASE_URL": "postgresql://nest@db.internal.example:5432/nest",
        }
        _, checks = self.preflight([*main_deployment(), exposed], env)
        self.assertEqual(checks["database_host"][0], "PASS")

    def test_overlapping_origin_subnet_names_the_other_deployment(self):
        other = {
            "Name": "main_grpc-origin",
            "Id": uuid.uuid4().hex,
            "IPAM": {"Config": [{"Subnet": "10.253.251.0/28"}]},
        }
        code, checks = self.preflight(main_deployment(), extra_networks=[other])
        self.assertEqual(code, 1)
        self.assertEqual(checks["origin_subnet_free"][0], "FAIL")
        self.assertIn("the origin network of deployment main", checks["origin_subnet_free"][1])

    def test_route_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "nest-grpc-main.yml").write_text(
                render("grpc.main.example.org", "v3.6.2", "--instance", "main").stdout
            )
            (path / "nest-grpc-release.yml").write_text(
                render("grpc.nest.example.org", "v3.6.2", "--instance", "release").stdout
            )
            _, checks = self.preflight(main_deployment(), routes_dir=directory)
            for name in ("route_names", "route_sni", "route_target"):
                self.assertEqual(checks[name][0], "PASS", checks[name][1])
            # The Release route rendered for Main's alias: Traefik would cross over.
            (path / "nest-grpc-release.yml").write_text(
                render(
                    "grpc.nest.example.org",
                    "v3.6.2",
                    "--instance",
                    "release",
                    "--edge-alias",
                    "nest-grpc-gateway-main",
                ).stdout
            )
            _, checks = self.preflight(main_deployment(), routes_dir=directory)
            self.assertEqual(checks["route_target"][0], "FAIL")
            self.assertIn("dials nest-grpc-gateway-main:8443", checks["route_target"][1])
            # The same name routed by two files.
            (path / "nest-grpc-release.yml").write_text(
                render("grpc.main.example.org", "v3.6.2", "--instance", "release").stdout
            )
            _, checks = self.preflight(main_deployment(), routes_dir=directory)
            self.assertEqual(checks["route_sni"][0], "FAIL")

    def test_changing_alias_or_name_under_an_installed_route_fails(self):
        """Review of #119: Main's Phase 2 values on today's staging application would
        orphan its installed staging route; preflight must refuse, not WARN."""
        staging = container(
            "release-grpc-gateway-1",
            "release",
            "grpc-gateway",
            env={
                "NEST_GRPC_EDGE_ALIAS": "nest-grpc-gateway",
                "NEST_GRPC_PUBLIC_HOST": "grpc.staging.example.org",
            },
            edge_aliases=["grpc-gateway", "nest-grpc-gateway"],
        )
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "nest-grpc.yml").write_text(
                render("grpc.staging.example.org", "v3.6.2").stdout
            )
            phase2 = {
                **RELEASE_ENV,
                "NEST_GRPC_EDGE_ALIAS": "nest-grpc-gateway-release",
                "NEST_GRPC_PUBLIC_HOST": "grpc.nest.example.org",
            }
            code, checks = self.preflight(
                [staging, *main_deployment()], phase2, routes_dir=directory
            )
            self.assertEqual(code, 1)
            self.assertEqual(checks["route_change"][0], "FAIL", checks["route_change"][1])
            self.assertIn("nest-grpc.yml", checks["route_change"][1])
            # Only the name changes: still refused.
            name_only = {**phase2, "NEST_GRPC_EDGE_ALIAS": "nest-grpc-gateway"}
            _, checks = self.preflight(
                [staging, *main_deployment()], name_only, routes_dir=directory
            )
            self.assertEqual(checks["route_change"][0], "FAIL")
            # Phase 1: the current values.
            phase1 = {
                **phase2,
                "NEST_GRPC_EDGE_ALIAS": "nest-grpc-gateway",
                "NEST_GRPC_PUBLIC_HOST": "grpc.staging.example.org",
            }
            _, checks = self.preflight([staging, *main_deployment()], phase1, routes_dir=directory)
            self.assertEqual(checks["route_change"][0], "PASS")
            # A recorded retirement of that file is a warning.
            _, checks = self.preflight(
                [staging, *main_deployment()],
                phase2,
                routes_dir=directory,
                extra=["--retire-route", "nest-grpc.yml"],
            )
            self.assertEqual(checks["route_change"][0], "WARN")

    def test_unreadable_route_directory_is_skipped_not_passed(self):
        _, checks = self.preflight(main_deployment(), routes_dir="/nonexistent/traefik/dynamic")
        self.assertEqual(checks["traefik_routes"][0], "SKIP")


class EdgeAliasRuntimeTests(unittest.TestCase):
    ORIGIN = "release_grpc-origin"

    def gateway(self, alias="nest-grpc-gateway-release", names=None):
        found = container(
            "release-grpc-gateway-1",
            "release",
            "grpc-gateway",
            env={"NEST_GRPC_EDGE_ALIAS": alias},
            edge_aliases=names if names is not None else ["grpc-gateway", alias],
        )
        found["NetworkSettings"]["Networks"][self.ORIGIN] = {"IPAddress": "10.253.251.3"}
        return found

    def check(self, gateway, *others):
        fake = FakeDocker([gateway, *others], [edge_network(gateway, *others)])
        return doctor.edge_alias_runtime(doctor.Docker(run=fake), gateway, self.ORIGIN)

    def test_unique_alias_passes(self):
        main = main_deployment()[0]
        result = self.check(self.gateway(), main)
        self.assertEqual(result.status, "PASS", result.detail)

    def test_alias_shared_on_the_edge_fails(self):
        main = main_deployment(alias="nest-grpc-gateway-release")[0]
        result = self.check(self.gateway(), main)
        self.assertEqual(result.status, "FAIL")
        self.assertIn("main-grpc-gateway-1", result.detail)

    def test_gateway_without_its_alias_fails(self):
        result = self.check(self.gateway(names=["grpc-gateway"]))
        self.assertEqual(result.status, "FAIL")
        self.assertIn("does not answer", result.detail)

    def test_service_name_alias_fails(self):
        result = self.check(self.gateway(alias="grpc-gateway"))
        self.assertEqual(result.status, "FAIL")


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MonitoringEnvironmentTests(unittest.TestCase):
    """#114's collectors and alerts with `--environment` (#118): each deployment's
    text files, state and rules stand side by side and never read the other's."""

    @classmethod
    def setUpClass(cls):
        cls.monitor = _load("nest_monitor", ROOT / "scripts" / "nest-monitor.py")
        cls.alerts = _load("nest_alerts", ROOT / "deploy" / "monitoring" / "nest_alerts.py")

    def metrics(self):
        metrics = self.monitor.Metrics()
        metrics.add(
            "nest_gateway_requests_total", "counter", "Requests.", 3, route="service", code="0"
        )
        metrics.add("nest_monitor_collector_success", "gauge", "OK.", 1)
        return metrics

    def test_default_output_is_unchanged(self):
        text = self.metrics().render()
        self.assertNotIn("environment", text)
        committed = (
            ROOT / "deploy" / "monitoring" / "prometheus" / "nest-alerts.rules.yml"
        ).read_text()
        self.assertEqual(self.alerts.prometheus(), committed)

    def test_each_environment_has_its_own_files_and_label(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            for name in ("main", "release"):
                self.monitor.write_textfile(path, "gateway", self.metrics(), name)
                self.monitor.save_state(path, "gateway", {"cursor": name}, name)
            self.monitor.write_textfile(path, "gateway", self.metrics())
            self.assertEqual(
                sorted(p.name for p in path.glob("*.prom")),
                ["nest_gateway.prom", "nest_main_gateway.prom", "nest_release_gateway.prom"],
            )
            release = (path / "nest_release_gateway.prom").read_text()
            self.assertIn(
                'nest_gateway_requests_total{code="0",environment="release",route="service"} 3',
                release,
            )
            self.assertIn('nest_monitor_collector_success{environment="release"} 1', release)
            self.assertNotIn("main", release)
            for name in ("main", "release"):
                self.assertEqual(self.monitor.load_state(path, "gateway", name), {"cursor": name})

    def test_unbounded_environment_names_are_refused(self):
        for bad in ("Main", "a-b", "a_b", "release01x", "", 'a"b'):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    self.metrics().render(bad)
                with self.assertRaises(ValueError):
                    self.alerts.prometheus(environment=bad)
        with tempfile.TemporaryDirectory() as directory:
            code = self.monitor.main(
                [
                    "gateway",
                    "--project",
                    "p",
                    "--textfile-dir",
                    directory,
                    "--state-dir",
                    directory,
                    "--environment",
                    "Release",
                ]
            )
        self.assertEqual(code, 2)

    def test_rules_are_scoped_to_one_environment(self):
        for name in ("main", "release"):
            rules = self.alerts.prometheus(environment=name).replace('\\"', '"')
            selectors = re.findall(r"nest_[a-z_]+(\{[^}]*\})", rules)
            self.assertTrue(selectors)
            for found in selectors:
                self.assertTrue(found.startswith(f'{{job="nest",environment="{name}"'), found)
            self.assertIn(f"name: nest-hosted-pilot-{name}", rules)
        main = self.alerts.grafana("u", "c", environment="main")["groups"][0]
        release = self.alerts.grafana("u", "c", environment="release")["groups"][0]
        default = self.alerts.grafana("u", "c")["groups"][0]
        uids = [r["uid"] for g in (main, release, default) for r in g["rules"]]
        self.assertEqual(len(uids), len(set(uids)))
        self.assertTrue(all(len(uid) <= 40 for uid in uids))
        self.assertEqual({main["name"], release["name"], default["name"]}.__len__(), 3)
        for rule in release["rules"]:
            self.assertEqual(rule["labels"]["environment"], "release")
            self.assertIn('environment="release"', rule["data"][0]["model"]["expr"])
            self.assertTrue(rule["title"].endswith("(release)"))


class MainClientConfigTests(unittest.TestCase):
    """The settings a Main developer uses (docs/source/multi-environment.md): an
    explicit endpoint with verified TLS and Main's own sign-in origin. The SDK never
    attaches the Release (hosted) session to Main, and never picks Main by default."""

    MAIN: ClassVar[dict[str, str]] = {
        "NEST_CONTROLLER_ADDRESS": "grpc.main.nest.cerulean.works:443",
        "NEST_CONTROLLER_TLS": "true",
        "NEST_AUTH_URL": "https://main.nest.cerulean.works",
    }

    def load(self, **env):
        from nest.config import load_client_config

        return load_client_config({k: v for k, v in env.items() if v is not None})

    def test_main_settings_load_with_their_own_origin(self):
        config = self.load(**self.MAIN)
        self.assertEqual(config.address, "grpc.main.nest.cerulean.works:443")
        self.assertTrue(config.tls)
        self.assertIsNone(config.tls_ca_file)  # system roots, verified
        self.assertEqual(config.auth_url, "https://main.nest.cerulean.works")
        self.assertEqual(config.profile, "custom")

    def test_main_endpoint_never_receives_the_hosted_session(self):
        from nest.config import HOSTED_AUTH_URL, ConfigError

        with self.assertRaises(ConfigError) as caught:
            self.load(**{**self.MAIN, "NEST_AUTH_URL": HOSTED_AUTH_URL})
        self.assertIn("NEST_HOSTED_SESSION_ADDRESS", str(caught.exception))
        # Without an origin, Main is called without any session at all.
        self.assertIsNone(self.load(**{**self.MAIN, "NEST_AUTH_URL": None}).auth_url)

    def test_the_release_gateway_refuses_mains_origin(self):
        from nest.config import HOSTED_GRPC_ADDRESS, ConfigError

        with self.assertRaises(ConfigError):
            self.load(
                NEST_CONTROLLER_ADDRESS=HOSTED_GRPC_ADDRESS,
                NEST_CONTROLLER_TLS="true",
                NEST_AUTH_URL="https://main.nest.cerulean.works",
            )

    def test_defaults_never_name_main(self):
        from nest import config

        self.assertFalse(config.HOSTED_PROFILE_DEFAULT)
        for value in (
            config.HOSTED_AUTH_URL,
            config.HOSTED_GRPC_ADDRESS,
            config.HOSTED_CONTROLLER_ADDRESS,
            *config.HOSTED_CONTROLLER_ALIASES,
        ):
            self.assertNotIn("main.", value)

    def test_sessions_are_kept_per_sign_in_origin(self):
        from nest.config import load_cli_auth_config

        with tempfile.TemporaryDirectory() as home:
            main = load_cli_auth_config(
                {"NEST_AUTH_URL": "https://main.nest.cerulean.works", "XDG_CONFIG_HOME": home}
            )
            release = load_cli_auth_config({"XDG_CONFIG_HOME": home})
        self.assertEqual(main.url, "https://main.nest.cerulean.works")
        self.assertNotEqual(main.url, release.url)


if __name__ == "__main__":
    unittest.main()


# --- Option B/C: copying Main's database into Release (review of #119) ---------------

try:
    from postgres_fixture import connect as _pg_connect
    from test_executions import _Base as _RegistryBase
except ImportError:  # the Controller image has no psycopg test fixtures
    _RegistryBase = None

if _RegistryBase is not None:

    class ReleaseCopyScriptTests(_RegistryBase):
        """deploy/environments/release-copy-{check,revoke}.sql on a restored copy that
        still holds Main's live attempt, invitations, sessions and service trust."""

        CHECK = (ENVIRONMENTS / "release-copy-check.sql").read_text()
        REVOKE = (ENVIRONMENTS / "release-copy-revoke.sql").read_text()

        def seed_main_credentials(self):
            token = lambda: os.urandom(32)
            with _pg_connect(self.url) as conn:
                conn.execute(
                    "INSERT INTO project_invite_links (project_id, role, created_by_user_id, "
                    "token_hash, expires_at, max_uses) VALUES (%s, 'member', %s, %s, "
                    "now() + interval '7 days', 5)",
                    (self.project, self.owner, token()),
                )
                conn.execute(
                    "INSERT INTO project_email_invitations (project_id, invited_email_normalized, "
                    "role, invited_by_user_id, token_hash, expires_at) VALUES "
                    "(%s, 'dev@example.org', 'member', %s, %s, now() + interval '7 days')",
                    (self.project, self.owner, token()),
                )
                family = uuid.uuid4()
                conn.execute(
                    "INSERT INTO user_sessions (user_id, kind, token_hash, expires_at, family_id, "
                    "family_expires_at) VALUES (%s, 'cli', %s, now() + interval '1 day', %s, "
                    "now() + interval '30 days')",
                    (self.owner, token(), family),
                )
                conn.execute(
                    "INSERT INTO email_outbox (message_key, kind, recipient, subject, body_text, "
                    "message_id, max_attempts) VALUES ('invitation:main-1', 'invitation', "
                    "'dev@example.org', 'Join p', 'https://main.example/invite/secret', "
                    "'<main-1@example.org>', 5)"
                )
            from nest.db import ServicePrincipalStore

            services = ServicePrincipalStore(self.db)
            principal = services.create_principal("ci-main", operator="ops", reason="test")
            services.add_trust_policy(
                principal.id,
                audience="nest-main",
                repository="example/app",
                repository_id="1",
                repository_owner_id="2",
                refs=["refs/heads/main"],
                workflows=[".github/workflows/ci.yml"],
                operator="ops",
                reason="test",
            )
            services.grant(
                principal.id,
                project_id=self.project,
                capabilities=["project.read"],
                operator="ops",
                reason="test",
            )

        def remaining(self) -> dict[str, int]:
            with _pg_connect(self.url) as conn:
                rows = conn.execute(self.CHECK.split("$$;", 1)[1]).fetchall()
            return {row[0]: row[1] for row in rows}

        def run_check(self):
            with _pg_connect(self.url) as conn:
                conn.execute(self.CHECK.split("$$;", 1)[0] + "$$;")

        def test_live_work_from_main_blocks_the_copy(self):
            import psycopg

            execution, _ = self.register()
            attempt = self.begin(execution.execution_id)
            self.assertEqual(attempt.status, "submitting")
            with self.assertRaises(psycopg.errors.RaiseException) as caught:
                self.run_check()
            self.assertIn("1 live attempt(s)", str(caught.exception))
            # Quiesced: the attempt ended on Main before the dump.
            self.registry.finish_attempt(attempt.id, "failed", error_kind="ssh_unreachable")
            self.run_check()

        def test_revoke_leaves_nothing_from_main_usable(self):
            self.seed_main_credentials()
            before = self.remaining()
            seeded = set(before) - {"service sessions unrevoked"}
            self.assertEqual(before["emails unsent"], 1)
            self.assertTrue(all(before[item] >= 1 for item in seeded), before)
            with _pg_connect(self.url) as conn:
                conn.execute("SELECT set_config('nest.revoker', %s, false)", (str(self.owner),))
                conn.execute("SELECT set_config('nest.operator', 'ops-test', false)")
                conn.execute(self.REVOKE)
                audited = conn.execute(
                    "SELECT count(*) FROM security_audit_events "
                    "WHERE actor_operator = 'ops-test' AND interface = 'database'"
                ).fetchone()[0]
            self.assertGreaterEqual(audited, 3)
            self.assertEqual(set(self.remaining().values()), {0}, self.remaining())
            self.run_check()

        def test_the_runbook_command_line_revokes(self):
            """Review of #119: step 4's exact psql invocation, with `-f -` reading the
            script from standard input after the two `-c` settings."""
            psql = shutil.which("psql")
            if psql is None:
                self.skipTest("needs the psql client")
            doc = (ROOT / "docs" / "source" / "multi-environment.md").read_text()
            block = doc[doc.index("4. **Revoke what Main issued:**") :]
            block = block[
                block.index("```bash") + len("```bash") : block.index(
                    "```", block.index("```bash") + 7
                )
            ]
            command = " ".join(
                line.strip().rstrip("\\").strip() for line in block.strip().splitlines()
            )
            prefix = "dc exec -T postgres psql -v ON_ERROR_STOP=1 -U nest -d nest "
            self.assertTrue(command.startswith(prefix), command)
            rest = command[len(prefix) :]
            self.assertIn(" -f - < deploy/environments/release-copy-revoke.sql", rest)
            rest = rest.replace("<Release platform admin User ID>", str(self.owner))
            rest = rest.replace("<your name>", "ops-test")
            self.seed_main_credentials()
            result = subprocess.run(
                f"{psql} -v ON_ERROR_STOP=1 -d '{self.url}' {rest}",
                shell=True,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(self.remaining().values()), {0}, self.remaining())

        def test_revoke_needs_the_revoker(self):
            import psycopg

            self.seed_main_credentials()
            with _pg_connect(self.url) as conn, self.assertRaises(psycopg.Error):
                conn.execute(self.REVOKE)
            # One transaction: nothing was revoked.
            self.assertGreaterEqual(self.remaining()["user sessions unrevoked"], 1)
