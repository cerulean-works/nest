"""Screenshots of every Console page with sanitized fixture data (G04 review evidence).

Not collected by the test runs. From `tests/`, with `NEST_TEST_DATABASE_URL` set:

    SHOTS_DIR=/tmp/after SHOTS_ROUTES=new PYTHONPATH=../src:. python -m unittest console_screenshots

`SHOTS_ROUTES=old` drives the Console as it was before G04.
"""

import json
import os
import urllib.request

import test_console
from test_authz import browser
from test_console import OPS, _BrowserBase

Base = getattr(test_console, "_EligibilityBrowserBase", _BrowserBase)

from nest.db import PlatformStore

OUT = os.environ.get("SHOTS_DIR", "")
NEW = os.environ.get("SHOTS_ROUTES") == "new"
SIZES = {"desktop": (1280, 860), "mobile": (390, 844)}


class Shots(Base):
    def shot(self, name):
        for label, (w, h) in SIZES.items():
            self.page.set_viewport_size({"width": w, "height": h})
            self.page.wait_for_timeout(250)
            self.page.screenshot(
                path=os.path.join(OUT, f"{name}-{label}.jpg"),
                full_page=True,
                type="jpeg",
                quality=72,
            )
        self.page.set_viewport_size({"width": 1280, "height": 860})

    def goto(self, hash_, wait):
        self.page.goto(f"{self.base}/console{hash_}")
        self.page.reload()
        self.page.wait_for_load_state("networkidle")
        try:
            self.page.get_by_role("heading", name=wait).first.wait_for()
        except Exception:
            print(self.page.inner_text("body"))
            raise

    def test_shots(self):
        if not OUT:
            self.skipTest("set SHOTS_DIR to take the screenshots")
        os.makedirs(OUT, exist_ok=True)
        self.page.goto(self.base + "/console")
        self.page.get_by_role("link", name="Sign in with Google").wait_for()
        self.shot("01-sign-in")
        me = self.sign_in()
        if NEW:
            self.shot("10-state-empty")
            self.page.goto(f"{self.base}/console#/admin")
            self.page.reload()
            self.page.get_by_role("heading", name="Administration not available").wait_for()
            self.shot("11-state-denied")
            import uuid as _uuid

            self.page.goto(f"{self.base}/console#/projects/{_uuid.uuid4()}")
            self.page.reload()
            self.page.get_by_role("heading", name="Project not available").wait_for()
            self.shot("12-state-not-available")
            self.page.route("**/api/projects", lambda r: r.abort())
            self.page.goto(f"{self.base}/console#/projects")
            self.page.reload()
            self.page.get_by_role("heading", name="Something went wrong").wait_for()
            self.shot("13-state-error")
            self.page.unroute("**/api/projects")
            import time as _t

            self.page.route("**/api/projects/*/members", lambda r: (_t.sleep(4), r.continue_()))
        if not NEW:
            platforms = PlatformStore(self.db)
            nano = platforms.create_platform("nano", "Nano", **OPS)
            platforms.add_login_node(nano.id, "login.nano", transport_modes=["A"], **OPS)
        pid, users = self.team()
        self.service.add_member(browser(users["owner"]), pid, me, "admin")
        from test_authz import owned_project

        other = owned_project(self.service, users["owner"], "Protein folding")
        self.service.add_member(browser(users["owner"]), other.id, me, "member")
        twin = owned_project(self.service, users["owner"], "shared")
        self.service.add_member(browser(users["owner"]), twin.id, me, "viewer")
        self.identities.grant_platform_role(me, **OPS)
        if NEW:
            self.make_ready(pid)
        self.page.reload()
        landing = "#/overview" if NEW else "#/projects"
        self.goto(landing, "Overview" if NEW else "Your Projects")
        self.shot("02-overview")
        if NEW:
            self.goto("#/connect", "Connect to a Project")
            self.shot("03-connect")
            self.goto(f"#/projects/{pid}/invitations", "Email invitations")
            self.shot("04b-project-invitations")
            self.goto("#/admin/new", "Create a Project")
            self.shot("06b-admin-create")
        if NEW:
            self.page.goto(f"{self.base}/console#/projects/{pid}")
            self.page.reload()
            self.page.locator("[aria-busy=true]").wait_for()
            self.page.screenshot(
                path=os.path.join(OUT, "14-state-loading-desktop.jpg"), type="jpeg", quality=72
            )
            self.page.unroute("**/api/projects/*/members")
        self.goto(f"#/projects/{pid}", "Members (5)")
        self.shot("04-project-members")
        self.goto("#/invitations", "Your invitations")
        self.shot("05-invitations")
        self.goto("#/admin", "User directory" if not NEW else "Projects")
        self.shot("06-admin")
        self.goto(f"#/admin/projects/{pid}", "Provider approvals")
        self.shot("07-admin-access")
        # CLI approval
        req = urllib.request.Request(
            self.base + "/auth/cli/start",
            data=json.dumps({"client_name": "nest CLI"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as r:
            started = json.load(r)
        self.page.goto(started["verification_uri_complete"])
        self.page.get_by_role("button", name="Approve").wait_for()
        self.shot("08-cli-approve")
        created = self.invitations_link(pid, users)
        self.page.goto(created)
        self.page.get_by_role("button", name="Review the invitation").click()
        self.page.get_by_role("button", name="Accept").wait_for()
        self.shot("09-invitation-review")

    def invitations_link(self, pid, users):
        from nest.authz import InvitationService

        inv = InvitationService(self.db, self.service, None, link_base_url=self.base)
        return inv.create_link(browser(users["owner"]), pid, "member", max_uses=3).link
