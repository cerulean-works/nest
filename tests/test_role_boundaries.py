"""Platform-role persistence and identity boundaries (M1 patch B01).

Runs against a real PostgreSQL (see `postgres_fixture`). Checks that `platform_admin`,
Organization roles, Project roles, HPC Platform grants and Slurm accounts are
separate records that never imply each other; that platform roles follow the
User's lifecycle and are never reused once revoked; that User, Organization and
Project identities are generated UUIDs never keyed by a Platform slug, Slurm
account name or login node; and that the schema and the roles survive a restart.
"""

import os
import threading
import time
import uuid
from unittest import mock

import psycopg
from postgres_fixture import connect
from test_platforms import OPS, _Base, browser

from nest.config import load_database_config
from nest.db import (
    Database,
    IdentityConflictError,
    IdentityStore,
    SlurmAccountStore,
    UserNotActiveError,
    load_migrations,
    upgrade,
    verify,
)

GOOGLE = "https://accounts.google.com"


class _RoleBase(_Base):
    def setUp(self):
        super().setUp()
        self.accounts = SlurmAccountStore(self.db)

    def role_events(self, user_id: uuid.UUID) -> list[str]:
        rows = self.conn.execute(
            "SELECT event FROM platform_audit_events WHERE target_user_id = %s "
            "AND event LIKE 'platform_role_%%' ORDER BY id",
            (user_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def assignments(self, user_id: uuid.UUID) -> list[tuple]:
        return self.conn.execute(
            "SELECT role, granted_at FROM platform_role_assignments WHERE user_id = %s",
            (user_id,),
        ).fetchall()


class RoleScopeTests(_RoleBase):
    """Each role vocabulary is accepted only in its own table."""

    def test_migration_follows_the_project_platform_policy(self):
        labels = [m.label for m in load_migrations()]
        self.assertEqual(
            labels[labels.index("0015_project_platform_policy") + 1],
            "0016_platform_role_boundaries",
        )

    def test_role_names_do_not_cross_scopes(self):
        alice = self.user()
        project = self.project(alice)
        org = self.identities.create_organization("Org", created_by_user_id=alice)
        for role in ("owner", "admin", "member", "viewer", "org_admin"):
            with (
                self.subTest(table="platform", role=role),
                self.assertRaises(psycopg.errors.CheckViolation),
            ):
                self.conn.execute(
                    "INSERT INTO platform_role_assignments (user_id, role) VALUES (%s, %s)",
                    (alice, role),
                )
        bob = self.user()
        for role in ("platform_admin", "org_admin"):
            with (
                self.subTest(table="project", role=role),
                self.assertRaises(psycopg.errors.CheckViolation),
            ):
                self.conn.execute(
                    "INSERT INTO project_memberships (project_id, user_id, role) "
                    "VALUES (%s, %s, %s)",
                    (project, bob, role),
                )
        for role in ("platform_admin", "owner", "admin", "viewer"):
            with (
                self.subTest(table="organization", role=role),
                self.assertRaises(psycopg.errors.CheckViolation),
            ):
                self.conn.execute(
                    "INSERT INTO organization_memberships (organization_id, user_id, role) "
                    "VALUES (%s, %s, %s)",
                    (org, bob, role),
                )
        with self.assertRaises(ValueError):
            self.identities.grant_platform_role(alice, "admin")

    def test_platform_admin_implies_no_project_organization_or_platform_authority(self):
        admin, owner = self.user(), self.user()
        project = self.project(owner)
        org = self.identities.create_organization("Org", created_by_user_id=owner)
        nano4 = self.store.create_platform("nano4", "Nano 4", **OPS)
        self.store.grant_project(nano4.id, project, **OPS)
        self.accounts.register_account(nano4.id, "acct1", **OPS)
        self.accounts.link_project(project, nano4.id, "acct1", modes="AB", **OPS)

        self.assertTrue(self.identities.grant_platform_role(admin))
        self.assertEqual(self.identities.platform_roles(admin), {"platform_admin"})
        self.assertIsNone(self.projects.role_of(project, admin))
        self.assertIsNone(self.identities.organization_role(org, admin))
        self.assertEqual(self.catalog.list_platforms(browser(admin)), [])
        self.assertEqual(self.store.authorized_platforms(admin), [])

    def test_project_and_organization_roles_imply_no_platform_role(self):
        owner, admin, member = self.user(), self.user(), self.user()
        project = self.project(owner)
        self.projects.add_member(project, admin, "admin", added_by_user_id=owner)
        org = self.identities.create_organization("Org", created_by_user_id=owner)
        self.identities.set_organization_role(org, member, "org_admin")
        for user in (owner, admin, member):
            self.assertEqual(self.identities.platform_roles(user), set())
        self.assertEqual(self.identities.platform_admins(), [])

    def test_a_platform_grant_is_not_a_project_role_or_a_platform_role(self):
        owner, outsider = self.user(), self.user()
        project = self.project(owner)
        nano4 = self.store.create_platform("nano4", "Nano 4", **OPS)
        self.store.grant_project(nano4.id, project, **OPS)
        self.assertIsNone(self.projects.role_of(project, outsider))
        self.assertEqual(self.identities.platform_roles(owner), set())
        self.assertEqual(self.catalog.list_platforms(browser(outsider)), [])


class PlatformRoleLifecycleTests(_RoleBase):
    def test_duplicate_assignments_are_refused_and_audited_once(self):
        alice = self.user()
        self.assertTrue(self.identities.grant_platform_role(alice, operator="ops"))
        self.assertFalse(self.identities.grant_platform_role(alice, operator="ops"))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "INSERT INTO platform_role_assignments (user_id, role) "
                "VALUES (%s, 'platform_admin')",
                (alice,),
            )
        self.assertEqual(len(self.assignments(alice)), 1)
        self.assertEqual(self.role_events(alice), ["platform_role_granted"])

    def test_concurrent_grants_create_one_assignment(self):
        alice = self.user()
        results: list[object] = [None, None]
        barrier = threading.Barrier(2)

        def grant(index: int) -> None:
            barrier.wait()
            results[index] = self.identities.grant_platform_role(alice, operator="ops")

        threads = [threading.Thread(target=grant, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(sorted(results, key=bool), [False, True])
        self.assertEqual(len(self.assignments(alice)), 1)

    def test_revocation_is_final_and_a_regrant_is_a_new_assignment(self):
        alice = self.user()
        self.identities.grant_platform_role(alice, operator="ops")
        ((_, first_granted_at),) = self.assignments(alice)
        self.assertTrue(self.identities.revoke_platform_role(alice, operator="ops"))
        self.assertFalse(self.identities.revoke_platform_role(alice, operator="ops"))
        self.assertEqual(self.identities.platform_roles(alice), set())
        self.assertEqual(self.assignments(alice), [])
        time.sleep(0.01)
        self.identities.grant_platform_role(alice, operator="ops")
        ((_, second_granted_at),) = self.assignments(alice)
        self.assertGreater(second_granted_at, first_granted_at)
        self.assertEqual(
            self.role_events(alice),
            ["platform_role_granted", "platform_role_revoked", "platform_role_granted"],
        )
        # A revoked assignment cannot be brought back by rewriting another row.
        bob = self.user()
        self.identities.grant_platform_role(bob, operator="ops")
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE platform_role_assignments SET user_id = %s WHERE user_id = %s",
                (uuid.uuid4(), bob),
            )

    def test_a_disabled_user_has_no_effective_platform_role(self):
        alice = self.user()
        self.identities.grant_platform_role(alice, operator="ops")
        self.identities.set_user_status(alice, "disabled")
        self.assertEqual(self.identities.platform_roles(alice), set())
        # The assignment itself is kept, so re-enabling restores exactly that.
        self.assertEqual(len(self.assignments(alice)), 1)
        self.identities.set_user_status(alice, "active")
        self.assertEqual(self.identities.platform_roles(alice), {"platform_admin"})
        self.assertEqual(self.role_events(alice), ["platform_role_granted"])

    def test_a_disabled_user_cannot_be_granted_a_platform_role(self):
        alice = self.user()
        self.identities.set_user_status(alice, "disabled")
        with self.assertRaises(UserNotActiveError):
            self.identities.grant_platform_role(alice, operator="ops")
        # Enforced by the database too, for every writer.
        with self.assertRaises(psycopg.errors.CheckViolation) as caught:
            self.conn.execute(
                "INSERT INTO platform_role_assignments (user_id, role) "
                "VALUES (%s, 'platform_admin')",
                (alice,),
            )
        self.assertEqual(
            caught.exception.diag.constraint_name, "platform_role_assignments_active_user"
        )
        self.assertEqual(self.assignments(alice), [])
        self.assertEqual(self.role_events(alice), [])

    def test_a_grant_and_a_concurrent_disable_serialize(self):
        alice = self.user()
        with psycopg.connect(self.url) as disabling:
            disabling.execute(
                "UPDATE users SET status = 'disabled', disabled_at = now() WHERE id = %s",
                (alice,),
            )
            outcome: list[object] = []

            def grant() -> None:
                try:
                    outcome.append(self.identities.grant_platform_role(alice, operator="ops"))
                except Exception as exc:  # noqa: BLE001 - asserted below
                    outcome.append(exc)

            granting = threading.Thread(target=grant)
            granting.start()
            time.sleep(0.5)
            self.assertTrue(granting.is_alive(), "the grant should wait for the disable")
            disabling.commit()
        granting.join(timeout=30)
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], UserNotActiveError)
        self.assertEqual(self.assignments(alice), [])

    def test_deleting_a_user_removes_their_assignment_and_keeps_the_history(self):
        alice = self.user()
        self.identities.grant_platform_role(alice, operator="ops")
        self.conn.execute("DELETE FROM users WHERE id = %s", (alice,))
        self.assertEqual(self.assignments(alice), [])
        self.assertEqual(
            self.role_events(alice), ["platform_role_granted", "platform_role_revoked"]
        )

    def test_nest_db_admin_grant_refuses_a_disabled_user(self):
        from nest.db.cli import main

        alice = self.user()
        self.identities.set_user_status(alice, "disabled")
        argv = ["admin", "grant", "--user-id", str(alice), "--operator", "ops", "--reason", "test"]
        with mock.patch.dict(os.environ, {"NEST_DATABASE_URL": self.url}):
            self.assertEqual(main(argv), 1)
        self.assertEqual(self.assignments(alice), [])


class IsolationTests(_RoleBase):
    def test_project_roles_are_isolated_per_project_and_per_user(self):
        alice, bob = self.user(), self.user()
        lab, other = self.project(alice, "lab"), self.project(bob, "other")
        self.projects.add_member(lab, bob, "viewer", added_by_user_id=alice)
        self.assertEqual(self.projects.role_of(lab, alice), "owner")
        self.assertEqual(self.projects.role_of(lab, bob), "viewer")
        self.assertIsNone(self.projects.role_of(other, alice))
        self.assertEqual(self.projects.role_of(other, bob), "owner")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "INSERT INTO project_memberships (project_id, user_id, role) "
                "VALUES (%s, %s, 'admin')",
                (lab, bob),
            )
        # A membership can never be moved to another Project or User.
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE project_memberships SET project_id = %s WHERE project_id = %s "
                "AND user_id = %s",
                (other, lab, bob),
            )

    def test_a_shared_slurm_account_never_merges_projects(self):
        alice, bob = self.user(), self.user()
        lab, other = self.project(alice, "lab"), self.project(bob, "other")
        nano4 = self.store.create_platform("nano4", "Nano 4", **OPS)
        for project in (lab, other):
            self.store.grant_project(nano4.id, project, **OPS)
        self.accounts.register_account(nano4.id, "shared", **OPS)
        self.accounts.link_project(lab, nano4.id, "shared", modes="AB", **OPS)
        self.accounts.link_project(other, nano4.id, "shared", modes="AB", **OPS)

        self.assertNotEqual(lab, other)
        self.assertIsNone(self.projects.role_of(lab, bob))
        self.assertIsNone(self.projects.role_of(other, alice))
        lab_slugs = {p.slug for p in self.store.authorized_platforms(alice, project_id=lab)}
        self.assertEqual(lab_slugs, {"nano4"})
        self.assertEqual(self.store.authorized_platforms(alice, project_id=other), [])
        # Unlinking one Project leaves the other's link untouched.
        self.accounts.unlink_project(lab, nano4.id, "shared", **OPS)
        self.assertIsNone(self.accounts.mapping(lab, nano4.id).link("shared"))
        self.assertIsNotNone(self.accounts.mapping(other, nano4.id).link("shared"))

    def test_same_named_accounts_on_different_platforms_stay_separate(self):
        alice = self.user()
        lab = self.project(alice)
        nano4 = self.store.create_platform("nano4", "Nano 4", **OPS)
        f1 = self.store.create_platform("f1", "F1", **OPS)
        self.store.grant_project(nano4.id, lab, **OPS)
        a = self.accounts.register_account(nano4.id, "acct", **OPS)
        b = self.accounts.register_account(f1.id, "acct", **OPS)
        self.assertNotEqual(a.id, b.id)
        self.accounts.link_project(lab, nano4.id, "acct", modes="AB", **OPS)
        self.assertIsNotNone(self.accounts.mapping(lab, nano4.id).link("acct"))
        # The same name on f1 is another account, and lab is not granted f1.
        with self.assertRaises(Exception):  # noqa: B017 - PlatformNotGrantedError
            self.accounts.link_project(lab, f1.id, "acct", modes="AB", **OPS)

    def test_oidc_subjects_are_scoped_to_their_issuer(self):
        alice, bob = self.user(), self.user()
        self.identities.link_identity(alice, issuer=GOOGLE, subject="123")
        self.identities.link_identity(bob, issuer="https://idp.example.org", subject="123")
        found = self.identities.find_user(issuer=GOOGLE, subject="123")
        assert found is not None
        self.assertEqual(found.id, alice)
        with self.assertRaises(IdentityConflictError):
            self.identities.link_identity(bob, issuer=GOOGLE, subject="123")
        # An identity can never be moved to another User.
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE user_identities SET user_id = %s WHERE issuer = %s AND subject = '123'",
                (bob, GOOGLE),
            )

    def test_tenant_identities_are_not_keyed_by_hpc_identifiers(self):
        """Users, Organizations, Projects and role tables reference no HPC record."""
        tenant_tables = (
            "users",
            "user_identities",
            "user_sessions",
            "platform_role_assignments",
            "organizations",
            "organization_memberships",
            "projects",
            "project_memberships",
        )
        keys = self.conn.execute(
            "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY (i.indkey) "
            "WHERE i.indisprimary AND c.relname = ANY (%s)",
            (list(tenant_tables),),
        ).fetchall()
        for table in ("users", "organizations", "projects"):
            self.assertIn((table, "id", "uuid"), keys)
        hpc_refs = self.conn.execute(
            "SELECT c.relname, f.relname FROM pg_constraint k "
            "JOIN pg_class c ON c.oid = k.conrelid JOIN pg_class f ON f.oid = k.confrelid "
            "WHERE k.contype = 'f' AND c.relname = ANY (%s) "
            "AND f.relname IN ('hpc_platforms', 'hpc_login_nodes', 'slurm_accounts', "
            "'provider_profiles', 'hpc_platform_project_grants', 'project_platform_accounts')",
            (list(tenant_tables),),
        ).fetchall()
        self.assertEqual(hpc_refs, [])
        hpc_columns = self.conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ANY (%s) "
            "AND column_name ~ '(platform|slug|account|host|provider|slurm|unix)'",
            (list(tenant_tables),),
        ).fetchall()
        self.assertEqual(hpc_columns, [])


class RestartTests(_RoleBase):
    def test_roles_and_schema_survive_a_restart(self):
        alice, bob = self.user(), self.user()
        lab = self.project(alice)
        self.projects.add_member(lab, bob, "member", added_by_user_id=alice)
        self.identities.grant_platform_role(alice, operator="ops")
        self.db.close()

        with connect(self.url) as conn:
            self.assertEqual(upgrade(conn), [])
            verify(conn)
        restarted = Database(load_database_config({"NEST_DATABASE_URL": self.url}))
        restarted.open()
        self.addCleanup(restarted.close)
        identities = IdentityStore(restarted)
        self.assertEqual(identities.platform_roles(alice), {"platform_admin"})
        self.assertEqual(identities.platform_roles(bob), set())
        from nest.db import ProjectStore

        projects = ProjectStore(restarted)
        self.assertEqual(projects.role_of(lab, alice), "owner")
        self.assertEqual(projects.role_of(lab, bob), "member")

    def test_upgrade_keeps_existing_assignments_of_disabled_users(self):
        url = self.database()
        migrations = load_migrations()
        at = [m.label for m in migrations].index("0016_platform_role_boundaries")
        with connect(url) as conn:
            upgrade(conn, migrations[:at])
            (user_id,) = conn.execute("INSERT INTO users DEFAULT VALUES RETURNING id").fetchone()
            conn.execute(
                "INSERT INTO platform_role_assignments (user_id, role) "
                "VALUES (%s, 'platform_admin')",
                (user_id,),
            )
            conn.execute(
                "UPDATE users SET status = 'disabled', disabled_at = now() WHERE id = %s",
                (user_id,),
            )
            applied = [m.label for m in upgrade(conn)]
            self.assertEqual(applied[0], "0016_platform_role_boundaries")
            verify(conn)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM platform_role_assignments WHERE user_id = %s",
                    (user_id,),
                ).fetchone(),
                (1,),
            )
