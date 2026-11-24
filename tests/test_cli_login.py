"""Browser-approved CLI sign-in and the `nest login` command (B05).

The flow tests sign a browser in through `fake_oidc.FakeGoogle`, drive the
Controller's auth listener over real HTTP against a real PostgreSQL (see
`postgres_fixture`), and run the CLI side with `nest.login` or the `nest` command.
"""

import io
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fake_oidc import CLIENT_ID, CLIENT_SECRET, FakeGoogle
from postgres_fixture import PostgresTestCase, connect
from test_google_auth import Browser, free_port

from nest import cli as nest_cli
from nest import login as nest_login
from nest.auth import (
    CLI_APPROVE_PATH,
    CLI_LOGOUT_PATH,
    CLI_REFRESH_PATH,
    CLI_RETURN_COOKIE,
    CLI_SESSION_PATH,
    CLI_START_PATH,
    CLI_TOKEN_PATH,
    LOGIN_PATH,
    SESSION_PATH,
    CLISignIn,
    CLISignInError,
    GoogleSignIn,
    normalize_user_code,
    start_auth_service,
)
from nest.auth.cli import clean_client_name, new_user_code, normalize_scope
from nest.config import (
    ConfigError,
    load_cli_auth_config,
    load_controller_config,
    load_database_config,
)
from nest.db import (
    CLILoginStore,
    Database,
    DecidingSessionNotActiveError,
    IdentityStore,
    UserNotActiveError,
    hash_token,
    upgrade,
)
from nest.login import (
    CredentialsFileError,
    CredentialStore,
    LoginDeniedError,
    NestSession,
    NotSignedInError,
)


def session_fixture(auth_url="https://nest.example.com", **overrides) -> NestSession:
    now = datetime.now(UTC)
    values = {
        "auth_url": auth_url,
        "user_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "scope": "jobs:run projects:read",
        "access_token": "access-" + "a" * 40,
        "refresh_token": "refresh-" + "r" * 40,
        "access_expires_at": now + timedelta(minutes=15),
        "refresh_expires_at": now + timedelta(hours=24),
        "session_expires_at": now + timedelta(days=7),
    }
    values.update(overrides)
    return NestSession(**values)


class UserCodeTests(unittest.TestCase):
    def test_new_codes_are_formatted_and_normalize(self):
        for _ in range(50):
            code = new_user_code()
            self.assertRegex(code, r"^[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}$")
            self.assertEqual(normalize_user_code(code), code.replace("-", ""))

    def test_people_may_retype_codes_loosely(self):
        self.assertEqual(normalize_user_code(" bcdf ghjk "), "BCDFGHJK")
        self.assertEqual(normalize_user_code("bcdf-ghjk"), "BCDFGHJK")
        for bad in (None, "", "BCDF", "BCDF-GHJKL", "ABCD-EFGH", "BCDF-GHJ1", "x" * 100):
            with self.subTest(bad=bad):
                self.assertIsNone(normalize_user_code(bad))

    def test_scopes(self):
        self.assertEqual(normalize_scope(None), "jobs:run projects:read")
        self.assertEqual(
            normalize_scope("projects:read jobs:run jobs:run"), "jobs:run projects:read"
        )
        self.assertEqual(normalize_scope("projects:read"), "projects:read")
        for bad in ("", "admin", "jobs:run admin", 7, "a" * 600):
            with self.subTest(bad=bad), self.assertRaises(CLISignInError) as caught:
                normalize_scope(bad)
            self.assertEqual(caught.exception.reason, "invalid_scope")

    def test_client_names_are_made_displayable(self):
        self.assertEqual(clean_client_name("nest CLI on login01"), "nest CLI on login01")
        self.assertEqual(clean_client_name("a\x1b[31mb\nc\u202ed"), "a [31mb c d")
        self.assertEqual(clean_client_name("x" * 200), "x" * 64)
        self.assertEqual(clean_client_name(None), "unnamed client")
        self.assertEqual(clean_client_name(" \n "), "unnamed client")


class CLIAuthConfigTests(unittest.TestCase):
    def test_url_and_default_credentials_file(self):
        config = load_cli_auth_config(
            {"NEST_AUTH_URL": "https://Nest.Example.com/", "XDG_CONFIG_HOME": "/home/u/.cfg"}
        )
        self.assertEqual(config.url, "https://nest.example.com")
        self.assertEqual(config.credentials_file, "/home/u/.cfg/nest/credentials.json")
        config = load_cli_auth_config({"HOME": "/home/u"}, url="http://127.0.0.1:8080")
        self.assertEqual(config.url, "http://127.0.0.1:8080")

    def test_invalid_settings(self):
        for env, expected in [
            ({}, "NEST_AUTH_URL is required"),
            ({"NEST_AUTH_URL": "http://nest.example.com"}, "must be an https:// URL"),
            ({"NEST_AUTH_URL": "https://u:p@nest.example.com"}, "no credentials"),
            ({"NEST_AUTH_URL": "https://nest.example.com/x?y"}, "must be an origin"),
            (
                {"NEST_AUTH_URL": "https://nest.example.com", "NEST_CLI_CREDENTIALS_FILE": "c"},
                "NEST_CLI_CREDENTIALS_FILE must be an absolute path",
            ),
        ]:
            with self.subTest(env=env), self.assertRaises(ConfigError) as caught:
                load_cli_auth_config(env)
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn("u:p", str(caught.exception).replace("'https://u:p@", ""))

    def test_redirect_url_cannot_be_a_cli_route(self):
        env = {
            "NEST_DATABASE_URL": "postgresql://nest@db.example.internal:5432/nest",
            "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
            "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
            "NEST_GOOGLE_REDIRECT_URL": "https://nest.example.com/auth/cli/approve",
        }
        with self.assertRaises(ConfigError) as caught:
            load_controller_config(env)
        self.assertIn("a CLI sign-in route", str(caught.exception))


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "config", "nest", "credentials.json")
        self.store = CredentialStore(self.path)

    def test_round_trip_with_private_permissions(self):
        session = session_fixture()
        self.store.save(session)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)
        self.assertEqual(self.store.load(session.auth_url), session)
        data = json.loads(Path(self.path).read_text())
        self.assertEqual(data["kind"], "nest-cli-session")
        # Another Nest's session is not used.
        self.assertIsNone(self.store.load("https://other.example.com"))
        self.assertTrue(self.store.clear())
        self.assertFalse(self.store.clear())
        self.assertIsNone(self.store.load(session.auth_url))

    def test_secrets_stay_out_of_repr(self):
        text = repr(session_fixture())
        self.assertNotIn("a" * 40, text)
        self.assertNotIn("r" * 40, text)

    def test_file_readable_by_others_is_refused(self):
        session = session_fixture()
        self.store.save(session)
        os.chmod(self.path, 0o644)
        with self.assertRaises(CredentialsFileError) as caught:
            self.store.load(session.auth_url)
        self.assertIn("accessible to other users", str(caught.exception))

    def test_unreadable_file_is_ignored(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"{not json")
        os.close(fd)
        self.assertIsNone(self.store.load("https://nest.example.com"))

    def test_status_without_session(self):
        env = {"NEST_AUTH_URL": "https://nest.example.com", "NEST_CLI_CREDENTIALS_FILE": self.path}
        config = load_cli_auth_config(env)
        self.assertEqual(nest_login.status(config)["signed_in"], False)
        with self.assertRaises(NotSignedInError):
            nest_login.ensure_session(config, interactive=False)

    def test_command_exit_statuses(self):
        out, err = io.StringIO(), io.StringIO()
        env = {"NEST_AUTH_URL": "", "NEST_CLI_CREDENTIALS_FILE": self.path}
        with unittest.mock.patch.dict(os.environ, env), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(nest_cli.main(["auth", "status"]), 2)
            self.assertEqual(
                nest_cli.main(["auth", "status", "--url", "https://nest.example.com"]), 1
            )
            self.assertEqual(
                nest_cli.main(["logout", "--local", "--url", "https://nest.example.com"]), 0
            )
        self.assertIn("NEST_AUTH_URL is required", err.getvalue())
        self.assertIn("Not signed in to https://nest.example.com", out.getvalue())

    def test_no_command_takes_a_secret_argument(self):
        parser = nest_cli._parser()
        options = set()
        stack = [parser]
        while stack:
            current = stack.pop()
            for action in current._actions:
                options.update(action.option_strings)
                choices = getattr(action, "choices", None)
                if isinstance(choices, dict):
                    stack.extend(choices.values())
        self.assertFalse(
            {o for o in options if re.search("token|password|secret|code|key", o)}, options
        )


class _Flow(PostgresTestCase):
    """A Controller auth listener with Google sign-in through the fake provider."""

    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = Database(load_database_config({"NEST_DATABASE_URL": self.url}))
        self.db.open()
        self.addCleanup(self.db.close)
        self.fake = FakeGoogle()
        self.addCleanup(self.fake.close)
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        self.config = load_controller_config(
            {
                "NEST_DATABASE_URL": self.url,
                "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
                "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
                "NEST_GOOGLE_REDIRECT_URL": f"{self.base}/auth/google/callback",
                "NEST_AUTH_HTTP_BIND": f"127.0.0.1:{port}",
            }
        ).google
        assert self.config is not None
        self.sign_in = GoogleSignIn(self.config, self.db, endpoints=self.fake.endpoints, timeout=5)
        self.cli = CLISignIn(self.db, self.config.sessions, poll_interval=0.5)
        self.service = start_auth_service(self.config, self.db, sign_in=self.sign_in, cli=self.cli)
        self.addCleanup(self.service.stop)
        self.identities = IdentityStore(self.db)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.credentials = os.path.join(directory.name, "nest", "credentials.json")
        self.client_config = load_cli_auth_config(
            {"NEST_AUTH_URL": self.base, "NEST_CLI_CREDENTIALS_FILE": self.credentials}
        )

    # Helpers ------------------------------------------------------------------

    def browser(self) -> Browser:
        """A browser signed in to Nest with Google."""
        browser = Browser()
        status, headers, _ = browser.get(self.base + LOGIN_PATH)
        self.assertEqual(status, 302)
        status, headers, _ = browser.get(headers["Location"])
        status, _, body = browser.get(headers["Location"])
        self.assertEqual(status, 200, body)
        return browser

    def api(self, path, payload=None, *, headers=None, raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(payload or {}).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read() or b"{}")

    def get_json(self, path, bearer=None):
        request = urllib.request.Request(self.base + path)
        if bearer:
            request.add_header("Authorization", f"Bearer {bearer}")
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, json.loads(response.read() or b"{}")

    def start(self, **payload):
        status, started = self.api(CLI_START_PATH, payload)
        self.assertEqual(status, 200, started)
        return started

    def approval_page(self, browser: Browser, code: str):
        return browser.get(f"{self.base}{CLI_APPROVE_PATH}?{urlencode({'code': code})}")

    def submit(self, browser: Browser, fields: dict[str, str], *, origin: str | None = "same"):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if origin == "same":
            headers["Origin"] = self.base
        elif origin is not None:
            headers["Origin"] = origin
        cookies = [
            f"{k}={v}" for k, (v, p, _) in browser.cookies.items() if CLI_APPROVE_PATH.startswith(p)
        ]
        headers["Cookie"] = "; ".join(cookies)
        request = urllib.request.Request(
            self.base + CLI_APPROVE_PATH,
            data=urlencode(fields).encode(),
            method="POST",
            headers=headers,
        )
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read().decode()

    def decide(self, browser: Browser, code: str, decision: str = "approve"):
        status, _, page = self.approval_page(browser, code)
        self.assertEqual(status, 200, page)
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page)
        assert csrf is not None
        return self.submit(browser, {"code": code, "csrf": csrf.group(1), "decision": decision})

    def poll(self, device_code: str):
        return self.api(CLI_TOKEN_PATH, {"device_code": device_code})

    def sql(self, query: str, *params):
        with connect(self.url) as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def run_inline(self, target):
        """Runs a CLI call in a thread and returns (thread, output, result holder)."""
        out = io.StringIO()
        result: dict[str, object] = {}

        def run():
            try:
                result["value"] = target(out)
            except Exception as exc:  # noqa: BLE001 - reported by the test
                result["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, out, result

    def wait_for_code(self, out: io.StringIO) -> str:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            match = re.search(r"\?code=([A-Z]{4}-[A-Z]{4})", out.getvalue())
            if match:
                return match.group(1)
            time.sleep(0.05)
        self.fail(f"no approval link printed: {out.getvalue()!r}")


class CLISignInFlowTests(_Flow):
    def test_inline_sign_in_from_the_sdk(self):
        """ensure_session() signs in through the browser when there is no session."""
        browser = self.browser()
        with self.assertLogs("nest", logging.DEBUG) as logs:
            thread, out, result = self.run_inline(
                lambda out: nest_login.ensure_session(
                    self.client_config, open_browser=False, out=out
                )
            )
            code = self.wait_for_code(out)
            self.assertIn(f"{self.base}{CLI_APPROVE_PATH}?code={code}", out.getvalue())
            status, body = self.decide(browser, code)
            self.assertEqual(status, 200, body)
            self.assertIn("CLI sign-in approved", body)
            thread.join(30)
        self.assertNotIn("error", result, result)
        session = result["value"]
        assert isinstance(session, NestSession)
        self.assertEqual(session.scope, "jobs:run projects:read")
        self.assertIn("Signed in to Nest.", out.getvalue())

        # Stored privately, and a second call reuses it without signing in again.
        self.assertEqual(stat.S_IMODE(os.stat(self.credentials).st_mode), 0o600)
        self.assertEqual(nest_login.ensure_session(self.client_config, interactive=False), session)

        stored = self.identities.active_session(hash_token(session.refresh_token))
        assert stored is not None
        self.assertEqual((stored.kind, stored.scope), ("cli", "jobs:run projects:read"))
        browser_user = self.identities.find_user(
            issuer="https://accounts.google.com", subject=self.fake.account.sub
        )
        assert browser_user is not None
        self.assertEqual(session.user_id, browser_user.id)
        approved = self.sql("SELECT status, decided_by_session_id FROM cli_login_requests")
        self.assertEqual(approved[0][0], "consumed")
        self.assertIsNotNone(approved[0][1])

        # The bearer token works on the CLI routes only.
        status, _, described = self.get_json(CLI_SESSION_PATH, session.access_token)
        self.assertEqual(status, 200, described)
        self.assertEqual(described["session_id"], str(session.session_id))
        self.assertEqual(described["scope"], "jobs:run projects:read")
        self.assertNotIn("access_token", described)
        cookie_browser = Browser()
        cookie_browser.cookies["nest_session"] = (session.access_token, "/", {})
        status, _, _ = cookie_browser.get(self.base + SESSION_PATH)
        self.assertEqual(status, 401)
        # ...and a browser session's token is not a CLI bearer token.
        browser_token = browser.cookies["nest_session"][0]
        self.assertEqual(self.get_json(CLI_SESSION_PATH, browser_token)[0], 401)

        # No secret reached a log line.
        text = "\n".join(logs.output)
        for secret in (session.access_token, session.refresh_token, code.replace("-", "")):
            self.assertNotIn(secret, text)

    def test_explicit_nest_login_command(self):
        browser = self.browser()
        env = {
            **os.environ,
            "NEST_AUTH_URL": self.base,
            "NEST_CLI_CREDENTIALS_FILE": self.credentials,
            "DISPLAY": "",
            "WAYLAND_DISPLAY": "",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "nest.cli", "login", "--no-browser"],
            env=env,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(process.kill)
        assert process.stderr is not None
        seen = ""
        code = None
        while code is None:
            line = process.stderr.readline()
            self.assertTrue(line, seen)
            seen += line
            match = re.search(r"\?code=([A-Z]{4}-[A-Z]{4})", line)
            code = match.group(1) if match else None
        status, body = self.decide(browser, code)
        self.assertEqual(status, 200, body)
        _, stderr = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0, seen + stderr)
        self.assertIn("Signed in to Nest.", stderr)
        self.assertNotIn("device_code", seen + stderr)

        status_run = subprocess.run(
            [sys.executable, "-m", "nest.cli", "auth", "status", "--check", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(status_run.returncode, 0, status_run.stderr)
        info = json.loads(status_run.stdout)
        self.assertTrue(info["accepted"])
        self.assertNotIn("access_token", info)
        stored = json.loads(Path(self.credentials).read_text())
        self.assertNotIn(stored["access_token"], status_run.stdout)
        self.assertNotIn(stored["refresh_token"], status_run.stdout)

        logout_run = subprocess.run(
            [sys.executable, "-m", "nest.cli", "logout"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(logout_run.returncode, 0, logout_run.stderr)
        self.assertIn("Signed out of Nest.", logout_run.stderr)
        self.assertFalse(os.path.exists(self.credentials))
        rows = self.sql(
            "SELECT revoked_reason FROM user_sessions WHERE kind = 'cli' AND token_hash = %s",
            hash_token(stored["refresh_token"]),
        )
        self.assertEqual(rows, [("logout",)])

    def test_denied_sign_in(self):
        browser = self.browser()
        thread, out, result = self.run_inline(
            lambda out: nest_login.login(self.client_config, open_browser=False, out=out)
        )
        code = self.wait_for_code(out)
        status, body = self.decide(browser, code, "deny")
        self.assertEqual(status, 200, body)
        self.assertIn("CLI sign-in denied", body)
        thread.join(30)
        self.assertIsInstance(result.get("error"), LoginDeniedError)
        self.assertFalse(os.path.exists(self.credentials))
        self.assertEqual(self.sql("SELECT count(*) FROM user_sessions WHERE kind = 'cli'"), [(0,)])

    def test_approval_link_signs_the_browser_in_first(self):
        started = self.start()
        code = started["user_code"]
        browser = Browser()
        status, headers, _ = browser.get(started["verification_uri_complete"])
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"{LOGIN_PATH}?cli_code={code.replace('-', '')}")
        status, headers, _ = browser.get(self.base + headers["Location"])
        self.assertEqual(status, 302)
        self.assertIn(CLI_RETURN_COOKIE, browser.cookies)
        status, headers, _ = browser.get(headers["Location"])
        status, headers, _ = browser.get(headers["Location"])
        # The callback signs in and returns to the approval of that code.
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], f"{CLI_APPROVE_PATH}?code={code}")
        self.assertNotIn(CLI_RETURN_COOKIE, browser.cookies)
        status, _, page = browser.get(self.base + headers["Location"])
        self.assertEqual(status, 200)
        self.assertIn(code, page)
        self.assertIn("Run and manage jobs in your Projects", page)
        self.assertEqual(
            self.poll(started["device_code"]), (400, {"error": "authorization_pending"})
        )

    def test_approval_page_shows_untrusted_client_name_escaped(self):
        started = self.start(client_name="<script>alert(1)</script>", scope="projects:read")
        status, _, page = self.approval_page(self.browser(), started["user_code"])
        self.assertEqual(status, 200)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("Run and manage jobs", page)
        self.assertIn("NCHC password", page)

    def test_form_pages_send_a_same_origin_referrer_policy(self):
        """A form POST from a `no-referrer` page carries `Origin: null`, which the
        approval route refuses; the pages holding its forms use `same-origin`.
        `test_cli_login_browser` checks this with a real browser."""
        browser = self.browser()
        started = self.start()
        status, headers, _ = self.approval_page(browser, started["user_code"])
        self.assertEqual((status, headers["Referrer-Policy"]), (200, "same-origin"))
        status, headers, _ = browser.get(self.base + CLI_APPROVE_PATH)
        self.assertEqual((status, headers["Referrer-Policy"]), (200, "same-origin"))
        status, headers, _ = self.approval_page(browser, "nonsense")
        self.assertEqual((status, headers["Referrer-Policy"]), (400, "same-origin"))
        # Every other page keeps `no-referrer`: the sign-in redirect, an unknown
        # code, and the page answering the decision.
        status, headers, _ = self.approval_page(Browser(), started["user_code"])
        self.assertEqual((status, headers["Referrer-Policy"]), (303, "no-referrer"))
        status, headers, _ = self.approval_page(browser, "BCDF-GHJK")
        self.assertEqual((status, headers["Referrer-Policy"]), (404, "no-referrer"))
        _, _, page = self.approval_page(browser, started["user_code"])
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page)
        assert csrf is not None
        token = browser.cookies["nest_session"][0]
        request = urllib.request.Request(
            self.base + CLI_APPROVE_PATH,
            data=urlencode(
                {"code": started["user_code"], "csrf": csrf.group(1), "decision": "deny"}
            ).encode(),
            method="POST",
            headers={"Origin": self.base, "Cookie": f"nest_session={token}"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertIn("CLI sign-in denied", response.read().decode())
            self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

    def test_approval_page_for_unknown_or_missing_code(self):
        browser = self.browser()
        status, _, page = self.approval_page(browser, "BCDF-GHJK")
        self.assertEqual(status, 404)
        status, _, page = self.approval_page(browser, "nonsense")
        self.assertEqual(status, 400)
        self.assertIn("not a valid code", page)
        status, _, page = browser.get(self.base + CLI_APPROVE_PATH)
        self.assertEqual(status, 200)
        self.assertIn('name="code"', page)

    def test_forged_approvals_are_refused(self):
        started = self.start()
        code = started["user_code"]
        browser = self.browser()
        _, _, page = self.approval_page(browser, code)
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page)
        assert csrf is not None
        fields = {"code": code, "csrf": csrf.group(1), "decision": "approve"}
        # A cross-site form post, or one without a real Origin.
        self.assertEqual(self.submit(browser, fields, origin="https://evil.example")[0], 403)
        self.assertEqual(self.submit(browser, fields, origin=None)[0], 403)
        self.assertEqual(self.submit(browser, fields, origin="null")[0], 403)
        # A CSRF token of another browser session.
        other = self.browser()
        self.assertEqual(self.submit(other, fields)[0], 403)
        # Not signed in.
        self.assertEqual(self.submit(Browser(), fields)[0], 401)
        self.assertEqual(
            self.poll(started["device_code"]), (400, {"error": "authorization_pending"})
        )
        # The real form works once; a second decision is refused.
        self.assertEqual(self.submit(browser, fields)[0], 200)
        self.assertEqual(self.submit(browser, {**fields, "decision": "deny"})[0], 404)

    def browser_session(self, browser: Browser):
        token = browser.cookies["nest_session"][0]
        grant = self.sign_in.session_store.authenticate(hash_token(token))
        assert grant is not None
        return grant.session

    def test_session_revoked_between_authentication_and_decision(self):
        """A logout between the page's session check and the decision approves nothing."""
        started = self.start()
        browser = self.browser()
        session = self.browser_session(browser)
        _, _, page = self.approval_page(browser, started["user_code"])
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page)
        assert csrf is not None
        real_decide = self.cli.store.decide

        def revoke_then_decide(*args, **kwargs):
            # Runs after _cli_decide() authenticated the browser session.
            self.identities.revoke_session(session.id, reason="logout")
            return real_decide(*args, **kwargs)

        fields = {"code": started["user_code"], "csrf": csrf.group(1), "decision": "approve"}
        with unittest.mock.patch.object(self.cli.store, "decide", revoke_then_decide):
            status, body = self.submit(browser, fields)
        self.assertEqual(status, 401, body)
        self.assertIn("Your Nest session has ended", body)
        self.assertEqual(self.sql("SELECT status FROM cli_login_requests"), [("pending",)])
        self.assertEqual(
            self.poll(started["device_code"]), (400, {"error": "authorization_pending"})
        )

    def test_user_disabled_between_authentication_and_decision(self):
        """Disabling the User after the page's session check is an authentication
        failure (401), not a server error, and approves nothing."""
        started = self.start()
        browser = self.browser()
        session = self.browser_session(browser)
        _, _, page = self.approval_page(browser, started["user_code"])
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page)
        assert csrf is not None
        real_decide = self.cli.store.decide

        def disable_then_decide(*args, **kwargs):
            # Runs after _cli_decide() authenticated the browser session. Only the
            # User is disabled here; the store sees it under its User lock.
            self.sql(
                "UPDATE users SET status = 'disabled', disabled_at = now() WHERE id = %s",
                session.user_id,
            )
            return real_decide(*args, **kwargs)

        fields = {"code": started["user_code"], "csrf": csrf.group(1), "decision": "approve"}
        for service in ("default", "raising UserNotActiveError"):
            with self.subTest(service=service):
                if service == "default":
                    patch = unittest.mock.patch.object(
                        self.cli.store, "decide", disable_then_decide
                    )
                else:
                    patch = unittest.mock.patch.object(
                        self.cli, "decide", side_effect=UserNotActiveError("disabled")
                    )
                with patch:
                    status, body = self.submit(browser, fields)
                self.assertEqual(status, 401, body)
                self.assertIn("Your Nest session has ended", body)
                self.assertEqual(self.sql("SELECT status FROM cli_login_requests"), [("pending",)])
                self.sql("UPDATE users SET status = 'active', disabled_at = NULL")
        self.assertEqual(
            self.poll(started["device_code"]), (400, {"error": "authorization_pending"})
        )

    def test_decision_waits_for_a_concurrent_revocation(self):
        """A revocation in progress holds the family lock; the decision waits for it
        and then refuses, instead of approving with the revoked session."""
        started = self.start()
        session = self.browser_session(self.browser())
        request = self.cli.find(started["user_code"])
        assert request is not None
        outcome: dict[str, object] = {}
        with self.db.connection() as held:
            self.identities.revoke_session(session.id, reason="admin_revoked", conn=held)

            def decide():
                try:
                    outcome["value"] = self.cli.store.decide(
                        request.id, approve=True, user_id=session.user_id, session_id=session.id
                    )
                except Exception as exc:  # noqa: BLE001 - checked below
                    outcome["error"] = exc

            thread = threading.Thread(target=decide, daemon=True)
            thread.start()
            thread.join(1.0)
            self.assertTrue(thread.is_alive(), "the decision did not wait for the revocation")
        # Leaving the block commits the revocation.
        thread.join(10)
        self.assertIsInstance(outcome.get("error"), DecidingSessionNotActiveError, outcome)
        self.assertEqual(self.sql("SELECT status FROM cli_login_requests"), [("pending",)])

    def test_decision_needs_an_active_browser_session_of_the_user(self):
        started = self.start()
        browser = self.browser()
        session = self.browser_session(browser)
        request = self.cli.find(started["user_code"])
        assert request is not None
        store = self.cli.store
        other, _, _ = self.identities.sign_in(
            issuer="https://accounts.google.com",
            subject="someone-else",
            email="else@example.org",
            email_verified=True,
        )
        now = datetime.now(UTC)
        cli_session = self.identities.create_session(
            session.user_id,
            kind="cli",
            token_hash=hash_token("cli-session-secret"),
            expires_at=now + timedelta(hours=1),
            scope="jobs:run",
        )
        for label, user_id, session_id in [
            ("another User's session", other.id, session.id),
            ("a CLI session", session.user_id, cli_session.id),
            ("an unknown session", session.user_id, uuid.uuid4()),
        ]:
            with self.subTest(label), self.assertRaises(DecidingSessionNotActiveError):
                store.decide(request.id, approve=True, user_id=user_id, session_id=session_id)
        self.sql(
            "UPDATE user_sessions SET expires_at = created_at + interval '1 microsecond' "
            "WHERE id = %s",
            session.id,
        )
        with self.assertRaises(DecidingSessionNotActiveError):
            store.decide(request.id, approve=False, user_id=session.user_id, session_id=session.id)
        self.assertEqual(self.sql("SELECT status FROM cli_login_requests"), [("pending",)])

    def test_decision_for_a_disabled_user_is_refused(self):
        started = self.start()
        session = self.browser_session(self.browser())
        request = self.cli.find(started["user_code"])
        assert request is not None
        self.identities.set_user_status(session.user_id, "disabled")
        with self.assertRaises(UserNotActiveError):
            self.cli.store.decide(
                request.id, approve=True, user_id=session.user_id, session_id=session.id
            )

    def test_device_code_is_redeemed_once_and_polls_are_paced(self):
        started = self.start()
        self.assertEqual(started["interval"], 0.5)
        self.assertEqual(started["expires_in"], 600)
        self.assertEqual(self.poll(started["device_code"])[1], {"error": "authorization_pending"})
        self.assertEqual(self.poll(started["device_code"])[1], {"error": "slow_down"})
        self.decide(self.browser(), started["user_code"])
        time.sleep(0.5)
        status, tokens = self.poll(started["device_code"])
        self.assertEqual(status, 200, tokens)
        self.assertEqual(tokens["token_type"], "Bearer")
        self.assertEqual(self.poll(started["device_code"]), (400, {"error": "invalid_grant"}))
        self.assertEqual(self.poll("not-a-device-code"), (400, {"error": "invalid_grant"}))
        self.assertEqual(self.sql("SELECT count(*) FROM user_sessions WHERE kind = 'cli'"), [(1,)])

    def test_disabled_user_gets_no_cli_session(self):
        started = self.start()
        self.decide(self.browser(), started["user_code"])
        user = self.identities.find_user(
            issuer="https://accounts.google.com", subject=self.fake.account.sub
        )
        assert user is not None
        self.identities.set_user_status(user.id, "disabled")
        self.assertEqual(self.poll(started["device_code"]), (400, {"error": "access_denied"}))

    def test_expired_request(self):
        store = CLILoginStore(self.db)
        store.create(
            device_code_hash=hash_token("device-code-for-expiry"),
            user_code_hash=hash_token("BCDFGHJK"),
            scope="jobs:run",
            client_name="test",
            ttl_seconds=0.3,
            max_pending=10,
        )
        time.sleep(0.6)
        self.assertEqual(self.poll("device-code-for-expiry"), (400, {"error": "expired_token"}))
        self.assertEqual(self.approval_page(self.browser(), "BCDF-GHJK")[0], 404)

    def test_cli_routes_refuse_browsers_and_odd_requests(self):
        status, body = self.api(CLI_START_PATH, {}, headers={"Origin": "https://evil.example"})
        self.assertEqual((status, body), (403, {"error": "cross_origin"}))
        status, body = self.api(CLI_START_PATH, {}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, body = self.api(CLI_START_PATH, {}, headers={"Content-Type": "text/plain"})
        self.assertEqual((status, body), (415, {"error": "invalid_request"}))
        self.assertEqual(self.api(CLI_START_PATH, raw=b"[1]")[0], 400)
        self.assertEqual(self.api(CLI_START_PATH, raw=b"x" * 5000)[0], 413)
        status, body = self.api(CLI_START_PATH, {"scope": "admin"})
        self.assertEqual((status, body["error"]), (400, "invalid_scope"))
        status, headers, _ = Browser().get(self.base + CLI_TOKEN_PATH)
        self.assertEqual((status, headers["Allow"]), (405, "POST"))
        status, headers, body = self.get_json(CLI_SESSION_PATH)
        self.assertEqual(status, 401)
        self.assertEqual(headers["WWW-Authenticate"], 'Bearer realm="nest"')
        self.assertEqual(self.sql("SELECT count(*) FROM cli_login_requests"), [(0,)])


class CLISessionLifecycleTests(_Flow):
    def signed_in(self) -> NestSession:
        started = self.start()
        self.decide(self.browser(), started["user_code"])
        status, tokens = self.poll(started["device_code"])
        self.assertEqual(status, 200, tokens)
        session = NestSession.from_response(self.base, tokens)
        CredentialStore(self.credentials).save(session)
        return session

    def test_expired_access_token_is_refreshed_and_rotated(self):
        session = self.signed_in()
        stale = replace(session, access_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        CredentialStore(self.credentials).save(stale)
        refreshed = nest_login.ensure_session(self.client_config, interactive=False)
        self.assertNotEqual(refreshed.refresh_token, session.refresh_token)
        self.assertNotEqual(refreshed.session_id, session.session_id)
        self.assertEqual(refreshed.scope, session.scope)
        self.assertEqual(CredentialStore(self.credentials).load(self.base), refreshed)
        self.assertEqual(self.get_json(CLI_SESSION_PATH, refreshed.access_token)[0], 200)
        # The old secrets no longer work.
        self.assertEqual(self.get_json(CLI_SESSION_PATH, session.access_token)[0], 401)

    def test_browser_refresh_route_refuses_cli_secret_and_back(self):
        session = self.signed_in()
        browser = Browser()
        browser.cookies["nest_refresh"] = (session.refresh_token, "/auth/session", {})
        status, _, _ = browser.get(self.base + "/auth/session/refresh", method="POST")
        self.assertEqual(status, 401)
        # Refused without side effects: the CLI session still refreshes.
        status, body = self.api(CLI_REFRESH_PATH, {"refresh_token": session.refresh_token})
        self.assertEqual(status, 200, body)

    def test_revoked_session_needs_a_new_sign_in(self):
        session = self.signed_in()
        self.identities.revoke_session(session.session_id, reason="admin_revoked")
        self.assertEqual(self.get_json(CLI_SESSION_PATH, session.access_token)[0], 401)
        stale = replace(session, access_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        CredentialStore(self.credentials).save(stale)
        with self.assertRaises(NotSignedInError):
            nest_login.ensure_session(self.client_config, interactive=False)
        self.assertFalse(os.path.exists(self.credentials))

    def test_status_check_reports_revocation(self):
        session = self.signed_in()
        self.assertTrue(nest_login.status(self.client_config, check=True)["accepted"])
        self.identities.revoke_session(session.session_id, reason="admin_revoked")
        info = nest_login.status(self.client_config, check=True)
        self.assertFalse(info["accepted"])
        self.assertFalse(info["signed_in"])

    def test_logout_revokes_at_nest_and_clears_locally(self):
        session = self.signed_in()
        result = nest_login.logout(self.client_config)
        self.assertEqual((result.removed, result.revoked), (True, True))
        self.assertFalse(os.path.exists(self.credentials))
        rows = self.sql(
            "SELECT revoked_reason FROM user_sessions WHERE id = %s", session.session_id
        )
        self.assertEqual(rows, [("logout",)])
        status, body = self.api(CLI_REFRESH_PATH, {"refresh_token": session.refresh_token})
        self.assertEqual((status, body), (401, {"error": "unauthenticated"}))
        # Logging out again, or with an unknown secret, is harmless.
        self.assertEqual(nest_login.logout(self.client_config).removed, False)
        self.assertEqual(
            self.api(CLI_LOGOUT_PATH, {"refresh_token": "x" * 40}), (200, {"signed_out": False})
        )

    def test_logout_without_reaching_nest_still_clears(self):
        self.signed_in()
        self.service.stop()
        result = nest_login.logout(self.client_config)
        self.assertEqual((result.removed, result.revoked), (True, None))

    def test_new_login_revokes_the_previous_session(self):
        first = self.signed_in()
        browser = self.browser()
        thread, out, result = self.run_inline(
            lambda out: nest_login.login(self.client_config, open_browser=False, out=out)
        )
        self.decide(browser, self.wait_for_code(out))
        thread.join(30)
        second = result["value"]
        assert isinstance(second, NestSession)
        self.assertNotEqual(second.session_id, first.session_id)
        rows = self.sql("SELECT revoked_reason FROM user_sessions WHERE id = %s", first.session_id)
        self.assertEqual(rows, [("logout",)])


class HPCCredentialSeparationTests(unittest.TestCase):
    def test_hpc_code_never_reads_nest_sessions(self):
        """Nest sessions are not HPC credentials: the Controller, SSH and Slurm code
        paths never import the CLI session module or read its file. The SDK client
        sends the session to the Controller only (see test_security)."""
        # The installed package: in the controller image the tests are not beside src/.
        source = Path(nest_login.__file__).resolve().parent
        for name in ("remote.py", "controller.py", "backends/slurm.py", "worker.py"):
            with self.subTest(module=name):
                text = (source / name).read_text()
                self.assertNotRegex(
                    text, r"\bfrom \.+login import|import nest\.login|credentials\.json"
                )
                self.assertNotIn("NEST_CLI_CREDENTIALS_FILE", text)

    def test_credentials_file_is_marked_as_a_nest_session(self):
        data = session_fixture().to_json()
        self.assertEqual(data["kind"], "nest-cli-session")
        self.assertEqual(
            set(data),
            {
                "kind",
                "version",
                "auth_url",
                "user_id",
                "session_id",
                "scope",
                "access_token",
                "refresh_token",
                "access_expires_at",
                "refresh_expires_at",
                "session_expires_at",
            },
        )
        self.assertEqual(urlsplit(str(data["auth_url"])).scheme, "https")


if __name__ == "__main__":
    unittest.main()
