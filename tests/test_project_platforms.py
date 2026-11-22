"""Project-to-Platform eligibility, default Platform selection and Organizations (Patch B06).

`PolicyTests` check the Project and Organization matrices without a database. The
other tests run against a real PostgreSQL (see `postgres_fixture`): the schema
rules for immutable tenant IDs and grant-bound defaults, the shared
`PlatformCatalog` policy (resolution order, unauthorized and cross-Project
selections, revoked roles and grants, concurrency), its use by the D08
`AccountResolver`, same-named Slurm accounts in distinct Projects, the
`OrganizationService`, and the HTTP routes and SDK.
"""

import json
import secrets
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from unittest import mock

import psycopg
from fake_oidc import CLIENT_ID, CLIENT_SECRET, FakeGoogle
from postgres_fixture import connect
from test_google_auth import free_port
from test_platforms import OPS, _Base, browser, cli

from nest.auth import GoogleSignIn, start_auth_service
from nest.auth.web import INSECURE_SESSION_COOKIE
from nest.authz import (
    AccessDeniedError,
    AccountResolutionError,
    AccountResolver,
    Action,
    HPCIdentity,
    OrganizationAction,
    OrganizationRuleError,
    OrganizationService,
    PlatformCatalog,
    PlatformResolutionError,
    ProjectService,
    Target,
    decide,
    decide_organization,
)
from nest.config import load_cli_auth_config, load_controller_config
from nest.db import PlatformNotFoundError, SessionStore, SlurmAccountStore, hash_token
from nest.platforms import PlatformNotAvailableError, project_platforms

ALICE, BOB = uuid.uuid4(), uuid.uuid4()


class PolicyTests(unittest.TestCase):
    def test_only_owners_and_admins_choose_the_default_from_a_browser(self):
        for role in ("owner", "admin", "member", "viewer", None):
            with self.subTest(role=role):
                self.assertEqual(
                    bool(decide(browser(ALICE), role, Action.PLATFORM_DEFAULT_SET)),
                    role in ("owner", "admin"),
                )
                self.assertEqual(
                    bool(decide(browser(ALICE), role, Action.PLATFORM_READ)), role is not None
                )
        refused = decide(
            cli(ALICE, "projects:read", "jobs:run"), "owner", Action.PLATFORM_DEFAULT_SET
        )
        self.assertEqual(refused.reason, "browser_only")
        self.assertTrue(decide(cli(ALICE, "jobs:run"), "viewer", Action.PLATFORM_READ))
        self.assertEqual(decide(cli(ALICE, "other"), "owner", Action.PLATFORM_READ).reason, "scope")

    def test_organization_matrix(self):
        admin_only = (
            OrganizationAction.UPDATE,
            OrganizationAction.DELETE,
            OrganizationAction.MEMBER_ADD,
        )
        for action in admin_only:
            with self.subTest(action=action):
                target = Target(new_role="member")
                self.assertTrue(decide_organization(browser(ALICE), "org_admin", action, target))
                self.assertEqual(
                    decide_organization(browser(ALICE), "member", action, target).reason, "role"
                )
        for action in (OrganizationAction.READ, OrganizationAction.MEMBER_LIST):
            for role in ("org_admin", "member"):
                self.assertTrue(decide_organization(browser(ALICE), role, action))
            self.assertTrue(decide_organization(cli(ALICE, "projects:read"), "member", action))
            self.assertEqual(
                decide_organization(cli(ALICE, "jobs:run"), "member", action).reason, "scope"
            )
            self.assertEqual(
                decide_organization(browser(ALICE), None, action).reason, "not_a_member"
            )
        leave = Target(member_user_id=ALICE, member_role="member")
        other = Target(member_user_id=BOB, member_role="member")
        self.assertTrue(
            decide_organization(browser(ALICE), "member", OrganizationAction.MEMBER_REMOVE, leave)
        )
        self.assertFalse(
            decide_organization(browser(ALICE), "member", OrganizationAction.MEMBER_REMOVE, other)
        )
        # Changes need a browser; roles must be Organization roles, never Project roles.
        self.assertEqual(
            decide_organization(
                cli(ALICE, "projects:read"), "org_admin", OrganizationAction.UPDATE
            ).reason,
            "browser_only",
        )
        for bad in ("owner", "admin", None):
            self.assertEqual(
                decide_organization(
                    browser(ALICE), "org_admin", OrganizationAction.MEMBER_ADD,
                    Target(new_role=bad),
                ).reason,
                "invalid_role",
            )  # fmt: skip
        self.assertEqual(
            decide_organization(browser(ALICE), "org_admin", "organization.nuke").reason,
            "unknown_action",
        )


class _PolicyBase(_Base):
    def setUp(self):
        super().setUp()
        self.nano4, self.f1 = self.nchc()
        self.hpc3 = self.store.create_platform("hpc3", "HPC 3", **OPS)
        self.alice, self.bob, self.carol, self.dave, self.erin = (
            self.user(n) for n in ("alice", "bob", "carol", "dave", "erin")
        )
        self.lab = self.project(self.alice, "lab")
        self.other = self.project(self.erin, "other")
        self.projects.add_member(self.lab, self.bob, "member")
        self.projects.add_member(self.lab, self.carol, "viewer")
        self.projects.add_member(self.lab, self.dave, "admin")
        for slug in ("nano4", "f1"):
            self.store.grant_project(slug, self.lab, **OPS)
        for slug in ("f1", "hpc3"):
            self.store.grant_project(slug, self.other, **OPS)
        self.service = ProjectService(self.db)
        self.catalog = PlatformCatalog(self.db, projects=self.service)

    def origin(self, who, project, reference=None) -> tuple[str, str]:
        chosen = self.catalog.select_platform(who, project, reference)
        return chosen.platform.slug, chosen.origin

    def unresolved(self, who, project) -> str:
        with self.assertRaises(PlatformResolutionError) as caught:
            self.catalog.select_platform(who, project)
        return caught.exception.reason

    def denials(self, event: str) -> list[tuple]:
        return self.conn.execute(
            "SELECT actor_user_id, project_id, reason FROM security_audit_events "
            "WHERE event = %s AND outcome = 'denied' ORDER BY id",
            (event,),
        ).fetchall()


class SchemaTests(_PolicyBase):
    def test_migration_follows_the_execution_registry(self):
        from nest.db import load_migrations

        labels = [m.label for m in load_migrations()]
        self.assertEqual(
            labels[labels.index("0014_execution_registry") + 1], "0015_project_platform_policy"
        )

    def test_tenant_ids_are_immutable(self):
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE projects SET id = gen_random_uuid() WHERE id = %s", (self.lab,)
            )
        org = self.identities.create_organization("Chem", created_by_user_id=self.alice)
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE organizations SET id = gen_random_uuid() WHERE id = %s", (org,)
            )
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE organization_memberships SET user_id = %s WHERE organization_id = %s",
                (self.bob, org),
            )

    def test_a_default_must_be_one_of_the_projects_grants(self):
        # hpc3 is granted to the other Project only: no writer can make it lab's default.
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.conn.execute(
                "INSERT INTO project_default_platforms (project_id, platform_id) VALUES (%s, %s)",
                (self.lab, self.hpc3.id),
            )


class ResolutionTests(_PolicyBase):
    def test_resolution_order_is_deterministic(self):
        alice = browser(self.alice)
        self.assertEqual(self.unresolved(alice, self.lab), "ambiguous")
        self.store.set_default("f1", **OPS)
        self.assertEqual(self.origin(alice, self.lab), ("f1", "server_default"))
        self.catalog.set_project_default(alice, self.lab, "nano4")
        self.assertEqual(self.origin(alice, self.lab), ("nano4", "project_default"))
        self.assertEqual(self.origin(alice, self.lab, "f1"), ("f1", "explicit"))
        self.assertEqual(self.origin(alice, self.lab, str(self.f1.id)), ("f1", "explicit"))
        # Every member, whatever the role, gets the same Project-level answer.
        for who in (self.bob, self.carol, self.dave):
            self.assertEqual(self.origin(browser(who), self.lab), ("nano4", "project_default"))
        # The server default is ignored where it is not eligible.
        self.store.set_default("nano4", **OPS)
        self.assertEqual(self.unresolved(browser(self.erin), self.other), "ambiguous")
        self.store.revoke_project("hpc3", self.other, **OPS)
        self.assertEqual(self.origin(browser(self.erin), self.other), ("f1", "only_eligible"))
        self.store.revoke_project("f1", self.other, **OPS)
        self.assertEqual(self.unresolved(browser(self.erin), self.other), "none_authorized")
        # The account resolver uses the same contract.
        self.assertEqual(self.catalog.resolve_platform(alice, project_id=self.lab).slug, "nano4")

    def test_explicit_unauthorized_selection_is_refused_not_substituted(self):
        alice = browser(self.alice)
        self.catalog.set_project_default(alice, self.lab, "nano4")
        for reference in ("hpc3", str(self.hpc3.id), "missing", str(uuid.uuid4()), " "):
            with self.subTest(reference=reference), self.assertRaises(PlatformNotFoundError):
                self.catalog.select_platform(alice, self.lab, reference)
        refused = self.denials("platform.resolve")
        self.assertEqual(len(refused), 5)
        self.assertEqual(
            {r[:3] for r in refused}, {(self.alice, self.lab, "platform_not_authorized")}
        )
        # A disabled Platform is refused even though its grant remains.
        self.store.set_status("f1", "disabled", **OPS)
        with self.assertRaises(PlatformNotFoundError):
            self.catalog.select_platform(alice, self.lab, "f1")

    def test_cross_project_access_is_refused(self):
        alice = browser(self.alice)
        # Alice is not a member of the other Project: no Platform, no view, no change.
        self.assertEqual(self.unresolved(alice, self.other), "none_authorized")
        for reference in ("f1", "hpc3"):
            with self.assertRaises(PlatformNotFoundError):
                self.catalog.select_platform(alice, self.other, reference)
        with self.assertRaises(AccessDeniedError) as caught:
            self.catalog.project_platforms(alice, self.other)
        self.assertFalse(caught.exception.visible)
        with self.assertRaises(AccessDeniedError) as caught:
            self.catalog.set_project_default(alice, self.other, "f1")
        self.assertEqual(caught.exception.reason, "not_a_member")
        # hpc3 is reachable through the other Project only, never through lab.
        with self.assertRaises(PlatformNotFoundError):
            self.catalog.select_platform(browser(self.erin), self.lab, "hpc3")
        self.assertEqual(self.origin(browser(self.erin), self.other, "hpc3"), ("hpc3", "explicit"))

    def test_disabled_default_is_refused_not_replaced(self):
        alice = browser(self.alice)
        self.catalog.set_project_default(alice, self.lab, "f1")
        self.store.set_default("nano4", **OPS)
        self.store.set_status("f1", "disabled", **OPS)
        self.assertEqual(self.unresolved(alice, self.lab), "default_unavailable")
        self.assertEqual(self.denials("platform.resolve")[-1][2], "default_unavailable")
        view = self.catalog.project_platforms(alice, self.lab)
        self.assertEqual(
            ([s.platform.slug for s in view.platforms], view.project_default_platform_id),
            (["nano4"], self.f1.id),
        )
        self.assertEqual((view.selection, view.unresolved_reason), (None, "default_unavailable"))
        # Naming an eligible Platform still works; clearing the default restores the order.
        self.assertEqual(self.origin(alice, self.lab, "nano4"), ("nano4", "explicit"))
        self.catalog.set_project_default(alice, self.lab, None)
        self.assertEqual(self.origin(alice, self.lab), ("nano4", "server_default"))


class DefaultChangeTests(_PolicyBase):
    def test_only_owners_and_admins_may_choose_among_eligible_platforms(self):
        for who, reason in (
            (browser(self.bob), "role"),
            (browser(self.carol), "role"),
            (cli(self.alice, "projects:read", "jobs:run"), "browser_only"),
        ):
            with self.subTest(who=who), self.assertRaises(AccessDeniedError) as caught:
                self.catalog.set_project_default(who, self.lab, "nano4")
            self.assertEqual(caught.exception.reason, reason)
        view = self.catalog.set_project_default(browser(self.dave), self.lab, "nano4")
        self.assertEqual(view.project_default_platform_id, self.nano4.id)
        selection = view.selection
        assert selection is not None
        self.assertEqual((selection.platform.slug, selection.origin), ("nano4", "project_default"))
        # Not eligible: refused as not found, audited, and the default is kept.
        for reference in ("hpc3", "missing"):
            with self.assertRaises(PlatformNotFoundError):
                self.catalog.set_project_default(browser(self.alice), self.lab, reference)
        self.assertEqual(
            [r[2] for r in self.denials("project.platform.default.set")][-2:],
            ["platform_not_authorized"] * 2,
        )
        self.assertEqual(
            self.catalog.project_platforms(
                browser(self.carol), self.lab
            ).project_default_platform_id,
            self.nano4.id,
        )
        self.catalog.set_project_default(browser(self.alice), self.lab, "f1")
        self.catalog.set_project_default(browser(self.alice), self.lab, None)
        events = self.conn.execute(
            "SELECT event, actor_user_id, resource_id FROM security_audit_events "
            "WHERE event LIKE 'project.default_platform.%%' AND project_id = %s ORDER BY id",
            (self.lab,),
        ).fetchall()
        self.assertEqual(
            events,
            [
                ("project.default_platform.set", self.dave, str(self.nano4.id)),
                ("project.default_platform.set", self.alice, str(self.f1.id)),
                ("project.default_platform.cleared", self.alice, str(self.f1.id)),
            ],
        )

    def test_revoked_grants_and_roles_take_effect_at_once(self):
        self.catalog.set_project_default(browser(self.alice), self.lab, "nano4")
        self.store.revoke_project("nano4", self.lab, **OPS)
        # The revocation cleared the default in the same transaction.
        view = self.catalog.project_platforms(browser(self.alice), self.lab)
        self.assertIsNone(view.project_default_platform_id)
        self.assertEqual(self.origin(browser(self.alice), self.lab), ("f1", "only_eligible"))
        with self.assertRaises(PlatformNotFoundError):
            self.catalog.select_platform(browser(self.alice), self.lab, "nano4")
        cleared = self.conn.execute(
            "SELECT actor_operator FROM security_audit_events "
            "WHERE event = 'project.default_platform.cleared'"
        ).fetchall()
        self.assertEqual(cleared, [("ops-alice",)])
        # A removed member loses every Platform of the Project.
        self.service.remove_member(browser(self.alice), self.lab, self.bob)
        self.assertEqual(self.unresolved(browser(self.bob), self.lab), "none_authorized")
        with self.assertRaises(PlatformNotFoundError):
            self.catalog.select_platform(browser(self.bob), self.lab, "f1")
        # A demoted Admin can no longer choose the default.
        self.service.change_role(browser(self.alice), self.lab, self.dave, "member")
        with self.assertRaises(AccessDeniedError):
            self.catalog.set_project_default(browser(self.dave), self.lab, "f1")
        # A disabled User sees nothing.
        self.identities.set_user_status(self.carol, "disabled")
        self.assertEqual(self.unresolved(browser(self.carol), self.lab), "none_authorized")
        # A deleted Project resolves nothing.
        self.service.delete_project(browser(self.alice), self.lab)
        self.assertEqual(self.unresolved(browser(self.alice), self.lab), "none_authorized")

    def test_a_concurrent_revocation_wins_over_choosing_it(self):
        revoker = connect(self.url)
        revoker.autocommit = False
        self.addCleanup(revoker.close)
        revoker.execute("SELECT set_config('nest.audit_operator', 'ops-bob', true)")
        revoker.execute(
            "DELETE FROM hpc_platform_project_grants WHERE platform_id = %s AND project_id = %s",
            (self.nano4.id, self.lab),
        )
        outcome: list[object] = []

        def choose():
            try:
                outcome.append(
                    self.catalog.set_project_default(browser(self.alice), self.lab, "nano4")
                )
            except Exception as exc:  # noqa: BLE001 - reported below
                outcome.append(exc)

        worker = threading.Thread(target=choose)
        worker.start()
        time.sleep(0.5)
        self.assertTrue(worker.is_alive(), "the choice must wait for the revocation")
        revoker.commit()
        worker.join(10)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], PlatformNotFoundError)
        rows = self.conn.execute("SELECT count(*) FROM project_default_platforms").fetchone()
        self.assertEqual(rows, (0,))


class AccountIsolationTests(_PolicyBase):
    def setUp(self):
        super().setUp()
        self.accounts = SlurmAccountStore(self.db)
        self.resolver = AccountResolver(self.db)
        self.store.grant_project("nano4", self.other, **OPS)
        self.accounts.register_account("nano4", "shared", **OPS)
        for pid in (self.lab, self.other):
            self.accounts.link_project(pid, "nano4", "shared", modes=["A"], default=True, **OPS)

    def test_same_slurm_account_does_not_combine_projects(self):
        identity = HPCIdentity.controller(self.nano4.id)
        lab = self.resolver.resolve(browser(self.bob), self.lab, platform="nano4", mode="A",
                                    identity=identity)  # fmt: skip
        other = self.resolver.resolve(browser(self.erin), self.other, platform="nano4", mode="A",
                                      identity=identity)  # fmt: skip
        self.assertEqual(lab.account_id, other.account_id)
        self.assertEqual((lab.project_id, other.project_id), (self.lab, self.other))
        # Sharing the account gives Bob nothing in the other Project, and Erin nothing in lab.
        for who, pid in ((self.bob, self.other), (self.erin, self.lab)):
            with self.subTest(who=who), self.assertRaises(PlatformNotFoundError):
                self.resolver.resolve(browser(who), pid, platform="nano4", mode="A",
                                      identity=identity, account="shared")  # fmt: skip
            with self.assertRaises(AccessDeniedError):
                self.service.members(browser(who), pid)
        self.assertNotIn(self.bob, {m.user_id for m in self.projects.members(self.other)})
        self.assertEqual({m.user_id for m in self.projects.members(self.other)}, {self.erin})

    def test_project_default_platform_flows_into_account_resolution(self):
        self.catalog.set_project_default(browser(self.alice), self.lab, "nano4")
        chosen = self.resolver.resolve(
            browser(self.bob), self.lab, mode="A", identity=HPCIdentity.controller(self.nano4.id)
        )
        self.assertEqual((chosen.platform_slug, chosen.account_name), ("nano4", "shared"))
        # Mode B never falls back to the Controller identity, whatever the default.
        with self.assertRaises(AccountResolutionError) as caught:
            self.resolver.resolve(
                browser(self.bob),
                self.lab,
                mode="B",
                identity=HPCIdentity.controller(self.nano4.id),
            )
        self.assertEqual(caught.exception.reason, "identity_not_allowed")
        # A User identity bound to someone else is refused.
        with self.assertRaises(AccountResolutionError) as caught:
            self.resolver.resolve(
                browser(self.bob), self.lab, mode="B",
                identity=HPCIdentity.user(self.nano4.id, self.alice),
            )  # fmt: skip
        self.assertEqual(caught.exception.reason, "identity_not_allowed")


class PlatformActionTests(_PolicyBase):
    """`PlatformCatalog.authorize_platform_action`, the check behind every
    Platform-bound operation (Patch B07)."""

    def allowed(self, who, project, platform, action=Action.RUN_SUBMIT) -> bool:
        try:
            self.catalog.authorize_platform_action(who, action, project, platform)
        except AccessDeniedError as exc:
            self.assertEqual(exc.reason, "platform_not_authorized")
            self.assertFalse(exc.visible)
            return False
        return True

    def test_only_the_projects_eligible_platforms_are_authorized(self):
        platform = self.catalog.authorize_platform_action(
            cli(self.bob, "jobs:run"), Action.RUN_SUBMIT, self.lab, self.nano4.id
        )
        self.assertEqual(platform.id, self.nano4.id)
        cases = {
            # hpc3 exists, but is granted to another Project only.
            "cross-Project Platform": (self.bob, self.lab, self.hpc3.id, False),
            # f1 is granted to `other`, but bob is not a member of it.
            "cross-user Project": (self.bob, self.other, self.f1.id, False),
            "unknown Platform": (self.bob, self.lab, uuid.uuid4(), False),
            "no Platform served": (self.bob, self.lab, None, False),
            "unknown Project": (self.bob, uuid.uuid4(), self.nano4.id, False),
            "viewer reads": (self.carol, self.lab, self.f1.id, True),
        }
        for name, (user, project, platform_id, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.allowed(browser(user), project, platform_id), expected)
        # A slug is not an immutable ID and is never accepted here.
        self.assertFalse(self.allowed(browser(self.bob), self.lab, "nano4"))
        denied = self.denials("run.submit")
        self.assertEqual(len(denied), 6)
        self.assertTrue(all(r[2] == "platform_not_authorized" for r in denied))

    def test_revocations_take_effect_at_once(self):
        who = browser(self.bob)
        self.assertTrue(self.allowed(who, self.lab, self.nano4.id))
        self.store.revoke_project("nano4", self.lab, **OPS)
        self.assertFalse(self.allowed(who, self.lab, self.nano4.id))
        self.store.set_status("f1", "disabled", **OPS)
        self.assertFalse(self.allowed(who, self.lab, self.f1.id))
        self.store.set_status("f1", "active", **OPS)
        self.assertTrue(self.allowed(who, self.lab, self.f1.id))
        self.service.remove_member(browser(self.alice), self.lab, self.bob)
        self.assertFalse(self.allowed(who, self.lab, self.f1.id))
        self.identities.set_user_status(self.carol, "disabled")
        self.assertFalse(self.allowed(browser(self.carol), self.lab, self.f1.id))
        self.service.delete_project(browser(self.alice), self.lab)
        self.assertFalse(self.allowed(browser(self.alice), self.lab, self.f1.id))

    def test_sessions_need_a_platform_scope(self):
        with self.assertRaises(AccessDeniedError) as caught:
            self.catalog.authorize_platform_action(
                cli(self.bob, "other"), Action.RUN_SUBMIT, self.lab, self.nano4.id
            )
        self.assertEqual(caught.exception.reason, "scope")
        self.assertEqual(self.denials("run.submit"), [(self.bob, None, "scope")])

    def test_denials_are_traceable_and_reveal_no_endpoint(self):
        self.store.add_login_node(
            "hpc3", "ln01.hpc3.internal", transport_modes=["A"], port=2222, **OPS
        )
        self.assertFalse(self.allowed(browser(self.bob), self.lab, self.hpc3.id))
        [(details,)] = self.conn.execute(
            "SELECT details FROM security_audit_events "
            "WHERE event = 'run.submit' AND outcome = 'denied'"
        ).fetchall()
        self.assertEqual(details, {"session_kind": "browser", "platform_id": str(self.hpc3.id)})
        self.assertNotIn("ln01.hpc3.internal", json.dumps(details))


class OrganizationTests(_PolicyBase):
    def setUp(self):
        super().setUp()
        self.orgs = OrganizationService(self.db)

    def test_crud_and_roles_are_enforced(self):
        alice, bob = browser(self.alice), browser(self.bob)
        org = self.orgs.create_organization(alice, "  Chemistry ")
        self.assertEqual((org.name, org.created_by_user_id), ("Chemistry", self.alice))
        self.assertEqual(
            [(o.id, r) for o, r in self.orgs.list_organizations(alice)], [(org.id, "org_admin")]
        )
        with self.assertRaises(AccessDeniedError) as caught:
            self.orgs.get_organization(bob, org.id)
        self.assertFalse(caught.exception.visible)
        self.orgs.add_member(alice, org.id, self.bob, "member")
        with self.assertRaises(OrganizationRuleError):
            self.orgs.add_member(alice, org.id, self.bob, "member")
        self.assertEqual(self.orgs.get_organization(bob, org.id)[1], "member")
        for change in (
            lambda: self.orgs.rename_organization(bob, org.id, "Mine"),
            lambda: self.orgs.add_member(bob, org.id, self.carol, "org_admin"),
            lambda: self.orgs.remove_member(bob, org.id, self.alice),
            lambda: self.orgs.delete_organization(bob, org.id),
            lambda: self.orgs.rename_organization(cli(self.alice, "projects:read"), org.id, "x"),
        ):
            with self.assertRaises(AccessDeniedError):
                change()
        renamed = self.orgs.rename_organization(alice, org.id, "Chem Dept")
        self.assertEqual((renamed.id, renamed.name), (org.id, "Chem Dept"))
        # The last org_admin can neither be demoted nor leave.
        with self.assertRaises(OrganizationRuleError) as caught:
            self.orgs.change_role(alice, org.id, self.alice, "member")
        self.assertEqual(caught.exception.reason, "last_org_admin")
        with self.assertRaises(OrganizationRuleError):
            self.orgs.remove_member(alice, org.id, self.alice)
        self.orgs.change_role(alice, org.id, self.bob, "org_admin")
        self.orgs.change_role(bob, org.id, self.alice, "member")
        # The demoted Alice lost her Organization authority at once.
        with self.assertRaises(AccessDeniedError):
            self.orgs.rename_organization(alice, org.id, "Again")
        self.orgs.remove_member(alice, org.id, self.alice)  # leaving is allowed
        with self.assertRaises(AccessDeniedError):
            self.orgs.members(alice, org.id)
        with self.assertRaises(LookupError):
            self.orgs.add_member(bob, org.id, uuid.uuid4(), "member")
        events = {
            r[0]
            for r in self.conn.execute(
                "SELECT event FROM security_audit_events WHERE resource_id = %s", (str(org.id),)
            ).fetchall()
        }
        self.assertTrue(
            {
                "organization.created",
                "organization.renamed",
                "organization.member.added",
                "organization.member.role_changed",
                "organization.member.removed",
            }
            <= events
        )

    def test_organization_roles_grant_no_project_role_or_platform(self):
        org = self.orgs.create_organization(browser(self.erin), "Erin's group")
        self.orgs.add_member(browser(self.erin), org.id, self.bob, "org_admin")
        filed = self.service.create_project(browser(self.erin), "filed", organization_id=org.id)
        self.store.grant_project("hpc3", filed.id, **OPS)
        # Bob administers the Organization but is not a member of its Project.
        with self.assertRaises(AccessDeniedError):
            self.service.get_project(browser(self.bob), filed.id)
        self.assertEqual(self.unresolved(browser(self.bob), filed.id), "none_authorized")
        # An Organization with Projects filed under it cannot be deleted.
        with self.assertRaises(OrganizationRuleError) as caught:
            self.orgs.delete_organization(browser(self.bob), org.id)
        self.assertEqual(caught.exception.reason, "not_empty")
        empty = self.orgs.create_organization(browser(self.bob), "Empty")
        self.orgs.delete_organization(browser(self.bob), empty.id)
        with self.assertRaises(AccessDeniedError):
            self.orgs.get_organization(browser(self.bob), empty.id)


class HTTPTests(_PolicyBase):
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

    def call(self, method, path, *, cookie=None, bearer=None, body=None):
        headers = {}
        if cookie:
            headers["Cookie"] = f"{INSECURE_SESSION_COOKIE}={cookie}"
            if method == "POST":
                headers.update({"Origin": self.base, "Sec-Fetch-Site": "same-origin"})
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        data = None
        if method == "POST":
            data = json.dumps(body if body is not None else {}).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read() or b"{}")

    def test_project_platforms_over_http(self):
        alice, bob = self.token(self.alice), self.token(self.bob)
        path = f"/api/projects/{self.lab}/platforms"
        status, body = self.call("GET", path, cookie=bob)
        self.assertEqual(status, 200)
        self.assertEqual([p["slug"] for p in body["platforms"]], ["f1", "nano4"])
        self.assertEqual(
            (body["default_platform_id"], body["unresolved_reason"]), (None, "ambiguous")
        )
        status, body = self.call("POST", path + "/default", cookie=alice, body={"platform": "f1"})
        self.assertEqual(status, 200)
        self.assertEqual(
            (body["project_default_platform_id"], body["default_origin"]),
            (str(self.f1.id), "project_default"),
        )
        self.assertEqual([p["is_project_default"] for p in body["platforms"]], [True, False])
        # Members cannot change it, CLI sessions cannot, and ineligible Platforms are not found.
        self.assertEqual(
            self.call("POST", path + "/default", cookie=bob, body={"platform": "nano4"}),
            (403, {"error": "forbidden", "reason": "role"}),
        )
        cli_token = self.token(self.alice, "cli", "projects:read jobs:run")
        self.assertEqual(
            self.call("POST", path + "/default", bearer=cli_token, body={"platform": "nano4"})[0],
            403,
        )
        self.assertEqual(
            self.call("POST", path + "/default", cookie=alice, body={"platform": "hpc3"}),
            (404, {"error": "not_found"}),
        )
        self.assertEqual(self.call("POST", path + "/default", cookie=alice, body={})[0], 400)
        self.assertEqual(
            self.call("POST", path + "/default", cookie=alice, body={"platform": 3})[0], 400
        )
        # Discovery for the Project reports the Project default.
        status, listed = self.call("GET", f"/api/platforms?project_id={self.lab}", bearer=cli_token)
        self.assertEqual((status, listed["default_platform_id"]), (200, str(self.f1.id)))
        # A non-member gets not found, also for a Project that exists.
        erin = self.token(self.erin)
        self.assertEqual(self.call("GET", path, cookie=erin), (404, {"error": "not_found"}))
        self.assertEqual(
            self.call("POST", path + "/default", cookie=erin, body={"platform": "f1"}),
            (404, {"error": "not_found"}),
        )
        status, body = self.call("POST", path + "/default", cookie=alice, body={"platform": None})
        self.assertEqual((status, body["project_default_platform_id"]), (200, None))
        self.assertEqual(self.call("GET", path + "/nodes", cookie=alice)[0], 404)
        self.assertEqual(self.call("POST", path, cookie=alice)[0], 405)

    def test_sdk_reads_the_project_default(self):
        self.catalog.set_project_default(browser(self.alice), self.lab, "nano4")
        config = load_cli_auth_config(
            {"NEST_CLI_CREDENTIALS_FILE": "/nonexistent/credentials.json"}, url=self.base
        )
        session = mock.Mock(access_token=self.token(self.carol, "cli", "projects:read"))
        with mock.patch("nest.platforms.ensure_session", return_value=session):
            view = project_platforms(self.lab, config)
            self.assertEqual(
                (view.default_platform_id, view.default_origin, view.project_default_platform_id),
                (self.nano4.id, "project_default", self.nano4.id),
            )
            with self.assertRaises(PlatformNotAvailableError):
                project_platforms(self.other, config)

    def test_organizations_over_http(self):
        alice, bob = self.token(self.alice), self.token(self.bob)
        status, org = self.call("POST", "/api/organizations", cookie=alice, body={"name": "Chem"})
        self.assertEqual((status, org["role"]), (201, "org_admin"))
        oid = org["id"]
        self.assertEqual(self.call("GET", f"/api/organizations/{oid}", cookie=bob)[0], 404)
        status, member = self.call(
            "POST", f"/api/organizations/{oid}/members", cookie=alice,
            body={"user_id": str(self.bob), "role": "member"},
        )  # fmt: skip
        self.assertEqual((status, member["role"]), (201, "member"))
        self.assertEqual(
            self.call("POST", f"/api/organizations/{oid}/rename", cookie=bob, body={"name": "x"}),
            (403, {"error": "forbidden", "reason": "role"}),
        )
        self.assertEqual(
            self.call(
                "POST", f"/api/organizations/{oid}/members/{self.alice}/remove", cookie=alice
            ),
            (409, {"error": "organization_rule", "reason": "last_org_admin"}),
        )
        status, listed = self.call("GET", "/api/organizations", cookie=bob)
        self.assertEqual([o["id"] for o in listed["organizations"]], [oid])
        status, members = self.call("GET", f"/api/organizations/{oid}/members", cookie=bob)
        self.assertEqual([m["role"] for m in members["members"]], ["org_admin", "member"])
        self.assertEqual(
            self.call("POST", f"/api/organizations/{oid}/delete", cookie=alice),
            (200, {"deleted": True}),
        )
        self.assertEqual(self.call("GET", "/api/organizations/not-a-uuid", cookie=alice)[0], 404)
        self.assertEqual(self.call("GET", "/api/organizations")[0], 401)

    def test_cli_shows_the_default(self):
        import contextlib
        import io

        from nest import cli as nest_cli

        session = mock.Mock(access_token=self.token(self.bob, "cli", "jobs:run"))
        env = {"NEST_CLI_CREDENTIALS_FILE": "/nonexistent/credentials.json"}
        args = ["platforms", "default", "--url", self.base, "--project-id", str(self.lab)]

        def run(*extra: str) -> tuple[int, str, str]:
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict("os.environ", env),
                mock.patch("nest.platforms.ensure_session", return_value=session),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                code = nest_cli.main([*args, *extra])
            return code, out.getvalue(), err.getvalue()

        code, _, err = run()
        self.assertEqual(code, 1)
        self.assertIn("none is the default", err)
        self.catalog.set_project_default(browser(self.alice), self.lab, "f1")
        self.assertEqual(run()[:2], (0, "f1 (project_default)\n"))
        code, out, _ = run("--json")
        self.assertEqual((code, json.loads(out)["default_platform_id"]), (0, str(self.f1.id)))
