"""Zero-config defaults for Cerulean-hosted Nest, and their safety boundaries.

Covers how the CLI and the SDK resolve the sign-in origin (`--url`, then
`NEST_AUTH_URL`, then the hosted default), that the hosted Controller never
becomes a default address or a plaintext default, and that stored sessions are
scoped to their sign-in origin: a custom or local Controller never receives the
hosted session or starts a hosted sign-in. None of these tests needs a database
or the network; the gRPC tests run a recording Controller on loopback.
"""

import io
import json
import os
import secrets
import stat
import tempfile
import types
import unittest
import unittest.mock
import uuid
from concurrent import futures
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import cloudpickle
import grpc

from nest import cli as nest_cli
from nest import client as nest_client
from nest import login as nest_login
from nest.config import (
    DEFAULT_CONTROLLER_ADDRESS,
    HOSTED_AUTH_URL,
    HOSTED_CONTROLLER_ADDRESS,
    ConfigError,
    is_hosted_controller,
    load_cli_auth_config,
    load_client_config,
)
from nest.errors import AuthenticationRequiredError
from nest.login import CredentialsFileError, CredentialStore, NestSession
from nest.proto import execution_pb2, execution_pb2_grpc

CUSTOM_AUTH_URL = "https://nest.example.com"


def session_for(auth_url: str, **overrides) -> NestSession:
    now = datetime.now(UTC)
    values = {
        "auth_url": auth_url,
        "user_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "scope": "jobs:run projects:read",
        "access_token": "access-" + secrets.token_urlsafe(24),
        "refresh_token": "refresh-" + secrets.token_urlsafe(24),
        "access_expires_at": now + timedelta(minutes=15),
        "refresh_expires_at": now + timedelta(hours=24),
        "session_expires_at": now + timedelta(days=7),
    }
    values.update(overrides)
    return NestSession(**values)


class _Credentials(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        os.chmod(directory.name, 0o700)
        self.path = os.path.join(directory.name, "nest", "credentials.json")
        self.store = CredentialStore(self.path)


class AuthURLResolutionTests(unittest.TestCase):
    def test_cli_defaults_to_the_hosted_origin(self):
        for env in ({}, {"NEST_AUTH_URL": ""}, {"NEST_AUTH_URL": "   "}):
            with self.subTest(env=env):
                config = load_cli_auth_config({"HOME": "/home/u", **env})
                self.assertEqual(config.url, HOSTED_AUTH_URL)
                self.assertEqual(config.url_source, "hosted default")
        self.assertEqual(HOSTED_AUTH_URL, "https://auth.nest.cerulean.works")

    def test_cli_precedence_is_url_then_environment_then_hosted(self):
        env = {"HOME": "/home/u", "NEST_AUTH_URL": "https://Self.Example.org/"}
        config = load_cli_auth_config(env)
        self.assertEqual(
            (config.url, config.url_source), ("https://self.example.org", "NEST_AUTH_URL")
        )
        config = load_cli_auth_config(env, url="http://127.0.0.1:8080")
        self.assertEqual((config.url, config.url_source), ("http://127.0.0.1:8080", "--url"))
        # A blank --url is no override.
        config = load_cli_auth_config(env, url="  ")
        self.assertEqual(config.url_source, "NEST_AUTH_URL")

    def test_invalid_explicit_values_never_fall_back_to_hosted(self):
        for env, url, expected in (
            ({"NEST_AUTH_URL": "http://self.example.org"}, None, "NEST_AUTH_URL must be an https"),
            ({}, "http://self.example.org", "--url must be an https"),
            ({"NEST_AUTH_URL": "https://ok.example.org"}, "nonsense", "--url must be"),
        ):
            with self.subTest(env=env, url=url), self.assertRaises(ConfigError) as caught:
                load_cli_auth_config({"HOME": "/home/u", **env}, url=url)
            self.assertIn(expected, str(caught.exception))
        # The SDK too: an invalid NEST_AUTH_URL is an error even for the hosted Controller.
        with self.assertRaisesRegex(ConfigError, "NEST_AUTH_URL must be an https"):
            load_client_config(
                {
                    "NEST_CONTROLLER_ADDRESS": HOSTED_CONTROLLER_ADDRESS,
                    "NEST_CONTROLLER_TLS": "false",
                    "NEST_AUTH_URL": "http://self.example.org",
                }
            )

    def test_sdk_uses_hosted_sign_in_only_for_the_hosted_controller(self):
        cases = [
            ({}, None),  # the default local Controller
            ({"NEST_AUTH_URL": ""}, None),
            ({"NEST_CONTROLLER_ADDRESS": "localhost:50051"}, None),
            (
                {
                    "NEST_CONTROLLER_ADDRESS": "controller.example.edu:50051",
                    "NEST_CONTROLLER_TLS": "true",
                },
                None,
            ),
            # Same host on another port is not the hosted Controller.
            (
                {
                    "NEST_CONTROLLER_ADDRESS": "nest.cerulean.works:443",
                    "NEST_CONTROLLER_TLS": "true",
                },
                None,
            ),
            (
                {
                    "NEST_CONTROLLER_ADDRESS": "evil-nest.cerulean.works.example:50051",
                    "NEST_CONTROLLER_TLS": "true",
                },
                None,
            ),
            (
                {
                    "NEST_CONTROLLER_ADDRESS": HOSTED_CONTROLLER_ADDRESS,
                    "NEST_CONTROLLER_TLS": "false",
                },
                (HOSTED_AUTH_URL, "hosted default"),
            ),
            (
                {
                    "NEST_CONTROLLER_ADDRESS": "NEST.Cerulean.Works.:50051",
                    "NEST_CONTROLLER_TLS": "false",
                },
                (HOSTED_AUTH_URL, "hosted default"),
            ),
            # An explicit NEST_AUTH_URL always wins, for any Controller.
            (
                {
                    "NEST_CONTROLLER_ADDRESS": HOSTED_CONTROLLER_ADDRESS,
                    "NEST_CONTROLLER_TLS": "false",
                    "NEST_AUTH_URL": CUSTOM_AUTH_URL,
                },
                (CUSTOM_AUTH_URL, "NEST_AUTH_URL"),
            ),
            ({"NEST_AUTH_URL": CUSTOM_AUTH_URL}, (CUSTOM_AUTH_URL, "NEST_AUTH_URL")),
        ]
        for env, expected in cases:
            with self.subTest(env=env):
                config = load_client_config(env)
                got = (config.auth_url, config.auth_url_source)
                self.assertEqual(got, expected or (None, None))

    def test_hosted_controller_is_never_a_default_address_or_plaintext_default(self):
        self.assertEqual(load_client_config({}).address, DEFAULT_CONTROLLER_ADDRESS)
        self.assertEqual(DEFAULT_CONTROLLER_ADDRESS, "127.0.0.1:50051")
        # Naming the hosted Controller still needs an explicit transport choice.
        with self.assertRaisesRegex(ConfigError, "requires an explicit NEST_CONTROLLER_TLS"):
            load_client_config({"NEST_CONTROLLER_ADDRESS": HOSTED_CONTROLLER_ADDRESS})
        self.assertTrue(is_hosted_controller(HOSTED_CONTROLLER_ADDRESS))
        for other in ("nest.cerulean.works", "127.0.0.1:50051", "auth.nest.cerulean.works:50051"):
            with self.subTest(other=other):
                self.assertFalse(is_hosted_controller(other))


class CredentialScopingTests(_Credentials):
    def test_sessions_of_different_origins_are_kept_apart(self):
        hosted, custom = session_for(HOSTED_AUTH_URL), session_for(CUSTOM_AUTH_URL)
        self.store.save(custom)
        self.store.save(hosted)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(self.store.load(HOSTED_AUTH_URL), hosted)
        self.assertEqual(self.store.load(CUSTOM_AUTH_URL), custom)
        self.assertIsNone(self.store.load("https://third.example.com"))
        # Replacing one origin's session keeps the other.
        newer = session_for(HOSTED_AUTH_URL)
        self.store.save(newer)
        self.assertEqual(self.store.load(HOSTED_AUTH_URL), newer)
        self.assertEqual(self.store.load(CUSTOM_AUTH_URL), custom)
        self.assertTrue(self.store.remove(HOSTED_AUTH_URL))
        self.assertFalse(self.store.remove(HOSTED_AUTH_URL))
        self.assertEqual(self.store.load(CUSTOM_AUTH_URL), custom)
        self.assertTrue(self.store.remove(CUSTOM_AUTH_URL))
        self.assertFalse(os.path.exists(self.path))

    def test_single_session_files_from_earlier_versions_are_read(self):
        custom = session_for(CUSTOM_AUTH_URL)
        os.makedirs(os.path.dirname(self.path), mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, json.dumps(custom.to_json()).encode())
        os.close(fd)
        self.assertEqual(self.store.load(CUSTOM_AUTH_URL), custom)
        self.assertIsNone(self.store.load(HOSTED_AUTH_URL))
        self.store.save(session_for(HOSTED_AUTH_URL))
        self.assertEqual(self.store.load(CUSTOM_AUTH_URL), custom)
        data = json.loads(Path(self.path).read_text())
        self.assertEqual((data["kind"], data["version"]), ("nest-cli-session", 2))
        self.assertEqual(
            [s["auth_url"] for s in data["sessions"]], sorted([HOSTED_AUTH_URL, CUSTOM_AUTH_URL])
        )

    def test_file_readable_by_others_is_refused_and_logout_deletes_it(self):
        self.store.save(session_for(CUSTOM_AUTH_URL))
        os.chmod(self.path, 0o644)
        with self.assertRaises(CredentialsFileError):
            self.store.load(HOSTED_AUTH_URL)
        config = load_cli_auth_config({"NEST_CLI_CREDENTIALS_FILE": self.path})
        result = nest_login.logout(config, revoke=True)  # never sends the unsafe secret
        self.assertTrue(result.removed)
        self.assertIsNone(result.revoked)
        self.assertFalse(os.path.exists(self.path))

    def test_hosted_logout_keeps_a_self_hosted_session(self):
        custom = session_for(CUSTOM_AUTH_URL)
        self.store.save(custom)
        config = load_cli_auth_config({"NEST_CLI_CREDENTIALS_FILE": self.path})
        with unittest.mock.patch.object(nest_login.AuthClient, "request") as request:
            result = nest_login.logout(config)
        request.assert_not_called()  # nothing stored for the hosted origin to revoke
        self.assertFalse(result.removed)
        self.assertEqual(self.store.load(CUSTOM_AUTH_URL), custom)

    def test_ended_session_is_removed_alone(self):
        custom = session_for(CUSTOM_AUTH_URL)
        ended = session_for(
            HOSTED_AUTH_URL,
            access_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            refresh_expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        self.store.save(custom)
        self.store.save(ended)
        config = load_cli_auth_config({"NEST_CLI_CREDENTIALS_FILE": self.path})
        with self.assertRaises(nest_login.NotSignedInError):
            nest_login.ensure_session(config, interactive=False)
        self.assertIsNone(self.store.load(HOSTED_AUTH_URL))
        self.assertEqual(self.store.load(CUSTOM_AUTH_URL), custom)

    def test_hosted_login_keeps_and_never_revokes_another_nest_session(self):
        custom = session_for(CUSTOM_AUTH_URL)
        self.store.save(custom)
        fresh = session_for(HOSTED_AUTH_URL)
        answers = iter(
            [
                (
                    200,
                    {
                        "device_code": "d" * 40,
                        "user_code": "BCDF-GHJK",
                        "verification_uri_complete": HOSTED_AUTH_URL
                        + "/auth/cli/approve?code=BCDF-GHJK",
                        "expires_in": 600,
                        "interval": 1,
                    },
                ),
                (200, {**fresh.to_json()}),
            ]
        )
        paths = []

        def request(_client, path, payload=None, *, bearer=None):
            paths.append((_client.auth_url, path))
            return next(answers)

        config = load_cli_auth_config({"NEST_CLI_CREDENTIALS_FILE": self.path})
        out = io.StringIO()
        with unittest.mock.patch.object(nest_login.AuthClient, "request", request):
            session = nest_login.login(config, open_browser=False, out=out, sleep=lambda _: None)
        self.assertEqual(session.auth_url, HOSTED_AUTH_URL)
        self.assertIn(
            "Signing in to Cerulean-hosted Nest at https://auth.nest.cerulean.works", out.getvalue()
        )
        self.assertEqual(
            paths,
            [(HOSTED_AUTH_URL, nest_login.START_PATH), (HOSTED_AUTH_URL, nest_login.TOKEN_PATH)],
        )
        self.assertEqual(self.store.load(CUSTOM_AUTH_URL), custom)
        stored = self.store.load(HOSTED_AUTH_URL)
        assert stored is not None
        self.assertEqual(stored.session_id, fresh.session_id)

    def test_explicit_login_names_no_hosted_default(self):
        config = load_cli_auth_config(
            {"NEST_CLI_CREDENTIALS_FILE": self.path, "NEST_AUTH_URL": CUSTOM_AUTH_URL}
        )
        start = {
            "device_code": "d" * 40,
            "user_code": "BCDF-GHJK",
            "verification_uri_complete": HOSTED_AUTH_URL + "/auth/cli/approve?code=X",
            "expires_in": 600,
        }
        out = io.StringIO()
        with (
            unittest.mock.patch.object(nest_login.AuthClient, "request", return_value=(200, start)),
            self.assertRaisesRegex(nest_login.AuthError, "another site"),
        ):
            nest_login.login(config, open_browser=False, out=out)
        self.assertNotIn("Cerulean-hosted", out.getvalue())


class _RecordingController(execution_pb2_grpc.ExecutionServiceServicer):
    """Records each call's metadata; refuses unauthenticated calls when told to."""

    def __init__(self, require_session: bool):
        self.require_session = require_session
        self.calls: list[dict[str, str]] = []

    def Execute(self, request, context):
        metadata = {k: v for k, v in context.invocation_metadata()}
        self.calls.append(metadata)
        if self.require_session and "authorization" not in metadata:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Authentication required")
        func, args, kwargs = cloudpickle.loads(request.payload)
        return execution_pb2.ExecuteResponse(result=cloudpickle.dumps(func(*args, **kwargs)))


class CustomControllerDisclosureTests(_Credentials):
    """A custom Controller never receives the hosted session or starts a hosted sign-in."""

    def serve(self, *, require_session: bool) -> tuple[_RecordingController, str]:
        controller = _RecordingController(require_session)
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(controller, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, None)
        return controller, f"127.0.0.1:{port}"

    def run_remote(self, env):
        environment = {
            "NEST_CLI_CREDENTIALS_FILE": self.path,
            "NEST_AUTH_URL": "",
            "NEST_PROJECT_ID": "",
            "NEST_CONTROLLER_TLS": "",
            "NEST_CONTROLLER_TLS_CA_FILE": "",
            **env,
        }
        resources = types.SimpleNamespace(gpu=0, cpu=0)
        with unittest.mock.patch.dict(os.environ, environment):
            return nest_client.execute_remote(lambda x: x * 2, resources, None, 21)

    def test_custom_controller_gets_no_hosted_session(self):
        hosted = session_for(HOSTED_AUTH_URL)
        self.store.save(hosted)
        controller, address = self.serve(require_session=False)
        self.assertEqual(self.run_remote({"NEST_CONTROLLER_ADDRESS": address}), 42)
        [metadata] = controller.calls
        self.assertNotIn("authorization", metadata)
        self.assertNotIn(hosted.access_token, json.dumps(metadata))

    def test_refusal_by_a_custom_controller_never_starts_a_hosted_sign_in(self):
        self.store.save(session_for(HOSTED_AUTH_URL))
        controller, address = self.serve(require_session=True)
        with (
            unittest.mock.patch("sys.stdin.isatty", return_value=True),
            unittest.mock.patch.object(nest_login, "login") as login,
            unittest.mock.patch.object(nest_login.AuthClient, "request") as request,
            self.assertRaises(AuthenticationRequiredError) as caught,
        ):
            self.run_remote({"NEST_CONTROLLER_ADDRESS": address})
        login.assert_not_called()
        request.assert_not_called()
        self.assertEqual(len(controller.calls), 1)
        self.assertIn("NEST_AUTH_URL", str(caught.exception))
        self.assertIn(HOSTED_CONTROLLER_ADDRESS, str(caught.exception))

    def test_explicit_auth_url_sends_only_that_nest_session(self):
        hosted, custom = session_for(HOSTED_AUTH_URL), session_for(CUSTOM_AUTH_URL)
        self.store.save(hosted)
        self.store.save(custom)
        controller, address = self.serve(require_session=True)
        result = self.run_remote(
            {"NEST_CONTROLLER_ADDRESS": address, "NEST_AUTH_URL": CUSTOM_AUTH_URL}
        )
        self.assertEqual(result, 42)
        [metadata] = controller.calls
        self.assertEqual(metadata["authorization"], f"Bearer {custom.access_token}")


class HostedProfileTests(_Credentials):
    def hosted_config(self):
        return load_client_config(
            {
                "NEST_CONTROLLER_ADDRESS": HOSTED_CONTROLLER_ADDRESS,
                "NEST_CONTROLLER_TLS": "false",
                "NEST_PROJECT_ID": str(uuid.uuid4()),
                "NEST_CLI_CREDENTIALS_FILE": self.path,
            }
        )

    def test_hosted_controller_receives_the_hosted_session(self):
        hosted = session_for(HOSTED_AUTH_URL)
        self.store.save(session_for(CUSTOM_AUTH_URL))
        self.store.save(hosted)
        with unittest.mock.patch.dict(os.environ, {"NEST_CLI_CREDENTIALS_FILE": self.path}):
            metadata = dict(nest_client.session_metadata(self.hosted_config(), interactive=False))
        self.assertEqual(metadata["authorization"], f"Bearer {hosted.access_token}")

    def test_hosted_profile_is_non_interactive_without_a_session(self):
        self.store.save(session_for(CUSTOM_AUTH_URL))
        with (
            unittest.mock.patch.dict(os.environ, {"NEST_CLI_CREDENTIALS_FILE": self.path}),
            unittest.mock.patch.object(nest_login, "login") as login,
            unittest.mock.patch.object(nest_login.AuthClient, "request") as request,
        ):
            metadata = dict(nest_client.session_metadata(self.hosted_config(), interactive=False))
        self.assertNotIn("authorization", metadata)
        login.assert_not_called()
        request.assert_not_called()

    def test_hosted_session_is_refreshed_at_the_hosted_origin(self):
        stale = session_for(HOSTED_AUTH_URL, access_expires_at=datetime.now(UTC))
        self.store.save(stale)
        fresh = session_for(HOSTED_AUTH_URL, session_id=stale.session_id)
        calls = []

        def request(client, path, payload=None, *, bearer=None):
            calls.append((client.auth_url, path))
            return 200, fresh.to_json()

        with (
            unittest.mock.patch.dict(os.environ, {"NEST_CLI_CREDENTIALS_FILE": self.path}),
            unittest.mock.patch.object(nest_login.AuthClient, "request", request),
        ):
            metadata = dict(nest_client.session_metadata(self.hosted_config(), interactive=False))
        self.assertEqual(calls, [(HOSTED_AUTH_URL, nest_login.REFRESH_PATH)])
        self.assertEqual(metadata["authorization"], f"Bearer {fresh.access_token}")

    def test_unauthenticated_message_names_the_origin(self):
        class Call(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.UNAUTHENTICATED

            def trailing_metadata(self):
                return ()

            def details(self):
                return ""

        error = nest_client._controller_error(Call(), "c:1", 1, auth_url=HOSTED_AUTH_URL)
        self.assertIsInstance(error, AuthenticationRequiredError)
        self.assertIn(f"nest login --url {HOSTED_AUTH_URL}", str(error))


class CLIDefaultTests(_Credentials):
    def run_cli(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        environment = {
            "NEST_CLI_CREDENTIALS_FILE": self.path,
            "NEST_AUTH_URL": "",
            "NEST_CONTROLLER_ADDRESS": "",
            **env,
        }
        with (
            unittest.mock.patch.dict(os.environ, environment),
            unittest.mock.patch.object(nest_cli, "login") as login,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = nest_cli.main(argv)
        return code, login, out.getvalue(), err.getvalue()

    def test_login_without_settings_uses_the_hosted_origin(self):
        code, login, _, err = self.run_cli(["login", "--no-browser"], {})
        self.assertEqual(code, 0)
        [call] = login.call_args_list
        self.assertEqual(call.args[0].url, HOSTED_AUTH_URL)
        self.assertEqual(err, "")
        code, login, _, _ = self.run_cli(
            ["login", "--no-browser"],
            {"NEST_CONTROLLER_ADDRESS": HOSTED_CONTROLLER_ADDRESS},
        )
        self.assertEqual(login.call_args.args[0].url, HOSTED_AUTH_URL)

    def test_login_warns_when_a_custom_controller_would_not_receive_it(self):
        _, login, _, err = self.run_cli(
            ["login"], {"NEST_CONTROLLER_ADDRESS": "controller.example.edu:50051"}
        )
        self.assertEqual(login.call_args.args[0].url, HOSTED_AUTH_URL)
        self.assertIn("is not the hosted Controller", err)
        # An explicit origin is the user's choice: no warning.
        _, login, _, err = self.run_cli(
            ["login"],
            {
                "NEST_CONTROLLER_ADDRESS": "controller.example.edu:50051",
                "NEST_AUTH_URL": CUSTOM_AUTH_URL,
            },
        )
        self.assertEqual(login.call_args.args[0].url, CUSTOM_AUTH_URL)
        self.assertEqual(err, "")

    def test_status_reports_the_origin_and_its_source(self):
        code, _, out, _ = self.run_cli(["auth", "status", "--json"], {})
        self.assertEqual(code, 1)
        info = json.loads(out)
        self.assertEqual(
            (info["auth_url"], info["auth_url_source"]), (HOSTED_AUTH_URL, "hosted default")
        )
        code, _, out, _ = self.run_cli(
            ["auth", "status", "--json", "--url", CUSTOM_AUTH_URL],
            {"NEST_AUTH_URL": "https://x.example"},
        )
        info = json.loads(out)
        self.assertEqual((info["auth_url"], info["auth_url_source"]), (CUSTOM_AUTH_URL, "--url"))


if __name__ == "__main__":
    unittest.main()
