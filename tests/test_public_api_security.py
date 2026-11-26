"""Public API security and abuse controls of the gRPC listener (H07).

`TransportTests` serve the real `ExecutionService` on the bounded server of
`controller.build_server`, with its `RpcGuard`, and need no database. The
`CredentialTests` reuse `test_registry_admission._Base` (real PostgreSQL, the
`FakeHpc` login node) to check that every refused credential, Project or
rate-limited call leaves nothing staged or submitted.
"""

import gzip
import logging
import os
import secrets
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import cast
from unittest import mock

import cloudpickle
import grpc
from test_registry_admission import _Base

from nest.config import ConfigError, ControllerConfig, RpcPolicy, load_controller_config
from nest.controller import ExecutionService, build_server
from nest.db import hash_token
from nest.proto import execution_pb2, execution_pb2_grpc
from nest.rpc_guard import (
    ORIGIN_KIND,
    RpcGuard,
    TokenBuckets,
    canonical_address,
    client_address,
    peer_address,
)

EXECUTE = "/nest.ExecutionService/Execute"


class _Unpickled:
    """Creates `path` when unpickled, so a test can tell whether it was."""

    def __init__(self, path: str):
        self.path = path

    def __reduce__(self):
        return (os.mkdir, (self.path,))


def serve(policy: RpcPolicy, service=None, guard=None) -> tuple[str, grpc.Server]:
    config = ControllerConfig(rpc=policy)
    server = build_server(config, guard or RpcGuard(policy))
    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
        service or ExecutionService(executor="local"), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return f"127.0.0.1:{port}", server


class TransportTests(unittest.TestCase):
    def start(self, policy=None, service=None, guard=None, **channel_options):
        address, server = serve(policy or RpcPolicy(), service, guard)
        self.addCleanup(server.stop, None)
        channel = grpc.insecure_channel(
            address,
            options=[("grpc.max_send_message_length", 64 * 1024 * 1024)],
            **channel_options,
        )
        self.addCleanup(channel.close)
        return channel, execution_pb2_grpc.ExecutionServiceStub(channel)

    def refused(self, call) -> grpc.Call:
        with self.assertRaises(grpc.RpcError) as caught:
            call()
        return cast(grpc.Call, caught.exception)

    def test_oversized_and_compressed_oversized_requests_are_refused_unread(self):
        marker = Path(tempfile.mkdtemp()) / "ran"
        policy = RpcPolicy(max_message_bytes=64 * 1024)
        for compression in (None, grpc.Compression.Gzip):
            with self.subTest(compression=compression):
                _, stub = self.start(policy)
                payload = cloudpickle.dumps(_Unpickled(str(marker))) + b"\0" * (256 * 1024)
                self.assertLess(len(gzip.compress(payload)), 64 * 1024)
                error = self.refused(
                    lambda stub=stub, payload=payload, compression=compression: stub.Execute(
                        execution_pb2.ExecuteRequest(payload=payload),
                        timeout=10,
                        compression=compression,
                    )
                )
                self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
                self.assertFalse(marker.exists())

    def test_oversized_response_keeps_its_status(self):
        _, stub = self.start(RpcPolicy(max_message_bytes=64 * 1024))
        payload = cloudpickle.dumps((lambda: b"x" * (128 * 1024), (), {}))
        error = self.refused(
            lambda: stub.Execute(execution_pb2.ExecuteRequest(payload=payload), timeout=10)
        )
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)

    def test_malformed_message_is_invalid_argument_before_the_handler(self):
        channel, _ = self.start()
        raw = channel.unary_unary(EXECUTE)
        with mock.patch.object(ExecutionService, "Execute") as handler:
            error = self.refused(lambda: raw(b"\xff\xff\xff\xff", timeout=10))
        self.assertEqual(error.code(), grpc.StatusCode.INVALID_ARGUMENT)
        self.assertEqual(error.details(), "Malformed request message")
        handler.assert_not_called()

    def test_status_details_and_trailers_pass_through_the_guard(self):
        _, stub = self.start()
        error = self.refused(lambda: stub.Execute(execution_pb2.ExecuteRequest(cpu=-1), timeout=10))
        self.assertEqual(error.code(), grpc.StatusCode.INVALID_ARGUMENT)
        self.assertEqual(error.details(), "Resource counts cannot be negative")
        # A trusted local call still works (existing private SDK compatibility).
        payload = cloudpickle.dumps((lambda: 41 + 1, (), {}))
        response = stub.Execute(execution_pb2.ExecuteRequest(payload=payload), timeout=10)
        self.assertEqual(cloudpickle.loads(response.result), 42)

    def test_connections_from_outside_the_allowed_peers_are_refused(self):
        marker = Path(tempfile.mkdtemp()) / "ran"
        request = execution_pb2.ExecuteRequest(payload=cloudpickle.dumps(_Unpickled(str(marker))))
        _, stub = self.start(RpcPolicy(allowed_peers=(ip_network("10.0.0.0/8"),)))
        error = self.refused(lambda: stub.Execute(request, timeout=10))
        self.assertEqual(error.code(), grpc.StatusCode.PERMISSION_DENIED)
        self.assertEqual(dict(error.trailing_metadata())["nest-error-kind"], "origin_not_allowed")
        self.assertFalse(marker.exists())
        _, stub = self.start(RpcPolicy(allowed_peers=(ip_network("127.0.0.1/32"),)))
        payload = cloudpickle.dumps((lambda: "ok", (), {}))
        stub.Execute(execution_pb2.ExecuteRequest(payload=payload), timeout=10)

    def test_peer_rate_limit_refuses_before_decoding(self):
        clock = [0.0]
        policy = RpcPolicy(peer_rate=6)  # a burst of 5
        guard = RpcGuard(policy, clock=lambda: clock[0])
        _, stub = self.start(policy, guard=guard)
        request = execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((lambda: 1, (), {})))
        for _ in range(5):
            stub.Execute(request, timeout=10)
        error = self.refused(lambda: stub.Execute(request, timeout=10))
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        metadata = dict(error.trailing_metadata())
        self.assertEqual(metadata["nest-error-kind"], "rate_limited")
        self.assertEqual(metadata["retry-after"], "10")
        clock[0] += 10.0
        stub.Execute(request, timeout=10)

    def test_concurrency_bound_refuses_excess_calls(self):
        started, release = threading.Event(), threading.Event()
        policy = RpcPolicy(max_workers=1, max_concurrent_rpcs=1)
        request = execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((lambda: 1, (), {})))
        # Block in the servicer, so the first call holds the only slot.
        original = ExecutionService.Execute

        def blocking(service, req, context):
            started.set()
            release.wait(10)
            return original(service, req, context)

        with mock.patch.object(ExecutionService, "Execute", blocking):
            _, stub = self.start(policy)
            future = stub.Execute.future(request, timeout=20)
            self.assertTrue(started.wait(10))
            error = self.refused(lambda: stub.Execute(request, timeout=10))
            self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
            release.set()
            future.result()

    def test_local_execution_refused_without_deserializing(self):
        marker = Path(tempfile.mkdtemp()) / "ran"
        access = mock.Mock()
        access.authenticate.return_value = mock.Mock(user_id="u", session_id=None)
        access.project_of.return_value = "p"
        service = ExecutionService(executor="local", access=access, local_execution=False)
        _, stub = self.start(service=service)
        request = execution_pb2.ExecuteRequest(payload=cloudpickle.dumps(_Unpickled(str(marker))))
        error = self.refused(lambda: stub.Execute(request, timeout=10))
        self.assertEqual(error.code(), grpc.StatusCode.FAILED_PRECONDITION)
        self.assertIn("does not run calls in its own process", error.details())
        self.assertFalse(marker.exists())


class AddressTests(unittest.TestCase):
    proxies = (ip_network("10.1.0.0/16"),)

    def test_peer_strings(self):
        self.assertEqual(peer_address("ipv4:10.1.2.3:5555"), ip_address("10.1.2.3"))
        self.assertEqual(peer_address("ipv6:[::1]:5555"), ip_address("::1"))
        # gRPC 1.5x+ renders IPv6 peers URI-escaped (`grpc._channel` errors show
        # `ipv6:%5B::1%5D:port` for gRPC 1.84); both spellings are the same peer.
        self.assertEqual(peer_address("ipv6:%5B::1%5D:5"), ip_address("::1"))
        self.assertEqual(peer_address("ipv6:%5Bfe80::1%25eth0%5D:5"), ip_address("fe80::1"))
        self.assertEqual(peer_address("ipv6:[fe80::1%eth0]:5"), ip_address("fe80::1"))
        for peer in (
            "unix:/tmp/sock",
            "",
            None,
            "ipv4:nonsense:1",
            "ipv6:::1:5",  # no brackets: the port cannot be told from the address
            "ipv6:%5Bnot-an-ip%5D:5",
            "vsock:3:5",
        ):
            self.assertIsNone(peer_address(peer))

    def test_ipv4_mapped_peers_are_their_ipv4_address(self):
        """H08 (#109): a dual-stack listener sees an IPv4 gateway as ::ffff:a.b.c.d."""
        gateway = ip_address("10.253.250.3")
        for peer in (
            "ipv4:10.253.250.3:41000",
            "ipv6:[::ffff:10.253.250.3]:41000",
            "ipv6:%5B::ffff:10.253.250.3%5D:41000",
            "ipv6:%5B::ffff:afd:fa03%5D:41000",
        ):
            with self.subTest(peer):
                self.assertEqual(peer_address(peer), gateway)
        # Ordinary IPv6 is not rewritten, and neither are other embedded forms.
        for text in ("2001:db8::3", "::1", "64:ff9b::afd:fa03", "::afd:fa03"):
            self.assertEqual(canonical_address(ip_address(text)), ip_address(text))

    def test_mapped_forms_match_allowlists_written_either_way(self):
        proxies = (ip_network("10.253.250.3/32"),)
        mapped_proxies = (ip_network("::ffff:10.253.250.3/128"),)
        forwarded = [("x-forwarded-for", "198.51.100.1")]
        for peer in ("10.253.250.3", "::ffff:10.253.250.3"):
            for trusted in (proxies, mapped_proxies):
                with self.subTest(peer=peer, trusted=trusted):
                    got = client_address(ip_address(peer), forwarded, trusted)
                    self.assertEqual(got, "198.51.100.1")
        # A mapped hop in the header is the same address as its IPv4 form, so the
        # gateway's own entry is skipped and a client is reported as IPv4.
        chained = [("x-forwarded-for", "::ffff:198.51.100.1, ::ffff:10.253.250.3")]
        self.assertEqual(
            client_address(ip_address("10.253.250.3"), chained, proxies), "198.51.100.1"
        )

    def test_untrusted_mapped_peers_cannot_spoof_forwarded_for(self):
        proxies = (ip_network("10.253.250.3/32"),)
        spoof = [("x-forwarded-for", "198.51.100.1")]
        for peer in ("::ffff:10.0.1.9", "::ffff:10.253.250.4", "2001:db8::3", "10.0.1.9"):
            with self.subTest(peer):
                expected = str(canonical_address(ip_address(peer)))
                self.assertEqual(client_address(ip_address(peer), spoof, proxies), expected)
        # An IPv6 entry admits an IPv4 peer only when written in mapped form:
        # a wide IPv6 range such as ::/0 or ::/8 never trusts an IPv4 gateway.
        for wide in ("::/0", "::/8", "::fffe:0:0/95"):
            with self.subTest(wide=wide):
                got = client_address(ip_address("10.0.0.1"), spoof, (ip_network(wide),))
                self.assertEqual(got, "10.0.0.1")
        mapped_range = (ip_network("::ffff:10.0.0.0/104"),)
        self.assertEqual(
            client_address(ip_address("10.0.0.1"), spoof, mapped_range), "198.51.100.1"
        )
        self.assertEqual(client_address(ip_address("11.0.0.1"), spoof, mapped_range), "11.0.0.1")

    def test_forwarded_for_is_trusted_only_from_a_proxy(self):
        gateway, outsider = ip_address("10.1.0.5"), ip_address("203.0.113.9")
        spoof = [("x-forwarded-for", "198.51.100.1")]
        self.assertEqual(client_address(outsider, spoof, self.proxies), "203.0.113.9")
        self.assertEqual(client_address(outsider, spoof, ()), "203.0.113.9")
        self.assertEqual(client_address(gateway, spoof, self.proxies), "198.51.100.1")
        # The client's own claim before the gateway's entry is never used.
        chained = [("x-forwarded-for", "1.2.3.4, 198.51.100.1, 10.1.0.7")]
        self.assertEqual(client_address(gateway, chained, self.proxies), "198.51.100.1")
        for metadata in (
            [],
            [("x-forwarded-for", "a"), ("x-forwarded-for", "b")],
            [("x-forwarded-for", "not-an-ip")],
            [("x-forwarded-for", "10.1.0.8")],
        ):
            self.assertEqual(client_address(gateway, metadata, self.proxies), "10.1.0.5")
        self.assertEqual(client_address(None, spoof, self.proxies), "unknown")

    def test_token_buckets_refill_and_stay_bounded(self):
        clock = [0.0]
        buckets = TokenBuckets(60, max_keys=3, clock=lambda: clock[0])
        for _ in range(6):  # a burst of a tenth of a minute's worth
            self.assertIsNone(buckets.allow("a"))
        self.assertAlmostEqual(buckets.allow("a") or 0, 1.0)
        clock[0] += 1.0
        self.assertIsNone(buckets.allow("a"))
        for key in "bcde":
            buckets.allow(key)
        self.assertEqual(len(buckets), 3)
        self.assertIsNone(TokenBuckets(0).allow("x"))


class _Aborted(Exception):
    pass


class _Context:
    """The parts of a gRPC server context the origin guard reads."""

    def __init__(self, peer: str, metadata=()):
        self._peer = peer
        self._metadata = tuple(metadata)
        self.trailers: tuple = ()
        self.code = None

    def peer(self):
        return self._peer

    def invocation_metadata(self):
        return self._metadata

    def set_trailing_metadata(self, trailers):
        self.trailers = tuple(trailers)

    def abort(self, code, details):
        self.code = code
        raise _Aborted(details)


class OriginGuardTests(unittest.TestCase):
    """H08 (#109): the allowlist admits the gateway however gRPC spells its address,
    and still refuses everything else, failing closed on a peer it cannot parse."""

    GATEWAY = (ip_network("10.253.250.3/32"),)

    def guard(self, allowed_peers=None, trusted_proxies=None):
        return RpcGuard(
            RpcPolicy(
                allowed_peers=allowed_peers or (*self.GATEWAY, ip_network("172.20.0.0/16")),
                trusted_proxies=self.GATEWAY if trusted_proxies is None else trusted_proxies,
            )
        )

    def test_gateway_admitted_in_every_spelling(self):
        guard = self.guard()
        forwarded = [("x-forwarded-for", "203.0.113.9")]
        for peer in (
            "ipv4:10.253.250.3:40000",
            "ipv6:[::ffff:10.253.250.3]:40000",
            "ipv6:%5B::ffff:10.253.250.3%5D:40000",
        ):
            with self.subTest(peer):
                self.assertEqual(guard._admit(_Context(peer, forwarded)), "203.0.113.9")

    def test_edge_and_unparsable_peers_refused(self):
        guard = self.guard()
        for peer in (
            "ipv4:10.0.1.9:40000",  # the Dokploy edge network (#109)
            "ipv6:%5B::ffff:10.0.1.9%5D:40000",
            "ipv6:%5B::ffff:10.253.250.4%5D:40000",  # another grpc-origin host
            "ipv6:%5B2001:db8::3%5D:40000",
            "ipv6:%5Bgarbage%5D:1",
            "unix:/run/nest.sock",
            "",
        ):
            with self.subTest(peer):
                context = _Context(peer, [("x-forwarded-for", "10.253.250.3")])
                with self.assertRaises(_Aborted), self.assertLogs("nest.rpc_guard", "WARNING"):
                    guard._admit(context)
                self.assertEqual(context.code, grpc.StatusCode.PERMISSION_DENIED)
                self.assertIn(("nest-error-kind", ORIGIN_KIND), context.trailers)

    def test_tailnet_peer_keeps_its_own_address(self):
        guard = self.guard()
        context = _Context("ipv6:%5B::ffff:172.20.0.5%5D:1", [("x-forwarded-for", "1.2.3.4")])
        self.assertEqual(guard._admit(context), "172.20.0.5")

    def test_wide_ipv6_allowlist_admits_no_ipv4_peer(self):
        guard = self.guard(allowed_peers=(ip_network("::/1"),), trusted_proxies=())
        for peer in ("ipv4:10.253.250.3:1", "ipv6:%5B::ffff:10.253.250.3%5D:1"):
            with (
                self.subTest(peer),
                self.assertRaises(_Aborted),
                self.assertLogs("nest.rpc_guard", "WARNING"),
            ):
                guard._admit(_Context(peer))

    def test_ipv6_allowlist_still_works(self):
        guard = self.guard(allowed_peers=(ip_network("fd00:abcd::/64"),), trusted_proxies=())
        self.assertEqual(guard._admit(_Context("ipv6:%5Bfd00:abcd::7%5D:1")), "fd00:abcd::7")
        with self.assertRaises(_Aborted), self.assertLogs("nest.rpc_guard", "WARNING"):
            guard._admit(_Context("ipv6:%5B::ffff:10.253.250.3%5D:1"))


class RpcConfigTests(unittest.TestCase):
    def test_defaults_match_the_transport_contract(self):
        rpc = load_controller_config({}).rpc
        self.assertEqual(rpc.max_message_bytes, 4 * 1024 * 1024)
        self.assertEqual((rpc.max_workers, rpc.max_concurrent_rpcs), (4, 32))
        self.assertEqual((rpc.allowed_peers, rpc.trusted_proxies), ((), ()))
        self.assertTrue(rpc.local_execution)

    def test_local_execution_off_by_default_on_a_shared_listener(self):
        base = {
            "NEST_CONTROLLER_BIND": "0.0.0.0:50051",
            "NEST_CONTROLLER_AUTH": "session",
            "NEST_CONTROLLER_ALLOW_PLAINTEXT": "true",
            "NEST_DATABASE_URL": "postgresql://nest@db.example.internal:5432/nest",
        }
        self.assertFalse(load_controller_config(base).rpc.local_execution)
        on = load_controller_config({**base, "NEST_CONTROLLER_LOCAL_EXECUTION": "true"})
        self.assertTrue(on.rpc.local_execution)
        loopback = {**base, "NEST_CONTROLLER_BIND": "127.0.0.1:50051"}
        self.assertTrue(load_controller_config(loopback).rpc.local_execution)
        # Without authentication nothing changes: any caller could already run code.
        self.assertTrue(
            load_controller_config({"NEST_CONTROLLER_BIND": "0.0.0.0:1"}).rpc.local_execution
        )

    def test_settings_are_read_and_validated(self):
        config = load_controller_config(
            {
                "NEST_GRPC_MAX_WORKERS": "8",
                "NEST_GRPC_MAX_CONCURRENT_RPCS": "16",
                "NEST_GRPC_RATE_LIMIT_PER_PEER": "0",
                "NEST_GRPC_ALLOWED_PEERS": "172.18.0.0/16, 10.0.0.5",
                "NEST_GRPC_TRUSTED_PROXIES": "172.18.0.2",
            }
        )
        self.assertEqual(config.rpc.max_workers, 8)
        self.assertEqual(config.rpc.peer_rate, 0)
        self.assertEqual(
            [str(n) for n in config.rpc.allowed_peers], ["172.18.0.0/16", "10.0.0.5/32"]
        )
        summary = config.rpc.summary()
        self.assertEqual(summary["trusted_proxies"], ["172.18.0.2/32"])
        for settings in (
            {"NEST_GRPC_MAX_WORKERS": "0"},
            {"NEST_GRPC_MAX_WORKERS": "8", "NEST_GRPC_MAX_CONCURRENT_RPCS": "4"},
            {"NEST_GRPC_MAX_MESSAGE_BYTES": str(65 * 1024 * 1024)},
            {"NEST_GRPC_MAX_MESSAGE_BYTES": "100"},
            {"NEST_GRPC_RATE_LIMIT_PER_USER": "-1"},
            {"NEST_GRPC_ALLOWED_PEERS": "not-a-network"},
            {"NEST_EXECUTOR": "slurm", "NEST_CONTROLLER_LOCAL_EXECUTION": "true"},
        ):
            with self.subTest(settings), self.assertRaises(ConfigError):
                load_controller_config(settings)


class CredentialTests(_Base):
    """Every refusal precedes deserialization, staging, SSH and Slurm."""

    def setUp(self):
        super().setUp()
        self.policy = RpcPolicy(user_rate=6)
        self.guard = RpcGuard(self.policy)
        service = ExecutionService(
            self.backend,
            executor="slurm",
            jobs=self.jobs,
            access=self.access,
            admission=self.admission,
            guard=self.guard,
        )
        address, server = serve(self.policy, service, self.guard)
        self.addCleanup(server.stop, None)
        channel = grpc.insecure_channel(address)
        self.addCleanup(channel.close)
        self.stub = execution_pb2_grpc.ExecutionServiceStub(channel)

    def session_token(self, ttl=600.0):
        session = self.identities.create_session(
            self.member,
            kind="cli",
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="jobs:run",
        )
        access = secrets.token_urlsafe(32)
        self.sessions.issue_access_token(session.id, token_hash=hash_token(access), ttl_seconds=ttl)
        return access, session.id

    def expired_token(self):
        token, _ = self.session_token(ttl=0.2)
        time.sleep(0.5)
        return token

    def revoked_token(self):
        token, session_id = self.session_token()
        self.identities.revoke_session(session_id)
        return token

    def test_refused_credentials_and_projects_send_nothing(self):
        self.guard.users = TokenBuckets(0)  # this matrix is not about the rate limit
        marker = Path(tempfile.mkdtemp()) / "ran"
        payload = cloudpickle.dumps(_Unpickled(str(marker)))
        cases = [
            ("missing", [("x-nest-project-id", str(self.project))], "UNAUTHENTICATED"),
            ("malformed", [("authorization", "Basic abc"), ("x-nest-project-id", str(self.project))],
             "UNAUTHENTICATED"),
            ("invalid", self.metadata("not-a-real-token"), "UNAUTHENTICATED"),
            ("expired", self.metadata(self.expired_token()), "UNAUTHENTICATED"),
            ("revoked", self.metadata(self.revoked_token()), "UNAUTHENTICATED"),
            ("wrong project", self.metadata(project=self.other), "NOT_FOUND"),
            ("no project", [("authorization", f"Bearer {self.token_member}")], "INVALID_ARGUMENT"),
            ("viewer scope", self.metadata(self.token(self.member, scope="projects:read")),
             "PERMISSION_DENIED"),
        ]  # fmt: skip
        for label, metadata, code in cases:
            with self.subTest(label):
                for call in (
                    lambda metadata=metadata: self.stub.Execute(
                        execution_pb2.ExecuteRequest(payload=payload),
                        timeout=30,
                        metadata=metadata,
                    ),
                    lambda metadata=metadata: self.stub.SubmitJob(
                        execution_pb2.SubmitJobRequest(workdir="/tmp", script="x.sbatch"),
                        timeout=30,
                        metadata=metadata,
                    ),
                ):
                    error = self.refused(call)
                    self.assertEqual(error.code().name, code, error.details())
        self.assert_nothing_sent()
        self.assertFalse(marker.exists())
        self.assertEqual(self.sql("SELECT count(*) AS n FROM executions"), [{"n": 0}])

    def test_legitimate_session_reuse_and_per_user_rate_limit(self):
        token = self.token(self.member)
        for _ in range(3):
            self.assertEqual(self.execute(token=token), 42)
        self.assertEqual(len(self.sbatch_calls()), 3)
        for _ in range(2):
            self.stub.GetJobStatus(
                execution_pb2.JobStatusRequest(job_id=self.sbatch_calls()[0]["job_id"]),
                timeout=30,
                metadata=self.metadata(token),
            )
        # The sixth call in the burst of five is refused, before staging anything.
        error = self.refused(lambda: self.execute(token=token))
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertEqual(dict(error.trailing_metadata())["nest-error-kind"], "rate_limited")
        self.assertEqual(len(self.sbatch_calls()), 3)
        # Another User, and the same User's other session, are counted apart and together.
        self.assertEqual(self.execute(token=self.token_outsider, project=self.other), 42)
        error = self.refused(lambda: self.execute(token=self.token(self.member)))
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        # Failed authentication is not charged to the User whose token was guessed.
        for _ in range(10):
            self.refused(lambda: self.execute(token="guess"))

    def test_logs_never_carry_credentials(self):
        token = self.token(self.member)
        with self.assertLogs("nest", level=logging.DEBUG) as logs:
            self.execute(token=token)
            self.refused(lambda: self.execute(token=token + "x"))
            for _ in range(6):
                try:
                    self.execute(token=token)
                except grpc.RpcError:
                    pass
        text = "\n".join(logs.output)
        self.assertIn("client=127.0.0.1", text)
        self.assertIn("rate-limited scope=user", text)
        self.assertNotIn(token, text)
        self.assertNotIn("Bearer", text)

    def refused(self, call) -> grpc.Call:
        with self.assertRaises(grpc.RpcError) as caught:
            call()
        return cast(grpc.Call, caught.exception)
