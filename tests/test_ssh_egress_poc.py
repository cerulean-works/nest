"""
Synthetic fixture tests for the D03 no-job egress probe and the restricted relay
contract (`poc/ssh-egress`). Everything runs on loopback against `fake_sshd.py` or
injected resolvers and connectors: no real host, account, login or Slurm job.
"""

import asyncio
import base64
import hashlib
import importlib.util
import io
import ipaddress
import json
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

POC = Path(__file__).resolve().parents[1] / "poc" / "ssh-egress"


def load(name: str):
    # The PoC scripts import egress_policy by its bare name, as they do when run
    # from their own directory, so register each module under that name.
    spec = importlib.util.spec_from_file_location(name, POC / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


egress_policy = load("egress_policy")
egress_probe = load("egress_probe")
fake_sshd = load("fake_sshd")
rc = load("relay_contract")

PUBLIC_V4 = "8.8.8.8"  # globally routable; never contacted (connectors are fixtures)
PUBLIC_V6 = "2001:4860:4860::8888"
PLATFORM = str(uuid.UUID(int=0xD03))
PROJECT = str(uuid.UUID(int=0xD03 + 1))
PIN_BLOB = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + bytes(range(32))).decode()
PIN = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(PIN_BLOB)).digest()).decode()
PIN = PIN.rstrip("=")


class AddressPolicyTests(unittest.TestCase):
    def test_public_addresses_pass(self):
        got = egress_policy.validate_addresses([PUBLIC_V4, PUBLIC_V6])
        self.assertEqual([str(a) for a in got], [PUBLIC_V4, PUBLIC_V6])

    def test_rejections(self):
        cases = {
            "100.100.1.2": "tailnet_address",  # Tailscale CGNAT: never Mode B
            "fd7a:115c:a1e0::1": "tailnet_address",
            "10.0.0.5": "not_public",
            "192.168.1.1": "not_public",
            "169.254.169.254": "not_public",  # cloud metadata
            "127.0.0.1": "not_public",
            "::1": "not_public",
            "::ffff:10.0.0.5": "not_public",
            "224.0.0.1": "not_unicast",
            "0.0.0.0": "not_unicast",
            "203.0.113.7": "not_public",
        }
        for address, reason in cases.items():
            with self.subTest(address=address):
                with self.assertRaises(egress_policy.DestinationRejected) as caught:
                    egress_policy.validate_addresses([address])
                self.assertEqual(caught.exception.reason, reason)

    def test_mixed_answer_is_rejected_whole(self):
        with self.assertRaises(egress_policy.DestinationRejected):
            egress_policy.validate_addresses([PUBLIC_V4, "10.0.0.5"])

    def test_loopback_only_with_fixture_flag_and_never_tailnet(self):
        egress_policy.validate_addresses(["127.0.0.1"], allow_loopback=True)
        with self.assertRaises(egress_policy.DestinationRejected):
            egress_policy.validate_addresses(["100.64.0.1"], allow_loopback=True)
        with self.assertRaises(egress_policy.DestinationRejected):
            egress_policy.validate_addresses(["10.0.0.1"], allow_loopback=True)

    def test_approved_address_set(self):
        egress_policy.validate_addresses([PUBLIC_V4], approved=[PUBLIC_V4])
        with self.assertRaises(egress_policy.DestinationRejected) as caught:
            egress_policy.validate_addresses([PUBLIC_V4, "9.9.9.9"], approved=[PUBLIC_V4])
        self.assertEqual(caught.exception.reason, "address_not_approved")

    def test_ports(self):
        self.assertEqual(egress_policy.validate_port(22), 22)
        for port in (2222, 0, 70000, True):
            with self.subTest(port=port), self.assertRaises(egress_policy.DestinationRejected):
                egress_policy.validate_port(port)
        self.assertEqual(egress_policy.validate_port(2222, {22, 2222}), 2222)

    def test_dns_failure_is_a_rejection(self):
        def broken(host, port):
            raise OSError("no such host")

        with self.assertRaises(egress_policy.DestinationRejected) as caught:
            egress_policy.resolve_once("ln.invalid", 22, broken)
        self.assertEqual(caught.exception.reason, "dns_failed")


class HostKeyTests(unittest.TestCase):
    def test_fingerprint_matches_openssh_format(self):
        self.assertRegex(egress_policy.fingerprint(PIN_BLOB), r"^SHA256:[A-Za-z0-9+/]{43}$")
        self.assertEqual(egress_policy.fingerprint(PIN_BLOB), PIN)

    def test_keyscan_match_and_mismatch(self):
        keys = egress_policy.parse_keyscan(f"# ln01:22 SSH-2.0-x\nln01 ssh-ed25519 {PIN_BLOB}\n")
        self.assertEqual(egress_policy.match_pins(keys, [PIN]), ([PIN], []))
        other = "SHA256:" + "A" * 43
        self.assertEqual(egress_policy.match_pins(keys, [other]), ([], [PIN]))

    def test_rejects_non_keyscan_text_and_bad_pins(self):
        with self.assertRaises(ValueError):
            egress_policy.parse_keyscan("ln01 password hunter2\n")
        with self.assertRaises(ValueError):
            egress_policy.normalize_pin("MD5:aa:bb")


class FixtureServer:
    """Runs fake_sshd on its own loop thread for the synchronous probe."""

    def __init__(self, **options):
        self.server = fake_sshd.FakeSshd(**options)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.port = asyncio.run_coroutine_threadsafe(self.server.start(), self.loop).result(5)
        return self

    def __exit__(self, *exc):
        asyncio.run_coroutine_threadsafe(self.server.close(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


def probe(*args, resolver=None):
    out = io.StringIO()
    code = egress_probe.run(list(args), out=out, resolver=resolver)
    return code, [json.loads(line) for line in out.getvalue().splitlines()]


class EgressProbeTests(unittest.TestCase):
    def test_fixture_smoke_passes_and_sends_nothing(self):
        with FixtureServer() as fixture, tempfile.TemporaryDirectory() as tmp:
            scan = Path(tmp) / "scan.txt"
            scan.write_text(f"[127.0.0.1]:{fixture.port} ssh-ed25519 {PIN_BLOB}\n")
            code, records = probe(
                "--host", "127.0.0.1", "--port", str(fixture.port),
                "--allowed-port", str(fixture.port), "--fixture",
                "--pin", PIN, "--keyscan-file", str(scan),
            )  # fmt: skip
            # The probe closes after reading the banner; the server saw zero bytes.
            for _ in range(50):
                if fixture.server.received:
                    break
                threading.Event().wait(0.05)
            self.assertEqual(fixture.server.received, [0])
        self.assertEqual(code, 0, records)
        checks = {r.get("check"): r for r in records}
        self.assertTrue(all(r["evidence"] == "synthetic" for r in records))
        self.assertEqual(checks["ssh_identification"]["bytes_sent"], 0)
        self.assertTrue(checks["ssh_identification"]["identification"].startswith("SSH-2.0-"))
        self.assertEqual(checks["host_key"]["matched"], [PIN])
        summary = records[-1]
        self.assertEqual((summary["slurm_jobs_submitted"], summary["logins_attempted"]), (0, 0))

    def test_live_mode_refuses_loopback_before_connecting(self):
        with FixtureServer() as fixture:
            code, records = probe(
                "--host", "127.0.0.1", "--port", str(fixture.port),
                "--allowed-port", str(fixture.port), "--approved-address", "127.0.0.1",
            )  # fmt: skip
            self.assertEqual(fixture.server.connections, 0)
        self.assertEqual(code, 1)
        self.assertEqual(records[0]["reason"], "not_public")
        self.assertEqual(records[0]["evidence"], "live")

    def test_tailnet_resolution_and_unapproved_port_are_refused(self):
        code, records = probe(
            "--host", "ln.example", "--approved-address", "100.101.102.103",
            resolver=lambda h, p: ["100.101.102.103"],
        )  # fmt: skip
        self.assertEqual((code, records[0]["reason"]), (1, "tailnet_address"))
        code, records = probe(
            "--host", PUBLIC_V4, "--port", "2222", "--approved-address", PUBLIC_V4
        )
        self.assertEqual((code, records[0]["reason"]), (1, "port_not_allowlisted"))

    def test_resolution_outside_approved_addresses_is_refused(self):
        code, records = probe(
            "--host", "ln.example", "--approved-address", PUBLIC_V4,
            resolver=lambda h, p: ["9.9.9.9"],
        )  # fmt: skip
        self.assertEqual((code, records[0]["reason"]), (1, "address_not_approved"))

    def test_non_ssh_service_and_pin_mismatch_fail(self):
        with FixtureServer(banner=b"HTTP/1.1 400 Bad Request\r\n\r\n") as fixture:
            code, records = probe(
                "--host", "127.0.0.1", "--port", str(fixture.port),
                "--allowed-port", str(fixture.port), "--fixture", "--timeout", "1",
            )  # fmt: skip
        self.assertEqual(code, 1)
        self.assertFalse({r.get("check"): r for r in records}["ssh_identification"]["ok"])
        with FixtureServer() as fixture, tempfile.TemporaryDirectory() as tmp:
            scan = Path(tmp) / "scan.txt"
            scan.write_text(f"[127.0.0.1]:{fixture.port} ssh-ed25519 {PIN_BLOB}\n")
            code, records = probe(
                "--host", "127.0.0.1", "--port", str(fixture.port),
                "--allowed-port", str(fixture.port), "--fixture",
                "--pin", "SHA256:" + "B" * 43, "--keyscan-file", str(scan),
            )  # fmt: skip
        self.assertEqual(code, 1)
        self.assertFalse({r.get("check"): r for r in records}["host_key"]["ok"])

    def test_keyscan_without_independent_pin_fails(self):
        with FixtureServer() as fixture, tempfile.TemporaryDirectory() as tmp:
            scan = Path(tmp) / "scan.txt"
            scan.write_text(f"[127.0.0.1]:{fixture.port} ssh-ed25519 {PIN_BLOB}\n")
            code, records = probe(
                "--host", "127.0.0.1", "--port", str(fixture.port),
                "--allowed-port", str(fixture.port), "--fixture", "--keyscan-file", str(scan),
            )  # fmt: skip
        self.assertEqual(code, 1)
        self.assertIn("independent", {r.get("check"): r for r in records}["host_key"]["detail"])

    def test_live_run_requires_the_approved_address_set(self):
        resolved = []
        code, records = probe(
            "--host", "ln.example", resolver=lambda h, p: resolved.append(h) or [PUBLIC_V4]
        )  # fmt: skip
        self.assertEqual(code, 1)
        self.assertEqual(records[0]["check"], "arguments")
        self.assertIn("--approved-address", records[0]["detail"])
        self.assertFalse(records[-1]["ok"])
        self.assertEqual(resolved, [])  # refused before any lookup or connection

    def test_keyscan_must_be_observed_at_the_validated_address(self):
        hosts = {
            "name": "ln01.platform.example",
            "other address": "[127.0.0.2]:{port}",
            "default port": "127.0.0.1",
            "hashed": "|1|abc=|def=",
        }
        for case, host in hosts.items():
            with self.subTest(case=case):
                with FixtureServer() as fixture, tempfile.TemporaryDirectory() as tmp:
                    scan = Path(tmp) / "scan.txt"
                    line = host.format(port=fixture.port)
                    scan.write_text(f"{line} ssh-ed25519 {PIN_BLOB}\n")
                    code, records = probe(
                        "--host", "127.0.0.1", "--port", str(fixture.port),
                        "--allowed-port", str(fixture.port), "--fixture",
                        "--pin", PIN, "--keyscan-file", str(scan),
                    )  # fmt: skip
                key = {r.get("check"): r for r in records}["host_key"]
                self.assertEqual(code, 1)
                self.assertFalse(key["ok"])
                if case != "hashed":
                    self.assertIn("keyscan_host_mismatch", key["detail"])

    def test_keyscan_runs_against_the_validated_literal(self):
        with FixtureServer() as fixture, tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "argv.json"
            fake = Path(tmp) / "fake-keyscan"
            fake.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                f"open({str(calls)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                "port, host = sys.argv[2], sys.argv[-1]\n"
                f"print(f'[{{host}}]:{{port}} ssh-ed25519 {PIN_BLOB}')\n"
            )
            fake.chmod(0o700)
            code, records = probe(
                "--host", "localhost", "--port", str(fixture.port),
                "--allowed-port", str(fixture.port), "--fixture",
                "--pin", PIN, "--keyscan", "--keyscan-command", str(fake),
                resolver=lambda h, p: ["127.0.0.1"],
            )  # fmt: skip
            argv = json.loads(calls.read_text())
        self.assertEqual(code, 0, records)
        self.assertEqual(argv[-1], "127.0.0.1")  # the literal, never the name
        self.assertNotIn("localhost", argv)
        key = {r.get("check"): r for r in records}["host_key"]
        self.assertEqual(key["observed_at"], [f"127.0.0.1:{fixture.port}"])

    def test_banner_read_is_bounded_against_floods(self):
        async def flood(reader, writer):
            try:
                while True:
                    writer.write(b"x\n" * 64)
                    await writer.drain()
                    await asyncio.sleep(0.001)
            except ConnectionError, asyncio.CancelledError:
                pass
            finally:
                writer.close()

        with FixtureServer(handler=flood) as fixture:
            started = time.monotonic()
            code, records = probe(
                "--host", "127.0.0.1", "--port", str(fixture.port),
                "--allowed-port", str(fixture.port), "--fixture", "--timeout", "5",
            )  # fmt: skip
            elapsed = time.monotonic() - started
        ident = {r.get("check"): r for r in records}["ssh_identification"]
        self.assertEqual(code, 1)
        self.assertIn("8 KiB", ident["detail"])
        self.assertLess(elapsed, 5)

    def test_banner_read_honours_the_timeout(self):
        async def trickle(reader, writer):
            try:
                while True:
                    writer.write(b"x\n")
                    await writer.drain()
                    await asyncio.sleep(0.2)
            except ConnectionError, asyncio.CancelledError:
                pass
            finally:
                writer.close()

        with FixtureServer(handler=trickle) as fixture:
            started = time.monotonic()
            code, records = probe(
                "--host", "127.0.0.1", "--port", str(fixture.port),
                "--allowed-port", str(fixture.port), "--fixture", "--timeout", "1",
            )  # fmt: skip
            elapsed = time.monotonic() - started
        ident = {r.get("check"): r for r in records}["ssh_identification"]
        self.assertEqual(code, 1)
        self.assertFalse(ident["ok"])  # "timed out", or the deadline check between reads
        self.assertLess(elapsed, 3)

    def test_banner_read_stops_at_the_deadline_under_a_fast_trickle(self):
        # Faster than any per-recv timeout, far below the 8 KiB cap: only the
        # deadline check between reads can end it.
        async def fast_trickle(reader, writer):
            try:
                while True:
                    writer.write(b"x\n")
                    await writer.drain()
                    await asyncio.sleep(0.005)
            except ConnectionError, asyncio.CancelledError:
                pass
            finally:
                writer.close()

        with FixtureServer(handler=fast_trickle) as fixture:
            started = time.monotonic()
            code, records = probe(
                "--host", "127.0.0.1", "--port", str(fixture.port),
                "--allowed-port", str(fixture.port), "--fixture", "--timeout", "0.2",
            )  # fmt: skip
            elapsed = time.monotonic() - started
        ident = {r.get("check"): r for r in records}["ssh_identification"]
        self.assertEqual(code, 1)
        # Ends on the deadline: the check between reads, or the last recv timing out.
        self.assertIn(ident["detail"], {"no identification line before --timeout", "timed out"})
        self.assertLess(elapsed, 2)


class Client:
    """A fake gRPC client side: queued frames in, collected frames out."""

    def __init__(self, frames=(), *, hold_open=False, slow=0.0):
        self.queue: asyncio.Queue = asyncio.Queue()
        for frame in frames:
            self.queue.put_nowait(frame)
        if not hold_open:
            self.queue.put_nowait(None)
        self.received = bytearray()
        self.half_closed = False
        self.slow = slow

    async def frames(self):
        while (frame := await self.queue.get()) is not None:
            yield frame

    async def send(self, chunk):
        if self.slow:
            await asyncio.sleep(self.slow)
        if chunk is None:
            self.half_closed = True
        else:
            self.received += chunk


class RelayContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sshd = fake_sshd.FakeSshd()
        self.port = await self.sshd.start()
        self.events: list[rc.AuditEvent] = []
        self.resolved: list[str] = []
        self.connected: list[tuple[str, int]] = []
        self.allowed = {("alice", PLATFORM)}

    async def asyncTearDown(self):
        await self.sshd.close()

    def resolver(self, answers):
        def resolve(host, port):
            self.resolved.append(host)
            return answers.pop(0) if answers and isinstance(answers[0], list) else answers

        return resolve

    def model(self, *, answers=None, limits=None, node_port=22, approved=None, **options):
        async def connector(address, port):
            # Map the validated public address onto the loopback fixture.
            self.connected.append((str(address), port))
            return await asyncio.open_connection("127.0.0.1", self.port)

        node = rc.ApprovedEndpoint(1, "ln01.platform.example", node_port, PIN, approved)
        return rc.RelayModel(
            {PLATFORM: [node]},
            lambda principal, request: (principal.user_id, request.platform_id) in self.allowed,
            self.resolver(answers if answers is not None else [PUBLIC_V4]),
            connector=options.pop("connector", connector),
            audit=self.events.append,
            limits=limits,
            **options,
        )

    def first(self, **extra):
        return {"platform_id": PLATFORM, "project_id": PROJECT, **extra}

    async def test_opaque_bytes_round_trip_with_half_close(self):
        payload = [bytes([i % 256]) * 1000 for i in range(20)]
        client = Client(payload)
        event = await self.model().open(
            rc.Principal("alice"), self.first(), client.frames(), client.send
        )
        self.assertEqual(bytes(client.received), fake_sshd.BANNER + b"".join(payload))
        self.assertTrue(client.half_closed)  # client EOF -> FIN -> server EOF -> half-close
        self.assertEqual(event.outcome, rc.Outcome.CLOSED)
        self.assertEqual((event.bytes_up, event.bytes_down), (20000, 20000 + len(fake_sshd.BANNER)))
        self.assertEqual(self.connected, [(PUBLIC_V4, 22)])
        self.assertEqual(self.resolved, ["ln01.platform.example"])  # resolved exactly once
        self.assertEqual(event.authority, "user_owned")
        self.assertEqual(event.transport, "direct_public_tcp")

    async def test_audit_never_contains_payload(self):
        secret = b"PASSWORD-OTP-" + uuid.uuid4().hex.encode()
        client = Client([secret])
        await self.model().open(rc.Principal("alice"), self.first(), client.frames(), client.send)
        dumped = json.dumps([vars(e) for e in self.events], default=str)
        self.assertNotIn(secret.decode(), dumped)
        self.assertNotIn("NestFixture", dumped)
        self.assertEqual([e.event for e in self.events], ["relay.connected", "relay.closed"])

    async def test_client_cannot_name_a_destination(self):
        for extra in ({"host": "10.0.0.1"}, {"port": 25}, {"target": "x:1"}, {"socks": True}):
            with self.subTest(extra=extra):
                client = Client([])
                with self.assertRaises(rc.RelayError) as caught:
                    await self.model().open(
                        rc.Principal("alice"), self.first(**extra), client.frames(), client.send
                    )
                self.assertEqual(caught.exception.outcome, rc.Outcome.INVALID_REQUEST)
        self.assertEqual((self.resolved, self.connected), ([], []))

    async def test_denial_happens_before_dns_and_never_falls_back(self):
        for principal, first in (
            (rc.Principal("mallory"), self.first()),
            (rc.Principal("alice"), {"platform_id": str(uuid.uuid4()), "project_id": PROJECT}),
        ):
            client = Client([b"x"])
            with self.assertRaises(rc.RelayError) as caught:
                await self.model().open(principal, first, client.frames(), client.send)
            self.assertEqual(caught.exception.outcome, rc.Outcome.PERMISSION_DENIED)
        self.allowed.add(("alice", PLATFORM.replace("d03", "d04")))
        with self.assertRaises(rc.RelayError) as caught:
            client = Client([])
            other = {"platform_id": PLATFORM.replace("d03", "d04"), "project_id": PROJECT}
            await self.model().open(rc.Principal("alice"), other, client.frames(), client.send)
        self.assertEqual(caught.exception.outcome, rc.Outcome.PLATFORM_NOT_APPROVED)
        self.assertEqual((self.resolved, self.connected, self.sshd.connections), ([], [], 0))
        self.assertTrue(all(e.event == "relay.closed" for e in self.events))

    async def test_destination_policy(self):
        cases = [
            ({"answers": ["10.0.0.7"]}, "not_public"),
            ({"answers": ["100.80.1.1"]}, "tailnet_address"),
            ({"answers": [PUBLIC_V4, "127.0.0.1"]}, "not_public"),
            ({"answers": ["9.9.9.9"], "approved": frozenset({PUBLIC_V4})}, "address_not_approved"),
            ({"node_port": 2222}, "port_not_allowlisted"),
        ]
        for options, reason in cases:
            with self.subTest(reason=reason):
                client = Client([])
                with self.assertRaises(rc.RelayError) as caught:
                    await self.model(**options).open(
                        rc.Principal("alice"), self.first(), client.frames(), client.send
                    )
                self.assertEqual(caught.exception.outcome, rc.Outcome.DESTINATION_REJECTED)
                self.assertIn(reason, str(caught.exception))
        self.assertEqual(self.connected, [])

    async def test_rebinding_defense_connects_to_the_validated_literal(self):
        # A second lookup would answer with a private address; it never happens.
        answers = [[PUBLIC_V4], ["10.0.0.9"]]
        model = self.model(answers=answers)
        client = Client([b"hello"])
        await model.open(rc.Principal("alice"), self.first(), client.frames(), client.send)
        self.assertEqual(len(self.resolved), 1)
        self.assertEqual(self.connected, [(PUBLIC_V4, 22)])
        self.assertIsInstance(ipaddress.ip_address(self.connected[0][0]), ipaddress.IPv4Address)

    async def test_concurrency_limit_and_release(self):
        limits = rc.RelayLimits(max_streams_per_user_platform=1)
        model = self.model(limits=limits)
        held = Client([], hold_open=True)
        task = asyncio.create_task(
            model.open(rc.Principal("alice"), self.first(), held.frames(), held.send)
        )
        while not any(e.event == "relay.connected" for e in self.events):
            await asyncio.sleep(0.01)
        second = Client([])
        with self.assertRaises(rc.RelayError) as caught:
            await model.open(rc.Principal("alice"), self.first(), second.frames(), second.send)
        self.assertEqual(caught.exception.outcome, rc.Outcome.RESOURCE_EXHAUSTED)
        held.queue.put_nowait(None)
        await task
        self.assertEqual(model.admission.total, 0)
        third = Client([])
        await model.open(rc.Principal("alice"), self.first(), third.frames(), third.send)

    async def test_open_rate_limit(self):
        model = self.model(limits=rc.RelayLimits(max_opens_per_minute_per_user=2))
        for _ in range(2):
            client = Client([])
            await model.open(rc.Principal("alice"), self.first(), client.frames(), client.send)
        with self.assertRaises(rc.RelayError) as caught:
            client = Client([])
            await model.open(rc.Principal("alice"), self.first(), client.frames(), client.send)
        self.assertEqual(caught.exception.outcome, rc.Outcome.RESOURCE_EXHAUSTED)

    async def test_size_limits(self):
        model = self.model(limits=rc.RelayLimits(max_frame_bytes=1024))
        client = Client([b"x" * 1025])
        with self.assertRaises(rc.RelayError) as caught:
            await model.open(rc.Principal("alice"), self.first(), client.frames(), client.send)
        self.assertEqual(caught.exception.outcome, rc.Outcome.FRAME_TOO_LARGE)
        model = self.model(limits=rc.RelayLimits(max_total_bytes=4096))
        client = Client([b"y" * 1024] * 8)
        with self.assertRaises(rc.RelayError) as caught:
            await model.open(rc.Principal("alice"), self.first(), client.frames(), client.send)
        self.assertEqual(caught.exception.outcome, rc.Outcome.BYTE_LIMIT)
        self.assertEqual(model.admission.total, 0)

    async def test_idle_and_total_timeouts(self):
        model = self.model(limits=rc.RelayLimits(idle_timeout_s=0.2))
        client = Client([], hold_open=True)
        with self.assertRaises(rc.RelayError) as caught:
            await model.open(rc.Principal("alice"), self.first(), client.frames(), client.send)
        self.assertEqual(caught.exception.outcome, rc.Outcome.IDLE_TIMEOUT)
        model = self.model(limits=rc.RelayLimits(max_duration_s=0.3))

        async def chatty():
            while True:
                yield b"."
                await asyncio.sleep(0.05)

        with self.assertRaises(rc.RelayError) as caught:
            await model.open(rc.Principal("alice"), self.first(), chatty(), Client().send)
        self.assertEqual(caught.exception.outcome, rc.Outcome.DEADLINE_EXCEEDED)
        self.assertEqual(model.admission.total, 0)

    async def test_cancellation_closes_socket_and_releases(self):
        model = self.model()
        client = Client([b"abc"], hold_open=True)
        task = asyncio.create_task(
            model.open(rc.Principal("alice"), self.first(), client.frames(), client.send)
        )
        while len(client.received) < len(fake_sshd.BANNER) + 3:
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(model.admission.total, 0)
        self.assertEqual(self.events[-1].outcome, rc.Outcome.CANCELLED)
        for _ in range(100):
            if self.sshd.received:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.sshd.received, [3])  # the login-node side saw the close

    async def test_backpressure_bounds_in_flight_data(self):
        # A slow client: the relay must not read ahead of what it has delivered.
        model = self.model(limits=rc.RelayLimits(max_frame_bytes=1024))
        client = Client([b"z" * 1024] * 16, slow=0.01)
        event = await model.open(rc.Principal("alice"), self.first(), client.frames(), client.send)
        self.assertEqual(len(client.received), 16 * 1024 + len(fake_sshd.BANNER))
        self.assertEqual(event.outcome, rc.Outcome.CLOSED)

    async def test_connect_failure(self):
        async def refuse(address, port):
            raise ConnectionRefusedError

        client = Client([])
        with self.assertRaises(rc.RelayError) as caught:
            await self.model(connector=refuse).open(
                rc.Principal("alice"), self.first(), client.frames(), client.send
            )
        self.assertEqual(caught.exception.outcome, rc.Outcome.CONNECT_FAILED)

    async def test_upstream_reset_is_reported(self):
        await self.sshd.close()
        resetter = await asyncio.start_server(self._reset, "127.0.0.1", 0)
        port = resetter.sockets[0].getsockname()[1]

        async def connector(address, p):
            return await asyncio.open_connection("127.0.0.1", port)

        client = Client([], hold_open=True)
        try:
            with self.assertRaises(rc.RelayError) as caught:
                await self.model(connector=connector, limits=rc.RelayLimits(idle_timeout_s=2)).open(
                    rc.Principal("alice"), self.first(), client.frames(), client.send
                )
        finally:
            resetter.close()
        # A FIN-then-close looks like a clean EOF downstream; the client stays open
        # until idle timeout or an explicit reset. Either way the stream ends.
        self.assertIn(
            caught.exception.outcome, {rc.Outcome.UPSTREAM_ERROR, rc.Outcome.IDLE_TIMEOUT}
        )

    async def _reset(self, reader, writer):
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        writer.close()


class DraftProtoTests(unittest.TestCase):
    def test_no_destination_or_secret_fields(self):
        text = (POC / "relay.proto").read_text()
        body = re.sub(r"//.*", "", text)
        fields = re.findall(r"^\s*(?:repeated\s+)?\w+\s+(\w+)\s*=\s*\d+;", body, re.MULTILINE)
        self.assertIn("platform_id", fields)
        banned = re.compile(r"host|port|addr|url|target|dest|password|otp|token|key|secret")
        self.assertEqual([f for f in fields if banned.search(f)], [])
        self.assertIn("rpc Open(stream ClientFrame) returns (stream ServerFrame)", body)

    def test_open_request_fields_match_the_draft(self):
        body = re.sub(r"//.*", "", (POC / "relay.proto").read_text())
        message = re.search(r"message OpenRelay \{(.*?)\}", body, re.DOTALL)
        assert message is not None
        names = set(re.findall(r"string (\w+) =", message.group(1)))
        self.assertEqual(names, set(rc.OpenRequest.FIELDS))


if __name__ == "__main__":
    unittest.main()
