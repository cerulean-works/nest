"""The privileged management API and durable Provider approvals (G03).

- `AdminServiceTests`: `authz.AdminService` and admin-only Project creation against a
  real PostgreSQL: deny by default, platform_admin checked in the changing
  transaction, explicit Owner created atomically, audited actor and reason.
- `ProviderApprovalTests`: `db.ProviderApprovalStore` and
  `authz.DatabaseProviderApprovals`: no restart, no cache across replicas, restart
  persistence, fail closed on outage, and the database guards.
- `ApprovalImportTests`: `nest-db provider import` of explicit IDs only, wildcard
  rejection, idempotency, revocations never regranted, and the upgrade that creates
  no approval.
- `DurableAdmissionTests`: registry admission with `NEST_PROVIDER_APPROVALS=database`:
  revocation between check and attempt, concurrent admission and revocation, and
  Provider, Platform and account denials independent of each other.
- `AdminHTTPTests`: the `/api/admin` routes over real HTTP: authentication, CSRF,
  browser-only sessions, pagination and secret exclusion.
- `ProviderApprovalConfigTests`: `NEST_PROVIDER_APPROVALS` and the Controller wiring.
"""

import io
import json
import os
import secrets
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from unittest import mock

import grpc
import psycopg
from admin_fixture import owned_project
from fake_oidc import CLIENT_ID, CLIENT_SECRET, FakeGoogle
from postgres_fixture import PostgresTestCase, connect
from test_google_auth import free_port
from test_registry_admission import _Base as _AdmissionBase

from nest.admission import AdmissionRefusedError, RegistryAdmission, SubmissionRequest
from nest.audit import AuditLog
from nest.auth import INSECURE_SESSION_COOKIE, GoogleSignIn, start_auth_service
from nest.authz import (
    AccessDeniedError,
    AdminService,
    AllowedProjects,
    Authorizer,
    DatabaseProviderApprovals,
    DirectoryUser,
    ExecutionAccess,
    Principal,
    ProjectService,
)
from nest.authz.policy import Action
from nest.config import ConfigError, load_controller_config, load_database_config
from nest.controller import build_access, build_admission
from nest.db import (
    Database,
    DatabaseError,
    IdentityStore,
    PlatformStore,
    ProjectNotFoundError,
    ProjectStore,
    ProviderApprovalStore,
    SessionStore,
    SlurmAccountStore,
    UserNotFoundError,
    hash_token,
    load_migrations,
    upgrade,
)
from nest.db.cli import main as nest_db

OPS: dict[str, Any] = {"operator": "ops-test", "reason": "privileged API tests"}


def browser(user_id: uuid.UUID) -> Principal:
    return Principal(user_id, "browser")


class _Base(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = self.open_database()
        self.identities = IdentityStore(self.db)
        self.audit = AuditLog(self.db)
        self.projects = ProjectService(self.db, Authorizer(self.db, None, self.audit))
        self.admin_service = AdminService(self.db, self.projects, self.audit)
        self.store = ProjectStore(self.db)
        self.platforms = PlatformStore(self.db)
        self.accounts = SlurmAccountStore(self.db)
        self.approvals = ProviderApprovalStore(self.db)
        self.root = self.admin("root")

    def open_database(self) -> Database:
        db = Database(
            load_database_config(
                {"NEST_DATABASE_URL": self.url, "NEST_DATABASE_POOL_MAX_SIZE": "8"}
            )
        )
        db.open()
        self.addCleanup(db.close)
        return db

    def user(self, name: str = "u") -> uuid.UUID:
        return self.identities.create_user(display_name=name, email=f"{name}@example.com").id

    def admin(self, name: str) -> uuid.UUID:
        user = self.user(name)
        self.identities.grant_platform_role(user, **OPS)
        return user

    def denied(self, call, reason: str) -> AccessDeniedError:
        with self.assertRaises(AccessDeniedError) as caught:
            call()
        self.assertEqual(caught.exception.reason, reason)
        return caught.exception

    def sql(self, query, *params):
        with connect(self.url) as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def platform(self, slug: str = "nano"):
        platform = self.platforms.create_platform(slug, slug.title(), **OPS)
        self.platforms.add_login_node(platform.id, f"login.{slug}", transport_modes=["A"], **OPS)
        return platform


class AdminServiceTests(_Base):
    def test_ordinary_users_cannot_create_projects_or_use_privileged_operations(self):
        alice = self.user("alice")
        pid = owned_project(self.projects, alice, "lab").id
        nano = self.platform()
        before = self.sql("SELECT count(*) FROM projects")
        self.denied(
            lambda: self.projects.create_project(browser(alice), "mine"),
            "platform_admin_required",
        )
        # Not even as the Owner of another Project, nor through the CLI.
        cli = Principal(alice, "cli", frozenset({"projects:read", "jobs:run"}))
        self.denied(lambda: self.projects.create_project(cli, "mine"), "browser_only")
        service = self.admin_service
        for name, call in {
            "users": lambda p: service.list_users(p),
            "user": lambda p: service.get_user(p, alice),
            "projects": lambda p: service.list_projects(p),
            "create": lambda p: service.create_project(p, "x", reason="r"),
            "access": lambda p: service.project_access(p, pid),
            "platforms": lambda p: service.list_platforms(p),
            "accounts": lambda p: service.list_accounts(p, nano.id),
            "grant": lambda p: service.grant_platform(p, pid, nano.id, reason="r"),
            "approve": lambda p: service.approve(p, "nano", pid, reason="r"),
            "approvals": lambda p: service.list_approvals(p),
            "audit": lambda p: service.audit_events(p),
        }.items():
            with self.subTest(name):
                self.denied(lambda c=call: c(browser(alice)), "platform_admin_required")
                # An administrator's CLI session is refused too: browser only.
                self.denied(
                    lambda c=call: c(Principal(self.root, "cli", frozenset({"projects:read"}))),
                    "browser_only",
                )
        self.assertEqual(self.sql("SELECT count(*) FROM projects"), before)
        self.assertEqual(self.platforms.granted_projects(nano.id), [])
        self.assertIsNone(self.approvals.get("nano", pid))
        refusals = self.audit.events(actor_user_id=alice, limit=100)
        self.assertTrue(refusals)
        self.assertTrue(all(e.outcome == "denied" for e in refusals))
        self.assertIn("platform_admin_required", {e.reason for e in refusals})
        self.assertIn("project.create", {e.event for e in refusals})

    def test_admin_creates_a_project_with_an_explicit_owner_atomically(self):
        owner = self.user("owner")
        project = self.admin_service.create_project(
            browser(self.root), "Chemistry", owner_user_id=owner, reason="onboard chemistry"
        )
        self.assertEqual((project.owner_user_id, project.created_by_user_id), (owner, self.root))
        # The administrator gains nothing in the Project: no role, no compute.
        self.assertIsNone(self.store.role_of(project.id, self.root))
        self.assertEqual(self.store.role_of(project.id, owner), "owner")
        self.assertEqual(
            Authorizer(self.db).check(browser(self.root), Action.PROJECT_READ, project.id).reason,
            "not_a_member",
        )
        history = list(reversed(self.store.audit_history(project.id)))
        self.assertEqual(
            [(e.event, e.actor_user_id, e.reason) for e in history],
            [
                ("project_created", self.root, "onboard chemistry"),
                ("member_added", self.root, "onboard chemistry"),
            ],
        )
        # The creator is the Owner when none is named.
        mine = self.admin_service.create_project(browser(self.root), "Ops", reason="ops")
        self.assertEqual(self.store.role_of(mine.id, self.root), "owner")
        # A missing or disabled Owner creates nothing at all.
        disabled = self.user("disabled")
        self.identities.set_user_status(disabled, "disabled")
        count = self.sql("SELECT count(*) FROM projects")
        for missing in (uuid.uuid4(), disabled):
            with self.subTest(owner=missing), self.assertRaises(UserNotFoundError):
                self.admin_service.create_project(
                    browser(self.root), "x", owner_user_id=missing, reason="r"
                )
        self.assertEqual(self.sql("SELECT count(*) FROM projects"), count)
        # Every admin change names a reason for the audit log.
        for reason in (None, "", " " * 3, "x" * 1001):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.admin_service.create_project(browser(self.root), "x", reason=reason)

    def test_revoked_or_disabled_admin_is_refused_from_the_next_request(self):
        other = self.admin("other")
        self.admin_service.list_users(browser(other))
        self.identities.revoke_platform_role(other, **OPS)
        self.denied(
            lambda: self.admin_service.list_users(browser(other)), "platform_admin_required"
        )
        self.identities.set_user_status(self.root, "disabled")
        self.denied(
            lambda: self.admin_service.create_project(browser(self.root), "x", reason="r"),
            "platform_admin_required",
        )

    def test_revocation_racing_a_change_waits_for_it_or_wins(self):
        owner = self.user("owner")
        locker = connect(self.url)
        locker.autocommit = False
        self.addCleanup(locker.close)
        locker.execute("SELECT set_config('nest.audit_operator', 'ops', true)")
        locker.execute("DELETE FROM platform_role_assignments WHERE user_id = %s", (self.root,))
        outcome: list[object] = []

        def create():
            try:
                outcome.append(
                    self.admin_service.create_project(
                        browser(self.root), "late", owner_user_id=owner, reason="r"
                    )
                )
            except AccessDeniedError as exc:
                outcome.append(exc)

        thread = threading.Thread(target=create)
        thread.start()
        time.sleep(0.5)
        self.assertTrue(thread.is_alive(), "the change must wait for the role row")
        locker.commit()
        thread.join(10)
        self.assertIsInstance(outcome[0], AccessDeniedError)
        self.assertEqual(self.store.projects_for_user(owner), [])

    def test_directory_is_paginated_and_minimal(self):
        users = [self.user(f"user{i}") for i in range(5)]
        self.identities.set_user_status(users[1], "disabled")
        seen: list[DirectoryUser] = []
        after = None
        while True:
            page = self.admin_service.list_users(browser(self.root), limit=2, after=after)
            seen.extend(page.items)
            if page.next_after is None:
                break
            self.assertEqual(len(page.items), 2)
            after = page.next_after
        self.assertEqual([u.id for u in seen], [self.root, *users])
        self.assertEqual(seen[0].platform_roles, ("platform_admin",))
        self.assertEqual(seen[2].status, "disabled")
        self.assertEqual(
            {f.name for f in fields(DirectoryUser)},
            {
                "id",
                "display_name",
                "email",
                "status",
                "created_at",
                "disabled_at",
                "platform_roles",
            },
        )
        found = self.admin_service.list_users(browser(self.root), email="USER3@example.com")
        self.assertEqual([u.id for u in found.items], [users[3]])
        self.assertEqual(self.admin_service.get_user(browser(self.root), users[4]).id, users[4])
        with self.assertRaises(LookupError):
            self.admin_service.get_user(browser(self.root), uuid.uuid4())
        for limit in (0, 201, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.admin_service.list_users(browser(self.root), limit=limit)
        # Every Project, member or not, also paginated.
        made = [owned_project(self.projects, users[0], f"p{i}").id for i in range(3)]
        page = self.admin_service.list_projects(browser(self.root), limit=2)
        rest = self.admin_service.list_projects(browser(self.root), after=page.next_after)
        self.assertEqual([p.id for p in page.items + rest.items], made)

    def test_grants_links_and_approvals_are_audited_with_actor_and_reason(self):
        owner = self.user("owner")
        pid = owned_project(self.projects, owner, "lab").id
        nano = self.platform()
        self.accounts.register_account(nano.id, "chem", **OPS)
        admin = browser(self.root)
        service = self.admin_service
        self.assertTrue(service.grant_platform(admin, pid, "nano", reason="grant nano"))
        self.assertFalse(service.grant_platform(admin, pid, "nano", reason="again"))
        link = service.link_account(
            admin, pid, "nano", "chem", modes=["A"], default=True, reason="link chem"
        )
        self.assertEqual((link.modes, link.is_default), (("A",), True))
        self.assertTrue(service.approve(admin, "nano", pid, reason="approve lab"))
        access = service.project_access(admin, pid)
        self.assertEqual([p.slug for p in access.platforms], ["nano"])
        self.assertEqual([a.account.account_name for a in access.accounts], ["chem"])
        self.assertEqual([(a.provider, a.status) for a in access.approvals], [("nano", "approved")])
        events = {e.event: e for e in self.audit.events(project_id=pid, limit=100) if e.outcome}
        for event, note in (
            ("platform.grant.added", "grant nano"),
            ("provider.approval.granted", "approve lab"),
        ):
            with self.subTest(event=event):
                self.assertEqual(
                    (events[event].actor_user_id, events[event].note, events[event].interface),
                    (self.root, note, "database"),
                )
        service.unlink_account(admin, pid, "nano", "chem", reason="unlink chem")
        self.assertTrue(service.revoke_approval(admin, "nano", pid, reason="offboard"))
        self.assertFalse(service.revoke_approval(admin, "nano", pid, reason="again"))
        self.assertTrue(service.revoke_platform(admin, pid, "nano", reason="offboard"))
        access = service.project_access(admin, pid)
        self.assertEqual((access.platforms, access.accounts), ([], []))
        self.assertEqual([a.status for a in access.approvals], ["revoked"])
        revoked = self.audit.events(event="provider.approval.revoked", project_id=pid)
        self.assertEqual([(e.actor_user_id, e.note) for e in revoked], [(self.root, "offboard")])
        audit = service.audit_events(admin, project_id=pid, limit=2)
        self.assertEqual(len(audit.items), 2)
        self.assertIsNotNone(audit.next_after)

    def test_database_refuses_grants_by_a_non_admin_actor(self):
        """Defense in depth: the triggers check the actor is an active platform_admin."""
        owner = self.user("owner")
        pid = owned_project(self.projects, owner, "lab").id
        nano = self.platform()
        for statement, params in (
            (
                "INSERT INTO hpc_platform_project_grants (platform_id, project_id) VALUES (%s, %s)",
                (nano.id, pid),
            ),
            (
                (
                    "INSERT INTO provider_project_approvals (provider, project_id, status) "
                    "VALUES ('nano', %s, 'approved')"
                ),
                (pid,),
            ),
        ):
            with self.subTest(statement=statement), connect(self.url) as conn:
                conn.execute(
                    "SELECT set_config('nest.audit_actor_user_id', %s, true)", (str(owner),)
                )
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement, params)
        # An administrator's actor setting still cannot change Platforms themselves.
        with connect(self.url) as conn:
            conn.execute(
                "SELECT set_config('nest.audit_actor_user_id', %s, true)", (str(self.root),)
            )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE hpc_platforms SET display_name = 'x' WHERE id = %s", (nano.id,)
                )

    def test_existing_projects_memberships_and_invitations_are_preserved(self):
        """Admin-only creation does not touch Project-scoped RBAC."""
        owner, member = self.user("owner"), self.user("member")
        pid = owned_project(self.projects, owner, "lab").id
        self.projects.add_member(browser(owner), pid, member, "admin")
        self.projects.rename_project(browser(member), pid, "renamed")
        self.denied(
            lambda: self.projects.add_member(browser(member), pid, self.user("x"), "owner"),
            "owner_by_transfer_only",
        )
        self.assertEqual(self.store.role_of(pid, member), "admin")


class ProviderApprovalTests(_Base):
    def setUp(self):
        super().setUp()
        self.owner = self.user("owner")
        self.pid = owned_project(self.projects, self.owner, "lab").id
        self.member = Principal(self.owner, "cli", frozenset({"jobs:run"}))

    def check(self, authorizer: Authorizer) -> str:
        return authorizer.check(self.member, Action.RUN_SUBMIT, self.pid, _target("nano")).reason

    def test_grant_and_revoke_apply_without_restart_on_every_replica(self):
        replicas = [
            Authorizer(db, DatabaseProviderApprovals(db)) for db in (self.db, self.open_database())
        ]
        self.assertEqual([self.check(a) for a in replicas], ["provider_denied"] * 2)
        self.approvals.approve("nano", self.pid, **OPS)
        self.assertEqual([self.check(a) for a in replicas], ["allowed"] * 2)
        # Another Provider is not approved by it.
        other = replicas[0].check(self.member, Action.RUN_SUBMIT, self.pid, _target("other"))
        self.assertEqual(other.reason, "provider_denied")
        self.approvals.revoke("nano", self.pid, **OPS)
        self.assertEqual([self.check(a) for a in replicas], ["provider_denied"] * 2)
        self.approvals.approve("nano", self.pid, **OPS)
        # A "restart": a new pool sees exactly what was committed.
        restarted = self.open_database()
        self.assertEqual(
            self.check(Authorizer(restarted, DatabaseProviderApprovals(restarted))), "allowed"
        )
        # A deleted Project is never approved, whatever its record says.
        self.projects.delete_project(browser(self.owner), self.pid)
        self.assertFalse(self.approvals.is_approved("nano", self.pid))

    def test_policy_outage_fails_closed(self):
        self.approvals.approve("nano", self.pid, **OPS)
        providers = DatabaseProviderApprovals(self.db)
        with (
            mock.patch.object(
                ProviderApprovalStore, "is_approved", side_effect=DatabaseError("down")
            ),
            self.assertRaises(DatabaseError),
        ):
            providers.permits(self.member, self.pid, Action.RUN_SUBMIT, "nano")
        down = self.open_database()
        down.close()
        with self.assertRaises(DatabaseError):
            DatabaseProviderApprovals(down).permits(
                self.member, self.pid, Action.RUN_SUBMIT, "nano"
            )
        # No Provider named is never approved.
        self.assertFalse(providers.permits(self.member, self.pid, Action.RUN_SUBMIT, None))

    def test_records_are_never_deleted_or_rewritten(self):
        self.approvals.approve("nano", self.pid, **OPS)
        self.approvals.revoke("nano", self.pid, **OPS)
        with connect(self.url) as conn:
            conn.execute("SELECT set_config('nest.audit_operator', 'ops', true)")
            for statement in (
                "DELETE FROM provider_project_approvals",
                "UPDATE provider_project_approvals SET provider = 'other'",
                "TRUNCATE provider_project_approvals",
            ):
                with (
                    self.subTest(statement=statement),
                    conn.transaction(),
                    self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
                ):
                    conn.execute(statement)
        # Without an operator or an administrator, no change at all.
        with connect(self.url) as conn, self.assertRaises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE provider_project_approvals SET status = 'approved'")
        with self.assertRaises(ValueError):
            self.approvals.approve("nano", self.pid)
        with self.assertRaises(ProjectNotFoundError):
            self.approvals.approve("nano", uuid.uuid4(), **OPS)
        for provider in ("", "*", "Nano", "a" * 33, "1abc"):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                self.approvals.approve(provider, self.pid, **OPS)


class ApprovalImportTests(_Base):
    def setUp(self):
        super().setUp()
        owner = self.user("owner")
        self.ids = [owned_project(self.projects, owner, f"p{i}").id for i in range(3)]

    def run_db(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, {"NEST_DATABASE_URL": self.url}),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = nest_db(list(argv))
        return code, out.getvalue(), err.getvalue()

    def imported(self, ids, *extra: str) -> tuple[int, str, str]:
        return self.run_db(
            "provider",
            "import",
            "nano",
            "--project-ids",
            ",".join(str(i) for i in ids),
            "--operator",
            "ops-alice",
            "--reason",
            "cut over to durable approvals",
            *extra,
        )

    def statuses(self) -> dict[uuid.UUID, str]:
        return {a.project_id: a.status for a in self.approvals.list_approvals(provider="nano")}

    def test_explicit_ids_are_imported_idempotently_without_regranting_revocations(self):
        first, second, third = self.ids
        # A revocation recorded before the import, and a preexisting approval.
        self.approvals.approve("nano", first, **OPS)
        self.approvals.revoke("nano", first, **OPS)
        self.approvals.approve("nano", second, **OPS)
        code, out, _ = self.imported(self.ids, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("dry run: nothing written", out)
        self.assertEqual(self.statuses(), {first: "revoked", second: "approved"})
        code, out, _ = self.imported(self.ids)
        self.assertEqual(code, 0, out)
        self.assertIn("1 approved, 1 already approved, 1 left revoked", out)
        self.assertEqual(self.statuses(), {first: "revoked", second: "approved", third: "approved"})
        # Again, as a restart or a repeated cutover would: nothing changes.
        code, out, _ = self.imported(self.ids)
        self.assertIn("0 approved, 2 already approved, 1 left revoked", out)
        events = self.audit.events(event="provider.approval.granted", limit=100)
        imported = [e for e in events if e.project_id == third]
        self.assertEqual(
            [(e.actor_operator, e.note) for e in imported],
            [("ops-alice", "cut over to durable approvals")],
        )
        # Only an explicit approval undoes a revocation.
        self.assertEqual(
            self.run_db("provider", "approve", "nano", "--project-id", str(first), *_ops_args())[0],
            0,
        )
        self.assertEqual(self.statuses()[first], "approved")

    def test_wildcard_invalid_and_missing_ids_import_nothing(self):
        for value, code in (("*", 2), (f"{self.ids[0]},*", 2), ("not-a-uuid", 2), ("", 2)):
            with self.subTest(value=value):
                result = self.run_db(
                    "provider", "import", "nano", "--project-ids", value, *_ops_args()
                )
                self.assertEqual(result[0], code, result)
        code, _, err = self.imported([self.ids[0], uuid.uuid4()])
        self.assertEqual(code, 1)
        self.assertIn("nothing was imported", err)
        project = self.store.get_project(self.ids[1])
        assert project is not None
        self.projects.delete_project(browser(project.owner_user_id), self.ids[1])
        self.assertEqual(self.imported([self.ids[1]])[0], 1)
        self.assertEqual(self.statuses(), {})

    def test_upgrade_creates_no_approval_and_keeps_existing_grants(self):
        url = self.database()
        migrations = load_migrations()
        at = [m.label for m in migrations].index("0017_provider_project_approvals")
        with connect(url) as conn:
            upgrade(conn, migrations[:at])
            (owner,) = conn.execute("INSERT INTO users DEFAULT VALUES RETURNING id").fetchone()
            with conn.transaction():
                (pid,) = conn.execute(
                    "INSERT INTO projects (name, created_by_user_id) VALUES ('p', %s) RETURNING id",
                    (owner,),
                ).fetchone()
                conn.execute(
                    "INSERT INTO project_memberships (project_id, user_id, role) "
                    "VALUES (%s, %s, 'owner')",
                    (pid, owner),
                )
            conn.execute("SELECT set_config('nest.audit_operator', 'ops', false)")
            (platform,) = conn.execute(
                "INSERT INTO hpc_platforms (slug, display_name) VALUES ('nano', 'Nano') "
                "RETURNING id"
            ).fetchone()
            conn.execute(
                "INSERT INTO hpc_platform_project_grants (platform_id, project_id) VALUES (%s, %s)",
                (platform, pid),
            )
            applied = [m.label for m in upgrade(conn)]
            self.assertEqual(applied[0], "0017_provider_project_approvals")
            self.assertEqual(
                conn.execute("SELECT count(*) FROM provider_project_approvals").fetchone(), (0,)
            )
            self.assertEqual(
                conn.execute("SELECT count(*) FROM hpc_platform_project_grants").fetchone(),
                (1,),
            )


class DurableAdmissionTests(_AdmissionBase):
    """Registry admission with `NEST_PROVIDER_APPROVALS=database` (Provider `nano`)."""

    def setUp(self):
        super().setUp()
        self.approvals = ProviderApprovalStore(self.db)
        self.approvals.approve("nano", self.project, **OPS)
        self.access = ExecutionAccess(
            self.db,
            DatabaseProviderApprovals(self.db),
            provider_id="nano",
            platform_id=self.platform_id,
            audit=self.audit,
        )
        self.admission = RegistryAdmission(
            self.db,
            self.backend,
            self.profile,
            self.platform_id,
            audit=self.audit,
            provider_approval="nano",
        )
        self.stub = self.serve(self.admission)
        self.principal = Principal(self.member, "cli", frozenset({"jobs:run"}))

    def admit(self, **request):
        return self.admission.admit(
            self.principal,
            self.project,
            SubmissionRequest("batch", workdir="/w", script="s", **request),
            execution_id=uuid.uuid4().hex,
        )

    def test_grant_and_revoke_without_redeploy(self):
        self.assertTrue(self.submit().job_id)
        self.approvals.revoke("nano", self.project, **OPS)
        error = self.refused(self.submit)
        self.assertEqual(error.code(), grpc.StatusCode.PERMISSION_DENIED)
        self.assertIn("provider_denied", error.details())
        self.approvals.approve("nano", self.project, **OPS)
        self.assertTrue(self.submit().job_id)
        self.assertEqual(len(self.sbatch_calls()), 2)

    def test_revocation_after_the_check_is_caught_before_the_attempt(self):
        # Authorized for run.submit, then revoked before the attempt is recorded.
        self.approvals.revoke("nano", self.project, **OPS)
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit()
        self.assertEqual(caught.exception.reason, "provider_denied")
        self.assertEqual(self.attempts(), [])
        self.assert_nothing_sent()

    def test_concurrent_admission_and_revocation_serialize(self):
        locker = connect(self.url)
        locker.autocommit = False
        self.addCleanup(locker.close)
        locker.execute("SELECT set_config('nest.audit_operator', 'ops', true)")
        locker.execute(
            "UPDATE provider_project_approvals SET status = 'revoked' "
            "WHERE provider = 'nano' AND project_id = %s",
            (self.project,),
        )
        outcome: list[object] = []

        def admit():
            try:
                outcome.append(self.admit())
            except AdmissionRefusedError as exc:
                outcome.append(exc)

        thread = threading.Thread(target=admit)
        thread.start()
        time.sleep(0.5)
        self.assertTrue(thread.is_alive(), "the attempt must wait for the revocation")
        locker.commit()
        thread.join(10)
        self.assertIsInstance(outcome[0], AdmissionRefusedError)
        self.assertEqual(self.attempts(), [])
        # The other way round: a revocation waits for an attempt holding the approval.
        self.approvals.approve("nano", self.project, **OPS)
        holder = connect(self.url)
        holder.autocommit = False
        self.addCleanup(holder.close)
        holder.execute(
            "SELECT 1 FROM provider_project_approvals WHERE provider = 'nano' "
            "AND project_id = %s FOR SHARE",
            (self.project,),
        )
        revoked: list[bool] = []
        thread = threading.Thread(
            target=lambda: revoked.append(self.approvals.revoke("nano", self.project, **OPS))
        )
        thread.start()
        time.sleep(0.5)
        self.assertTrue(thread.is_alive(), "the revocation must wait for the attempt")
        holder.commit()
        thread.join(10)
        self.assertEqual(revoked, [True])

    def test_revocation_cancels_no_running_job(self):
        admitted = self.admit()
        self.approvals.revoke("nano", self.project, **OPS)
        [attempt] = self.attempts()
        self.assertEqual((attempt["id"], attempt["status"]), (admitted.attempt.id, "submitting"))
        self.assertEqual(self.hpc.calls("scancel"), [])

    def test_provider_platform_and_account_are_independent(self):
        # Approved and granted, but no account: refused by the account check.
        self.accounts.unlink_project(self.project, self.platform_id, "proj", **OPS)
        self.accounts.unlink_project(self.project, self.platform_id, "spare", **OPS)
        error = self.refused(self.submit)
        self.assertIn("no_account", error.details())
        # Approved, but the Platform grant revoked: refused by the Platform check.
        self.platforms.revoke_project(self.platform_id, self.project, **OPS)
        error = self.refused(self.submit)
        self.assertIn("none_authorized", error.details())
        self.platforms.grant_project(self.platform_id, self.project, **OPS)
        self.accounts.link_project(
            self.project, self.platform_id, "proj", modes=["A"], default=True, **OPS
        )
        # Granted and linked, but not approved: refused by the Provider check.
        self.approvals.revoke("nano", self.project, **OPS)
        error = self.refused(self.submit)
        self.assertIn("provider_denied", error.details())
        # The other Project has a grant and an account, and no approval at all.
        error = self.refused(lambda: self.submit(token=self.token_outsider, project=self.other))
        self.assertIn("provider_denied", error.details())
        self.assert_nothing_sent()
        # A deny-all environment list never approves what the database does.
        legacy = ExecutionAccess(self.db, AllowedProjects([]), provider_id="nano")
        self.approvals.approve("nano", self.project, **OPS)
        with self.assertRaises(AccessDeniedError):
            legacy.authorize_submit(self.principal, self.project)


class AdminHTTPTests(_Base):
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
        self.tokens: list[str] = []

    def token(self, user_id: uuid.UUID, kind: str = "browser", scope: str | None = None) -> str:
        session = self.identities.create_session(
            user_id,
            kind=kind,
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=scope,
        )
        access = secrets.token_urlsafe()
        self.sessions.issue_access_token(session.id, token_hash=hash_token(access), ttl_seconds=600)
        self.tokens.append(access)
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
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            with exc:
                raw, status = exc.read(), exc.code
        for token in self.tokens:
            self.assertNotIn(token.encode(), raw, "a response must never echo a token")
        return status, json.loads(raw or b"{}")

    def test_admin_onboards_a_project_end_to_end(self):
        owner = self.user("owner")
        nano = self.platform()
        self.accounts.register_account(nano.id, "chem", **OPS)
        r = self.token(self.root)
        status, created = self.call(
            "POST",
            "/api/admin/projects",
            cookie=r,
            body={"name": "Chemistry", "owner_user_id": str(owner), "reason": "onboard"},
        )
        self.assertEqual((status, created["owner_user_id"]), (201, str(owner)))
        pid = created["id"]
        for path, body in (
            (f"/api/admin/projects/{pid}/platforms/grant", {"platform": "nano"}),
            (
                f"/api/admin/projects/{pid}/accounts/link",
                {"platform": "nano", "account": "chem", "modes": ["A"], "default": True},
            ),
            ("/api/admin/provider-approvals/approve", {"provider": "nano", "project_id": pid}),
        ):
            with self.subTest(path=path):
                status, answer = self.call(
                    "POST", path, cookie=r, body={**body, "reason": "onboard"}
                )
                self.assertEqual(status, 200, answer)
        status, access = self.call("GET", f"/api/admin/projects/{pid}/access", cookie=r)
        self.assertEqual(status, 200)
        self.assertEqual([p["slug"] for p in access["platforms"]], ["nano"])
        self.assertEqual(access["accounts"][0]["account"]["account_name"], "chem")
        self.assertEqual(access["provider_approvals"][0]["status"], "approved")
        status, page = self.call("GET", "/api/admin/provider-approvals?provider=nano", cookie=r)
        self.assertEqual([a["project_id"] for a in page["provider_approvals"]], [pid])
        status, audit = self.call("GET", f"/api/admin/audit?project_id={pid}&limit=50", cookie=r)
        self.assertIn("provider.approval.granted", {e["event"] for e in audit["events"]})
        status, users = self.call("GET", "/api/admin/users?limit=1", cookie=r)
        self.assertEqual((status, len(users["users"])), (200, 1))
        self.assertIsNotNone(users["next_after"])
        status, more = self.call("GET", f"/api/admin/users?after={users['next_after']}", cookie=r)
        self.assertEqual([u["id"] for u in more["users"]], [str(owner)])
        self.assertEqual(
            set(more["users"][0]),
            {
                "id",
                "display_name",
                "email",
                "status",
                "created_at",
                "disabled_at",
                "platform_roles",
            },
        )
        status, platforms = self.call("GET", "/api/admin/platforms", cookie=r)
        self.assertEqual([p["slug"] for p in platforms["platforms"]], ["nano"])
        status, accounts = self.call("GET", "/api/admin/platforms/nano/accounts", cookie=r)
        self.assertEqual([a["account_name"] for a in accounts["accounts"]], ["chem"])
        # The Owner manages members as before; the administrator is not one.
        o = self.token(owner)
        self.assertEqual(self.call("GET", f"/api/projects/{pid}", cookie=o)[1]["role"], "owner")
        self.assertEqual(self.call("GET", f"/api/projects/{pid}", cookie=r)[0], 404)

    def test_every_admin_route_is_denied_by_default(self):
        alice = self.user("alice")
        pid = owned_project(self.projects, alice, "lab").id
        a = self.token(alice)
        routes = [
            ("GET", "/api/admin/users", None),
            ("GET", f"/api/admin/users/{alice}", None),
            ("GET", "/api/admin/projects", None),
            ("POST", "/api/admin/projects", {"name": "x", "reason": "r"}),
            ("GET", f"/api/admin/projects/{pid}/access", None),
            (
                "POST",
                f"/api/admin/projects/{pid}/platforms/grant",
                {"platform": "x", "reason": "r"},
            ),
            ("GET", "/api/admin/platforms", None),
            ("GET", "/api/admin/provider-approvals", None),
            (
                "POST",
                "/api/admin/provider-approvals/approve",
                {"provider": "nano", "project_id": str(pid), "reason": "r"},
            ),
            ("GET", "/api/admin/audit", None),
        ]
        cli = self.token(self.root, "cli", "projects:read jobs:run")
        for method, path, body in routes:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.call(method, path, body=body)[0], 401)
                status, answer = self.call(method, path, cookie=a, body=body)
                self.assertEqual(
                    (status, answer),
                    (403, {"error": "forbidden", "reason": "platform_admin_required"}),
                )
                status, answer = self.call(method, path, bearer=cli, body=body)
                self.assertEqual((status, answer.get("reason")), (403, "browser_only"))
        # The existing Projects route is admin-only too.
        status, answer = self.call("POST", "/api/projects", cookie=a, body={"name": "x"})
        self.assertEqual((status, answer["reason"]), (403, "platform_admin_required"))
        self.assertEqual([p.id for p, _ in self.projects.list_projects(browser(alice))], [pid])

    def test_csrf_sessions_and_malformed_requests(self):
        r = self.token(self.root)
        status, answer = self.call(
            "POST", "/api/admin/projects", cookie=r, body={"name": "x", "reason": "r"}, origin=False
        )
        self.assertEqual((status, answer), (403, {"error": "cross_origin"}))
        owner = self.user("owner")
        pid = owned_project(self.projects, owner, "lab").id
        for method, path, body, expected in [
            ("GET", "/api/admin/nothing", None, 404),
            ("GET", "/api/admin/users/not-a-uuid", None, 404),
            ("POST", "/api/admin/users", {}, 405),
            ("GET", "/api/admin/users?limit=0", None, 400),
            ("GET", "/api/admin/users?limit=x", None, 400),
            ("GET", f"/api/admin/users/{uuid.uuid4()}", None, 404),
            ("POST", "/api/admin/projects", {"name": "x"}, 400),
            ("POST", "/api/admin/projects", {"name": "x", "reason": "r", "owner_user_id": 7}, 400),
            (
                "POST",
                "/api/admin/projects",
                {"name": "x", "reason": "r", "owner_user_id": str(uuid.uuid4())},
                404,
            ),
            ("GET", f"/api/admin/projects/{uuid.uuid4()}/access", None, 404),
            (
                "POST",
                f"/api/admin/projects/{pid}/platforms/grant",
                {"platform": "missing", "reason": "r"},
                404,
            ),
            (
                "POST",
                f"/api/admin/projects/{pid}/accounts/link",
                {"platform": "missing", "account": "a", "modes": "A", "reason": "r"},
                400,
            ),
            (
                "POST",
                "/api/admin/provider-approvals/approve",
                {"provider": "*", "project_id": str(pid), "reason": "r"},
                400,
            ),
            (
                "POST",
                "/api/admin/provider-approvals/approve",
                {"provider": "nano", "project_id": str(uuid.uuid4()), "reason": "r"},
                404,
            ),
            ("GET", "/api/admin/provider-approvals?after=bad", None, 400),
        ]:
            with self.subTest(method=method, path=path, body=body):
                self.assertEqual(self.call(method, path, cookie=r, body=body)[0], expected)
        # A revoked session is refused at once.
        self.identities.revoke_user_sessions(self.root)
        self.assertEqual(self.call("GET", "/api/admin/users", cookie=r)[0], 401)

    def test_redirect_url_cannot_be_an_admin_route(self):
        with self.assertRaises(ConfigError) as caught:
            load_controller_config(
                {
                    "NEST_DATABASE_URL": self.url,
                    "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
                    "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
                    "NEST_GOOGLE_REDIRECT_URL": "https://nest.example.com/api/admin/callback",
                }
            )
        self.assertIn("privileged management API", str(caught.exception))


class ProviderApprovalConfigTests(unittest.TestCase):
    BASE: ClassVar[dict[str, str]] = {
        "NEST_DATABASE_URL": "postgresql://nest@db.example.com/nest",
        "NEST_CONTROLLER_ALLOW_PLAINTEXT": "true",
    }

    def load(self, **env):
        return load_controller_config({**self.BASE, **env})

    def problems(self, **env) -> str:
        with self.assertRaises(ConfigError) as caught:
            self.load(**env)
        return str(caught.exception)

    def test_default_keeps_an_existing_list_and_uses_the_database_otherwise(self):
        pid = str(uuid.uuid4())
        self.assertEqual(self.load().provider_approvals, "database")
        listed = self.load(NEST_PROVIDER_ALLOWED_PROJECTS=pid)
        self.assertEqual(
            (listed.provider_approvals, listed.allowed_projects), ("environment", {pid})
        )
        self.assertEqual(
            self.load(NEST_PROVIDER_APPROVALS="environment").provider_approvals, "environment"
        )
        self.assertEqual(
            self.load(
                NEST_PROVIDER_ALLOWED_PROJECTS="*", NEST_PROVIDER_APPROVALS="environment"
            ).provider_approvals,
            "environment",
        )
        self.assertIn(
            "nest-db provider import",
            self.problems(NEST_PROVIDER_APPROVALS="database", NEST_PROVIDER_ALLOWED_PROJECTS=pid),
        )
        self.assertIn("must be one of", self.problems(NEST_PROVIDER_APPROVALS="both"))
        self.assertIn(
            "needs NEST_CONTROLLER_AUTH=session",
            self.problems(NEST_CONTROLLER_AUTH="none", NEST_PROVIDER_APPROVALS="database"),
        )
        summary = self.load().summary()
        self.assertEqual(summary["provider_approvals"], "database")

    def test_controller_wiring_follows_the_mode(self):
        database = mock.Mock(spec=Database)
        with mock.patch("nest.controller.environment_platform_id", return_value=uuid.uuid4()):
            access = build_access(self.load(), database)
            assert access is not None
            self.assertIsInstance(access.authorizer.providers, DatabaseProviderApprovals)
            pid = str(uuid.uuid4())
            legacy = build_access(self.load(NEST_PROVIDER_ALLOWED_PROJECTS=pid), database)
            assert legacy is not None
            self.assertIsInstance(legacy.authorizer.providers, AllowedProjects)
        # Without a Slurm Platform there is no registry admission to recheck in.
        self.assertIsNone(build_admission(self.load(), database, None, access))


def _target(provider: str):
    from nest.authz import Target

    return Target(provider=provider)


def _ops_args() -> list[str]:
    return ["--operator", "ops-alice", "--reason", "approval tests"]


if __name__ == "__main__":
    unittest.main()
