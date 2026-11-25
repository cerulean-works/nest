"""E08: the first-party hosted connection profile and its safety boundaries.

Covers the settings contract (an explicit opt-in until H09, no mixing with custom
endpoint settings, the hosted session bound to the hosted Controller), verified TLS
with no plaintext fallback (wrong roots, wrong name and a plaintext server all fail
before any call reaches a Controller), credential isolation for custom endpoints,
the channel options (no retries, keepalive), and the zero-configuration flow
(install, sign in on first call, select the only Project, run) with the hosted
default switched on in the test only. Everything runs on loopback.
"""

import io
import json
import types
import uuid
from unittest import mock

from hosted_fixture import (
    CertificateAuthority,
    HostedCase,
    RecordingController,
    serve,
    session_for,
)

from nest import cli as nest_cli
from nest import client as nest_client
from nest import config as nest_config
from nest import login as nest_login
from nest.client import channel_options, execute_remote
from nest.config import (
    DEFAULT_CONTROLLER_ADDRESS,
    HOSTED_AUTH_URL,
    HOSTED_CONTROLLER_ADDRESS,
    HOSTED_GRPC_ADDRESS,
    HOSTED_PROFILE_DEFAULT,
    PUBLIC_GATEWAY_IDLE_TIMEOUT,
    ConfigError,
    is_hosted_controller,
    load_client_config,
)

CUSTOM_AUTH_URL = "https://nest.example.com"
RESOURCES = types.SimpleNamespace(gpu=0, cpu=0)


def double(x):
    return x * 2


class HostedSettingsTests(HostedCase):
    def test_hosted_default_is_inactive_until_h09(self):
        self.assertIs(HOSTED_PROFILE_DEFAULT, False)
        config = load_client_config({})
        self.assertEqual((config.address, config.profile), (DEFAULT_CONTROLLER_ADDRESS, "local"))
        self.assertFalse(config.tls)
        self.assertIsNone(config.auth_url)
        self.assertEqual(HOSTED_GRPC_ADDRESS, "grpc.nest.cerulean.works:443")

    def test_explicit_hosted_profile(self):
        config = load_client_config({"NEST_PROFILE": "Hosted"})
        self.assertEqual(config.address, HOSTED_GRPC_ADDRESS)
        self.assertEqual(config.profile, "hosted")
        self.assertTrue(config.tls)
        self.assertIsNone(config.tls_ca_file)
        self.assertIsNone(config.tls_root_certificates)  # the system's public roots
        self.assertEqual(
            (config.auth_url, config.auth_url_source), (HOSTED_AUTH_URL, "hosted profile")
        )
        self.assertEqual(config.keepalive, 30)
        # The same origin named explicitly is fine.
        same = load_client_config(
            {"NEST_PROFILE": "hosted", "NEST_AUTH_URL": HOSTED_AUTH_URL + "/"}
        )
        self.assertEqual(same.auth_url, HOSTED_AUTH_URL)

    def test_hosted_profile_refuses_custom_endpoint_settings(self):
        for extra in (
            {"NEST_CONTROLLER_ADDRESS": "controller.example.edu:443"},
            {"NEST_CONTROLLER_TLS": "false"},
            {"NEST_CONTROLLER_TLS": "true"},
        ):
            with (
                self.subTest(extra=extra),
                self.assertRaisesRegex(ConfigError, "fixes the Controller"),
            ):
                load_client_config({"NEST_PROFILE": "hosted", **extra})

    def test_hosted_profile_accepts_only_hosted_sessions(self):
        with self.assertRaisesRegex(ConfigError, "accepts only sessions from"):
            load_client_config({"NEST_PROFILE": "hosted", "NEST_AUTH_URL": CUSTOM_AUTH_URL})
        with self.assertRaisesRegex(ConfigError, "NEST_AUTH_URL must be an https"):
            load_client_config({"NEST_PROFILE": "hosted", "NEST_AUTH_URL": "http://x.example"})

    def test_naming_the_gateway_applies_the_hosted_rules(self):
        config = load_client_config(
            {
                "NEST_CONTROLLER_ADDRESS": "GRPC.nest.cerulean.works.:443",
                "NEST_CONTROLLER_TLS": "true",
            }
        )
        self.assertEqual((config.profile, config.address), ("hosted", HOSTED_GRPC_ADDRESS))
        self.assertEqual(config.auth_url, HOSTED_AUTH_URL)
        self.assertTrue(is_hosted_controller(HOSTED_GRPC_ADDRESS))
        with self.assertRaisesRegex(ConfigError, "serves only verified TLS"):
            load_client_config(
                {"NEST_CONTROLLER_ADDRESS": HOSTED_GRPC_ADDRESS, "NEST_CONTROLLER_TLS": "false"}
            )
        ca = self.home / "ca.pem"
        ca.write_bytes(CertificateAuthority().pem)
        with self.assertRaisesRegex(ConfigError, "public roots"):
            load_client_config(
                {
                    "NEST_CONTROLLER_ADDRESS": HOSTED_GRPC_ADDRESS,
                    "NEST_CONTROLLER_TLS_CA_FILE": str(ca),
                }
            )
        with self.assertRaisesRegex(ConfigError, "accepts only sessions from"):
            load_client_config(
                {
                    "NEST_CONTROLLER_ADDRESS": HOSTED_GRPC_ADDRESS,
                    "NEST_CONTROLLER_TLS": "true",
                    "NEST_AUTH_URL": CUSTOM_AUTH_URL,
                }
            )
        # Lookalikes are custom Controllers and get no hosted session.
        for other in (
            "grpc.nest.cerulean.works:50051",
            "grpc.nest.cerulean.works.evil.example:443",
        ):
            with self.subTest(other=other):
                custom = load_client_config(
                    {"NEST_CONTROLLER_ADDRESS": other, "NEST_CONTROLLER_TLS": "true"}
                )
                self.assertEqual((custom.profile, custom.auth_url), ("custom", None))

    def test_other_settings(self):
        with self.assertRaisesRegex(ConfigError, "NEST_PROFILE must be one of"):
            load_client_config({"NEST_PROFILE": "public"})
        with self.assertRaisesRegex(ConfigError, "NEST_RPC_TIMEOUT must be at most"):
            load_client_config(
                {"NEST_PROFILE": "hosted", "NEST_RPC_TIMEOUT": str(PUBLIC_GATEWAY_IDLE_TIMEOUT + 1)}
            )
        for value, expected in (("0", 0.0), ("45", 45.0)):
            config = load_client_config(
                {"NEST_PROFILE": "hosted", "NEST_CONTROLLER_KEEPALIVE": value}
            )
            self.assertEqual(config.keepalive, expected)
        for value in ("5", "301"):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "KEEPALIVE"):
                load_client_config({"NEST_CONTROLLER_KEEPALIVE": value})
        with self.assertRaisesRegex(ConfigError, "NEST_PREFERENCES_FILE must be an absolute"):
            load_client_config({"NEST_PREFERENCES_FILE": "prefs.json"})

    def test_activated_default_keeps_explicit_and_local_choices(self):
        with mock.patch.object(nest_config, "HOSTED_PROFILE_DEFAULT", True):
            self.assertEqual(load_client_config({}).profile, "hosted")
            self.assertEqual(load_client_config({"NEST_PROFILE": "local"}).profile, "local")
            custom = load_client_config(
                {"NEST_CONTROLLER_ADDRESS": "nest.lab.example:50051", "NEST_CONTROLLER_TLS": "true"}
            )
            self.assertEqual((custom.profile, custom.auth_url), ("custom", None))
            local = load_client_config({"NEST_CONTROLLER_ADDRESS": "127.0.0.1:50051"})
            self.assertEqual((local.profile, local.tls), ("custom", False))
            # The Tailnet path stays available for rollback.
            tailnet = load_client_config(
                {
                    "NEST_CONTROLLER_ADDRESS": HOSTED_CONTROLLER_ADDRESS,
                    "NEST_CONTROLLER_TLS": "false",
                }
            )
            self.assertEqual(
                (tailnet.address, tailnet.auth_url), (HOSTED_CONTROLLER_ADDRESS, HOSTED_AUTH_URL)
            )

    def test_channel_never_retries_and_keeps_alive_only_when_asked(self):
        local = load_client_config({})
        self.assertEqual(channel_options(local), [("grpc.enable_retries", 0)])
        hosted = dict(channel_options(load_client_config({"NEST_PROFILE": "hosted"})))
        self.assertEqual(hosted["grpc.enable_retries"], 0)
        self.assertEqual(hosted["grpc.keepalive_time_ms"], 30_000)
        self.assertEqual(hosted["grpc.keepalive_permit_without_calls"], 0)
        self.assertNotIn("grpc.ssl_target_name_override", hosted)


class HostedTransportTests(HostedCase):
    """Verified TLS to the hosted profile, with no plaintext fallback."""

    def setUp(self):
        super().setUp()
        self.ca = CertificateAuthority()
        self.session = session_for(HOSTED_AUTH_URL)
        self.store.save(self.session)
        self.project = str(uuid.uuid4())

    def run_hosted(self, **env):
        with mock.patch.dict("os.environ", {"NEST_PROFILE": "hosted", **env}):
            return execute_remote(double, RESOURCES, None, 21)

    def test_verified_tls_carries_the_hosted_session_and_project(self):
        controller = RecordingController(projects=[(self.project, "alpha")])
        port = serve(self, controller, tls=self.ca.issue("localhost"))
        self.hosted_at(port, self.ca.pem)
        self.assertEqual(self.run_hosted(), 42)
        self.assertEqual(controller.methods(), ["ListEligibleProjects", "Execute"])
        _, metadata = controller.calls[-1]
        self.assertEqual(metadata["authorization"], f"Bearer {self.session.access_token}")
        self.assertEqual(metadata["x-nest-project-id"], self.project)

    def test_untrusted_or_mismatched_certificate_and_plaintext_fail_closed(self):
        other = CertificateAuthority("unrelated CA")
        cases = {
            "untrusted CA": other.issue("localhost"),
            "wrong name": self.ca.issue("grpc.other.example"),
            "plaintext server": None,
        }
        for label, served in cases.items():
            with self.subTest(label):
                controller = RecordingController(projects=[(self.project, "alpha")])
                port = serve(self, controller, tls=served)
                self.hosted_at(port, self.ca.pem)
                with self.assertRaises(ConnectionError) as caught:
                    self.run_hosted(NEST_RPC_TIMEOUT="5")
                self.assertIn("verified TLS", str(caught.exception))
                self.assertIn("never falls back", str(caught.exception))
                # Nothing reached the Controller: no call, no token.
                self.assertEqual(controller.calls, [])

    def test_custom_endpoint_never_receives_the_hosted_session(self):
        controller = RecordingController(require_session=False)
        port = serve(self, controller, tls=self.ca.issue("localhost"))
        ca = self.home / "ca.pem"
        ca.write_bytes(self.ca.pem)
        with mock.patch.dict(
            "os.environ",
            {
                "NEST_CONTROLLER_ADDRESS": f"localhost:{port}",
                "NEST_CONTROLLER_TLS_CA_FILE": str(ca),
            },
        ):
            self.assertEqual(execute_remote(double, RESOURCES, None, 21), 42)
        self.assertEqual(controller.methods(), ["Execute"])  # no session, so no listing
        text = json.dumps(controller.calls)
        self.assertNotIn("authorization", text)
        self.assertNotIn(self.session.access_token, text)

    def test_another_nest_session_goes_only_to_its_controller(self):
        custom = session_for(CUSTOM_AUTH_URL)
        self.store.save(custom)
        controller = RecordingController(projects=[(self.project, "alpha")])
        port = serve(self, controller)
        with mock.patch.dict(
            "os.environ",
            {"NEST_CONTROLLER_ADDRESS": f"127.0.0.1:{port}", "NEST_AUTH_URL": CUSTOM_AUTH_URL},
        ):
            self.assertEqual(execute_remote(double, RESOURCES, None, 21), 42)
        text = json.dumps(controller.calls)
        self.assertIn(custom.access_token, text)
        self.assertNotIn(self.session.access_token, text)


class ZeroConfigFlowTests(HostedCase):
    """install -> first-call sign-in -> only Project -> run, with no NEST_ setting at all.

    The hosted default is switched on here only, as H09 would switch it on.
    """

    def test_first_call_signs_in_selects_and_runs(self):
        ca = CertificateAuthority()
        project = str(uuid.uuid4())
        controller = RecordingController(projects=[(project, "alpha")])
        port = serve(self, controller, tls=ca.issue("localhost"))
        self.hosted_at(port, ca.pem)
        fresh = session_for(HOSTED_AUTH_URL)

        def login(config, **_options):
            self.assertEqual(config.url, HOSTED_AUTH_URL)
            self.store.save(fresh)
            return fresh

        with (
            mock.patch.object(nest_config, "HOSTED_PROFILE_DEFAULT", True),
            mock.patch("sys.stdin.isatty", return_value=True),
            mock.patch.object(nest_login, "login", side_effect=login) as signed_in,
        ):
            self.assertEqual(execute_remote(double, RESOURCES, None, 21), 42)
            signed_in.assert_called_once()
            # The second call reuses the stored session without signing in again.
            self.assertEqual(execute_remote(double, RESOURCES, None, 5), 10)
            signed_in.assert_called_once()
        self.assertEqual(
            controller.methods(),
            ["Execute", "ListEligibleProjects", "Execute", "ListEligibleProjects", "Execute"],
        )
        first, *_ = controller.calls
        self.assertNotIn("authorization", first[1])  # refused before anything ran
        for method, metadata in controller.calls[1:]:
            self.assertEqual(metadata["authorization"], f"Bearer {fresh.access_token}")
            if method == "Execute":
                self.assertEqual(metadata["x-nest-project-id"], project)

    def test_without_a_terminal_it_asks_for_nest_login(self):
        controller = RecordingController(projects=[(str(uuid.uuid4()), "alpha")])
        ca = CertificateAuthority()
        port = serve(self, controller, tls=ca.issue("localhost"))
        self.hosted_at(port, ca.pem)
        with (
            mock.patch.object(nest_config, "HOSTED_PROFILE_DEFAULT", True),
            mock.patch("sys.stdin.isatty", return_value=False),
            mock.patch.object(nest_login, "login") as login,
            self.assertRaises(nest_client.AuthenticationRequiredError) as caught,
        ):
            execute_remote(double, RESOURCES, None, 1)
        login.assert_not_called()
        self.assertIn("nest login", str(caught.exception))
        self.assertEqual(controller.methods(), ["Execute"])

    def test_cli_status_names_the_hosted_origin(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = nest_cli.main(["auth", "status"])
        self.assertEqual(code, 1)
        self.assertIn(HOSTED_AUTH_URL, out.getvalue())
