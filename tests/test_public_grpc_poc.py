"""
The H06 public gRPC transport PoC harness (`poc/public-grpc`) is itself correct:
its matrix passes against a TLS origin with no gateway, it never prints the token,
its negative checks fail when they should, and its origin never executes code.
"""

import contextlib
import importlib.util
import io
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import grpc

POC = Path(__file__).resolve().parents[1] / "poc" / "public-grpc"


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"h06_{name}", POC / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through it
    spec.loader.exec_module(module)
    return module


make_certs = load("make_certs")
origin = load("origin")
probe = load("probe")

TOKEN = "synthetic-h06-token-0123456789abcdef"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PublicGrpcPocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        certs = Path(cls.tmp.name)
        make_certs.make(certs)
        cls.ca = str(certs / "ca.crt")
        credentials = grpc.ssl_server_credentials(
            [((certs / "server.key").read_bytes(), (certs / "server.crt").read_bytes())]
        )
        check = origin.TokenCheck.from_token(TOKEN)
        cls.server, cls.port = origin.build_server(check, "127.0.0.1:0", credentials=credentials)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop(0).wait()
        cls.tmp.cleanup()

    def target(self, token: str = TOKEN) -> probe.Target:
        return probe.Target(f"127.0.0.1:{self.port}", make_certs.DEFAULT_NAME, self.ca, token)

    def args(self, *extra: str):
        return probe.parser().parse_args(["unused", "--stream-seconds", "2", *extra])

    def test_matrix_passes_against_a_tls_origin(self):
        closed = f"127.0.0.1:{free_port()}"
        with patch("nest.controller.execute") as execute:
            results = probe.matrix(self.target(), self.args("--silent", "1", "--origin", closed))
        failed = {r.check: r.detail for r in results if not r.passed}
        self.assertEqual(failed, {})
        names = {r.check for r in results}
        for expected in (
            "tls_certificate_sni_alpn_h2",
            "controller_unauthenticated",
            "controller_authenticated_probe",
            "unary_metadata_trailers",
            "status_codes",
            "bidirectional_stream",
            "server_stream",
            "deadline_propagation",
            "client_cancellation",
            "message_limits",
            "silent_unary_1s",
            f"origin_unreachable_{closed}",
        ):
            self.assertIn(expected, names)
        execute.assert_not_called()

    def test_output_never_contains_the_token(self):
        token_file = Path(self.tmp.name) / "token"
        token_file.write_text(TOKEN)
        argv = [
            "probe.py",
            f"127.0.0.1:{self.port}",
            "--server-name",
            make_certs.DEFAULT_NAME,
            "--ca-file",
            self.ca,
            "--stream-seconds",
            "2",
        ]
        out = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.dict("os.environ", NEST_POC_TOKEN_FILE=str(token_file)),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit) as caught,
        ):
            probe.main()
        self.assertEqual(caught.exception.code, 0, out.getvalue())
        self.assertNotIn(TOKEN, out.getvalue())
        self.assertIn('"failed": []', out.getvalue())

    def test_a_wrong_token_is_refused_by_both_services(self):
        target = self.target("wrong-token-wrong-token-wrong")
        with target.channel() as channel:
            ok, _, _ = probe.check_controller_authenticated(channel, target)
            self.assertFalse(ok)
            with self.assertRaises(grpc.RpcError) as caught:
                probe._unary(channel, "Echo")(b"", metadata=target.metadata(), timeout=5)
        error = cast(grpc.Call, caught.exception)
        self.assertEqual(error.code(), grpc.StatusCode.UNAUTHENTICATED)

    def test_a_reachable_origin_fails_the_bypass_check(self):
        server, port = origin.build_server(origin.TokenCheck.from_token(TOKEN), "127.0.0.1:0")
        server.start()
        try:
            ok, detail, data = probe.check_origin_unreachable(f"127.0.0.1:{port}", TOKEN)
        finally:
            server.stop(0).wait()
        self.assertFalse(ok)
        self.assertEqual(data, {"tcp": "open", "grpc": "accepted"}, detail)

    def test_synthetic_access_authorizes_nothing_beyond_authentication(self):
        access = origin.SyntheticAccess(origin.TokenCheck.from_token(TOKEN))
        with self.assertRaises(RuntimeError):
            access.project_of(())
        with self.assertRaises(origin.UnauthenticatedError):
            access.authenticate((("authorization", "Bearer nope"),), "Execute")

    def test_short_tokens_are_refused(self):
        with self.assertRaises(ValueError):
            origin.TokenCheck.from_token("short")


if __name__ == "__main__":
    unittest.main()
