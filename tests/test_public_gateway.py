"""H08: the public gRPC gateway and the Controller's bounded-pilot profile.

Four parts:

- The gateway's rendered Envoy configuration (`deploy/public-grpc/`) and its
  Traefik route keep the H06 transport contract: SNI, TLS 1.2+, ALPN h2, PROXY
  protocol only from the edge proxy, the path allowlist, no retries, no upstream
  PINGs, the idle and deadline bounds, connection and request limits, and access
  logs without headers. The renderer refuses unsafe or malformed values.
- `NEST_PUBLIC_PILOT=true` fails closed unless every pilot requirement holds.
- Audit records of gRPC calls carry the client address the gateway named.
- With an Envoy binary (`NEST_TEST_ENVOY`, or `envoy` on PATH; required when
  `NEST_TEST_ENVOY_REQUIRED=1`, as in CI), the real gateway runs in front of the
  real Controller, PostgreSQL and the fake login node, behind a local stand-in for
  Traefik's PROXY protocol passthrough. The SDK's hosted profile then signs,
  selects and runs through it, and TLS, authentication, revocation, origin bypass,
  rate limits, timeouts and the absence of duplicate submissions are checked
  end to end. No real host, DNS name, certificate authority or Cloudflare zone is
  contacted.
"""

import importlib.util
import json
import os
import re
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import types
import unittest
import urllib.request
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from ipaddress import ip_network
from pathlib import Path
from typing import ClassVar, cast
from unittest import mock

import cloudpickle
import grpc
from hosted_fixture import CertificateAuthority
from test_registry_admission import _Base, browser

from nest import config as nest_config
from nest.audit import AuditLog, audit_interface
from nest.client import execute_remote
from nest.config import (
    PUBLIC_GATEWAY_IDLE_TIMEOUT,
    PUBLIC_PILOT_REQUIREMENTS,
    ConfigError,
    ControllerConfig,
    RpcPolicy,
    load_controller_config,
)
from nest.controller import ExecutionService, build_server, record_start
from nest.errors import RemoteAccessDeniedError, RpcTimeoutError
from nest.login import CredentialStore, NestSession
from nest.proto import execution_pb2, execution_pb2_grpc
from nest.rpc_guard import RpcGuard

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "deploy" / "public-grpc"
RESOURCES = types.SimpleNamespace(gpu=0, cpu=0)
CLIENT = "203.0.113.7"
"""The client address the stand-in edge proxy announces by PROXY protocol."""


# A lambda is pickled by value: the worker on the (fake) cluster cannot import tests.
answer_42 = lambda: 42


def render(out: Path, **env) -> subprocess.CompletedProcess:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    return subprocess.run(
        ["/bin/sh", str(GATEWAY / "gateway-entrypoint.sh"), "--render", str(out)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


class GatewayConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def rendered(self, **env) -> str:
        result = render(self.out, NEST_GRPC_EDGE_PROXY="10.0.1.7/32", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.out / "envoy.yaml").read_text()

    def test_contract_of_the_rendered_configuration(self):
        text = self.rendered()
        listener, clusters = text.split("\n  clusters:\n")
        # TLS for the one public name, h2 only, TLS 1.2 or later, reloadable certificate.
        self.assertIn('server_names: ["grpc.nest.cerulean.works"]', text)
        self.assertIn('alpn_protocols: ["h2"]', text)
        self.assertIn("tls_minimum_protocol_version: TLSv1_2", text)
        self.assertIn(f"path: {self.out}/certificate-sds.yaml", text)
        sds = (self.out / "certificate-sds.yaml").read_text()
        self.assertIn("watched_directory: { path: /certs }", sds)
        # PROXY protocol is trusted only from the edge proxy's own address.
        self.assertIn("envoy.filters.listener.proxy_protocol", text)
        self.assertIn('direct_remote_ip: { address_prefix: "10.0.1.7", prefix_len: 32 }', text)
        self.assertIn("use_remote_address: true", text)
        self.assertIn("xff_num_trusted_hops: 0", text)
        # Only the Nest service; everything else is 404.
        routes = [line.strip() for line in text.splitlines() if "match: { prefix:" in line]
        self.assertEqual(
            routes,
            [
                '- match: { prefix: "/nest.ExecutionService/", grpc: {} }',
                '- match: { prefix: "/" }',
            ],
        )
        self.assertIn("direct_response: { status: 404 }", text)
        # Deadlines and idle bounds match the Controller's pilot contract.
        self.assertIn(f"stream_idle_timeout: {PUBLIC_GATEWAY_IDLE_TIMEOUT:g}s", text)
        self.assertIn(f"grpc_timeout_header_max: {PUBLIC_GATEWAY_IDLE_TIMEOUT:g}s", text)
        self.assertIn("timeout: 0s", text)
        # Admission limits at the gateway.
        for needle in (
            "max_active_downstream_connections: 1024",
            "max_connections: 512",
            "envoy.filters.network.local_ratelimit",
            "envoy.filters.http.local_ratelimit",
            "rate_limited_as_resource_exhausted: true",
            "max_concurrent_streams: 16",
            "max_request_headers_kb: 16",
            "request_headers_timeout: 10s",
        ):
            self.assertIn(needle, text)
        # Never retried, never PINGs the Controller, admin only on loopback.
        self.assertNotIn("retry_policy", text)
        self.assertIn("max_retries: 0", clusters)
        self.assertNotIn("connection_keepalive", clusters)
        self.assertIn("connection_keepalive", listener)
        self.assertIn("address: 127.0.0.1, port_value: 9901", text)
        self.assertIn('socket_address: { address: "controller", port_value: 50051 }', clusters)
        # The access log names no header except the path.
        log = next(line for line in text.splitlines() if "inline_string" in line)
        self.assertEqual(log.count("%REQ("), 1)
        self.assertIn("%REQ(:PATH)%", log)

    def test_values_are_substituted(self):
        text = self.rendered(
            NEST_GRPC_PUBLIC_HOST="grpc.staging.example.org",
            NEST_GRPC_UPSTREAM="nest-controller:50052",
            NEST_GRPC_LISTEN_PORT="9443",
            NEST_GRPC_CERT_DIR="/run/certs",
        )
        self.assertIn('server_names: ["grpc.staging.example.org"]', text)
        self.assertIn('address: "nest-controller", port_value: 50052', text)
        self.assertIn("port_value: 9443", text)
        self.assertIn("/run/certs/fullchain.pem", (self.out / "certificate-sds.yaml").read_text())
        self.assertNotRegex(text, r"@[A-Z][A-Z_]*@")

    def test_unsafe_or_malformed_values_are_refused(self):
        cases = {
            "unset edge": {},
            "default route": {"NEST_GRPC_EDGE_PROXY": "0.0.0.0/0"},
            "wide network": {"NEST_GRPC_EDGE_PROXY": "10.0.0.0/7"},
            "no prefix": {"NEST_GRPC_EDGE_PROXY": "10.0.1.7"},
            "not an address": {"NEST_GRPC_EDGE_PROXY": "traefik/32"},
            "injected name": {
                "NEST_GRPC_EDGE_PROXY": "10.0.1.7/32",
                "NEST_GRPC_PUBLIC_HOST": 'x"]\nadmin: {',
            },
            "bad upstream": {"NEST_GRPC_EDGE_PROXY": "10.0.1.7/32", "NEST_GRPC_UPSTREAM": "a|b:1"},
            "bad port": {"NEST_GRPC_EDGE_PROXY": "10.0.1.7/32", "NEST_GRPC_LISTEN_PORT": "70000"},
            "relative certs": {
                "NEST_GRPC_EDGE_PROXY": "10.0.1.7/32",
                "NEST_GRPC_CERT_DIR": "certs",
            },
        }
        for label, env in cases.items():
            with self.subTest(label):
                result = render(self.out / label.replace(" ", "-"), **env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.out / label.replace(" ", "-") / "envoy.yaml").exists())

    @unittest.skipUnless(
        os.environ.get("NEST_TEST_ENVOY", "").strip() or shutil.which("envoy"),
        "set NEST_TEST_ENVOY to an Envoy binary to validate the configuration",
    )
    def test_envoy_accepts_the_production_configuration(self):
        certs = self.out / "certs"
        certs.mkdir()
        chain, key = CertificateAuthority().issue("grpc.nest.cerulean.works")
        (certs / "fullchain.pem").write_bytes(chain)
        (certs / "privkey.pem").write_bytes(key)
        self.rendered(NEST_GRPC_CERT_DIR=str(certs))
        envoy = os.environ.get("NEST_TEST_ENVOY", "").strip() or shutil.which("envoy")
        result = subprocess.run(
            [str(envoy), "--mode", "validate", "-c", str(self.out / "envoy.yaml")],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ci_tests_the_envoy_that_compose_deploys(self):
        compose = (ROOT / "compose.dokploy.yaml").read_text()
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text()
        deployed = re.search(r"NEST_ENVOY_IMAGE:-(envoyproxy/envoy:v[0-9.]+)", compose)
        assert deployed is not None
        self.assertIn(f"docker create --name envoy-binary {deployed.group(1)}", workflow)
        self.assertIn('echo "NEST_TEST_ENVOY_REQUIRED=1"', workflow)

    def test_traefik_route_passes_tls_through_with_proxy_protocol(self):
        text = (GATEWAY / "traefik" / "nest-grpc.yml").read_text()
        self.assertIn("rule: HostSNI(`grpc.nest.cerulean.works`)", text)
        self.assertIn("passthrough: true", text)
        self.assertIn("- address: nest-grpc-gateway:8443", text)
        self.assertRegex(text, r"proxyProtocol:\n\s+version: 2")
        self.assertIn("- websecure", text)

    def test_certificate_renewal_defaults_to_acme_staging_and_fails_closed(self):
        script = (GATEWAY / "renew-certificate.sh").read_text()
        self.assertIn("acme-staging-v02.api.letsencrypt.org", script)
        self.assertIn('NEST_GRPC_ACME_ACCEPT_TOS:-}" = "true"', script)
        result = subprocess.run(
            ["/bin/sh", str(GATEWAY / "renew-certificate.sh")],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("terms of service", result.stderr)


class PublicPilotConfigTests(unittest.TestCase):
    PILOT: ClassVar[dict[str, str]] = {
        "NEST_PUBLIC_PILOT": "true",
        "NEST_EXECUTOR": "slurm",
        "NEST_SLURM_HOST": "login",
        "NEST_SLURM_ACCOUNT": "acct",
        "NEST_SLURM_WORKDIR": "/w",
        "NEST_SLURM_SSH_USER": "op",
        "NEST_DATABASE_URL": "postgresql://nest@db.example.internal:5432/nest",
        "NEST_CONTROLLER_BIND": "0.0.0.0:50051",
        "NEST_CONTROLLER_AUTH": "session",
        "NEST_CONTROLLER_ALLOW_PLAINTEXT": "true",
        "NEST_EXECUTION_REGISTRY": "required",
        "NEST_LIMIT_BATCH_SCRIPTS": "refuse",
        "NEST_GRPC_ALLOWED_PEERS": "172.31.250.0/28,100.64.0.0/10",
        "NEST_GRPC_TRUSTED_PROXIES": "172.31.250.2/32",
        "NEST_ENVIRONMENT_TIMEOUT": "540",
        "NEST_RESULT_TIMEOUT": "600",
    }

    def problems(self, **changes) -> str:
        env = {**self.PILOT, **changes}
        env = {k: v for k, v in env.items() if v is not None}
        with self.assertRaises(ConfigError) as caught:
            load_controller_config(env)
        return str(caught.exception)

    def test_a_complete_pilot_configuration_loads(self):
        config = load_controller_config(self.PILOT)
        self.assertTrue(config.public_pilot)
        self.assertTrue(config.summary()["public_pilot"])
        self.assertFalse(load_controller_config({}).public_pilot)

    def test_each_requirement_is_enforced(self):
        cases = {
            "NEST_CONTROLLER_AUTH=session": {
                "NEST_CONTROLLER_AUTH": "none",
                "NEST_EXECUTION_REGISTRY": None,
            },
            "NEST_EXECUTION_REGISTRY=required": {"NEST_EXECUTION_REGISTRY": "legacy"},
            "NEST_LIMIT_BATCH_SCRIPTS=refuse": {"NEST_LIMIT_BATCH_SCRIPTS": "trusted"},
            "NEST_GRPC_ALLOWED_PEERS (the gateway": {"NEST_GRPC_ALLOWED_PEERS": None},
            "without a default route": {"NEST_GRPC_ALLOWED_PEERS": "0.0.0.0/0"},
            "NEST_GRPC_TRUSTED_PROXIES (the gateway": {"NEST_GRPC_TRUSTED_PROXIES": None},
            "inside NEST_GRPC_ALLOWED_PEERS": {"NEST_GRPC_TRUSTED_PROXIES": "10.9.9.9/32"},
            "RATE_LIMIT_PER_USER above 0": {"NEST_GRPC_RATE_LIMIT_PER_USER": "0"},
            "RATE_LIMIT_PER_PEER and": {"NEST_GRPC_RATE_LIMIT_PER_PEER": "0"},
            "the gateway's message limit": {"NEST_GRPC_MAX_MESSAGE_BYTES": str(8 * 1024 * 1024)},
            "not '*'": {
                "NEST_PROVIDER_APPROVALS": "environment",
                "NEST_PROVIDER_ALLOWED_PROJECTS": "*",
            },
            "the gateway's idle timeout": {"NEST_ENVIRONMENT_TIMEOUT": None},
        }
        for expected, changes in cases.items():
            with self.subTest(expected):
                self.assertIn(expected, self.problems(**changes))
        self.assertIn("NEST_EXECUTOR=slurm", self.problems(NEST_EXECUTOR="local"))
        self.assertEqual(len(PUBLIC_PILOT_REQUIREMENTS), 11)

    def test_tls_or_an_explicit_private_plaintext_is_required(self):
        problems = self.problems(NEST_CONTROLLER_ALLOW_PLAINTEXT=None)
        self.assertIn("ALLOW_PLAINTEXT", problems)

    def test_the_start_is_audited(self):
        config = load_controller_config(self.PILOT)
        with self.assertLogs("nest.audit", "INFO") as logs:
            record_start(config, None)
        self.assertIn('"public_pilot": true', "\n".join(logs.output))


class AuditClientAddressTests(unittest.TestCase):
    def test_grpc_events_record_the_client_address(self):
        from nest._logging import client_context

        with self.assertLogs("nest.audit", "INFO") as logs:
            with audit_interface("grpc"), client_context("203.0.113.9"):
                AuditLog(None).record("run.submit", "denied", reason="role")
            with audit_interface("http"), client_context("203.0.113.9"):
                AuditLog(None).record("project.read", "denied", reason="role")
            with audit_interface("grpc"):
                AuditLog(None).record("run.submit", "denied", reason="role")
        grpc_line, http_line, outside = logs.output
        self.assertIn('"client_address": "203.0.113.9"', grpc_line)
        self.assertNotIn("client_address", http_line)
        self.assertNotIn("client_address", outside)


ENVOY = os.environ.get("NEST_TEST_ENVOY", "").strip() or shutil.which("envoy")
ENVOY_REQUIRED = os.environ.get("NEST_TEST_ENVOY_REQUIRED", "").strip() == "1"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _proxy_v2_header(source: str, source_port: int, destination_port: int) -> bytes:
    """A PROXY protocol v2 header for TCP over IPv4, as Traefik sends."""
    addresses = (
        socket.inet_aton(source)
        + socket.inet_aton("127.0.0.1")
        + struct.pack("!HH", source_port, destination_port)
    )
    return (
        b"\r\n\r\n\x00\r\nQUIT\n"
        + bytes([0x21, 0x11])
        + struct.pack("!H", len(addresses))
        + addresses
    )


class EdgeProxy:
    """
    A stand-in for Dokploy's Traefik TCP router: accepts on loopback, connects to the
    gateway from `bind_address`, sends a PROXY v2 header naming `client`, and relays
    bytes both ways without terminating TLS.
    """

    def __init__(self, upstream_port: int, bind_address: str, client: str = CLIENT):
        self.upstream_port = upstream_port
        self.bind_address = bind_address
        self.client = client
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed:
            try:
                downstream, address = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(downstream, address), daemon=True).start()

    def _relay(self, downstream: socket.socket, address):
        try:
            upstream = socket.create_connection(
                ("127.0.0.1", self.upstream_port), source_address=(self.bind_address, 0)
            )
        except OSError:
            downstream.close()
            return
        upstream.sendall(_proxy_v2_header(self.client, address[1], 443))

        def pump(source: socket.socket, sink: socket.socket):
            try:
                while data := source.recv(65536):
                    sink.sendall(data)
            except OSError:
                pass
            finally:
                for sock in (source, sink):
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        threading.Thread(target=pump, args=(downstream, upstream), daemon=True).start()
        pump(upstream, downstream)

    def close(self):
        self.closed = True
        self.listener.close()


def _edge_bind_address() -> str:
    """127.0.0.2 where the loopback network allows it (Linux), so a direct
    connection from 127.0.0.1 is not the edge proxy; else 127.0.0.1."""
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.2", 0))
        return "127.0.0.2"
    except OSError:
        return "127.0.0.1"


@unittest.skipUnless(
    ENVOY or ENVOY_REQUIRED, "set NEST_TEST_ENVOY to an Envoy binary to run the gateway tests"
)
class EnvoyGatewayTests(_Base):
    """The real gateway in front of the real Controller (CI evidence for H08)."""

    def setUp(self):
        if not ENVOY:
            self.fail("NEST_TEST_ENVOY_REQUIRED=1 but no Envoy binary was found")
        super().setUp()
        work = Path(self.enterContext(tempfile.TemporaryDirectory()))
        certs = work / "certs"
        certs.mkdir()
        self.ca = CertificateAuthority()
        self.ca_file = work / "ca.pem"
        self.ca_file.write_bytes(self.ca.pem)
        chain, key = self.ca.issue("localhost")
        (certs / "fullchain.pem").write_bytes(chain)
        (certs / "privkey.pem").write_bytes(key)

        loopback = (ip_network("127.0.0.1/32"),)
        self.policy = RpcPolicy(user_rate=120, allowed_peers=loopback, trusted_proxies=loopback)
        self.guard = RpcGuard(self.policy)
        service = ExecutionService(
            self.backend,
            executor="slurm",
            jobs=self.jobs,
            access=self.access,
            admission=self.admission,
            guard=self.guard,
            local_execution=False,
        )
        server = build_server(ControllerConfig(rpc=self.policy), self.guard)
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(service, server)
        controller_port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, None)

        self.edge_address = _edge_bind_address()
        listen, admin = _free_port(), _free_port()
        result = render(
            work / "run",
            NEST_GRPC_PUBLIC_HOST="localhost",
            NEST_GRPC_EDGE_PROXY=f"{self.edge_address}/32",
            NEST_GRPC_UPSTREAM=f"127.0.0.1:{controller_port}",
            NEST_GRPC_LISTEN_PORT=str(listen),
            NEST_GRPC_ADMIN_PORT=str(admin),
            NEST_GRPC_CERT_DIR=str(certs),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.listen = listen
        self.log = work / "envoy.log"
        handle = self.enterContext(open(self.log, "w"))
        envoy = subprocess.Popen(
            [
                str(ENVOY),
                "-c",
                str(work / "run" / "envoy.yaml"),
                "--use-dynamic-base-id",
                "--log-level",
                "warning",
            ],
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        self.addCleanup(envoy.wait, 30)
        self.addCleanup(envoy.terminate)
        self.wait_ready(admin, envoy)
        self.edge = EdgeProxy(listen, self.edge_address)
        self.addCleanup(self.edge.close)

        # The SDK's hosted profile, pointed at the edge: install, sign in, run.
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        os.chmod(self.home, 0o700)
        self.hosted = nest_config.HOSTED_PROFILE
        profile = nest_config.HostedProfile(
            address=f"localhost:{self.edge.port}",
            auth_url=self.hosted.auth_url,
            root_certificates=self.ca.pem,
        )
        self.enterContext(mock.patch.object(nest_config, "HOSTED_PROFILE", profile))
        self.store_session(self.member, self.token_member)

    def wait_ready(self, admin: int, envoy: subprocess.Popen):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if envoy.poll() is not None:
                self.fail(f"Envoy exited: {self.log.read_text()[-4000:]}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{admin}/ready", timeout=2) as answer:
                    if answer.read().strip() == b"LIVE":
                        return
            except OSError:
                pass
            time.sleep(0.2)
        self.fail(f"Envoy did not become ready: {self.log.read_text()[-4000:]}")

    def store_session(self, user_id, token):
        now = datetime.now(UTC)
        CredentialStore(str(self.home / "nest" / "credentials.json")).save(
            NestSession(
                auth_url=self.hosted.auth_url,
                user_id=user_id,
                session_id=uuid.uuid4(),
                scope="jobs:run",
                access_token=token,
                refresh_token="refresh-" + "r" * 30,
                access_expires_at=now + timedelta(minutes=9),
                refresh_expires_at=now + timedelta(hours=1),
                session_expires_at=now + timedelta(hours=1),
            )
        )

    def run_hosted(self, **env):
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("NEST_") or k.startswith("NEST_TEST")
        }
        environment.update(XDG_CONFIG_HOME=str(self.home), NEST_PROFILE="hosted", **env)
        with mock.patch.dict(os.environ, environment, clear=True):
            return execute_remote(answer_42, RESOURCES, None)

    def stub(self) -> execution_pb2_grpc.ExecutionServiceStub:
        channel = grpc.secure_channel(
            f"localhost:{self.edge.port}", grpc.ssl_channel_credentials(self.ca.pem)
        )
        self.addCleanup(channel.close)
        return execution_pb2_grpc.ExecutionServiceStub(channel)

    def tls(self, server_name: str, alpn: list[str], port: int | None = None) -> ssl.SSLSocket:
        context = ssl.create_default_context(cadata=self.ca.pem.decode())
        context.check_hostname = False
        context.set_alpn_protocols(alpn)
        raw = socket.create_connection(("127.0.0.1", port or self.edge.port), timeout=10)
        wrapped = context.wrap_socket(raw, server_hostname=server_name)
        self.addCleanup(wrapped.close)
        return wrapped

    def test_tls_sni_and_alpn(self):
        good = self.tls("localhost", ["h2", "http/1.1"])
        self.assertEqual(good.selected_alpn_protocol(), "h2")
        self.assertIn(good.version(), ("TLSv1.2", "TLSv1.3"))
        with self.assertRaises((ssl.SSLError, OSError)):
            self.tls("grpc.other.example", ["h2"])
        try:
            http1 = self.tls("localhost", ["http/1.1"])
            self.assertNotEqual(http1.selected_alpn_protocol(), "h2")
        except ssl.SSLError, OSError:
            pass  # refusing the handshake is as good

    def test_hosted_sdk_selects_and_runs_through_the_gateway(self):
        self.assertEqual(self.run_hosted(), 42)
        self.assertEqual(
            self.sql("SELECT project_id FROM executions"), [{"project_id": self.project}]
        )
        admits = [e for e in self.audit.events(event="execution.admit") if e.outcome == "succeeded"]
        self.assertEqual(len(admits), 1)
        # The gateway's x-forwarded-for, from the PROXY header, names the client.
        self.assertEqual(admits[0].details.get("client_address"), CLIENT)
        log = self.log.read_text()
        self.assertIn("/nest.ExecutionService/Execute", log)
        self.assertIn("/nest.ExecutionService/ListEligibleProjects", log)
        self.assertNotIn(self.token_member, log)

    def test_client_cannot_choose_its_audited_address(self):
        self.stub().Execute(
            execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((answer_42, (), {}))),
            timeout=60,
            metadata=self.metadata(x_forwarded_for="198.51.100.66"),
        )
        [admit] = [
            e for e in self.audit.events(event="execution.admit") if e.outcome == "succeeded"
        ]
        self.assertEqual(admit.details.get("client_address"), CLIENT)

    def test_unauthenticated_and_unknown_paths_are_refused_before_anything_runs(self):
        with self.assertRaises(grpc.RpcError) as caught:
            self.stub().ListEligibleProjects(
                execution_pb2.ListEligibleProjectsRequest(), timeout=30
            )
        error = cast(grpc.Call, caught.exception)
        self.assertEqual(error.code(), grpc.StatusCode.UNAUTHENTICATED)
        self.assertIn("nest login", error.details() or "")
        channel = grpc.secure_channel(
            f"localhost:{self.edge.port}", grpc.ssl_channel_credentials(self.ca.pem)
        )
        self.addCleanup(channel.close)
        health = channel.unary_unary("/grpc.health.v1.Health/Check")
        with self.assertRaises(grpc.RpcError) as caught:
            health(b"", timeout=30)
        self.assertEqual(cast(grpc.Call, caught.exception).code(), grpc.StatusCode.UNIMPLEMENTED)
        self.assertEqual(self.sbatch_calls(), [])

    def test_revocation_is_enforced_through_the_gateway(self):
        self.assertEqual(self.run_hosted(), 42)
        self.projects.remove_member(browser(self.owner), self.project, self.member)
        with redirect_stderr(StringIO()), self.assertRaises(Exception) as caught:
            self.run_hosted()
        self.assertIn("no Nest Project", str(caught.exception))
        with self.assertRaises(RemoteAccessDeniedError):
            self.run_hosted(NEST_PROJECT_ID=str(self.project))
        self.assertEqual(len(self.sbatch_calls()), 1)

    def test_rate_limits_answer_through_the_gateway(self):
        self.guard.users.capacity = 2.0
        self.guard.users.per_second = 0.01
        codes = []
        for _ in range(4):
            try:
                self.stub().ListEligibleProjects(
                    execution_pb2.ListEligibleProjectsRequest(),
                    timeout=30,
                    metadata=self.metadata(),
                )
            except grpc.RpcError as exc:
                call = cast(grpc.Call, exc)
                codes.append(call.code())
                trailers = dict(call.trailing_metadata() or ())
                self.assertEqual(trailers.get("nest-error-kind"), "rate_limited")
        self.assertIn(grpc.StatusCode.RESOURCE_EXHAUSTED, codes)

    def test_timeout_is_reported_and_the_submission_is_not_repeated(self):
        self.hpc.hold_jobs("RUNNING")
        # The client's deadline or the gateway's (the same grpc-timeout) ends the call.
        with self.assertRaises((RpcTimeoutError, ConnectionError)):
            self.run_hosted(NEST_RPC_TIMEOUT="5")
        self.assertEqual(len(self.sbatch_calls()), 1)
        time.sleep(1)
        self.assertEqual(len(self.sbatch_calls()), 1)

    def test_the_controller_is_reached_only_through_the_edge_proxy(self):
        if self.edge_address == "127.0.0.1":
            self.skipTest("needs a second loopback address (Linux) to tell the edge proxy apart")
        # Straight to the gateway's listener, not from the edge proxy: closed by RBAC.
        with self.assertRaises((ssl.SSLError, OSError)):
            self.tls("localhost", ["h2"], port=self.listen)
        # A PROXY header from a non-edge peer cannot be used to pick an address either.
        with socket.create_connection(("127.0.0.1", self.listen), timeout=10) as raw:
            raw.sendall(_proxy_v2_header("198.51.100.66", 1, 443))
            raw.settimeout(10)
            try:
                self.assertEqual(raw.recv(1), b"")
            except ConnectionResetError, TimeoutError:
                pass

    def test_operator_verification_script_passes_through_the_gateway(self):
        spec = importlib.util.spec_from_file_location(
            "verify_public_grpc", ROOT / "scripts" / "verify-public-grpc.py"
        )
        assert spec is not None and spec.loader is not None
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        closed = _free_port()
        out = StringIO()
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("NEST_") or k.startswith("NEST_TEST")
        }
        environment.update(XDG_CONFIG_HOME=str(self.home), NEST_PROFILE="hosted")
        with mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(out):
            code = script.main(
                [
                    f"localhost:{self.edge.port}",
                    "--ca-file",
                    str(self.ca_file),
                    "--origin",
                    f"127.0.0.1:{closed}",
                    "--signed-in",
                    "--execute",
                ]
            )
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(lines[-1], {"summary": "pass", "failed": 0})
        by_name = {line["check"]: line for line in lines[:-1]}
        self.assertEqual(by_name["tls"]["alpn"], "h2")
        self.assertEqual(by_name["signed_in_projects"]["eligible_projects"], 1)
        self.assertEqual(by_name["remote_execution"]["result"], "nest-public-grpc-ok")
        self.assertNotIn(self.token_member, out.getvalue())
