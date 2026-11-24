"""Collaborative Projects and centralized, deny-by-default authorization (B06).

`PolicyMatrixTests` check the pure permission matrix without a database. The other
tests run against a real PostgreSQL (see `postgres_fixture`): the service tests call
`nest.authz.ProjectService` and `Authorizer` directly, and the HTTP tests drive the
Projects API on the Controller's auth listener over real HTTP.
"""

import itertools
import json
import secrets
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from admin_fixture import owned_project, platform_admin
from fake_oidc import CLIENT_ID, CLIENT_SECRET, FakeGoogle
from postgres_fixture import PostgresTestCase, connect
from test_google_auth import free_port

from nest.auth import INSECURE_SESSION_COOKIE, GoogleSignIn, start_auth_service
from nest.authz import (
    CLI_ACTION_SCOPES,
    AccessDeniedError,
    Action,
    Authorizer,
    Principal,
    ProjectService,
    Target,
    decide,
    may_create_project,
)
from nest.config import ConfigError, load_controller_config, load_database_config
from nest.db import (
    Database,
    IdentityStore,
    MemberNotFoundError,
    OwnerRuleError,
    ProjectStore,
    SessionStore,
    hash_token,
    upgrade,
)

ROLES = ("owner", "admin", "member", "viewer")
ALICE, BOB = uuid.uuid4(), uuid.uuid4()


def browser(user_id: uuid.UUID) -> Principal:
    return Principal(user_id, "browser")


def cli(user_id: uuid.UUID, *scopes: str) -> Principal:
    return Principal(user_id, "cli", frozenset(scopes))


class PolicyMatrixTests(unittest.TestCase):
    """The fixed Owner/Admin/Member/Viewer matrix of `authz.decide`."""

    def allowed(self, role, action, target=None, principal=None) -> bool:
        return decide(principal or browser(ALICE), role, action, target or Target()).allowed

    def test_every_role_may_read(self):
        for role, action in itertools.product(
            ROLES,
            (
                Action.PROJECT_READ,
                Action.MEMBER_LIST,
                Action.RUN_READ,
                Action.ARTIFACT_READ,
                Action.PLATFORM_READ,
            ),
        ):
            with self.subTest(role=role, action=action):
                self.assertTrue(self.allowed(role, action))

    def test_matrix(self):
        member = Target(member_user_id=BOB, member_role="member", new_role="viewer")
        own_run = Target(run_submitted_by=ALICE)
        expected = {
            # action, target: allowed roles
            (Action.PROJECT_UPDATE, Target()): {"owner", "admin"},
            (Action.PROJECT_AUDIT_READ, Target()): {"owner", "admin"},
            (Action.PROJECT_DELETE, Target()): {"owner"},
            (Action.PROJECT_TRANSFER, Target()): {"owner"},
            (Action.MEMBER_INVITE, Target(new_role="admin")): {"owner", "admin"},
            (Action.MEMBER_INVITE, Target(new_role="member")): {"owner", "admin"},
            (Action.MEMBER_INVITE, Target(new_role="viewer")): {"owner", "admin"},
            (Action.MEMBER_ROLE_CHANGE, member): {"owner", "admin"},
            (Action.MEMBER_REMOVE, member): {"owner", "admin"},
            (Action.RUN_SUBMIT, Target()): {"owner", "admin", "member"},
            (Action.RUN_CANCEL, own_run): {"owner", "admin", "member"},
            (Action.ARTIFACT_READ_SENSITIVE, own_run): {"owner", "admin", "member"},
            (Action.ARTIFACT_READ_SENSITIVE, Target(run_submitted_by=BOB)): {"owner", "admin"},
        }
        for (action, target), roles in expected.items():
            for role in ROLES:
                with self.subTest(action=action, target=target, role=role):
                    self.assertEqual(self.allowed(role, action, target), role in roles)

    def test_nobody_grants_owner_outside_a_transfer(self):
        for role in ROLES:
            with self.subTest(role=role):
                invite = decide(
                    browser(ALICE), role, Action.MEMBER_INVITE, Target(new_role="owner")
                )
                self.assertFalse(invite)
                change = decide(
                    browser(ALICE),
                    role,
                    Action.MEMBER_ROLE_CHANGE,
                    Target(member_user_id=ALICE, member_role=role, new_role="owner"),
                )
                self.assertFalse(change)
        decision = decide(browser(ALICE), "admin", Action.MEMBER_INVITE, Target(new_role="owner"))
        self.assertEqual(decision.reason, "owner_by_transfer_only")

    def test_owner_membership_is_protected(self):
        owner = Target(member_user_id=BOB, member_role="owner", new_role="viewer")
        for role in ROLES:
            for action in (Action.MEMBER_ROLE_CHANGE, Action.MEMBER_REMOVE):
                with self.subTest(role=role, action=action):
                    decision = decide(browser(ALICE), role, action, owner)
                    protected = role in ("owner", "admin") or action == Action.MEMBER_REMOVE
                    self.assertEqual(decision.reason, "owner_protected" if protected else "role")
        # The Owner cannot leave either: they transfer first.
        self_owner = Target(member_user_id=ALICE, member_role="owner")
        self.assertFalse(decide(browser(ALICE), "owner", Action.MEMBER_REMOVE, self_owner))

    def test_any_non_owner_may_leave(self):
        for role in ("admin", "member", "viewer"):
            with self.subTest(role=role):
                leave = Target(member_user_id=ALICE, member_role=role)
                self.assertTrue(self.allowed(role, Action.MEMBER_REMOVE, leave))

    def test_admin_cannot_elevate_self_to_owner(self):
        self_admin = Target(member_user_id=ALICE, member_role="admin", new_role="owner")
        self.assertFalse(self.allowed("admin", Action.MEMBER_ROLE_CHANGE, self_admin))
        self.assertFalse(self.allowed("admin", Action.PROJECT_TRANSFER))

    def test_cancel_only_own_runs(self):
        for role in ROLES:
            with self.subTest(role=role):
                decision = decide(
                    browser(ALICE), role, Action.RUN_CANCEL, Target(run_submitted_by=BOB)
                )
                self.assertFalse(decision)
        self.assertFalse(self.allowed("member", Action.RUN_CANCEL, Target()))

    def test_viewer_is_read_only(self):
        reads = {
            Action.PROJECT_READ,
            Action.MEMBER_LIST,
            Action.RUN_READ,
            Action.ARTIFACT_READ,
            Action.PLATFORM_READ,
        }
        leave = Target(member_user_id=ALICE, member_role="viewer")
        for action in Action:
            target = (
                leave
                if action == Action.MEMBER_REMOVE
                else Target(new_role="viewer", run_submitted_by=ALICE)
            )
            with self.subTest(action=action):
                allowed = self.allowed("viewer", action, target)
                self.assertEqual(allowed, action in reads or action == Action.MEMBER_REMOVE)

    def test_denied_by_default(self):
        for action in Action:
            with self.subTest(action=action):
                decision = decide(browser(ALICE), None, action, Target(run_submitted_by=ALICE))
                self.assertEqual(decision.reason, "not_a_member")
        self.assertEqual(decide(browser(ALICE), "owner", "project.nuke").reason, "unknown_action")
        self.assertEqual(
            decide(browser(ALICE), "superuser", Action.PROJECT_READ).reason, "not_a_member"
        )
        odd_session = Principal(ALICE, "service")
        self.assertEqual(decide(odd_session, "owner", Action.PROJECT_READ).reason, "browser_only")
        self.assertFalse(may_create_project(odd_session))

    def test_cli_scopes(self):
        read_only = cli(ALICE, "projects:read")
        jobs = cli(ALICE, "jobs:run")
        self.assertTrue(decide(read_only, "owner", Action.MEMBER_LIST))
        self.assertEqual(decide(read_only, "owner", Action.RUN_SUBMIT).reason, "scope")
        self.assertTrue(decide(jobs, "member", Action.RUN_SUBMIT))
        self.assertEqual(decide(jobs, "member", Action.MEMBER_LIST).reason, "scope")
        everything = cli(ALICE, "projects:read", "jobs:run")
        for action in Action:
            if action not in CLI_ACTION_SCOPES:
                with self.subTest(action=action):
                    decision = decide(everything, "owner", action, Target(new_role="viewer"))
                    self.assertEqual(decision.reason, "browser_only")
        self.assertFalse(may_create_project(everything))
        self.assertTrue(may_create_project(browser(ALICE)))


class AllowProviders:
    """A Provider policy that permits listed Users only, recording each question."""

    def __init__(self, *users: uuid.UUID):
        self.users = set(users)
        self.asked: list[tuple[uuid.UUID, uuid.UUID, Action, str | None]] = []

    def permits(self, principal, project_id, action, provider):
        self.asked.append((principal.user_id, project_id, action, provider))
        return principal.user_id in self.users


class _Base(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = Database(
            load_database_config(
                {"NEST_DATABASE_URL": self.url, "NEST_DATABASE_POOL_MAX_SIZE": "8"}
            )
        )
        self.db.open()
        self.addCleanup(self.db.close)
        self.identities = IdentityStore(self.db)
        self.store = ProjectStore(self.db)
        self.providers = AllowProviders()
        self.authorizer = Authorizer(self.db, self.providers)
        self.service = ProjectService(self.db, self.authorizer)
        self.conn = connect(self.url)
        self.addCleanup(self.conn.close)

    def user(self, name: str = "u") -> uuid.UUID:
        return self.identities.create_user(display_name=name).id

    def team(self):
        """A Project with an Owner, an Admin, a Member and a Viewer, and an outsider."""
        users = {role: self.user(role) for role in (*ROLES, "outsider")}
        project = owned_project(self.service, users["owner"], "shared")
        for role in ("admin", "member", "viewer"):
            self.service.add_member(browser(users["owner"]), project.id, users[role], role)
        return project.id, users

    def project(self, project_id):
        project = self.store.get_project(project_id)
        assert project is not None
        return project

    def denied(self, call, reason: str | None = None) -> AccessDeniedError:
        with self.assertRaises(AccessDeniedError) as caught:
            call()
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        return caught.exception


class ProjectServiceTests(_Base):
    def test_creator_is_owner_and_users_share_many_projects(self):
        alice, bob = self.user("alice"), self.user("bob")
        first = owned_project(self.service, alice, "first")
        second = owned_project(self.service, bob, "second")
        self.assertEqual(first.owner_user_id, alice)
        self.service.add_member(browser(alice), first.id, bob, "member")
        self.service.add_member(browser(bob), second.id, alice, "viewer")
        mine = {p.id: role for p, role in self.service.list_projects(browser(alice))}
        self.assertEqual(mine, {first.id: "owner", second.id: "viewer"})
        theirs = {p.id: role for p, role in self.service.list_projects(browser(bob))}
        self.assertEqual(theirs, {first.id: "member", second.id: "owner"})
        self.assertEqual(len(self.service.members(browser(bob), first.id)), 2)

    def test_project_id_is_independent_of_owner(self):
        pid, users = self.team()
        self.service.transfer_ownership(browser(users["owner"]), pid, users["member"])
        project, role = self.service.get_project(browser(users["member"]), pid)
        self.assertEqual((project.id, project.owner_user_id, role), (pid, users["member"], "owner"))
        # The administrator who created it is recorded as creator, and is no member.
        creator = platform_admin(self.db).user_id
        self.assertEqual(project.created_by_user_id, creator)
        self.assertIsNone(self.store.role_of(pid, creator))
        self.service.rename_project(browser(users["member"]), pid, "renamed")
        self.assertEqual(self.project(pid).id, pid)

    def test_owner_alone_transfers_and_deletes(self):
        pid, users = self.team()
        for role in ("admin", "member", "viewer", "outsider"):
            with self.subTest(role=role):
                who, target = browser(users[role]), users[role]
                expected = "not_a_member" if role == "outsider" else "owner_only"
                self.denied(lambda w=who: self.service.delete_project(w, pid), expected)
                self.denied(
                    lambda w=who, t=target: self.service.transfer_ownership(w, pid, t), expected
                )
        self.service.transfer_ownership(browser(users["owner"]), pid, users["admin"])
        # The former Owner is an Admin now and has lost both powers.
        self.denied(lambda: self.service.delete_project(browser(users["owner"]), pid), "owner_only")
        self.service.delete_project(browser(users["admin"]), pid)
        self.denied(lambda: self.service.get_project(browser(users["admin"]), pid), "not_a_member")

    def test_admin_manages_settings_invitations_and_roles(self):
        pid, users = self.team()
        admin = browser(users["admin"])
        self.service.rename_project(admin, pid, "by admin")
        newcomer = self.user("newcomer")
        self.service.authorize_invitation(admin, pid, "admin")
        self.service.add_member(admin, pid, newcomer, "admin")
        self.service.change_role(admin, pid, newcomer, "viewer")
        self.service.change_role(admin, pid, users["member"], "admin")
        self.service.change_role(admin, pid, users["member"], "member")
        self.service.remove_member(admin, pid, newcomer)
        self.assertIsNone(self.store.role_of(pid, newcomer))

    def test_admin_cannot_take_or_touch_ownership(self):
        pid, users = self.team()
        admin = browser(users["admin"])
        self.denied(
            lambda: self.service.change_role(admin, pid, users["admin"], "owner"),
            "owner_by_transfer_only",
        )
        self.denied(lambda: self.service.authorize_invitation(admin, pid, "owner"))
        self.denied(
            lambda: self.service.change_role(admin, pid, users["owner"], "viewer"),
            "owner_protected",
        )
        self.denied(
            lambda: self.service.remove_member(admin, pid, users["owner"]), "owner_protected"
        )
        self.denied(
            lambda: self.service.remove_member(browser(users["owner"]), pid, users["owner"]),
            "owner_protected",
        )
        self.assertEqual(self.project(pid).owner_user_id, users["owner"])

    def test_member_and_viewer_cannot_manage(self):
        pid, users = self.team()
        newcomer = self.user("newcomer")
        for role in ("member", "viewer"):
            who = browser(users[role])
            with self.subTest(role=role):
                self.denied(lambda w=who: self.service.rename_project(w, pid, "x"), "role")
                self.denied(
                    lambda w=who: self.service.add_member(w, pid, newcomer, "viewer"), "role"
                )
                self.denied(
                    lambda w=who: self.service.authorize_invitation(w, pid, "viewer"), "role"
                )
                self.denied(
                    lambda w=who: self.service.change_role(w, pid, users["viewer"], "member"),
                    "role",
                )
                self.denied(
                    lambda w=who: self.service.remove_member(w, pid, users["admin"]), "role"
                )
                self.denied(lambda w=who: self.service.audit_history(w, pid), "role")
        # Each may still leave.
        self.service.remove_member(browser(users["viewer"]), pid, users["viewer"])
        self.assertIsNone(self.store.role_of(pid, users["viewer"]))

    def test_store_errors_pass_through_after_authorization(self):
        pid, users = self.team()
        owner = browser(users["owner"])
        missing = self.denied(
            lambda: self.service.change_role(owner, pid, users["outsider"], "viewer"),
            "member_not_found",
        )
        self.assertTrue(missing.visible)
        with self.assertRaises(MemberNotFoundError):
            self.store.set_role(pid, users["outsider"], "viewer")
        with self.assertRaises(OwnerRuleError):
            self.service.transfer_ownership(owner, pid, users["owner"])

    def test_revocation_applies_to_the_next_request(self):
        pid, users = self.team()
        member = browser(users["member"])
        self.assertTrue(self.authorizer.check(member, Action.RUN_READ, pid))
        self.service.change_role(browser(users["owner"]), pid, users["member"], "viewer")
        self.assertEqual(self.authorizer.check(member, Action.RUN_SUBMIT, pid).reason, "role")
        self.service.remove_member(browser(users["admin"]), pid, users["member"])
        self.assertEqual(self.authorizer.check(member, Action.RUN_READ, pid).reason, "not_a_member")
        self.denied(lambda: self.service.members(member, pid), "not_a_member")
        self.assertNotIn(pid, [p.id for p, _ in self.service.list_projects(member)])

    def test_disabled_user_loses_access(self):
        pid, users = self.team()
        self.identities.set_user_status(users["admin"], "disabled")
        admin = browser(users["admin"])
        self.denied(lambda: self.service.rename_project(admin, pid, "x"), "not_a_member")
        self.assertEqual(self.service.list_projects(admin), [])
        self.denied(lambda: self.service.create_project(admin, "new"))

    def test_cross_project_and_cross_user_isolation(self):
        _, users = self.team()
        other = owned_project(self.service, users["outsider"], "elsewhere")
        owner = browser(users["owner"])
        for call in (
            lambda: self.service.get_project(owner, other.id),
            lambda: self.service.members(owner, other.id),
            lambda: self.service.rename_project(owner, other.id, "mine now"),
            lambda: self.service.add_member(owner, other.id, users["admin"], "admin"),
            lambda: self.service.remove_member(owner, other.id, users["outsider"]),
            lambda: self.service.delete_project(owner, other.id),
            lambda: self.service.audit_history(owner, other.id),
        ):
            error = self.denied(call, "not_a_member")
            self.assertFalse(error.visible)
        # A missing Project is indistinguishable from someone else's.
        self.denied(lambda: self.service.get_project(owner, uuid.uuid4()), "not_a_member")
        self.assertEqual(self.project(other.id).name, "elsewhere")

    def test_platform_admin_gets_no_project_or_provider_authority(self):
        pid, _ = self.team()
        root = self.user("root")
        self.identities.grant_platform_role(root, "platform_admin")
        operator = browser(root)
        for action in Action:
            with self.subTest(action=action):
                decision = self.authorizer.check(
                    operator, action, pid, Target(run_submitted_by=root, new_role="viewer")
                )
                self.assertEqual(decision.reason, "not_a_member")
        self.denied(lambda: self.service.rename_project(operator, pid, "x"), "not_a_member")
        # Even as a Project Owner, a Platform Admin needs the Provider's permission.
        mine = self.service.create_project(operator, "root's").id
        self.assertEqual(
            self.authorizer.check(operator, Action.RUN_SUBMIT, mine).reason, "provider_denied"
        )

    def test_runs_need_provider_authority(self):
        pid, users = self.team()
        member = browser(users["member"])
        # Deny by default: no Provider permits anything until its policy says so.
        default = Authorizer(self.db)
        self.assertEqual(default.check(member, Action.RUN_SUBMIT, pid).reason, "provider_denied")
        self.assertEqual(
            self.authorizer.check(member, Action.RUN_SUBMIT, pid, Target(provider="nano4")).reason,
            "provider_denied",
        )
        self.providers.users.add(users["member"])
        self.assertTrue(
            self.authorizer.check(member, Action.RUN_SUBMIT, pid, Target(provider="nano4"))
        )
        self.assertEqual(
            self.providers.asked[-1], (users["member"], pid, Action.RUN_SUBMIT, "nano4")
        )
        own = Target(run_submitted_by=users["member"], provider="nano4")
        self.assertTrue(self.authorizer.check(member, Action.RUN_CANCEL, pid, own))
        theirs = Target(run_submitted_by=users["admin"], provider="nano4")
        self.assertEqual(
            self.authorizer.check(member, Action.RUN_CANCEL, pid, theirs).reason, "not_own_run"
        )
        # The Provider is never asked about an action the role already refuses.
        asked = len(self.providers.asked)
        self.providers.users.add(users["viewer"])
        self.assertEqual(
            self.authorizer.check(browser(users["viewer"]), Action.RUN_SUBMIT, pid).reason, "role"
        )
        self.assertEqual(len(self.providers.asked), asked)
        with self.assertRaises(AccessDeniedError):
            self.authorizer.require(browser(users["outsider"]), Action.RUN_READ, pid)

    def test_sensitive_artifacts(self):
        pid, users = self.team()
        mine = Target(run_submitted_by=users["member"])
        theirs = Target(run_submitted_by=users["owner"])
        check = self.authorizer.check
        self.assertTrue(check(browser(users["member"]), Action.ARTIFACT_READ_SENSITIVE, pid, mine))
        self.assertFalse(
            check(browser(users["member"]), Action.ARTIFACT_READ_SENSITIVE, pid, theirs)
        )
        self.assertFalse(
            check(browser(users["viewer"]), Action.ARTIFACT_READ_SENSITIVE, pid, theirs)
        )
        self.assertTrue(check(browser(users["viewer"]), Action.ARTIFACT_READ, pid, theirs))
        self.assertTrue(check(browser(users["admin"]), Action.ARTIFACT_READ_SENSITIVE, pid, theirs))

    def test_cli_sessions_are_scoped(self):
        pid, users = self.team()
        reader = cli(users["owner"], "projects:read")
        self.assertEqual(len(self.service.members(reader, pid)), 4)
        self.denied(lambda: self.service.rename_project(reader, pid, "x"), "browser_only")
        self.denied(lambda: self.service.create_project(reader, "x"), "browser_only")
        self.denied(lambda: self.service.members(cli(users["owner"], "jobs:run"), pid), "scope")
        self.denied(lambda: self.service.list_projects(cli(users["owner"])), "scope")

    def test_organization_needs_membership(self):
        alice, bob = self.user("alice"), self.user("bob")
        org = self.identities.create_organization("lab", created_by_user_id=alice)
        project = owned_project(self.service, alice, "lab project", organization_id=org)
        self.assertEqual(project.organization_id, org)
        self.denied(lambda: owned_project(self.service, bob, "x", organization_id=org))
        # An Organization role grants no Project role.
        self.denied(lambda: self.service.get_project(browser(alice), uuid.uuid4()))

    def test_role_check_and_change_are_atomic(self):
        """An Admin demoted while their change waits on the Project lock is refused."""
        pid, users = self.team()
        locker = connect(self.url)
        locker.autocommit = False
        self.addCleanup(locker.close)
        locker.execute("SELECT 1 FROM projects WHERE id = %s FOR NO KEY UPDATE", (pid,))
        locker.execute(
            "UPDATE project_memberships SET role = 'viewer' WHERE project_id = %s AND user_id = %s",
            (pid, users["admin"]),
        )
        outcome: list[object] = []

        def rename():
            try:
                outcome.append(self.service.rename_project(browser(users["admin"]), pid, "late"))
            except AccessDeniedError as exc:
                outcome.append(exc)

        thread = threading.Thread(target=rename)
        thread.start()
        time.sleep(0.5)
        self.assertTrue(thread.is_alive(), "the change must wait for the Project lock")
        locker.commit()
        thread.join(10)
        self.assertIsInstance(outcome[0], AccessDeniedError)
        self.assertEqual(self.project(pid).name, "shared")


class ProjectAuditTests(_Base):
    def test_every_change_is_audited_with_its_actor(self):
        pid, users = self.team()
        owner, admin = browser(users["owner"]), browser(users["admin"])
        self.service.rename_project(admin, pid, "renamed")
        self.service.change_role(admin, pid, users["viewer"], "member")
        self.service.remove_member(admin, pid, users["viewer"])
        self.service.transfer_ownership(owner, pid, users["admin"])
        self.service.delete_project(admin, pid)
        events = [
            (e.event, e.target_user_id, e.old_role, e.new_role, e.actor_user_id)
            for e in reversed(self.store.audit_history(pid))
        ]
        o, a, m, v = (users[r] for r in ROLES)
        creator = platform_admin(self.db).user_id
        self.assertEqual(
            events,
            [
                ("project_created", None, None, None, creator),
                ("member_added", o, None, "owner", creator),
                ("member_added", a, None, "admin", o),
                ("member_added", m, None, "member", o),
                ("member_added", v, None, "viewer", o),
                ("project_renamed", None, None, None, a),
                ("member_role_changed", v, "viewer", "member", a),
                ("member_removed", v, "member", None, a),
                ("member_role_changed", o, "owner", "admin", o),
                ("member_role_changed", a, "admin", "owner", o),
                ("project_deleted", None, None, None, a),
            ],
        )
        # History outlives the Project and is still readable to operators.
        self.assertEqual(len(self.store.audit_history(pid, limit=2)), 2)
        newest = self.store.audit_history(pid)[0].id
        self.assertEqual(len(self.store.audit_history(pid, before_id=newest)), 10)

    def test_audit_history_is_immutable(self):
        pid, _ = self.team()
        for statement in (
            "UPDATE project_audit_events SET actor_user_id = NULL",
            "DELETE FROM project_audit_events",
            "TRUNCATE project_audit_events",
        ):
            with (
                self.subTest(statement=statement),
                self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            ):
                self.conn.execute(statement)
        self.assertEqual(len(self.store.audit_history(pid)), 5)

    def test_direct_database_changes_are_audited_too(self):
        pid, users = self.team()
        with self.conn.transaction():
            self.conn.execute(
                "SELECT set_config('nest.audit_operator', 'ops', true), "
                "set_config('nest.audit_reason', 'ticket 7', true)"
            )
            self.conn.execute(
                "DELETE FROM project_memberships WHERE project_id = %s AND user_id = %s",
                (pid, users["viewer"]),
            )
        latest = self.store.audit_history(pid, limit=1)[0]
        self.assertEqual(
            (latest.event, latest.target_user_id, latest.actor_operator, latest.reason),
            ("member_removed", users["viewer"], "ops", "ticket 7"),
        )

    def test_projects_cannot_be_hard_deleted(self):
        """A direct DELETE or TRUNCATE would bypass soft deletion and its audit entry."""
        pid, users = self.team()
        before = self.store.audit_history(pid)
        for statement, args in (
            ("DELETE FROM projects WHERE id = %s", (pid,)),
            ("TRUNCATE projects CASCADE", ()),
        ):
            with (
                self.subTest(statement=statement),
                self.assertRaises(psycopg.errors.IntegrityConstraintViolation) as caught,
            ):
                self.conn.execute(statement, args)
            self.assertEqual(caught.exception.diag.constraint_name, "projects_soft_delete_only")
        # Nothing changed: the Project, its members and its history are intact.
        self.assertEqual(self.project(pid).id, pid)
        self.assertEqual(len(self.store.members(pid)), 4)
        self.assertEqual(self.store.audit_history(pid), before)
        # Even a deleted Project's row stays, with its project_deleted entry.
        self.service.delete_project(browser(users["owner"]), pid)
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute("DELETE FROM projects WHERE id = %s", (pid,))
        self.assertEqual(self.store.audit_history(pid, limit=1)[0].event, "project_deleted")
        self.assertIsNotNone(self.store.get_project(pid, include_deleted=True))

    def test_a_deleted_project_cannot_be_restored(self):
        pid, users = self.team()
        self.service.delete_project(browser(users["owner"]), pid)
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE projects SET deleted_at = NULL, deleted_by_user_id = NULL WHERE id = %s",
                (pid,),
            )


class ProjectsAPITests(_Base):
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

    def token(self, user_id: uuid.UUID, kind: str = "browser", scope: str | None = None) -> str:
        expires = datetime.now(UTC) + timedelta(hours=1)
        session = self.identities.create_session(
            user_id,
            kind=kind,
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=expires,
            scope=scope,
        )
        access = secrets.token_urlsafe()
        self.sessions.issue_access_token(session.id, token_hash=hash_token(access), ttl_seconds=600)
        return access

    def call(self, method, path, *, cookie=None, bearer=None, body=None, origin=True):
        headers = {}
        if cookie:
            headers["Cookie"] = f"{INSECURE_SESSION_COOKIE}={cookie}"
            if origin and method == "POST":
                headers.update({"Origin": self.base, "Sec-Fetch-Site": "same-origin"})
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
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

    def admin(self, name: str) -> uuid.UUID:
        user = self.user(name)
        self.identities.grant_platform_role(user, operator="tests", reason="create Projects")
        return user

    def test_browser_session_manages_a_project(self):
        alice, bob = self.admin("alice"), self.user("bob")
        a, b = self.token(alice), self.token(bob)
        status, created = self.call("POST", "/api/projects", cookie=a, body={"name": "shared"})
        self.assertEqual(
            (status, created["role"], created["owner_user_id"]), (201, "owner", str(alice))
        )
        pid = created["id"]
        status, _ = self.call(
            "POST",
            f"/api/projects/{pid}/members",
            cookie=a,
            body={"user_id": str(bob), "role": "viewer"},
        )
        self.assertEqual(status, 201)
        status, listed = self.call("GET", "/api/projects", cookie=b)
        self.assertEqual([(p["id"], p["role"]) for p in listed["projects"]], [(pid, "viewer")])
        status, body = self.call(
            "POST", f"/api/projects/{pid}/rename", cookie=b, body={"name": "x"}
        )
        self.assertEqual((status, body), (403, {"error": "forbidden", "reason": "role"}))
        status, members = self.call("GET", f"/api/projects/{pid}/members", cookie=b)
        self.assertEqual([m["role"] for m in members["members"]], ["owner", "viewer"])
        status, body = self.call(
            "POST", f"/api/projects/{pid}/members/{bob}/role", cookie=a, body={"role": "owner"}
        )
        self.assertEqual((status, body["reason"]), (403, "owner_by_transfer_only"))
        status, body = self.call(
            "POST", f"/api/projects/{pid}/transfer", cookie=a, body={"user_id": str(bob)}
        )
        self.assertEqual((status, body["owner_user_id"]), (200, str(bob)))
        status, audit = self.call("GET", f"/api/projects/{pid}/audit?limit=2", cookie=b)
        self.assertEqual([e["event"] for e in audit["events"]], ["member_role_changed"] * 2)
        status, _ = self.call("POST", f"/api/projects/{pid}/delete", cookie=b)
        self.assertEqual(status, 200)
        self.assertEqual(self.call("GET", f"/api/projects/{pid}", cookie=b)[0], 404)

    def test_revoked_membership_is_refused_on_the_next_request(self):
        alice, bob = self.admin("alice"), self.user("bob")
        a, b = self.token(alice), self.token(bob)
        pid = self.call("POST", "/api/projects", cookie=a, body={"name": "p"})[1]["id"]
        self.call(
            "POST",
            f"/api/projects/{pid}/members",
            cookie=a,
            body={"user_id": str(bob), "role": "admin"},
        )
        self.assertEqual(self.call("GET", f"/api/projects/{pid}", cookie=b)[0], 200)
        status, _ = self.call("POST", f"/api/projects/{pid}/members/{bob}/remove", cookie=a)
        self.assertEqual(status, 200)
        # Same access token, no re-sign-in: the next request sees the removal.
        self.assertEqual(
            self.call("GET", f"/api/projects/{pid}", cookie=b), (404, {"error": "not_found"})
        )
        status, _ = self.call("POST", f"/api/projects/{pid}/rename", cookie=b, body={"name": "x"})
        self.assertEqual(status, 404)

    def test_authentication_and_csrf(self):
        alice = self.user("alice")
        a = self.token(alice)
        self.assertEqual(self.call("GET", "/api/projects")[0], 401)
        self.assertEqual(self.call("GET", "/api/projects", bearer="nope")[0], 401)
        # A browser token is not a bearer token, nor the reverse.
        self.assertEqual(self.call("GET", "/api/projects", bearer=a)[0], 401)
        c = self.token(alice, "cli", "projects:read")
        self.assertEqual(self.call("GET", "/api/projects", cookie=c)[0], 401)
        status, body = self.call(
            "POST", "/api/projects", cookie=a, body={"name": "x"}, origin=False
        )
        self.assertEqual((status, body), (403, {"error": "cross_origin"}))
        self.assertEqual(self.store.projects_for_user(alice), [])

    def test_cli_bearer_is_scoped(self):
        alice = self.admin("alice")
        pid = self.call("POST", "/api/projects", cookie=self.token(alice), body={"name": "p"})[1][
            "id"
        ]
        reader = self.token(alice, "cli", "projects:read")
        self.assertEqual(self.call("GET", f"/api/projects/{pid}/members", bearer=reader)[0], 200)
        status, body = self.call("POST", "/api/projects", bearer=reader, body={"name": "x"})
        self.assertEqual((status, body["reason"]), (403, "browser_only"))
        status, body = self.call("POST", f"/api/projects/{pid}/delete", bearer=reader)
        self.assertEqual((status, body["reason"]), (403, "browser_only"))
        runner = self.token(alice, "cli", "jobs:run")
        status, body = self.call("GET", f"/api/projects/{pid}/members", bearer=runner)
        self.assertEqual((status, body["reason"]), (403, "scope"))

    def test_malformed_requests(self):
        alice = self.admin("alice")
        a = self.token(alice)
        pid = self.call("POST", "/api/projects", cookie=a, body={"name": "p"})[1]["id"]
        for method, path, body, expected in [
            ("GET", "/api/projects/not-a-uuid", None, 404),
            ("GET", f"/api/projects/{pid}/nothing", None, 404),
            ("GET", f"/api/projects/{pid}/rename", None, 405),
            ("POST", f"/api/projects/{pid}", {}, 405),
            ("POST", "/api/projects", {"name": 7}, 400),
            ("POST", "/api/projects", {"name": ""}, 400),
            ("POST", f"/api/projects/{pid}/members", {"user_id": "x", "role": "viewer"}, 400),
            (
                "POST",
                f"/api/projects/{pid}/members",
                {"user_id": str(uuid.uuid4()), "role": "viewer"},
                404,
            ),
            ("POST", f"/api/projects/{pid}/members", {"user_id": str(alice), "role": "admin"}, 409),
            ("POST", f"/api/projects/{pid}/members/{uuid.uuid4()}/remove", {}, 404),
            ("GET", f"/api/projects/{pid}/audit?limit=0", None, 400),
        ]:
            with self.subTest(method=method, path=path, body=body):
                self.assertEqual(self.call(method, path, cookie=a, body=body)[0], expected)

    def test_redirect_url_cannot_be_an_api_route(self):
        with self.assertRaises(ConfigError) as caught:
            load_controller_config(
                {
                    "NEST_DATABASE_URL": self.url,
                    "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
                    "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
                    "NEST_GOOGLE_REDIRECT_URL": "https://nest.example.com/api/projects/callback",
                }
            )
        self.assertIn("a Projects API route", str(caught.exception))
