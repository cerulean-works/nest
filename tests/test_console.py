"""The browser Management Console (G02a).

The HTTP tests check what the auth listener serves for the Console (its pages,
headers and sign-in return) and that the JSON routes the Console calls keep
enforcing the Project role matrix, sessions and the same-origin (CSRF) check
whatever a client sends. The browser tests drive the Console in a real Chromium
(through Playwright) signed in with the fake Google provider in `fake_oidc`.

Platform-admin features (Project creation with an explicit Owner, the user
directory, Platform grants, Slurm account links and Provider approvals) use the
privileged management API of G03 (#79): `test_privileged_ui_is_gated_on_the_admin_api`
checks that the Console shows none of them without it, and `ConsoleAdminBrowserTests`
drives them against it, in each Provider approval mode, checking what the page
says against the admission authority the Controller would use.
"""

import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any, ClassVar, NoReturn, cast
from unittest import mock

from admin_fixture import owned_project
from fake_oidc import CLIENT_ID, CLIENT_SECRET, Account, FakeGoogle
from test_authz import _Base, browser
from test_google_auth import Browser, free_port

from nest import project_selection
from nest.auth import INSECURE_SESSION_COOKIE, LOGIN_PATH, GoogleSignIn, start_auth_service
from nest.auth.console import CONSOLE_CSP, PAGE_CSP, connection_profiles
from nest.authz import (
    Action,
    AllowedProjects,
    DatabaseProviderApprovals,
    ExecutionAccess,
    Principal,
)
from nest.authz.admin import ProviderPolicy
from nest.config import (
    HOSTED_AUTH_URL,
    ConfigError,
    ConsoleConnectConfig,
    load_client_config,
    load_controller_config,
)
from nest.db import (
    PlatformStore,
    ProviderApprovalStore,
    SessionStore,
    SlurmAccountStore,
    hash_token,
)

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the environment
    sync_playwright = None

BROWSER_REQUIRED = os.environ.get("NEST_TEST_BROWSER_REQUIRED", "").strip() == "1"
API_CSP = "default-src 'none'; frame-ancestors 'none'"
EVIL = "https://evil.example"
ASSETS = files("nest.auth").joinpath("console_assets")


class _ConsoleBase(_Base):
    """An auth listener with Google sign-in through the fake provider."""

    provider_policy: ProviderPolicy | None = None
    """The Provider policy the listener reports to the admin API, as the Controller passes it."""

    console_env: ClassVar[dict[str, str]] = {}
    """`NEST_CONSOLE_*` settings: the connection profiles the Console offers."""

    def eligibility(self) -> ExecutionAccess | None:
        """The execution access check the listener asks for eligibility; none by default."""
        return None

    def setUp(self):
        super().setUp()
        self.fake = FakeGoogle()
        self.addCleanup(self.fake.close)
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        config = load_controller_config(
            {
                "NEST_DATABASE_URL": self.url,
                "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
                "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
                "NEST_GOOGLE_REDIRECT_URL": f"{self.base}/auth/google/callback",
                "NEST_AUTH_HTTP_BIND": f"127.0.0.1:{port}",
                **self.console_env,
            }
        ).google
        assert config is not None
        sign_in = GoogleSignIn(config, self.db, endpoints=self.fake.endpoints, timeout=5)
        self.http = start_auth_service(
            config,
            self.db,
            sign_in=sign_in,
            provider_policy=self.provider_policy,
            eligibility=self.eligibility(),
        )
        self.addCleanup(self.http.stop)
        self.sessions = SessionStore(self.db)

    def token(self, user_id, kind="browser", scope=None) -> str:
        """A browser (or CLI) access token for `user_id`."""
        session = self.identities.create_session(
            user_id,
            kind=kind,
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=scope,
        )
        access = secrets.token_urlsafe()
        self.sessions.issue_access_token(session.id, token_hash=hash_token(access), ttl_seconds=600)
        return access

    def request(self, method, path, *, cookie=None, headers=None, body=None, raw=None):
        sent = dict(headers or {})
        if cookie:
            sent["Cookie"] = f"{INSECURE_SESSION_COOKIE}={cookie}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            sent.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=sent)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read().decode()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers, exc.read().decode()

    def call(self, method, path, cookie, body=None, *, origin: str | None = "same"):
        """A JSON request as the Console sends it: same-origin, with the session cookie."""
        headers = {}
        if method == "POST" and origin is not None:
            headers["Origin"] = self.base if origin == "same" else origin
            headers["Sec-Fetch-Site"] = "same-origin" if origin == "same" else "cross-site"
        if method == "POST" and body is None:
            body = {}
        status, _, text = self.request(method, path, cookie=cookie, headers=headers, body=body)
        return status, json.loads(text or "{}")


class ConsoleHTTPTests(_ConsoleBase):
    def test_console_page_and_assets(self):
        status, headers, page = self.request("GET", "/console")
        self.assertEqual(status, 200, page)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(headers["Content-Security-Policy"], CONSOLE_CSP)
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertIsNone(headers["Access-Control-Allow-Origin"])
        self.assertIn('<script src="/console/console.js" defer></script>', page)
        # No inline script or style: the CSP would refuse it anyway.
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)")
        self.assertNotIn("<style", page)
        self.assertNotIn("style=", page)
        self.assertEqual(self.request("GET", "/console/")[0], 200)
        for name, kind in (
            ("console.js", "text/javascript; charset=utf-8"),
            ("console.css", "text/css; charset=utf-8"),
            ("nest-mark.svg", "image/svg+xml"),
        ):
            status, headers, _ = self.request("GET", f"/console/{name}")
            self.assertEqual(status, 200, name)
            self.assertEqual(headers["Content-Type"], kind)
            self.assertEqual(headers["Content-Security-Policy"], CONSOLE_CSP)
        for path in ("/console/index.html", "/console/../web.py", "/console/x/console.js"):
            self.assertEqual(self.request("GET", path)[0], 404, path)
        status, headers, _ = self.request("POST", "/console", headers={"Origin": self.base})
        self.assertEqual((status, headers["Allow"]), (405, "GET"))

    def test_api_routes_keep_their_strict_csp(self):
        for path in ("/api/projects", "/api/connect", "/auth/session"):
            status, headers, _ = self.request("GET", path)
            self.assertEqual(status, 401, path)
            self.assertEqual(headers["Content-Security-Policy"], API_CSP, path)
        # The listener's own pages may load the Console's stylesheet and mark, never a
        # same-origin script: the invitation page allows only its one hashed script.
        status, headers, page = self.request("GET", "/invitations/accept")
        self.assertEqual(status, 200)
        csp = headers["Content-Security-Policy"]
        self.assertTrue(csp.startswith(PAGE_CSP), csp)
        self.assertNotIn("script-src 'self'", csp)
        self.assertIn('href="/console/console.css"', page)
        self.assertNotIn("style=", page)

    def test_console_holds_no_secret_and_inserts_no_html(self):
        user = self.user("someone")
        cookie = self.token(user)
        _, _, page = self.request("GET", "/console", cookie=cookie)
        bundle = page + "".join(
            ASSETS.joinpath(name).read_text() for name in ("console.js", "console.css")
        )
        # The mark is a plain drawing: no script, link or reference to anything else.
        mark = ASSETS.joinpath("nest-mark.svg").read_text()
        self.assertEqual(re.findall(r"https?://[^\"]+", mark), ["http://www.w3.org/2000/svg"])
        for forbidden in ("<script", "href", "on", "<foreignObject"):
            self.assertNotRegex(mark, rf"{forbidden}\w*=" if forbidden == "on" else forbidden)
        for secret in (cookie, CLIENT_SECRET, self.url):
            self.assertNotIn(secret, bundle)
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function",
            "sessionStorage",
            "document.cookie",
            "http://",
            "https://",
            "style=",
        ):
            self.assertNotIn(forbidden, bundle, forbidden)
        # The only environment name the page mentions is the documented override;
        # every setting it shows comes from the server's answer.
        self.assertEqual(set(re.findall(r"NEST_[A-Z_]+", bundle)), {"NEST_PROJECT_ID"})
        # The browser stores one thing: the ID of the Project last chosen, per User.
        script = ASSETS.joinpath("console.js").read_text()
        self.assertEqual(
            re.findall(r"localStorage\.(\w+)\(", script), ["getItem", "setItem", "removeItem"]
        )
        self.assertIn("const STORE_PREFIX = 'nest.console.project.';", script)

    def test_console_creates_projects_only_through_the_admin_api(self):
        # POST /api/projects is the creator-is-Owner route that G03 restricts; the
        # Console never offers it, so ordinary users get no Project creation form.
        script = ASSETS.joinpath("console.js").read_text()
        self.assertNotIn("api('POST', '/api/projects'", script)
        self.assertIn("api('POST', '/api/admin/projects'", script)

    def test_privileged_ui_is_gated_on_the_admin_api(self):
        # The Console shows platform-admin features only where the server has the
        # G03 management API (`AuthHTTPServer.admin`), and even then asks it whether
        # the caller is a platform admin before showing one.
        server = self.http.server
        assert server is not None
        original = getattr(server, "admin", None)
        self.addCleanup(setattr, server, "admin", original)
        vars(server)["admin"] = None
        _, _, page = self.request("GET", "/console")
        self.assertIn('data-privileged-api="unavailable"', page)
        vars(server)["admin"] = object()
        _, _, page = self.request("GET", "/console")
        self.assertIn('data-privileged-api="available"', page)

    def test_sign_in_returns_to_the_console(self):
        browser = Browser()
        status, headers, _ = browser.get(f"{self.base}{LOGIN_PATH}?next=console")
        self.assertEqual(status, 302)
        cookie = browser.cookies["nest_console_return"]
        self.assertEqual((cookie[0], cookie[1]), ("1", "/auth/google/callback"))
        self.assertEqual(cookie[2]["httponly"], "True")
        status, headers, _ = browser.get(headers["Location"])
        status, headers, body = browser.get(headers["Location"])
        self.assertEqual((status, headers["Location"]), (303, "/console"), body)
        self.assertNotIn("nest_console_return", browser.cookies)
        session = browser.cookies[INSECURE_SESSION_COOKIE]
        self.assertEqual((session[1], session[2]["httponly"]), ("/", "True"))
        self.assertEqual(session[2]["samesite"], "Lax")
        self.assertEqual(browser.cookies["nest_refresh"][1], "/auth/session")
        self.assertEqual(browser.cookies["nest_refresh"][2]["samesite"], "Strict")
        # A plain sign-in still ends on the "Signed in" page.
        plain = Browser()
        _, headers, _ = plain.get(self.base + LOGIN_PATH)
        _, headers, _ = plain.get(headers["Location"])
        status, _, body = plain.get(headers["Location"])
        self.assertEqual(status, 200)
        self.assertIn("Signed in", body)

    def test_redirect_url_cannot_be_a_console_route(self):
        for path in ("/console", "/console/console.js"):
            with self.assertRaises(ConfigError) as caught:
                load_controller_config(
                    {
                        "NEST_DATABASE_URL": self.url,
                        "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
                        "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
                        "NEST_GOOGLE_REDIRECT_URL": f"{self.base}{path}",
                    }
                )
            self.assertIn("Management Console route", str(caught.exception))

    def test_cross_site_and_non_json_changes_are_refused(self):
        pid, users = self.team()
        owner = self.token(users["owner"])
        newcomer = str(self.user("newcomer"))
        path = f"/api/projects/{pid}/members"
        body = {"user_id": newcomer, "role": "viewer"}
        self.assertEqual(
            self.call("POST", path, owner, body, origin=EVIL), (403, {"error": "cross_origin"})
        )
        self.assertEqual(
            self.call("POST", path, owner, body, origin=None), (403, {"error": "cross_origin"})
        )
        # A form or text/plain POST (which needs no CORS preflight) is refused.
        status, _, _ = self.request(
            "POST",
            path,
            cookie=owner,
            headers={"Origin": self.base, "Content-Type": "text/plain"},
            raw=json.dumps(body).encode(),
        )
        self.assertEqual(status, 415)
        # No CORS: a cross-origin page can neither read answers nor preflight.
        status, headers, _ = self.request("GET", path, cookie=owner, headers={"Origin": EVIL})
        self.assertEqual(status, 200)
        self.assertIsNone(headers["Access-Control-Allow-Origin"])
        self.assertIsNone(headers["Access-Control-Allow-Credentials"])
        status, headers, _ = self.request(
            "OPTIONS", path, headers={"Origin": EVIL, "Access-Control-Request-Method": "POST"}
        )
        self.assertNotIn(status, (200, 204))
        self.assertIsNone(headers["Access-Control-Allow-Origin"])
        self.assertEqual(
            self.count("SELECT count(*) FROM project_memberships WHERE user_id = %s", newcomer),
            0,
        )
        # Refreshing and signing out are same-origin only too.
        for route in ("/auth/session/refresh", "/auth/session/logout"):
            status, _, text = self.request("POST", route, cookie=owner, headers={"Origin": EVIL})
            self.assertEqual((status, json.loads(text)), (403, {"error": "cross_origin"}), route)
        self.assertEqual(self.call("GET", "/api/projects", owner)[0], 200)

    def count(self, sql: str, *args) -> int:
        row = self.conn.execute(sql, args).fetchone()
        assert row is not None
        return row[0]

    def test_role_matrix_on_the_console_routes(self):
        pid, users = self.team()
        token = {role: self.token(user) for role, user in users.items()}
        newcomer = str(self.user("newcomer"))
        members = f"/api/projects/{pid}/members"
        member_id, viewer_id = users["member"], users["viewer"]

        # A nonmember can neither read nor change the Project, and learns nothing.
        outsider = token["outsider"]
        for path in (f"/api/projects/{pid}", members, f"/api/projects/{pid}/invitations"):
            self.assertEqual(self.call("GET", path, outsider), (404, {"error": "not_found"}))
        self.assertEqual(
            self.call("POST", members, outsider, {"user_id": newcomer, "role": "viewer"}),
            (404, {"error": "not_found"}),
        )
        listed = self.call("GET", "/api/projects", outsider)[1]["projects"]
        self.assertEqual(listed, [])

        # Viewers and Members read, but change nothing.
        for role in ("viewer", "member"):
            status, project = self.call("GET", f"/api/projects/{pid}", token[role])
            self.assertEqual((status, project["role"]), (200, role))
            self.assertEqual(self.call("GET", members, token[role])[0], 200)
            denied = (403, {"error": "forbidden", "reason": "role"})
            self.assertEqual(
                self.call("POST", members, token[role], {"user_id": newcomer, "role": "viewer"}),
                denied,
            )
            other = viewer_id if role == "member" else member_id
            self.assertEqual(
                self.call("POST", f"{members}/{other}/role", token[role], {"role": "admin"}),
                denied,
            )
            self.assertEqual(self.call("POST", f"{members}/{other}/remove", token[role]), denied)
            self.assertEqual(
                self.call(
                    "POST",
                    f"/api/projects/{pid}/invitations",
                    token[role],
                    {"email": "x@example.org", "role": "viewer"},
                ),
                denied,
            )
            self.assertEqual(
                self.call("GET", f"/api/projects/{pid}/invite-links", token[role]), denied
            )

        # Admins manage non-Owner members, but cannot touch the Owner or grant `owner`.
        admin = token["admin"]
        owner_id = users["owner"]
        status, added = self.call("POST", members, admin, {"user_id": newcomer, "role": "viewer"})
        self.assertEqual((status, added["role"]), (201, "viewer"))
        self.assertEqual(
            self.call("POST", f"{members}/{newcomer}/role", admin, {"role": "member"})[0], 200
        )
        self.assertEqual(
            self.call("POST", f"{members}/{newcomer}/role", admin, {"role": "owner"}),
            (403, {"error": "forbidden", "reason": "owner_by_transfer_only"}),
        )
        self.assertEqual(
            self.call("POST", f"{members}/{owner_id}/role", admin, {"role": "viewer"}),
            (403, {"error": "forbidden", "reason": "owner_protected"}),
        )
        self.assertEqual(
            self.call("POST", f"{members}/{owner_id}/remove", admin),
            (403, {"error": "forbidden", "reason": "owner_protected"}),
        )
        # The Owner cannot leave without a transfer.
        self.assertEqual(
            self.call("POST", f"{members}/{owner_id}/remove", token["owner"]),
            (403, {"error": "forbidden", "reason": "owner_protected"}),
        )

        # Removal takes effect on the very next request of the removed member.
        self.assertEqual(self.call("POST", f"{members}/{member_id}/remove", admin)[0], 200)
        self.assertEqual(
            self.call("GET", f"/api/projects/{pid}", token["member"]), (404, {"error": "not_found"})
        )
        # A demotion does too: the former Admin, now a Viewer, can change nothing.
        self.assertEqual(
            self.call(
                "POST", f"{members}/{users['admin']}/role", token["owner"], {"role": "viewer"}
            )[0],
            200,
        )
        self.assertEqual(
            self.call("POST", f"{members}/{newcomer}/remove", admin),
            (403, {"error": "forbidden", "reason": "role"}),
        )

    def test_revoked_or_unknown_sessions_are_refused(self):
        pid, users = self.team()
        owner = self.token(users["owner"])
        self.assertEqual(self.call("GET", f"/api/projects/{pid}", owner)[0], 200)
        self.identities.revoke_user_sessions(users["owner"])
        for method, path in (("GET", f"/api/projects/{pid}"), ("GET", "/auth/session")):
            self.assertEqual(self.call(method, path, owner), (401, {"error": "unauthenticated"}))
        self.assertEqual(
            self.call("POST", f"/api/projects/{pid}/members", owner, {"user_id": "x"}),
            (401, {"error": "unauthenticated"}),
        )
        self.assertEqual(
            self.call("GET", "/api/projects", "not-a-session"), (401, {"error": "unauthenticated"})
        )
        # A CLI token in the cookie is not a browser session.
        cli = self.token(users["owner"], kind="cli", scope="projects:read")
        self.assertEqual(self.call("GET", "/api/projects", cli)[0], 401)


class _BrowserBase(_ConsoleBase):
    """The Console in Chromium. Skipped without Playwright unless required (CI)."""

    def unavailable(self, message: str) -> NoReturn:
        if BROWSER_REQUIRED:
            self.fail(f"NEST_TEST_BROWSER_REQUIRED=1 but {message}")
        self.skipTest(message)

    def setUp(self):
        if sync_playwright is None:
            self.unavailable("playwright is not installed")
        super().setUp()
        manager = sync_playwright().start()
        self.addCleanup(manager.stop)
        executable = os.environ.get("NEST_TEST_CHROMIUM") or None
        try:
            chromium = manager.chromium.launch(executable_path=executable)
        except PlaywrightError as exc:
            self.unavailable(f"Chromium cannot be launched: {exc}")
        self.addCleanup(chromium.close)
        self.context = chromium.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10_000)
        self.problems: list[str] = []
        self.page.on(
            "console",
            # Chromium logs every 4xx answer; the Console expects those (a refusal, or
            # the 401 that makes it refresh). Anything else, such as a CSP violation
            # or a script error, is a problem.
            lambda m: (
                self.problems.append(m.text)
                if m.type in ("error", "warning")
                and not m.text.startswith("Failed to load resource: the server responded")
                else None
            ),
        )
        self.page.on("pageerror", lambda e: self.problems.append(str(e)))
        self.posts: list[dict[str, str]] = []
        self.page.on(
            "request",
            lambda r: self.posts.append(r.all_headers()) if r.method == "POST" else None,
        )
        self.dialogs: list[str] = []
        self.page.on("dialog", self.accept_dialog)

    def confirm(self, action: str):
        """Confirms the Console's own dialog with its `action` button."""
        dialog = self.page.get_by_role("dialog")
        dialog.wait_for()
        self.dialogs.append(dialog.inner_text())
        dialog.get_by_role("button", name=action, exact=True).click()
        dialog.wait_for(state="detached")

    def dismiss(self):
        """Cancels the Console's own dialog."""
        dialog = self.page.get_by_role("dialog")
        dialog.wait_for()
        self.dialogs.append(dialog.inner_text())
        dialog.get_by_role("button", name="Cancel").click()
        dialog.wait_for(state="detached")

    answer = True
    """How the next browser `confirm()` dialogs are answered (the Console uses none)."""

    def accept_dialog(self, dialog):
        self.dialogs.append(dialog.message)
        if self.answer:
            dialog.accept()
        else:
            dialog.dismiss()

    def sign_in(self, account: Account | None = None):
        """Signs in from the Console's own button. Returns the new User's ID."""
        if account is not None:
            self.fake.account = account
        self.page.goto(self.base + "/console")
        self.page.get_by_role("heading", name="Sign in to Nest").wait_for()
        self.page.get_by_role("link", name="Sign in with Google").click()
        self.page.wait_for_url(self.base + "/console")
        self.page.get_by_role("heading", name="Overview").wait_for()
        email = (account or self.fake.account).email
        row = self.conn.execute(
            "SELECT user_id FROM user_identities WHERE email = %s", (email,)
        ).fetchone()
        assert row is not None
        return row[0]

    def assert_clean(self):
        self.assertEqual(self.problems, [])
        for headers in self.posts:
            if headers.get("origin") is not None:
                self.assertEqual(headers["origin"], self.base)
                self.assertEqual(headers.get("sec-fetch-site", "same-origin"), "same-origin")

    def role(self, pid, user_id) -> str | None:
        row = self.conn.execute(
            "SELECT role FROM project_memberships WHERE project_id = %s AND user_id = %s",
            (pid, user_id),
        ).fetchone()
        return row[0] if row else None


class _EligibilityBrowserBase(_BrowserBase):
    """The Console of a Controller that serves the Platform `nano` with database
    Provider approvals, so `/api/connect` reports eligibility as the SDK gets it."""

    console_env: ClassVar[dict[str, str]] = {
        "NEST_CONSOLE_CONTROLLER_ADDRESS": "nest-controller.example.ts.net:50051",
        "NEST_CONSOLE_CONTROLLER_TLS": "false",
    }

    def eligibility(self) -> ExecutionAccess | None:
        self.platforms = PlatformStore(self.db)
        self.nano = self.platforms.create_platform("nano", "Nano", **OPS)
        self.platforms.add_login_node(self.nano.id, "login.nano", transport_modes=["A"], **OPS)
        self.approvals = ProviderApprovalStore(self.db)
        self.accounts = SlurmAccountStore(self.db)
        self.accounts.register_account(self.nano.id, "lab-acct", **OPS)
        return ExecutionAccess(
            self.db,
            DatabaseProviderApprovals(self.db),
            provider_id="nano",
            platform_id=self.nano.id,
        )

    def make_ready(self, pid):
        """Everything a Project needs to run on `nano`, granted by the operator."""
        self.approvals.approve("nano", pid, **OPS)
        self.platforms.grant_project(self.nano.id, pid, **OPS)
        self.accounts.link_project(pid, self.nano.id, "lab-acct", modes=["A"], default=True, **OPS)


class ConsoleBrowserTests(_BrowserBase):
    def test_owner_manages_members_and_invitations(self):
        me = self.sign_in()
        self.page.get_by_text("You are not in any Project yet").wait_for()
        # The Console offers no Project creation to an ordinary user.
        self.assertEqual(self.page.get_by_role("button", name="Create Project").count(), 0)
        self.assertEqual(self.page.get_by_role("link", name="Administration").count(), 0)
        pid, users = self.team()
        self.service.add_member(browser(users["owner"]), pid, me, "admin")
        self.page.reload()
        self.page.get_by_role("navigation", name="Console").get_by_role(
            "link", name="Projects"
        ).click()
        self.page.get_by_role("heading", name="Your Projects").wait_for()
        self.page.get_by_role("link", name="shared", exact=True).click()
        self.page.get_by_role("heading", name="shared", exact=True).wait_for()
        self.assertTrue(self.page.locator("h1").evaluate("e => e === document.activeElement"))
        self.page.get_by_role("heading", name="Members (5)").wait_for()
        # Members are shown by name, not only by ID.
        self.page.locator("#main").get_by_text("Test Researcher").wait_for()

        # Add an existing user; a malformed ID is refused by the browser first.
        newcomer = str(self.user("newcomer"))
        self.page.get_by_label("User ID").fill("not-a-uuid")
        self.page.get_by_role("button", name="Add member").click()
        self.assertTrue(self.page.get_by_label("User ID").evaluate("e => e.matches(':invalid')"))
        self.page.get_by_label("User ID").fill(newcomer)
        self.page.get_by_role("button", name="Add member").click()
        self.page.get_by_text("Member added.").wait_for()
        self.page.get_by_role("heading", name="Members (6)").wait_for()
        # Adding them again reports the server's refusal, keeps the input and
        # moves focus to the message.
        self.page.get_by_label("User ID").fill(newcomer)
        self.page.get_by_role("button", name="Add member").click()
        error = self.page.locator(".form-error", has_text="already a member")
        error.wait_for()
        self.assertTrue(error.evaluate("e => e === document.activeElement"))
        self.assertEqual(self.page.get_by_label("User ID").input_value(), newcomer)

        # Change their role, then remove them after confirming.
        self.page.get_by_label(f"New role for newcomer ({newcomer})").select_option("viewer")
        self.page.get_by_role("button", name="Change role of newcomer").click()
        self.page.get_by_text("newcomer is now Viewer.").wait_for()
        self.assertEqual(self.role(pid, newcomer), "viewer")
        # The Owner's row has no controls, for an Admin.
        owner_row = self.page.locator(f'tr[data-user="{users["owner"]}"]')
        self.assertEqual(owner_row.get_by_role("button").count(), 0)
        # Cancelling the confirmation changes nothing.
        self.page.get_by_role("button", name=f"Remove newcomer ({newcomer})").click()
        self.page.get_by_role("dialog").get_by_role("button", name="Cancel").click()
        self.assertEqual(self.role(pid, newcomer), "viewer")
        self.page.get_by_role("button", name=f"Remove newcomer ({newcomer})").click()
        self.confirm("Remove member")
        self.assertIn("loses access to shared", self.dialogs[-1])
        self.page.get_by_text("Member removed.").wait_for()
        self.assertIsNone(self.role(pid, newcomer))

        # Invite by email: the link is shown once, then revoke it.
        self.page.get_by_role("link", name="Invitations", exact=True).last.click()
        self.page.get_by_role("heading", name="Email invitations").wait_for()
        self.page.get_by_label("Email address").fill("guest@example.org")
        self.page.get_by_label("Email the invitation").uncheck()
        self.page.get_by_role("button", name="Invite", exact=True).click()
        self.page.get_by_text("Invitation created.").wait_for()
        link = self.page.get_by_label("Invitation link.").input_value()
        self.assertTrue(link.startswith(self.base + "/invitations/accept#token="), link)
        self.page.get_by_role("button", name="Revoke invitation to guest@example.org").click()
        self.confirm("Revoke invitation")
        self.page.get_by_text("Invitation revoked.").wait_for()
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM project_email_invitations "
                "WHERE invited_email_normalized = 'guest@example.org'"
            ).fetchone(),
            ("revoked",),
        )

        # An Invite Link, joined by another user from their Console.
        self.page.get_by_role("button", name="Create Invite Link").click()
        self.page.get_by_text("Invite Link created.").wait_for()
        invite_link = self.page.get_by_label("Invite Link.").input_value()
        self.page.get_by_role("link", name="Activity").click()
        self.page.get_by_role("heading", name="Recent membership changes").wait_for()
        self.page.get_by_text("Member removed").first.wait_for()
        self.assert_clean()

        self.page.get_by_label("Account: Test Researcher").click()
        self.page.get_by_role("button", name="Sign out").click()
        self.page.get_by_text("You have signed out.").wait_for()
        joiner = self.sign_in(Account(sub="108000000000000000777", email="joiner@example.org"))
        self.page.get_by_role("navigation", name="Console").get_by_role(
            "link", name="Invitations"
        ).click()
        self.page.get_by_label("Invitation link").fill(invite_link)
        self.page.get_by_role("button", name="Review invitation").click()
        self.page.get_by_text("as Viewer?").wait_for()
        self.page.get_by_role("button", name="Accept").click()
        self.page.get_by_text("You joined shared.").wait_for()
        self.assertEqual(self.role(pid, str(joiner)), "viewer")
        self.assert_clean()

    def test_viewer_sees_no_management_and_cannot_bypass_it(self):
        me = self.sign_in()
        pid, users = self.team()
        self.service.add_member(browser(users["owner"]), pid, me, "viewer")
        self.page.goto(f"{self.base}/console#/projects/{pid}")
        self.page.get_by_role("heading", name="shared", exact=True).wait_for()
        self.page.get_by_text("Only the Project’s Owner and Admins can manage").wait_for()
        for name in ("Add member", "Invite", "Create Invite Link", "Change role", "Remove"):
            self.assertEqual(self.page.get_by_role("button", name=name, exact=True).count(), 0)
        self.assertEqual(self.page.get_by_role("navigation", name="Project sections").count(), 0)
        # A direct link to a manager's tab shows the members instead.
        self.page.goto(f"{self.base}/console#/projects/{pid}/invitations")
        self.page.get_by_role("heading", name="Members (5)").wait_for()
        self.assertEqual(self.page.get_by_role("heading", name="Email invitations").count(), 0)
        self.page.get_by_role("button", name="Leave Project").wait_for()
        # The same request the hidden form would send, made from the page itself.
        answer = self.page.evaluate(
            """async ([pid, user]) => {
                const r = await fetch(`/api/projects/${pid}/members`, {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({user_id: user, role: 'admin'})});
                return [r.status, await r.json()];
            }""",
            [str(pid), str(me)],
        )
        self.assertEqual(answer, [403, {"error": "forbidden", "reason": "role"}])
        for path in (f"/api/projects/{pid}/invitations", f"/api/projects/{pid}/audit"):
            status = self.page.evaluate(
                "async (p) => (await fetch(p, {credentials: 'same-origin'})).status", path
            )
            self.assertEqual(status, 403, path)
        # Leaving removes the membership and returns to the (now empty) list.
        self.page.get_by_role("button", name="Leave Project").click()
        self.confirm("Leave Project")
        self.page.get_by_text("You left the Project.").wait_for()
        self.page.get_by_text("You are not a member of any Project yet.").wait_for()
        self.assertIsNone(self.role(pid, str(me)))
        self.assert_clean()

    def test_nonmember_link_and_stale_access(self):
        me = self.sign_in()
        pid, users = self.team()
        self.page.goto(f"{self.base}/console#/projects/{pid}")
        self.page.get_by_role("heading", name="Project not available").wait_for()
        # Removed while the page is open: the next action shows the loss of access.
        self.service.add_member(browser(users["owner"]), pid, me, "admin")
        self.page.reload()
        self.page.get_by_role("heading", name="shared", exact=True).wait_for()
        self.service.remove_member(browser(users["owner"]), pid, me)
        self.page.get_by_label("User ID").fill(str(self.user("late")))
        self.page.get_by_role("button", name="Add member").click()
        self.page.get_by_role("heading", name="Project not available").wait_for()
        self.assertEqual(self.problems, [])

    def test_revoked_session_returns_to_sign_in(self):
        me = self.sign_in()
        self.identities.revoke_user_sessions(me)
        self.page.get_by_role("navigation", name="Console").get_by_role(
            "link", name="Invitations"
        ).click()
        self.page.get_by_role("heading", name="Sign in to Nest").wait_for()
        self.page.get_by_text("Your session has ended. Sign in again.").wait_for()
        self.assertEqual(self.page.get_by_role("button", name="Sign out").count(), 0)
        self.assertEqual(self.page.locator("#project-switcher").count(), 0)

    def test_expired_access_token_is_refreshed(self):
        self.sign_in()
        # Expire the access token only: the Console refreshes it and carries on.
        self.conn.execute(
            "UPDATE session_access_tokens SET created_at = now() - interval '10 min', expires_at = now() - interval '1 s'"
        )
        self.page.get_by_role("navigation", name="Console").get_by_role(
            "link", name="Invitations"
        ).click()
        self.page.get_by_role("heading", name="Your invitations").wait_for()
        self.page.get_by_text("No pending invitations").wait_for()
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM user_sessions WHERE revoked_at IS NULL AND replaced_by_session_id IS NULL"
            ).fetchone(),
            (1,),
        )
        self.assert_clean()


OPS: dict[str, Any] = {"operator": "console-tests", "reason": "Console admin tests"}
WHO = Principal(uuid.uuid4(), "browser")
"""Any caller: the Provider authority decides per Project, not per User."""


class _AdminBrowserBase(_BrowserBase):
    """A platform admin in the Console, against the merged G03 management API."""

    def setUp(self):
        super().setUp()
        platforms = PlatformStore(self.db)
        nano = platforms.create_platform("nano", "Nano", **OPS)
        platforms.add_login_node(nano.id, "login.nano", transport_modes=["A"], **OPS)
        self.approvals = ProviderApprovalStore(self.db)

    def admin_signs_in(self):
        me = self.sign_in()
        self.assertEqual(self.page.get_by_role("link", name="Administration").count(), 0)
        self.identities.grant_platform_role(me, **OPS)
        self.page.reload()
        self.page.get_by_role("navigation", name="Console").get_by_role(
            "link", name="Administration"
        ).click()
        self.page.get_by_role("heading", name="Administration").wait_for()
        self.page.get_by_role("heading", name="Projects", exact=True).wait_for()
        return me

    def create_project(self, owner, name: str = "lab") -> uuid.UUID:
        """Creates a Project from the Administration page, picking `owner` in the directory."""
        self.page.get_by_role("link", name="Create a Project").first.click()
        self.page.get_by_role("heading", name="Choose the Owner").wait_for()
        self.page.get_by_text("Browse the user directory").click()
        row = self.page.locator("tr", has=self.page.get_by_text(str(owner)))
        row.get_by_role("button", name="as Owner").click()
        self.assertEqual(
            self.page.get_by_label("Initial Owner (user ID)").input_value(), str(owner)
        )
        self.page.get_by_label("Project name").fill(name)
        self.page.get_by_role("button", name="Create Project").click()
        # The reason is required before anything is sent.
        self.assertTrue(
            self.page.get_by_label("Reason", exact=True).evaluate("e => e.matches(':invalid')")
        )
        self.page.get_by_label("Reason", exact=True).fill(f"onboard {name}")
        self.page.get_by_role("button", name="Create Project").click()
        self.page.get_by_role("heading", name=f"Access: {name}").wait_for()
        self.page.get_by_text(f"Created {name}.").wait_for()
        row = self.conn.execute("SELECT id FROM projects WHERE name = %s", (name,)).fetchone()
        assert row is not None
        return row[0]

    def reason(self, text: str):
        self.page.get_by_label("Reason for changes").fill(text)

    def effect(self, provider: str) -> str:
        row = self.page.locator("tr", has=self.page.locator("code", has_text=provider))
        return row.locator("td").nth(2).inner_text()

    def audited(self, me, pid) -> list[tuple[str, str | None]]:
        return self.conn.execute(
            "SELECT event, note FROM security_audit_events "
            "WHERE actor_user_id = %s AND project_id = %s AND event LIKE 'provider.approval.%%' "
            "ORDER BY id",
            (me, pid),
        ).fetchall()


class ConsoleAdminDatabaseModeTests(_AdminBrowserBase):
    def test_revocation_seen_while_paging_the_directory_drops_administration(self):
        # More users than one directory page (25), so "Load more users" is offered.
        for n in range(30):
            self.user(f"user {n:02}")
        me = self.admin_signs_in()
        self.page.get_by_role("link", name="Users", exact=True).click()
        self.page.get_by_role("heading", name="User directory").wait_for()
        more = self.page.get_by_role("button", name="Load more users")
        more.wait_for()
        self.identities.revoke_platform_role(me, **OPS)
        more.click()
        self.page.get_by_role("heading", name="Administration not available").wait_for()
        self.page.get_by_text("Only platform administrators can do this.").first.wait_for()
        self.assertEqual(self.page.get_by_role("link", name="Administration").count(), 0)
        self.assertEqual(self.page.get_by_role("button", name="Create Project").count(), 0)
        self.assert_clean()

    provider_policy = ProviderPolicy("database", "nano")

    def test_admin_creates_a_project_and_manages_effective_approvals(self):
        owner = self.user("future owner")
        me = self.admin_signs_in()
        pid = self.create_project(owner)
        self.assertEqual(self.role(pid, owner), "owner")
        self.assertIsNone(self.role(pid, me))  # An admin who names an Owner joins nothing.
        authority = DatabaseProviderApprovals(self.db)
        self.assertFalse(authority.permits(WHO, pid, Action.RUN_SUBMIT, "nano"))

        self.page.get_by_text("Database: approvals and revocations recorded here decide").wait_for()
        policy = self.page.get_by_label("Provider policy of this Controller")
        self.assertIn("nano", policy.inner_text())
        self.assertEqual(self.page.get_by_text("only records an approval").count(), 0)
        self.assertEqual(self.page.get_by_role("textbox", name="Provider").input_value(), "nano")

        # Every change needs a reason. The Platform is chosen from the catalog.
        self.page.get_by_label("Platform (slug or ID)").select_option("nano")
        self.page.get_by_role("button", name="Grant Platform").click()
        self.page.get_by_text("Enter a reason first").wait_for()
        self.reason("platform for lab")
        self.page.get_by_role("button", name="Grant Platform").click()
        self.page.get_by_text("Platform granted.").wait_for()
        self.page.get_by_role("button", name="Revoke nano", exact=True).wait_for()

        self.reason("approve lab")
        self.page.get_by_role("button", name="Approve Provider").click()
        self.page.get_by_text("Approval recorded. It is effective for new submissions").wait_for()
        self.assertEqual(self.dialogs, [])
        # Every requirement the access page lists is now in place but the account.
        needs = self.page.locator(".checklist li")
        self.assertEqual(needs.nth(0).get_attribute("class"), None)
        self.assertEqual(needs.nth(1).get_attribute("class"), None)
        self.assertEqual(needs.nth(2).get_attribute("class"), "missing")
        self.assertIn("recorded and effective", self.effect("nano"))
        self.assertTrue(authority.permits(WHO, pid, Action.RUN_SUBMIT, "nano"))

        self.reason("offboard lab")
        self.page.get_by_role("button", name="Revoke nano approval").click()
        self.confirm("Revoke approval")
        self.page.get_by_text("Revocation recorded. New submissions are refused").wait_for()
        self.assertIn("Revoked: recorded and effective", self.effect("nano"))
        self.assertFalse(authority.permits(WHO, pid, Action.RUN_SUBMIT, "nano"))

        # A record for a Provider this Controller does not check decides nothing here.
        self.reason("other provider")
        self.page.get_by_role("textbox", name="Provider").fill("other")
        self.page.get_by_role("button", name="Approve Provider").click()
        self.confirm("Record approval")
        self.page.get_by_text("It does not enable submissions on this Controller").wait_for()
        self.assertIn("will NOT enable submissions", self.dialogs[-1])
        self.assertIn("checks Provider nano, not other", self.dialogs[-1])
        self.assertIn("recorded only, not effective", self.effect("other"))
        self.assertEqual(
            self.audited(me, pid),
            [
                ("provider.approval.granted", "approve lab"),
                ("provider.approval.revoked", "offboard lab"),
                ("provider.approval.granted", "other provider"),
            ],
        )
        self.page.screenshot(
            path=os.path.join(_evidence(), "console-admin-database.png"), full_page=True
        )
        self.assert_clean()


class ConsoleAdminEnvironmentModeTests(_AdminBrowserBase):
    provider_policy = ProviderPolicy("environment", "nano")

    def test_recorded_approvals_are_labelled_not_effective(self):
        owner = self.user("future owner")
        self.admin_signs_in()
        pid = self.create_project(owner)
        self.page.get_by_text("Environment allow-list: the Controller").wait_for()
        self.page.get_by_text("only records an approval. It does not enable submissions").wait_for()
        self.assertEqual(self.page.get_by_role("button", name="Approve Provider").count(), 0)
        record = self.page.get_by_role("button", name="Record approval (not effective here)")

        # Cancelling the warning records nothing.
        self.reason("try")
        record.click()
        self.dismiss()
        self.assertIn("will NOT enable submissions", self.dialogs[-1])
        self.assertIn("environment allow-list", self.dialogs[-1])
        self.assertIsNone(self.approvals.get("nano", pid))

        record.click()
        self.confirm("Record approval")
        self.page.get_by_text(
            "Approval recorded. It does not enable submissions on this Controller"
        ).wait_for()
        self.assertEqual(self.page.get_by_text("effective for new submissions").count(), 0)
        self.assertIn("recorded only, not effective", self.effect("nano"))
        # What the Controller would decide: the database approval admits nothing,
        # only its environment list does.
        unlisted = AllowedProjects([], revocations=self.approvals)
        listed = AllowedProjects([str(pid)], revocations=self.approvals)
        self.assertFalse(unlisted.permits(WHO, pid, Action.RUN_SUBMIT, "nano"))
        self.assertTrue(listed.permits(WHO, pid, Action.RUN_SUBMIT, "nano"))

        # A recorded revocation does take effect, even for a listed Project.
        self.reason("offboard")
        self.page.get_by_role("button", name="Revoke nano approval").click()
        self.confirm("Revoke approval")
        self.page.get_by_text("Revocation recorded. New submissions are refused").wait_for()
        self.assertIn("Revoked: recorded and effective", self.effect("nano"))
        self.assertFalse(listed.permits(WHO, pid, Action.RUN_SUBMIT, "nano"))
        self.page.screenshot(
            path=os.path.join(_evidence(), "console-admin-environment.png"), full_page=True
        )
        self.assert_clean()


class ConsoleAdminRevocationTests(_AdminBrowserBase):
    """No Provider policy reported (`unknown`), and admin rights that end mid-session."""

    def test_admin_role_and_session_revocation_take_effect(self):
        owner = self.user("future owner")
        me = self.admin_signs_in()
        pid = self.create_project(owner)
        self.page.get_by_text("Unknown: this server did not report its Provider policy").wait_for()
        self.page.get_by_role("button", name="Record approval (not effective here)").wait_for()

        # The role is revoked while the page is open: the next change is refused by
        # the server and the Console drops its Administration pages.
        self.identities.revoke_platform_role(me, **OPS)
        self.reason("too late")
        self.page.get_by_label("Platform (slug or ID)").select_option("nano")
        self.page.get_by_role("button", name="Grant Platform").click()
        self.page.get_by_role("heading", name="Administration not available").wait_for()
        self.assertEqual(self.page.get_by_role("link", name="Administration").count(), 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM hpc_platform_project_grants WHERE project_id = %s", (pid,)
            ).fetchone(),
            (0,),
        )
        # Nor can the page go around the Console.
        answer = self.page.evaluate(
            """async () => {
                const r = await fetch('/api/admin/projects', {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'sneaky', reason: 'bypass'})});
                return [r.status, await r.json()];
            }"""
        )
        self.assertEqual(answer, [403, {"error": "forbidden", "reason": "platform_admin_required"}])
        # Reloaded, the server's answer still decides: no Administration pages.
        self.page.reload()
        self.page.get_by_role("heading", name="Administration not available").wait_for()
        self.assertEqual(self.page.get_by_role("link", name="Administration").count(), 0)

        # An admin whose sessions are revoked is back at sign-in.
        self.identities.grant_platform_role(me, **OPS)
        self.page.reload()
        console_nav = self.page.get_by_role("navigation", name="Console")
        console_nav.get_by_role("link", name="Administration").wait_for()
        self.identities.revoke_user_sessions(me)
        console_nav.get_by_role("link", name="Administration").click()
        self.page.get_by_text("Your session has ended. Sign in again.").wait_for()
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM projects WHERE name = 'sneaky'").fetchone(),
            (0,),
        )
        self.assert_clean()


def _evidence() -> str:
    """Where browser screenshots go: `NEST_TEST_EVIDENCE_DIR`, or a temporary directory."""
    path = os.environ.get("NEST_TEST_EVIDENCE_DIR") or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "nest-console-evidence"
    )
    os.makedirs(path, exist_ok=True)
    return path


class ConnectAPITests(_ConsoleBase):
    """`GET /api/connect` (G02c): operator profiles and server-side eligibility."""

    console_env: ClassVar[dict[str, str]] = {
        "NEST_CONSOLE_CONTROLLER_ADDRESS": "nest-controller.example.ts.net:50051",
        "NEST_CONSOLE_CONTROLLER_TLS": "false",
    }

    def eligibility(self) -> ExecutionAccess | None:
        self.platforms = PlatformStore(self.db)
        self.nano = self.platforms.create_platform("nano", "Nano", **OPS)
        self.approvals = ProviderApprovalStore(self.db)
        self.accounts = SlurmAccountStore(self.db)
        self.accounts.register_account(self.nano.id, "secret-acct-name", **OPS)
        return ExecutionAccess(
            self.db,
            DatabaseProviderApprovals(self.db),
            provider_id="nano",
            platform_id=self.nano.id,
        )

    def test_profiles_and_commands_come_from_the_server(self):
        user = self.user("someone")
        status, body = self.call("GET", "/api/connect", self.token(user))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["auth_url"], self.base)
        self.assertEqual(body["auth_export"], f"export NEST_AUTH_URL={self.base}")
        self.assertEqual(
            body["install"], "uv add 'nest @ git+https://github.com/cerulean-works/nest@main'"
        )
        [profile] = body["profiles"]
        self.assertEqual((profile["id"], profile["status"]), ("private", "available"))
        self.assertEqual(
            profile["exports"],
            [
                f"export NEST_AUTH_URL={self.base}",
                "export NEST_CONTROLLER_ADDRESS=nest-controller.example.ts.net:50051",
                "export NEST_CONTROLLER_TLS=false",
            ],
        )
        # The exact settings the panel copies are accepted by the SDK as they are.
        client = load_client_config(settings_of(profile))
        self.assertEqual(
            (client.address, client.tls, client.auth_url),
            ("nest-controller.example.ts.net:50051", False, self.base),
        )
        self.assertEqual(body["eligibility"], {"evaluated": True, "projects": []})

    def test_eligibility_reasons_follow_each_grant_and_never_name_an_account(self):
        pid, users = self.team()
        tokens = {role: self.token(user) for role, user in users.items()}

        def reasons(role):
            status, body = self.call("GET", "/api/connect", tokens[role])
            self.assertEqual(status, 200, body)
            self.assertNotIn("secret-acct-name", json.dumps(body))
            return {p["id"]: (p["eligible"], p["reasons"]) for p in body["eligibility"]["projects"]}

        self.assertEqual(reasons("member")[str(pid)], (False, ["provider", "platform", "account"]))
        self.assertEqual(reasons("outsider"), {})
        self.approvals.approve("nano", pid, **OPS)
        self.platforms.grant_project(self.nano.id, pid, **OPS)
        self.assertEqual(reasons("member")[str(pid)], (False, ["account"]))
        self.accounts.link_project(
            pid, self.nano.id, "secret-acct-name", modes=["A"], default=True, **OPS
        )
        self.assertEqual(reasons("member")[str(pid)], (True, []))
        self.assertEqual(reasons("owner")[str(pid)], (True, []))
        # Management access and compute eligibility differ: a Viewer may read, not run.
        self.assertEqual(reasons("viewer")[str(pid)], (False, ["role"]))
        # A revoked grant shows on the next read, as the SDK's listing would.
        self.platforms.revoke_project(self.nano.id, pid, **OPS)
        eligible, missing = reasons("member")[str(pid)]
        self.assertFalse(eligible)
        self.assertIn("platform", missing)

    def test_route_is_read_only_authenticated_and_same_origin(self):
        user = self.user("someone")
        cookie = self.token(user)
        self.assertEqual(
            self.call("GET", "/api/connect", None), (401, {"error": "unauthenticated"})
        )
        self.assertEqual(self.call("POST", "/api/connect", cookie, origin=EVIL)[0], 403)
        status, headers, _ = self.request(
            "POST",
            "/api/connect",
            cookie=cookie,
            headers={"Origin": self.base, "Sec-Fetch-Site": "same-origin"},
            body={},
        )
        self.assertEqual((status, headers["Allow"]), (405, "GET"))
        self.assertEqual(self.call("GET", "/api/connect/x", cookie)[0], 404)
        # A CLI session needs its Projects scope to read eligibility.
        cli = self.token(user, kind="cli", scope="jobs:run")
        status, _, _ = self.request(
            "GET", "/api/connect", headers={"Authorization": f"Bearer {cli}"}
        )
        self.assertEqual(status, 200)

    def test_session_names_the_signed_in_user_and_members_have_names(self):
        pid, users = self.team()
        cookie = self.token(users["member"])
        status, body = self.call("GET", "/auth/session", cookie)
        self.assertEqual((status, body["display_name"]), (200, "member"))
        _, listed = self.call("GET", f"/api/projects/{pid}/members", cookie)
        self.assertEqual(
            {m["role"]: m["display_name"] for m in listed["members"]},
            {"owner": "owner", "admin": "admin", "member": "member", "viewer": "viewer"},
        )


def settings_of(profile: dict[str, Any]) -> dict[str, str]:
    """A profile's settings as an environment."""
    return {name: value for name, value in cast(list[list[str]], profile["settings"])}


class ConnectProfileTests(unittest.TestCase):
    """Which profiles the Console offers, and that each is valid for the SDK."""

    def test_no_settings_offer_no_profile(self):
        self.assertEqual(connection_profiles(ConsoleConnectConfig(), "https://nest.example"), [])

    def test_hosted_profile_is_a_labelled_pilot_on_the_hosted_origin_only(self):
        connect = ConsoleConnectConfig(
            "nest-controller.tail8e5ba1.ts.net:50051", False, hosted_profile="pilot"
        )
        private, hosted = connection_profiles(connect, HOSTED_AUTH_URL)
        self.assertEqual(private["id"], "private")  # the existing Tailnet path first
        self.assertEqual(
            (hosted["status"], hosted["exports"]), ("pilot", ["export NEST_PROFILE=hosted"])
        )
        for profile in (private, hosted):
            load_client_config(settings_of(profile))
        # Another origin is never offered the hosted session's profile.
        self.assertEqual(
            [p["id"] for p in connection_profiles(connect, "https://nest.example")], ["private"]
        )
        # After H09 the hosted profile is the default and needs no setting.
        with mock.patch("nest.config.HOSTED_PROFILE_DEFAULT", True):
            hosted = connection_profiles(connect, HOSTED_AUTH_URL)[1]
        self.assertEqual((hosted["status"], hosted["exports"]), ("default", []))

    def test_a_custom_address_gets_the_hosted_session_only_when_named_twice_over_tls(self):
        connect = ConsoleConnectConfig("staging.example.org:443", True)
        [profile] = connection_profiles(connect, HOSTED_AUTH_URL)
        self.assertIn(
            ("NEST_HOSTED_SESSION_ADDRESS", "staging.example.org:443"),
            settings_of(profile).items(),
        )
        load_client_config(settings_of(profile))


class ConnectBrowserTests(_EligibilityBrowserBase):
    """The Connect panel, the Project switcher and eligibility in a real browser."""

    def setUp(self):
        super().setUp()
        self.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=self.base)

    def test_connect_panel_copies_safe_commands_for_the_chosen_project(self):
        me = self.sign_in()
        owner = self.user("pi")
        ready = owned_project(self.service, owner, "Shared lab")
        twin = owned_project(self.service, owner, "Shared lab")
        hostile = owned_project(self.service, owner, "<img src=x onerror=alert(1)>'; rm -rf ~")
        for pid, role in ((ready.id, "member"), (twin.id, "member"), (hostile.id, "viewer")):
            self.service.add_member(browser(owner), pid, me, role)
        self.make_ready(ready.id)
        self.page.reload()
        self.page.get_by_role("heading", name="Overview").wait_for()
        # The only Project that can run is chosen first; duplicates are told apart.
        switcher = self.page.get_by_label("Current Project", exact=True)
        self.assertEqual(switcher.input_value(), str(ready.id))
        labels = switcher.locator("option").all_inner_texts()
        self.assertIn(f"Shared lab ({str(ready.id)[:8]}) · Member", labels)
        self.assertIn(f"Shared lab ({str(twin.id)[:8]}) · Member", labels)
        # Names are text, never markup.
        self.assertIn("<img src=x onerror=alert(1)>'; rm -rf ~ · Viewer", labels)
        self.assertEqual(self.page.locator("#main img").count(), 0)

        self.page.get_by_role("navigation", name="Console").get_by_role(
            "link", name="Connect"
        ).click()
        self.page.get_by_role("heading", name="Connect to a Project").wait_for()
        self.page.get_by_text("can run on this Controller.").wait_for()
        block = self.page.locator('[data-command="Select the Project"]')
        block.get_by_role("button").click()
        block.get_by_role("button", name="Copy: Select the Project").filter(
            has_text="Copied"
        ).wait_for()
        copied = self.page.evaluate("navigator.clipboard.readText()")
        self.assertEqual(
            copied, f"uv run nest projects use {ready.id}\nuv run nest projects current"
        )
        settings = self.page.locator('[data-command="Connection settings and sign-in"] pre')
        self.assertEqual(
            settings.inner_text().splitlines(),
            [
                f"export NEST_AUTH_URL={self.base}",
                "export NEST_CONTROLLER_ADDRESS=nest-controller.example.ts.net:50051",
                "export NEST_CONTROLLER_TLS=false",
                "uv run nest login",
                "uv run nest auth status --check",
            ],
        )

        # A Project that cannot run says why, and offers no select command.
        self.page.get_by_label("Project", exact=True).select_option(str(hostile.id))
        self.page.get_by_text("Your role cannot run work").wait_for()
        self.page.get_by_text("not approved for this Controller").wait_for()
        self.assertEqual(self.page.locator('[data-command="Select the Project"]').count(), 0)
        commands = self.page.locator("#connect-steps pre").all_inner_texts()
        self.assertFalse(any("rm -rf" in c or "onerror" in c for c in commands))
        self.assertEqual(
            self.page.get_by_label("Current Project", exact=True).input_value(), str(hostile.id)
        )
        self.page.screenshot(path=os.path.join(_evidence(), "console-connect.png"), full_page=True)
        self.assert_clean()

    def test_switching_remembers_the_project_and_handles_revocation(self):
        me = self.sign_in()
        owner = self.user("pi")
        first = owned_project(self.service, owner, "alpha")
        second = owned_project(self.service, owner, "beta")
        for pid in (first.id, second.id):
            self.service.add_member(browser(owner), pid, me, "member")
            self.make_ready(pid)
        self.page.reload()
        self.page.get_by_label("Current Project", exact=True).select_option(str(second.id))
        self.page.locator("#main").get_by_text("beta", exact=True).wait_for()
        # Kept across reloads, in this browser only, as a bare Project ID.
        self.page.reload()
        self.page.get_by_role("heading", name="Overview").wait_for()
        self.assertEqual(
            self.page.get_by_label("Current Project", exact=True).input_value(), str(second.id)
        )
        stored = self.page.evaluate("Object.entries(localStorage)")
        self.assertEqual(stored, [[f"nest.console.project.{me}", str(second.id)]])
        # Revoked grant: shown as soon as the page is read again.
        self.platforms.revoke_project(self.nano.id, second.id, **OPS)
        self.page.reload()
        self.page.get_by_text("This Project cannot run work on this Controller yet.").wait_for()
        self.page.get_by_text("HPC Platform is not granted to the Project").wait_for()
        # Removed from the remembered Project: another is chosen, and the page says so.
        self.service.remove_member(browser(owner), second.id, me)
        self.page.reload()
        self.page.get_by_text("no longer available to you").wait_for()
        self.assertEqual(
            self.page.get_by_label("Current Project", exact=True).input_value(), str(first.id)
        )
        self.assert_clean()

    def test_layout_fits_a_phone_and_works_from_the_keyboard(self):
        me = self.sign_in()
        owner = self.user("pi")
        project = owned_project(self.service, owner, "Long Project name " * 4)
        self.service.add_member(browser(owner), project.id, me, "admin")
        self.page.set_viewport_size({"width": 360, "height": 740})
        for route in ("#/overview", "#/connect", "#/projects", f"#/projects/{project.id}"):
            self.page.goto(f"{self.base}/console{route}")
            self.page.reload()
            self.page.locator("#main h1").wait_for()
            self.page.wait_for_load_state("networkidle")
            overflow = self.page.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            self.assertLessEqual(overflow, 0, route)
        # The skip link is the first focusable element and moves focus to the content.
        first = self.page.evaluate(
            "document.querySelector('a[href], button, input, select, [tabindex]').className"
        )
        self.assertEqual(first, "skip")
        self.page.locator(".skip").focus()
        self.page.keyboard.press("Enter")
        self.assertEqual(self.page.evaluate("document.activeElement.id"), "main")
        self.assertTrue(self.page.url.endswith(f"#/projects/{project.id}"), self.page.url)
        # A confirmation opens on Cancel, closes on Escape and gives focus back.
        newcomer = self.user("newcomer")
        self.service.add_member(browser(owner), project.id, newcomer, "viewer")
        self.page.reload()
        remove = self.page.get_by_role("button", name=f"Remove newcomer ({newcomer})")
        remove.focus()
        self.page.keyboard.press("Enter")
        self.page.get_by_role("dialog").wait_for()
        self.assertEqual(self.page.evaluate("document.activeElement.textContent"), "Cancel")
        self.page.keyboard.press("Escape")
        self.page.get_by_role("dialog").wait_for(state="detached")
        self.assertTrue(remove.evaluate("e => e === document.activeElement"))
        self.assertEqual(self.role(project.id, newcomer), "viewer")
        self.page.screenshot(path=os.path.join(_evidence(), "console-mobile.png"), full_page=True)
        self.assert_clean()

    def test_without_a_profile_only_install_and_sign_in_are_offered(self):
        server = self.http.server
        assert server is not None
        server.connect = ConsoleConnectConfig()  # the operator published no profile
        me = self.sign_in()
        owner = self.user("pi")
        project = owned_project(self.service, owner, "lab")
        self.service.add_member(browser(owner), project.id, me, "member")
        self.make_ready(project.id)
        self.page.goto(f"{self.base}/console#/connect")
        self.page.reload()
        self.page.get_by_text("has not published connection settings").first.wait_for()
        commands = [
            c.get_attribute("data-command") for c in self.page.locator("[data-command]").all()
        ]
        self.assertEqual(commands, ["Install Nest", "Connection settings and sign-in"])
        settings = self.page.locator('[data-command="Connection settings and sign-in"] pre')
        self.assertEqual(
            settings.inner_text().splitlines(),
            [
                f"export NEST_AUTH_URL={self.base}",
                "uv run nest login",
                "uv run nest auth status --check",
            ],
        )
        self.page.locator("#connect-needs-controller").wait_for()
        self.assert_clean()

    # Runnable commands (G02c, #95): offered only for a Project the server says can run
    # here, and bound to it, so a copied command never runs in another Project.

    SETUP_ONLY: ClassVar[list[str]] = ["Install Nest", "Connection settings and sign-in"]
    RUNNABLE: ClassVar[list[str]] = [
        *SETUP_ONLY,
        "Select the Project",
        "Example main.py",
        "Run the example",
    ]

    def offered(self) -> list[str | None]:
        """The names of the copyable command blocks the Connect page shows now."""
        return [c.get_attribute("data-command") for c in self.page.locator("[data-command]").all()]

    def command(self, name: str) -> str:
        return self.page.locator(f'[data-command="{name}"] pre').inner_text()

    def open_connect(self):
        self.page.goto(f"{self.base}/console#/connect")
        self.page.reload()
        self.page.get_by_role("heading", name="Connect to a Project").wait_for()

    def choose(self, pid):
        self.page.get_by_label("Project", exact=True).select_option(str(pid))

    def assert_runnable_for(self, pid):
        self.page.get_by_text("can run on this Controller.").wait_for()
        self.assertEqual(self.offered(), self.RUNNABLE)
        self.assertEqual(self.page.locator("#connect-not-runnable").count(), 0)
        self.assertEqual(
            self.command("Select the Project").splitlines(),
            [f"uv run nest projects use {pid}", "uv run nest projects current"],
        )
        self.assertEqual(
            self.command("Run the example"), f"NEST_PROJECT_ID={pid} uv run python main.py"
        )

    def assert_not_runnable(self, why: str):
        self.page.locator("#connect-not-runnable").get_by_text(why).wait_for()
        self.assertEqual(self.offered(), self.SETUP_ONLY)
        text = "\n".join(self.page.locator("#connect-steps pre").all_inner_texts())
        self.assertNotIn("NEST_PROJECT_ID", text)
        self.assertNotIn("python main.py", text)
        self.assertNotIn("projects use", text)

    def test_an_ineligible_project_offers_no_selection_or_run_commands(self):
        me = self.sign_in()
        owner = self.user("pi")
        a = owned_project(self.service, owner, "alpha")
        b = owned_project(self.service, owner, "beta")
        for p in (a, b):
            self.service.add_member(browser(owner), p.id, me, "member")
        self.make_ready(a.id)  # B is never approved
        self.open_connect()
        self.assert_runnable_for(a.id)
        self.choose(b.id)
        self.page.get_by_text("beta cannot run on this Controller yet.").wait_for()
        self.assert_not_runnable("Selecting and running commands appear here once")
        # The Console keeps the Project the user chose; it never switches to A by itself.
        self.assertEqual(
            self.page.get_by_label("Current Project", exact=True).input_value(), str(b.id)
        )
        self.choose(a.id)
        self.assert_runnable_for(a.id)
        self.assert_clean()

    def test_revoked_eligibility_removes_runnable_commands(self):
        me = self.sign_in()
        owner = self.user("pi")
        project = owned_project(self.service, owner, "lab")
        self.service.add_member(browser(owner), project.id, me, "member")
        self.make_ready(project.id)
        self.open_connect()
        self.assert_runnable_for(project.id)
        self.platforms.revoke_project(self.nano.id, project.id, **OPS)
        self.open_connect()
        self.assert_not_runnable("Selecting and running commands appear here once")
        self.assert_clean()

    def test_unknown_eligibility_offers_no_runnable_commands(self):
        server = self.http.server
        assert server is not None
        server.eligibility = None  # the Controller cannot evaluate eligibility
        me = self.sign_in()
        owner = self.user("pi")
        project = owned_project(self.service, owner, "lab")
        self.service.add_member(browser(owner), project.id, me, "member")
        self.open_connect()
        self.assert_not_runnable("could not confirm that lab can run")
        self.assert_clean()

    def test_without_a_project_no_runnable_commands_are_offered(self):
        self.sign_in()
        self.open_connect()
        self.page.get_by_text("You are not in any Project yet").first.wait_for()
        self.assert_not_runnable("Join a Project")
        self.assert_clean()

    def test_run_command_is_bound_to_the_chosen_project_despite_overrides(self):
        me = self.sign_in()
        owner = self.user("pi")
        first = owned_project(self.service, owner, "alpha")
        second = owned_project(self.service, owner, "beta")
        for p in (first, second):
            self.service.add_member(browser(owner), p.id, me, "member")
            self.make_ready(p.id)
        self.open_connect()
        self.choose(second.id)
        self.page.get_by_text("beta can run on this Controller.").wait_for()
        self.assert_runnable_for(second.id)
        self.page.get_by_text("Saves this Project for this Controller").wait_for()
        block = self.page.locator('[data-command="Run the example"]')
        block.get_by_role("button").click()
        block.get_by_role("button", name="Copy: Run the example").filter(
            has_text="Copied"
        ).wait_for()
        run = self.page.evaluate("navigator.clipboard.readText()")
        self.assertEqual(run, f"NEST_PROJECT_ID={second.id} uv run python main.py")
        settings = self.command("Connection settings and sign-in").splitlines()
        exports = [line for line in settings if line.startswith("export ")]

        # In a real POSIX shell, with the other Project both saved for this account and
        # exported as NEST_PROJECT_ID, the copied command still runs in the chosen one.
        with tempfile.TemporaryDirectory() as home:
            prefs = os.path.join(home, "prefs.json")
            env = {k: v for k, v in os.environ.items() if not k.startswith("NEST_")}
            env["NEST_PREFERENCES_FILE"] = prefs
            exported = (shlex.split(e.removeprefix("export "))[0].split("=", 1) for e in exports)
            saved = load_client_config({**env, **dict(exported)})
            key = project_selection.preference_key(saved, me)
            assert key is not None
            project_selection.PreferenceStore(prefs).set(key, str(first.id))
            probe = (
                "from nest.config import load_client_config\n"
                "from nest import project_selection as s\n"
                f"c = s.select_project(load_client_config(), None, 'token', {str(me)!r},"
                " interactive=False)\n"
                "print(c.project_id, c.source)\n"
            )
            program = f"{shlex.quote(sys.executable)} -c {shlex.quote(probe)}"
            script = "\n".join(
                [
                    *exports,
                    f"export NEST_PROJECT_ID={first.id}",
                    run.removesuffix("uv run python main.py") + program,
                ]
            )
            self.assertTrue(run.endswith(" uv run python main.py"))
            done = subprocess.run(
                ["sh", "-c", script], env=env, capture_output=True, text=True, check=True
            )
            # Nothing global was changed: the saved choice is still the other Project.
            self.assertEqual(project_selection.PreferenceStore(prefs).get(key), str(first.id))
        self.assertEqual(done.stdout.split(), [str(second.id), "NEST_PROJECT_ID"])
        self.assert_clean()

    def test_projects_list_searches_filters_and_pages(self):
        me = self.sign_in()
        owner = self.user("pi")
        for n in range(20):
            p = owned_project(self.service, owner, f"course {n:02}")
            self.service.add_member(browser(owner), p.id, me, "viewer" if n % 2 else "member")
        self.page.goto(f"{self.base}/console#/projects")
        self.page.get_by_role("heading", name="Your Projects").wait_for()
        rows = self.page.locator("#project-results tbody tr")
        self.assertEqual(rows.count(), 15)
        self.page.get_by_text("1–15 of 20").wait_for()
        self.page.get_by_role("button", name="Next").click()
        self.assertEqual(rows.count(), 5)
        search = self.page.get_by_label("Search Projects")
        search.fill("course 1")
        self.assertEqual(rows.count(), 10)
        self.assertTrue(search.evaluate("e => e === document.activeElement"))
        self.page.get_by_label("Role", exact=True).select_option("member")
        self.assertEqual(rows.count(), 5)
        search.fill("nothing like it")
        self.page.get_by_text("No Project matches.").wait_for()
        self.assert_clean()


class ConsoleConnectConfigTests(unittest.TestCase):
    """`NEST_CONSOLE_*`: operator-set, shell-safe, and never a way around the SDK's rules."""

    def load(self, origin="https://nest.example.com", **extra):
        return load_controller_config(
            {
                "NEST_DATABASE_URL": "postgresql://nest@db.example.internal:5432/nest",
                "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
                "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
                "NEST_GOOGLE_REDIRECT_URL": f"{origin}/auth/google/callback",
                **extra,
            }
        ).google

    def problems(self, **extra) -> str:
        with self.assertRaises(ConfigError) as caught:
            self.load(**extra)
        return str(caught.exception)

    def test_defaults_offer_nothing(self):
        google = self.load()
        assert google is not None
        self.assertEqual(google.console, ConsoleConnectConfig())
        self.assertEqual(google.summary()["console"]["hosted_profile"], "off")

    def test_private_address_needs_an_explicit_transport(self):
        google = self.load(
            NEST_CONSOLE_CONTROLLER_ADDRESS="controller.example.org:50051",
            NEST_CONSOLE_CONTROLLER_TLS="true",
        )
        assert google is not None
        self.assertEqual(google.console.controller_address, "controller.example.org:50051")
        self.assertIn(
            "explicit NEST_CONSOLE_CONTROLLER_TLS",
            self.problems(NEST_CONSOLE_CONTROLLER_ADDRESS="controller.example.org:50051"),
        )
        self.assertIn(
            "needs NEST_CONSOLE_CONTROLLER_ADDRESS",
            self.problems(NEST_CONSOLE_CONTROLLER_TLS="false"),
        )

    def test_values_that_could_inject_into_a_shell_are_refused(self):
        for address in (
            "host;rm -rf ~:50051",
            "$(id):50051",
            "host:50051 extra",
            "host",
            "host:0",
            "`x`:1",
            "https://host:443",
        ):
            self.assertIn(
                "must be the Controller's host:port",
                self.problems(
                    NEST_CONSOLE_CONTROLLER_ADDRESS=address, NEST_CONSOLE_CONTROLLER_TLS="true"
                ),
                address,
            )
        self.assertIn(
            "NEST_CONSOLE_HOSTED_PROFILE must be one of off, pilot",
            self.problems(NEST_CONSOLE_HOSTED_PROFILE="on"),
        )

    def test_hosted_rules_match_the_sdk(self):
        # The hosted profile is offered only by the hosted sign-in origin.
        self.assertIn(
            "this Nest signs in at https://nest.example.com",
            self.problems(NEST_CONSOLE_HOSTED_PROFILE="pilot"),
        )
        google = self.load(origin=HOSTED_AUTH_URL, NEST_CONSOLE_HOSTED_PROFILE="pilot")
        assert google is not None
        self.assertEqual(google.console.hosted_profile, "pilot")
        # The gateway is the hosted profile, not a private address.
        self.assertIn(
            "names the hosted public gateway",
            self.problems(
                NEST_CONSOLE_CONTROLLER_ADDRESS="grpc.nest.cerulean.works:443",
                NEST_CONSOLE_CONTROLLER_TLS="true",
            ),
        )
        # The hosted session never goes in plaintext to a non-hosted address...
        self.assertIn(
            "only over TLS",
            self.problems(
                origin=HOSTED_AUTH_URL,
                NEST_CONSOLE_CONTROLLER_ADDRESS="staging.example.org:50051",
                NEST_CONSOLE_CONTROLLER_TLS="false",
            ),
        )
        # ...but the existing Tailnet path stays available.
        google = self.load(
            origin=HOSTED_AUTH_URL,
            NEST_CONSOLE_CONTROLLER_ADDRESS="nest-controller.tail8e5ba1.ts.net:50051",
            NEST_CONSOLE_CONTROLLER_TLS="false",
        )
        assert google is not None
        self.assertFalse(google.console.controller_tls)

    def test_settings_need_google_sign_in(self):
        with self.assertRaises(ConfigError) as caught:
            load_controller_config({"NEST_CONSOLE_HOSTED_PROFILE": "pilot"})
        self.assertIn(
            "NEST_CONSOLE_HOSTED_PROFILE needs NEST_GOOGLE_CLIENT_ID", str(caught.exception)
        )
