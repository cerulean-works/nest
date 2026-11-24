"""CLI sign-in approved in a real browser (Chromium through Playwright).

`test_cli_login` drives the approval form with hand-written request headers. A
browser decides those headers itself: from a `Referrer-Policy: no-referrer` page
it sends a form POST with `Origin: null`, which the approval route refuses. These
tests let Chromium render the pages and submit the forms, so the headers checked
are the ones a browser really sends.

They need the `playwright` package (a dev dependency) and a Chromium, installed
with `uv run playwright install chromium`; without them they are skipped, unless
`NEST_TEST_BROWSER_REQUIRED=1` (set in CI), which turns the skip into a failure.
`NEST_TEST_CHROMIUM` names another Chromium binary to use.
"""

import os
import re
import threading
from typing import NoReturn

from test_cli_login import _Flow

from nest import login as nest_login
from nest.auth import CLI_APPROVE_PATH, CLI_SESSION_PATH, LOGIN_PATH
from nest.login import LoginDeniedError, NestSession

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - depends on the environment
    sync_playwright = None

REQUIRED = os.environ.get("NEST_TEST_BROWSER_REQUIRED", "").strip() == "1"


class BrowserApprovalTests(_Flow):
    def unavailable(self, message: str) -> NoReturn:
        if REQUIRED:
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
        context = chromium.new_context()
        self.addCleanup(context.close)
        self.page = context.new_page()
        # The approval POSTs exactly as the browser sent them.
        self.posts: list[dict[str, str]] = []
        self.page.on("request", self.record)

    def record(self, request):
        if request.method == "POST" and request.url == self.base + CLI_APPROVE_PATH:
            self.posts.append(request.all_headers())

    def sign_in_page(self, code: str):
        """Opens the approval link; the browser signs in with Google on the way."""
        response = self.page.goto(f"{self.base}{CLI_APPROVE_PATH}?code={code}")
        assert response is not None
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["referrer-policy"], "same-origin")
        self.assertIn(code, self.page.content())

    def login_in_background(self) -> tuple[threading.Thread, dict[str, object], str]:
        thread, out, result = self.run_inline(
            lambda out: nest_login.login(self.client_config, open_browser=False, out=out)
        )
        return thread, result, self.wait_for_code(out)

    def click(self, name: str) -> tuple[int, str]:
        with self.page.expect_response(self.base + CLI_APPROVE_PATH) as answer:
            self.page.click(f'button[value="{name}"]')
        self.page.wait_for_load_state()
        return answer.value.status, self.page.content()

    def assert_same_origin_post(self):
        headers = self.posts[-1]
        self.assertEqual(headers.get("origin"), self.base)
        self.assertEqual(headers.get("sec-fetch-site", "same-origin"), "same-origin")

    def test_browser_approval_signs_in_nest_login(self):
        thread, result, code = self.login_in_background()
        self.sign_in_page(code)
        form = self.page.content()
        status, page = self.click("approve")
        self.assertEqual(status, 200, page)
        self.assertIn("CLI sign-in approved", page)
        self.assert_same_origin_post()
        thread.join(30)
        self.assertNotIn("error", result, result)
        session = result["value"]
        assert isinstance(session, NestSession)
        # The redeemed CLI session is valid at the Controller.
        status, _, described = self.get_json(CLI_SESSION_PATH, bearer=session.access_token)
        self.assertEqual(status, 200, described)
        self.assertEqual(described["session_id"], str(session.session_id))

        # Submitting the same form again, from a same-origin page, approves nothing.
        replay = self.base + "/replay"
        self.page.route(replay, lambda route: route.fulfill(content_type="text/html", body=form))
        self.page.goto(replay)
        status, page = self.click("approve")
        self.assertEqual(status, 404, page)
        self.assertIn("Unknown code", page)

    def test_browser_denial_fails_nest_login(self):
        thread, result, code = self.login_in_background()
        self.sign_in_page(code)
        status, page = self.click("deny")
        self.assertEqual(status, 200, page)
        self.assertIn("CLI sign-in denied", page)
        self.assert_same_origin_post()
        thread.join(30)
        self.assertIsInstance(result.get("error"), LoginDeniedError)
        self.assertEqual(self.sql("SELECT count(*) FROM user_sessions WHERE kind = 'cli'"), [(0,)])

    def test_code_typed_into_the_page(self):
        code = self.start()["user_code"]
        response = self.page.goto(self.base + LOGIN_PATH)
        assert response is not None
        self.page.goto(self.base + CLI_APPROVE_PATH)
        self.page.fill('input[name="code"]', code.lower())
        with self.page.expect_navigation():
            self.page.click('button[type="submit"]')
        self.assertIn(code, self.page.content())
        status, page = self.click("approve")
        self.assertEqual(status, 200, page)
        self.assertIn("CLI sign-in approved", page)
        self.assert_same_origin_post()

    def test_form_posted_from_another_site_is_refused(self):
        started = self.start()
        code = started["user_code"]
        self.sign_in_page(code)
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', self.page.content())
        assert csrf is not None
        # The same fields, posted by a page of another origin.
        evil = "http://localhost:9/"
        self.page.route(
            evil,
            lambda route: route.fulfill(
                content_type="text/html",
                body=(
                    f'<form method="post" action="{self.base}{CLI_APPROVE_PATH}">'
                    f'<input name="code" value="{code}">'
                    f'<input name="csrf" value="{csrf.group(1)}">'
                    '<button name="decision" value="approve">Go</button></form>'
                ),
            ),
        )
        self.page.goto(evil)
        status, page = self.click("approve")
        self.assertEqual(status, 403, page)
        self.assertIn("cross_origin", page)
        self.assertEqual(
            self.poll(started["device_code"]), (400, {"error": "authorization_pending"})
        )
