"""The browser Management Console (G02a).

The HTTP tests check what the auth listener serves for the Console (its pages,
headers and sign-in return) and that the JSON routes the Console calls keep
enforcing the Project role matrix, sessions and the same-origin (CSRF) check
whatever a client sends. The browser tests drive the Console in a real Chromium
(through Playwright) signed in with the fake Google provider in `fake_oidc`.

Platform-admin features (Project creation with an explicit Owner, the user
directory, Platform grants, Slurm account links and Provider approvals) need the
privileged management API of G03 (#79). Without it the Console shows none of
them, which `test_privileged_ui_is_gated_on_the_admin_api` checks; their
positive acceptance is deferred until that API is merged.
"""

import json
import os
import secrets
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import NoReturn

from fake_oidc import CLIENT_ID, CLIENT_SECRET, Account, FakeGoogle
from test_authz import _Base, browser
from test_google_auth import Browser, free_port

from nest.auth import INSECURE_SESSION_COOKIE, LOGIN_PATH, GoogleSignIn, start_auth_service
from nest.auth.console import CONSOLE_CSP
from nest.config import ConfigError, load_controller_config
from nest.db import SessionStore, hash_token

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
            }
        ).google
        assert config is not None
        sign_in = GoogleSignIn(config, self.db, endpoints=self.fake.endpoints, timeout=5)
        self.http = start_auth_service(config, self.db, sign_in=sign_in)
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
        for name, kind in (("console.js", "text/javascript"), ("console.css", "text/css")):
            status, headers, _ = self.request("GET", f"/console/{name}")
            self.assertEqual(status, 200, name)
            self.assertEqual(headers["Content-Type"], f"{kind}; charset=utf-8")
            self.assertEqual(headers["Content-Security-Policy"], CONSOLE_CSP)
        for path in ("/console/index.html", "/console/../web.py", "/console/x/console.js"):
            self.assertEqual(self.request("GET", path)[0], 404, path)
        status, headers, _ = self.request("POST", "/console", headers={"Origin": self.base})
        self.assertEqual((status, headers["Allow"]), (405, "GET"))

    def test_api_routes_keep_their_strict_csp(self):
        for path in ("/api/projects", "/auth/session", "/invitations/accept"):
            status, headers, _ = self.request("GET", path)
            self.assertIn(status, (200, 401), path)
            if path != "/invitations/accept":
                self.assertEqual(headers["Content-Security-Policy"], API_CSP, path)
            self.assertNotIn("'self'", headers["Content-Security-Policy"].split("form-action")[0])

    def test_console_holds_no_secret_and_inserts_no_html(self):
        user = self.user("someone")
        cookie = self.token(user)
        _, _, page = self.request("GET", "/console", cookie=cookie)
        bundle = page + "".join(
            ASSETS.joinpath(name).read_text() for name in ("console.js", "console.css")
        )
        for secret in (cookie, CLIENT_SECRET, self.url):
            self.assertNotIn(secret, bundle)
        for forbidden in (
            "NEST_",
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function",
            "localStorage",
            "document.cookie",
            "http://",
            "https://",
        ):
            self.assertNotIn(forbidden, bundle, forbidden)

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


class ConsoleBrowserTests(_ConsoleBase):
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
        self.page.on("dialog", lambda dialog: dialog.accept())

    def sign_in(self, account: Account | None = None):
        """Signs in from the Console's own button. Returns the new User's ID."""
        if account is not None:
            self.fake.account = account
        self.page.goto(self.base + "/console")
        self.page.get_by_role("heading", name="Sign in to Nest").wait_for()
        self.page.get_by_role("link", name="Sign in with Google").click()
        self.page.wait_for_url(self.base + "/console")
        self.page.get_by_role("heading", name="Your Projects").wait_for()
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

    def test_owner_manages_members_and_invitations(self):
        me = self.sign_in()
        self.page.get_by_text("You are not a member of any Project yet.").wait_for()
        # The Console offers no Project creation to an ordinary user.
        self.assertEqual(self.page.get_by_role("button", name="Create Project").count(), 0)
        self.assertEqual(self.page.get_by_role("link", name="Administration").count(), 0)
        pid, users = self.team()
        self.service.add_member(browser(users["owner"]), pid, me, "admin")
        self.page.reload()
        self.page.get_by_role("link", name="shared").click()
        self.page.get_by_role("heading", name="shared").wait_for()
        self.assertTrue(self.page.locator("h1").evaluate("e => e === document.activeElement"))
        self.page.get_by_role("heading", name="Members (5)").wait_for()

        # Add an existing user; a malformed ID is refused by the browser first.
        newcomer = str(self.user("newcomer"))
        self.page.get_by_label("User ID").fill("not-a-uuid")
        self.page.get_by_role("button", name="Add member").click()
        self.assertTrue(self.page.get_by_label("User ID").evaluate("e => e.matches(':invalid')"))
        self.page.get_by_label("User ID").fill(newcomer)
        self.page.get_by_role("button", name="Add member").click()
        self.page.get_by_text("Member added.").wait_for()
        self.page.get_by_role("heading", name="Members (6)").wait_for()
        # Adding them again reports the server's refusal.
        self.page.get_by_label("User ID").fill(newcomer)
        self.page.get_by_role("button", name="Add member").click()
        self.page.get_by_text("That user is already a member of this Project.").first.wait_for()

        # Change their role, then remove them.
        self.page.get_by_label(f"New role for {newcomer}").select_option("viewer")
        row = self.page.locator("tr", has=self.page.get_by_text(newcomer))
        row.get_by_role("button", name="Change role").click()
        self.page.get_by_text("Role changed.").wait_for()
        self.assertEqual(self.role(pid, newcomer), "viewer")
        # The Owner's row has no controls, for an Admin.
        owner_row = self.page.locator("tr", has=self.page.get_by_text(str(users["owner"])))
        self.assertEqual(owner_row.get_by_role("button").count(), 0)
        self.page.get_by_role("button", name=f"Remove {newcomer}").click()
        self.page.get_by_text("Member removed.").wait_for()
        self.assertIsNone(self.role(pid, newcomer))

        # Invite by email: the link is shown once, then revoke it.
        self.page.get_by_label("Email address").fill("guest@example.org")
        self.page.get_by_label("Email the invitation").uncheck()
        self.page.get_by_role("button", name="Invite", exact=True).click()
        self.page.get_by_text("Invitation created.").wait_for()
        link = self.page.get_by_label("Invitation link.").input_value()
        self.assertTrue(link.startswith(self.base + "/invitations/accept#token="), link)
        self.page.get_by_role("button", name="Revoke invitation to guest@example.org").click()
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
        self.page.get_by_role("heading", name="Recent membership changes").wait_for()
        self.page.screenshot(path=os.path.join(_evidence(), "console-project.png"), full_page=True)
        self.assert_clean()

        self.page.get_by_role("button", name="Sign out").click()
        self.page.get_by_text("You have signed out.").wait_for()
        joiner = self.sign_in(Account(sub="108000000000000000777", email="joiner@example.org"))
        self.page.get_by_role("navigation").get_by_role("link", name="Invitations").click()
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
        self.page.get_by_role("heading", name="shared").wait_for()
        self.page.get_by_text("Only the Project’s Owner and Admins can manage").wait_for()
        for name in ("Add member", "Invite", "Create Invite Link", "Change role", "Remove"):
            self.assertEqual(self.page.get_by_role("button", name=name, exact=True).count(), 0)
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
        # Leaving removes the membership and returns to the (now empty) list.
        self.page.get_by_role("button", name="Leave Project").click()
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
        self.page.get_by_role("heading", name="shared").wait_for()
        self.service.remove_member(browser(users["owner"]), pid, me)
        self.page.get_by_label("User ID").fill(str(self.user("late")))
        self.page.get_by_role("button", name="Add member").click()
        self.page.get_by_role("heading", name="Project not available").wait_for()
        self.assertEqual(self.problems, [])

    def test_revoked_session_returns_to_sign_in(self):
        me = self.sign_in()
        self.identities.revoke_user_sessions(me)
        self.page.get_by_role("navigation").get_by_role("link", name="Invitations").click()
        self.page.get_by_role("heading", name="Sign in to Nest").wait_for()
        self.page.get_by_text("Your session has ended. Sign in again.").wait_for()
        self.assertEqual(self.page.get_by_role("button", name="Sign out").count(), 0)

    def test_expired_access_token_is_refreshed(self):
        self.sign_in()
        # Expire the access token only: the Console refreshes it and carries on.
        self.conn.execute(
            "UPDATE session_access_tokens SET created_at = now() - interval '10 min', expires_at = now() - interval '1 s'"
        )
        self.page.get_by_role("navigation").get_by_role("link", name="Invitations").click()
        self.page.get_by_role("heading", name="Your invitations").wait_for()
        self.page.get_by_text("No pending invitations").wait_for()
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM user_sessions WHERE revoked_at IS NULL AND replaced_by_session_id IS NULL"
            ).fetchone(),
            (1,),
        )
        self.assert_clean()

    def role(self, pid, user_id) -> str | None:
        row = self.conn.execute(
            "SELECT role FROM project_memberships WHERE project_id = %s AND user_id = %s",
            (pid, user_id),
        ).fetchone()
        return row[0] if row else None


def _evidence() -> str:
    """Where browser screenshots go: `NEST_TEST_EVIDENCE_DIR`, or a temporary directory."""
    path = os.environ.get("NEST_TEST_EVIDENCE_DIR") or os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "nest-console-evidence"
    )
    os.makedirs(path, exist_ok=True)
    return path
