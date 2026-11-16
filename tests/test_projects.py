"""Users, identities, sessions, Projects, memberships and invitations (B01).

Every test runs against a real PostgreSQL named by `NEST_TEST_DATABASE_URL` (see
`postgres_fixture`). Concurrency tests open several connections and hold row locks
in one while another waits, so they exercise PostgreSQL's own locking rather than
a simulation of it.
"""

import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from postgres_fixture import PostgresTestCase, connect

from nest.config import load_controller_config, load_database_config
from nest.db import (
    Database,
    DatabaseError,
    DuplicateMemberError,
    IdentityConflictError,
    IdentityStore,
    JobStore,
    MemberNotFoundError,
    OwnerRuleError,
    Project,
    ProjectNotFoundError,
    ProjectStore,
    UserNotFoundError,
    hash_token,
    load_migrations,
    sync_providers,
    upgrade,
)
from nest.db._transaction import transaction
from nest.errors import DatabaseUnavailableError

GOOGLE = "https://accounts.google.com"
PRIVATE_EMAIL = "pending.invitee@example.org"


def in_hours(hours: float) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


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
        self.projects = ProjectStore(self.db)
        self.conn = connect(self.url)
        self.addCleanup(self.conn.close)

    def user(self, name: str = "u") -> uuid.UUID:
        return self.identities.create_user(display_name=name).id

    def owners(self, project_id) -> list[uuid.UUID]:
        rows = self.conn.execute(
            "SELECT user_id FROM project_memberships WHERE project_id = %s AND role = 'owner'",
            (project_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def run_concurrently(self, *calls):
        """Runs each call in its own thread, started together. Returns results or errors."""
        barrier = threading.Barrier(len(calls))
        results: list[object] = [None] * len(calls)

        def run(index, call):
            barrier.wait()
            try:
                results[index] = call()
            except Exception as exc:  # noqa: BLE001 - returned to the test to assert on
                results[index] = exc

        threads = [threading.Thread(target=run, args=item) for item in enumerate(calls)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results


class SchemaTests(_Base):
    def test_migration_is_part_of_the_one_chain(self):
        labels = [m.label for m in load_migrations()]
        self.assertIn("0003_identity_and_projects", labels)

    def test_user_and_project_keys_are_generated_uuids(self):
        rows = self.conn.execute(
            """
            SELECT c.table_name, c.data_type, c.column_default
            FROM information_schema.columns c
            WHERE c.table_schema = 'public' AND c.column_name = 'id'
              AND c.table_name IN ('users', 'projects', 'organizations')
            ORDER BY c.table_name
            """
        ).fetchall()
        self.assertEqual(
            rows,
            [
                (table, "uuid", "gen_random_uuid()")
                for table in ("organizations", "projects", "users")
            ],
        )

    def test_identity_tables_do_not_reference_provider_accounts(self):
        tables = (
            "users",
            "user_identities",
            "user_sessions",
            "platform_role_assignments",
            "organizations",
            "organization_memberships",
            "projects",
            "project_memberships",
            "project_email_invitations",
            "project_invite_links",
            "project_invitation_redemptions",
        )
        referenced = self.conn.execute(
            """
            SELECT DISTINCT confrelid::regclass::text FROM pg_constraint
            WHERE contype = 'f' AND conrelid::regclass::text = ANY(%s)
            """,
            (list(tables),),
        ).fetchall()
        self.assertNotIn("provider_profiles", {row[0] for row in referenced})
        columns = self.conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = ANY(%s) "
            "AND (column_name LIKE '%%account%%' OR column_name LIKE '%%provider%%')",
            (list(tables),),
        ).fetchall()
        self.assertEqual(columns, [])

    def test_same_display_name_creates_distinct_projects(self):
        owner = self.user()
        first = self.projects.create_project("ACD115190", created_by_user_id=owner)
        second = self.projects.create_project("ACD115190", created_by_user_id=owner)
        self.assertNotEqual(first.id, second.id)
        self.assertIsInstance(first.id, uuid.UUID)


class MembershipTests(_Base):
    def test_users_and_projects_are_many_to_many(self):
        alice, bob, carol = self.user("alice"), self.user("bob"), self.user("carol")
        training = self.projects.create_project("llm-training", created_by_user_id=alice)
        evaluation = self.projects.create_project("model-evaluation", created_by_user_id=bob)
        self.projects.add_member(training.id, bob, "admin", added_by_user_id=alice)
        self.projects.add_member(training.id, carol, "viewer", added_by_user_id=bob)
        self.projects.add_member(evaluation.id, alice, "member", added_by_user_id=bob)

        self.assertEqual(
            [(m.user_id, m.role) for m in self.projects.members(training.id)],
            [(alice, "owner"), (bob, "admin"), (carol, "viewer")],
        )
        self.assertEqual(
            {(p.name, role, p.owner_user_id) for p, role in self.projects.projects_for_user(alice)},
            {("llm-training", "owner", alice), ("model-evaluation", "member", bob)},
        )
        self.assertEqual(self.projects.role_of(evaluation.id, carol), None)

    def test_duplicate_membership_is_refused(self):
        owner, member = self.user(), self.user()
        project = self.projects.create_project("p", created_by_user_id=owner)
        self.projects.add_member(project.id, member, "member")
        for role in ("member", "admin"):
            with self.subTest(role=role), self.assertRaises(DuplicateMemberError):
                self.projects.add_member(project.id, member, role)
        with self.assertRaises(DuplicateMemberError):
            self.projects.add_member(project.id, owner, "viewer")
        # The database refuses a duplicate row even without the store.
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "INSERT INTO project_memberships (project_id, user_id, role) "
                "VALUES (%s, %s, 'viewer')",
                (project.id, member),
            )
        count = self.conn.execute(
            "SELECT count(*) FROM project_memberships WHERE project_id = %s", (project.id,)
        ).fetchone()
        self.assertEqual(count, (2,))

    def test_concurrent_adds_create_one_membership(self):
        owner, member = self.user(), self.user()
        project = self.projects.create_project("p", created_by_user_id=owner)
        results = self.run_concurrently(
            *[lambda: self.projects.add_member(project.id, member, "member")] * 6
        )
        self.assertEqual(sum(not isinstance(r, Exception) for r in results), 1)
        self.assertTrue(
            all(isinstance(r, DuplicateMemberError) for r in results if isinstance(r, Exception))
        )
        self.assertEqual(len(self.projects.members(project.id)), 2)

    def test_roles_are_fixed(self):
        owner, member = self.user(), self.user()
        project = self.projects.create_project("p", created_by_user_id=owner)
        with self.assertRaises(ValueError):
            self.projects.add_member(project.id, member, "superuser")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "INSERT INTO project_memberships (project_id, user_id, role) "
                "VALUES (%s, %s, 'superuser')",
                (project.id, member),
            )

    def test_missing_records_are_reported(self):
        owner = self.user()
        project = self.projects.create_project("p", created_by_user_id=owner)
        with self.assertRaises(UserNotFoundError):
            self.projects.add_member(project.id, uuid.uuid4(), "member")
        with self.assertRaises(UserNotFoundError):
            self.projects.create_project("p", created_by_user_id=uuid.uuid4())
        with self.assertRaises(ProjectNotFoundError):
            self.projects.add_member(uuid.uuid4(), owner, "member")
        with self.assertRaises(MemberNotFoundError):
            self.projects.set_role(project.id, self.user(), "admin")
        with self.assertRaises(ValueError):
            self.projects.role_of("not-a-uuid", owner)

    def test_removed_member_loses_access_immediately(self):
        owner, member = self.user(), self.user()
        project = self.projects.create_project("p", created_by_user_id=owner)
        self.projects.add_member(project.id, member, "admin")
        self.assertEqual(self.projects.role_of(project.id, member), "admin")
        self.projects.set_role(project.id, member, "viewer")
        self.assertEqual(self.projects.role_of(project.id, member), "viewer")
        self.projects.remove_member(project.id, member)
        self.assertIsNone(self.projects.role_of(project.id, member))
        self.assertEqual(self.projects.projects_for_user(member), [])
        with self.assertRaises(MemberNotFoundError):
            self.projects.remove_member(project.id, member)
        # Rejoining later is a fresh membership, not a duplicate.
        self.projects.add_member(project.id, member, "member")
        self.assertEqual(self.projects.role_of(project.id, member), "member")

    def test_membership_changes_join_the_callers_transaction(self):
        owner, member = self.user(), self.user()
        project = self.projects.create_project("p", created_by_user_id=owner)
        with self.assertRaises(RuntimeError), self.db.connection() as conn:
            self.projects.add_member(project.id, member, "member", conn=conn)
            raise RuntimeError("caller failed after adding the member")
        self.assertIsNone(self.projects.role_of(project.id, member))

    def test_autocommit_connection_gets_one_transaction_per_call(self):
        owner = self.user()
        # self.conn is in autocommit mode: the Project and its Owner must still be
        # written in one transaction, or the deferred Owner check would fail.
        project = self.projects.create_project("p", created_by_user_id=owner, conn=self.conn)
        self.assertEqual(self.owners(project.id), [owner])


class OwnershipTests(_Base):
    def setUp(self):
        super().setUp()
        self.alice, self.bob, self.carol = self.user("alice"), self.user("bob"), self.user("carol")
        self.project = self.projects.create_project("shared", created_by_user_id=self.alice)
        self.projects.add_member(self.project.id, self.bob, "admin")
        self.projects.add_member(self.project.id, self.carol, "member")

    def test_creator_starts_as_the_only_owner(self):
        self.assertEqual(self.project.owner_user_id, self.alice)
        self.assertEqual(self.project.created_by_user_id, self.alice)
        self.assertEqual(self.owners(self.project.id), [self.alice])

    def test_owner_is_never_granted_removed_or_demoted_directly(self):
        pid = self.project.id
        with self.assertRaises(OwnerRuleError):
            self.projects.set_role(pid, self.bob, "owner")
        with self.assertRaises(OwnerRuleError):
            self.projects.add_member(pid, self.user(), "owner")
        with self.assertRaises(OwnerRuleError):
            self.projects.set_role(pid, self.alice, "admin")
        with self.assertRaises(OwnerRuleError):
            self.projects.remove_member(pid, self.alice)
        self.assertEqual(self.owners(pid), [self.alice])

    def test_only_the_owner_can_transfer_or_delete(self):
        pid = self.project.id
        with self.assertRaises(OwnerRuleError):
            self.projects.transfer_ownership(
                pid, actor_user_id=self.bob, new_owner_user_id=self.bob
            )
        with self.assertRaises(OwnerRuleError):
            self.projects.delete_project(pid, actor_user_id=self.bob)
        with self.assertRaises(MemberNotFoundError):
            self.projects.transfer_ownership(
                pid, actor_user_id=self.alice, new_owner_user_id=self.user()
            )
        with self.assertRaises(OwnerRuleError):
            self.projects.transfer_ownership(
                pid, actor_user_id=self.alice, new_owner_user_id=self.alice
            )
        self.assertEqual(self.owners(pid), [self.alice])

    def test_transfer_keeps_the_creator_and_demotes_the_previous_owner(self):
        pid = self.project.id
        project = self.projects.transfer_ownership(
            pid, actor_user_id=self.alice, new_owner_user_id=self.carol
        )
        self.assertEqual(
            (project.owner_user_id, project.created_by_user_id), (self.carol, self.alice)
        )
        self.assertEqual(self.projects.role_of(pid, self.alice), "admin")
        self.projects.transfer_ownership(
            pid,
            actor_user_id=self.carol,
            new_owner_user_id=self.bob,
            previous_owner_role="viewer",
        )
        self.assertEqual(self.projects.role_of(pid, self.carol), "viewer")
        self.assertEqual(self.owners(pid), [self.bob])
        # The former Owner, now an Admin, can be removed; the creator field remains.
        self.projects.remove_member(pid, self.alice)
        project = self.projects.get_project(pid)
        assert project is not None
        self.assertEqual(project.created_by_user_id, self.alice)
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE projects SET created_by_user_id = %s WHERE id = %s", (self.bob, pid)
            )

    def test_database_refuses_a_project_without_exactly_one_owner(self):
        pid = self.project.id
        # A second Owner fails at once.
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "UPDATE project_memberships SET role = 'owner' WHERE project_id = %s "
                "AND user_id = %s",
                (pid, self.bob),
            )
        # Removing or demoting the Owner fails when the transaction commits.
        for statement in (
            "DELETE FROM project_memberships WHERE project_id = %s AND role = 'owner'",
            (
                "UPDATE project_memberships SET role = 'admin' WHERE project_id = %s "
                "AND role = 'owner'"
            ),
        ):
            with self.subTest(statement=statement):
                with (
                    self.assertRaises(psycopg.errors.IntegrityConstraintViolation) as caught,
                    self.conn.transaction(),
                ):
                    self.conn.execute(statement, (pid,))
                self.assertIn("exactly one owner", str(caught.exception))
        # A Project inserted without its Owner membership is refused too.
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "INSERT INTO projects (name, created_by_user_id) VALUES ('orphan', %s)",
                (self.alice,),
            )
        self.assertEqual(self.owners(pid), [self.alice])

    def test_concurrent_transfers_leave_exactly_one_owner(self):
        pid = self.project.id
        results = self.run_concurrently(
            lambda: self.projects.transfer_ownership(
                pid, actor_user_id=self.alice, new_owner_user_id=self.bob
            ),
            lambda: self.projects.transfer_ownership(
                pid, actor_user_id=self.alice, new_owner_user_id=self.carol
            ),
        )
        failures = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(failures), 1, results)
        self.assertIsInstance(failures[0], OwnerRuleError)
        winner = next(r for r in results if isinstance(r, Project))
        self.assertEqual(self.owners(pid), [winner.owner_user_id])

    def test_concurrent_transfer_and_removal_of_the_target(self):
        pid = self.project.id
        results = self.run_concurrently(
            lambda: self.projects.transfer_ownership(
                pid, actor_user_id=self.alice, new_owner_user_id=self.carol
            ),
            lambda: self.projects.remove_member(pid, self.carol),
        )
        transfer, removal = results
        owners = self.owners(pid)
        self.assertEqual(len(owners), 1)
        if isinstance(transfer, Exception):
            # The removal ran first, so the target was gone.
            self.assertIsInstance(transfer, MemberNotFoundError)
            self.assertIsNone(removal)
            self.assertEqual(owners, [self.alice])
        else:
            # The transfer ran first, so the removal found an Owner.
            self.assertIsInstance(removal, OwnerRuleError)
            self.assertEqual(owners, [self.carol])

    def test_many_concurrent_ownership_changes_never_break_the_invariant(self):
        pid = self.project.id
        members = [self.alice, self.bob, self.carol] + [self.user() for _ in range(3)]
        for extra in members[3:]:
            self.projects.add_member(pid, extra, "member")

        def churn(seed: int):
            outcomes = []
            for step in range(6):
                current = self.projects.get_project(pid)
                assert current is not None
                target = members[(seed + step) % len(members)]
                try:
                    if (seed + step) % 3 == 0:
                        self.projects.remove_member(pid, target)
                        self.projects.add_member(pid, target, "member")
                    else:
                        self.projects.transfer_ownership(
                            pid, actor_user_id=current.owner_user_id, new_owner_user_id=target
                        )
                    outcomes.append("ok")
                except OwnerRuleError, MemberNotFoundError, DuplicateMemberError:
                    outcomes.append("refused")
            return outcomes

        results = self.run_concurrently(*[lambda s=s: churn(s) for s in range(6)])
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        self.assertEqual(len(self.owners(pid)), 1)

    def test_database_alone_stops_racing_raw_transfers(self):
        """Two writers that bypass the store and its lock still cannot create two Owners."""
        pid = self.project.id
        demote = (
            "UPDATE project_memberships SET role = 'admin' WHERE project_id = %s AND user_id = %s"
        )
        promote = (
            "UPDATE project_memberships SET role = 'owner' WHERE project_id = %s AND user_id = %s"
        )
        first = connect(self.url)
        self.addCleanup(first.close)
        second_error: list[BaseException] = []
        with first.transaction():
            first.execute(demote, (pid, self.alice))
            first.execute(promote, (pid, self.bob))

            def second():
                try:
                    with connect(self.url) as conn, conn.transaction():
                        conn.execute(demote, (pid, self.alice))
                        conn.execute(promote, (pid, self.carol))
                except psycopg.Error as exc:
                    second_error.append(exc)

            thread = threading.Thread(target=second)
            thread.start()
            time.sleep(0.5)  # The second writer now waits on the first one's row lock.
            self.assertTrue(thread.is_alive())
        thread.join(timeout=30)
        self.assertEqual(len(second_error), 1)
        self.assertIsInstance(second_error[0], psycopg.errors.UniqueViolation)
        self.assertEqual(self.owners(pid), [self.bob])

    def test_database_alone_stops_removing_a_new_owner(self):
        """A raw removal that waited for a transfer cannot delete the new Owner."""
        pid = self.project.id
        holder = connect(self.url)
        self.addCleanup(holder.close)
        errors: list[BaseException] = []
        with holder.transaction():
            holder.execute(
                "UPDATE project_memberships SET role = 'admin' WHERE project_id = %s "
                "AND user_id = %s",
                (pid, self.alice),
            )
            holder.execute(
                "UPDATE project_memberships SET role = 'owner' WHERE project_id = %s "
                "AND user_id = %s",
                (pid, self.carol),
            )

            def remove():
                try:
                    with connect(self.url) as conn, conn.transaction():
                        conn.execute(
                            "DELETE FROM project_memberships WHERE project_id = %s "
                            "AND user_id = %s",
                            (pid, self.carol),
                        )
                except psycopg.Error as exc:
                    errors.append(exc)

            thread = threading.Thread(target=remove)
            thread.start()
            time.sleep(0.5)
            self.assertTrue(thread.is_alive())
        thread.join(timeout=30)
        self.assertEqual(len(errors), 1)
        self.assertIn("exactly one owner", str(errors[0]))
        self.assertEqual(self.owners(pid), [self.carol])

    def test_deleted_project_is_hidden_and_frozen(self):
        pid = self.project.id
        deleted = self.projects.delete_project(pid, actor_user_id=self.alice)
        self.assertIsNotNone(deleted.deleted_at)
        self.assertEqual(deleted.deleted_by_user_id, self.alice)
        self.assertIsNone(self.projects.get_project(pid))
        self.assertIsNotNone(self.projects.get_project(pid, include_deleted=True))
        for user in (self.alice, self.bob):
            self.assertIsNone(self.projects.role_of(pid, user))
            self.assertEqual(self.projects.projects_for_user(user), [])
        with self.assertRaises(ProjectNotFoundError):
            self.projects.add_member(pid, self.user(), "member")
        with self.assertRaises(ProjectNotFoundError):
            self.projects.transfer_ownership(
                pid, actor_user_id=self.alice, new_owner_user_id=self.bob
            )
        with self.assertRaises(ProjectNotFoundError):
            self.projects.delete_project(pid, actor_user_id=self.alice)

    def test_concurrent_delete_and_transfer(self):
        pid = self.project.id
        delete, transfer = self.run_concurrently(
            lambda: self.projects.delete_project(pid, actor_user_id=self.alice),
            lambda: self.projects.transfer_ownership(
                pid, actor_user_id=self.alice, new_owner_user_id=self.bob
            ),
        )
        # Whichever runs second sees the first one's result.
        if isinstance(delete, Exception):
            self.assertIsInstance(delete, OwnerRuleError)
            self.assertEqual(self.owners(pid), [self.bob])
        else:
            self.assertIsInstance(transfer, ProjectNotFoundError)
            self.assertEqual(self.owners(pid), [self.alice])


class AttributionTests(_Base):
    def setUp(self):
        super().setUp()
        config = load_controller_config(
            {"NEST_SLURM_HOST": "hpc", "NEST_SLURM_ACCOUNT": "acct", "NEST_SLURM_WORKDIR": "/w"}
        )
        sync_providers(self.db, config.providers, config.default_provider)
        self.jobs = JobStore(self.db, "default")

    def test_creator_owner_and_submitter_are_independent(self):
        alice, bob = self.user("alice"), self.user("bob")
        project = self.projects.create_project("p", created_by_user_id=alice)
        self.projects.add_member(project.id, bob, "member")
        record = self.jobs.begin("batch", "d" * 32, project_id=project.id, submitted_by_user_id=bob)
        self.projects.transfer_ownership(project.id, actor_user_id=alice, new_owner_user_id=bob)
        self.projects.transfer_ownership(project.id, actor_user_id=bob, new_owner_user_id=alice)
        self.projects.set_role(project.id, bob, "viewer")
        self.projects.remove_member(project.id, bob)
        self.jobs.finish(record, "succeeded")

        row = self.jobs.get(record)
        assert row is not None
        self.assertEqual((row["project_id"], row["submitted_by_user_id"]), (project.id, bob))
        current = self.projects.get_project(project.id)
        assert current is not None
        self.assertEqual((current.created_by_user_id, current.owner_user_id), (alice, alice))

    def test_submitter_and_project_are_immutable(self):
        alice, bob = self.user(), self.user()
        project = self.projects.create_project("p", created_by_user_id=alice)
        record = self.jobs.begin(
            "batch", "e" * 32, project_id=project.id, submitted_by_user_id=alice
        )
        unattributed = self.jobs.begin("batch", "f" * 32)
        for column, value, row_id in (
            ("submitted_by_user_id", bob, record),
            ("project_id", None, record),
            ("execution_id", "a" * 32, record),
            ("submitted_by_user_id", bob, unattributed),
        ):
            with (
                self.subTest(column=column, row=row_id),
                self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            ):
                self.conn.execute(f"UPDATE jobs SET {column} = %s WHERE id = %s", (value, row_id))

    def test_project_and_submitter_are_recorded_together(self):
        alice = self.user()
        project = self.projects.create_project("p", created_by_user_id=alice)
        with self.assertRaises(DatabaseUnavailableError):
            self.jobs.begin("batch", "1" * 32, project_id=project.id)
        with self.assertRaises(DatabaseUnavailableError):
            self.jobs.begin("batch", "2" * 32, submitted_by_user_id=alice)

    def test_attributed_records_keep_their_project_and_submitter(self):
        alice = self.user()
        project = self.projects.create_project("p", created_by_user_id=alice)
        self.jobs.begin("batch", "3" * 32, project_id=project.id, submitted_by_user_id=alice)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), self.conn.transaction():
            self.conn.execute("DELETE FROM projects WHERE id = %s", (project.id,))
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), self.conn.transaction():
            self.conn.execute("DELETE FROM users WHERE id = %s", (alice,))


class IdentityTests(_Base):
    def test_identity_is_the_stable_subject_not_the_email(self):
        alice = self.user()
        self.identities.link_identity(
            alice, issuer=GOOGLE, subject="1001", email="a@example.com", email_verified=True
        )
        # The same subject with a new address is the same User.
        identity = self.identities.link_identity(
            alice, issuer=GOOGLE, subject="1001", email="a.new@example.com", email_verified=True
        )
        self.assertEqual(identity.email, "a.new@example.com")
        found = self.identities.find_user(issuer=GOOGLE, subject="1001")
        assert found is not None
        self.assertEqual(found.id, alice)
        # Another subject reusing the old address is not that User.
        self.assertIsNone(self.identities.find_user(issuer=GOOGLE, subject="1002"))
        # The same subject string under another issuer is another identity.
        self.assertIsNone(self.identities.find_user(issuer="https://idp.example", subject="1001"))
        self.assertEqual(len(self.identities.identities(alice)), 1)

    def test_identity_cannot_move_to_another_user(self):
        alice, mallory = self.user(), self.user()
        self.identities.link_identity(alice, issuer=GOOGLE, subject="1001")
        with self.assertRaises(IdentityConflictError):
            self.identities.link_identity(mallory, issuer=GOOGLE, subject="1001")
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE user_identities SET user_id = %s WHERE subject = '1001'", (mallory,)
            )
        found = self.identities.find_user(issuer=GOOGLE, subject="1001")
        assert found is not None
        self.assertEqual(found.id, alice)

    def test_concurrent_first_logins_link_once(self):
        alice = self.user()
        results = self.run_concurrently(
            *[lambda: self.identities.link_identity(alice, issuer=GOOGLE, subject="7")] * 5
        )
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        count = self.conn.execute("SELECT count(*) FROM user_identities").fetchone()
        self.assertEqual(count, (1,))

    def test_sessions_are_persisted_and_revocable(self):
        alice = self.user()
        token = hash_token("browser-secret")
        session = self.identities.create_session(
            alice, kind="browser", token_hash=token, expires_at=in_hours(1)
        )
        active = self.identities.active_session(token)
        assert active is not None
        self.assertEqual((active.id, active.user_id, active.kind), (session.id, alice, "browser"))
        self.assertIsNone(self.identities.active_session(hash_token("other")))
        self.assertTrue(self.identities.revoke_session(session.id))
        self.assertFalse(self.identities.revoke_session(session.id))
        self.assertIsNone(self.identities.active_session(token))
        stored = self.conn.execute(
            "SELECT token_hash, revoked_reason FROM user_sessions WHERE id = %s", (session.id,)
        ).fetchone()
        self.assertEqual(stored, (token, "logout"))

    def test_expired_sessions_are_rejected(self):
        alice = self.user()
        token = hash_token("cli-secret")
        session = self.identities.create_session(
            alice, kind="cli", token_hash=token, expires_at=in_hours(1)
        )
        self.conn.execute(
            "UPDATE user_sessions SET created_at = now() - interval '2 hours', "
            "expires_at = now() - interval '1 second' WHERE id = %s",
            (session.id,),
        )
        self.assertIsNone(self.identities.active_session(token))
        with self.assertRaises(ValueError):
            self.identities.create_session(
                alice, kind="cli", token_hash=b"short", expires_at=in_hours(1)
            )

    def test_revoking_or_disabling_a_user_ends_every_session(self):
        alice = self.user()
        tokens = [hash_token(f"s{i}") for i in range(3)]
        for kind, token in zip(("browser", "cli", "cli"), tokens, strict=True):
            self.identities.create_session(
                alice, kind=kind, token_hash=token, expires_at=in_hours(1)
            )
        first = self.identities.active_session(tokens[0])
        assert first is not None
        self.identities.revoke_session(first.id)
        self.assertEqual(self.identities.revoke_user_sessions(alice), 2)
        self.assertTrue(all(self.identities.active_session(t) is None for t in tokens))
        late = hash_token("late")
        self.identities.create_session(
            alice, kind="browser", token_hash=late, expires_at=in_hours(1)
        )
        disabled = self.identities.set_user_status(alice, "disabled")
        self.assertIsNotNone(disabled.disabled_at)
        self.assertIsNone(self.identities.active_session(late))
        self.identities.set_user_status(alice, "active")
        self.assertIsNone(self.identities.active_session(late))

    def test_platform_and_organization_roles_are_persisted(self):
        alice, bob = self.user(), self.user()
        self.assertTrue(self.identities.grant_platform_role(alice))
        self.assertFalse(self.identities.grant_platform_role(alice))
        self.assertEqual(self.identities.platform_roles(alice), {"platform_admin"})
        self.assertEqual(self.identities.platform_roles(bob), set())
        # A platform role is not a Project role.
        project = self.projects.create_project("p", created_by_user_id=bob)
        self.assertIsNone(self.projects.role_of(project.id, alice))
        self.assertTrue(self.identities.revoke_platform_role(alice))
        self.assertEqual(self.identities.platform_roles(alice), set())

        org = self.identities.create_organization("NCHC users", created_by_user_id=alice)
        self.assertEqual(self.identities.organization_role(org, alice), "org_admin")
        self.identities.set_organization_role(org, bob, "member")
        self.assertEqual(self.identities.organization_role(org, bob), "member")
        self.identities.set_organization_role(org, bob, "org_admin")
        self.assertEqual(self.identities.organization_role(org, bob), "org_admin")
        self.assertTrue(self.identities.remove_organization_member(org, bob))
        self.assertIsNone(self.identities.organization_role(org, bob))
        filed = self.projects.create_project("p", created_by_user_id=alice, organization_id=org)
        self.assertEqual(filed.organization_id, org)
        # An Organization role grants no Project role.
        self.assertIsNone(self.projects.role_of(project.id, alice))


class InvitationSchemaTests(_Base):
    """Constraints B08's invitation flows rely on. The flows themselves are B08."""

    def setUp(self):
        super().setUp()
        self.owner = self.user()
        self.project = self.projects.create_project("p", created_by_user_id=self.owner)

    def invite(self, email: str = PRIVATE_EMAIL, role: str = "member", **columns):
        values = {
            "project_id": self.project.id,
            "invited_email_normalized": email,
            "role": role,
            "invited_by_user_id": self.owner,
            "expires_at": in_hours(24),
            **columns,
        }
        names = ", ".join(values)
        marks = ", ".join(["%s"] * len(values))
        row = self.conn.execute(
            f"INSERT INTO project_email_invitations ({names}) VALUES ({marks}) RETURNING id",
            list(values.values()),
        ).fetchone()
        assert row is not None
        return row[0]

    def link(self, max_uses: int = 3, role: str = "member") -> int:
        row = self.conn.execute(
            "INSERT INTO project_invite_links (project_id, role, created_by_user_id, token_hash, "
            "expires_at, max_uses) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                self.project.id,
                role,
                self.owner,
                hash_token(uuid.uuid4().hex),
                in_hours(24),
                max_uses,
            ),
        ).fetchone()
        assert row is not None
        return row[0]

    def test_pending_invitation_needs_no_user_and_is_not_a_membership(self):
        self.invite(token_hash=hash_token("recipient-bound"))
        users = self.conn.execute("SELECT count(*) FROM users").fetchone()
        self.assertEqual(users, (1,))
        self.assertEqual(len(self.projects.members(self.project.id)), 1)

    def test_invitations_cannot_grant_owner(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.invite(role="owner")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.link(role="owner")

    def test_one_pending_invitation_per_address(self):
        first = self.invite()
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.invite()
        self.conn.execute(
            "UPDATE project_email_invitations SET status = 'revoked', revoked_at = now(), "
            "revoked_by_user_id = %s WHERE id = %s",
            (self.owner, first),
        )
        self.invite()  # A new invitation once the old one is closed.

    def test_invitation_status_matches_its_timestamps(self):
        invitation = self.invite()
        invitee = self.user()
        for statement, params in (
            ("UPDATE project_email_invitations SET status = 'accepted' WHERE id = %s", ()),
            ("UPDATE project_email_invitations SET status = 'revoked' WHERE id = %s", ()),
            (
                (
                    "UPDATE project_email_invitations SET accepted_at = now(), "
                    "accepted_by_user_id = %s WHERE id = %s"
                ),
                (invitee,),
            ),
        ):
            with (
                self.subTest(statement=statement),
                self.assertRaises(psycopg.errors.CheckViolation),
            ):
                self.conn.execute(statement, (*params, invitation))
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE project_email_invitations SET role = 'admin' WHERE id = %s",
                (invitation,),
            )

    def test_invitation_rows_never_appear_in_database_errors(self):
        self.invite()
        with (
            self.assertRaises(DatabaseError) as caught,
            self.db.connection() as conn,
            transaction(self.db, conn, "test") as cursor,
        ):
            cursor.execute(
                "INSERT INTO project_email_invitations (project_id, "
                "invited_email_normalized, role, invited_by_user_id, expires_at) "
                "VALUES (%s, %s, 'member', %s, now() + interval '1 day')",
                (self.project.id, PRIVATE_EMAIL, self.owner),
            )
        self.assertNotIn(PRIVATE_EMAIL, str(caught.exception))
        self.assertIn("project_email_invitations_one_pending", str(caught.exception))

    def test_link_uses_are_bounded_under_concurrency(self):
        link = self.link(max_uses=3)
        users = [self.user() for _ in range(8)]

        def redeem(user_id):
            with connect(self.url) as conn, conn.transaction():
                row = conn.execute(
                    "UPDATE project_invite_links SET use_count = use_count + 1 WHERE id = %s "
                    "AND use_count < max_uses AND revoked_at IS NULL RETURNING use_count",
                    (link,),
                ).fetchone()
                if row is None:
                    return "exhausted"
                conn.execute(
                    "INSERT INTO project_invitation_redemptions (invite_link_id, user_id) "
                    "VALUES (%s, %s)",
                    (link, user_id),
                )
                return "redeemed"

        results = self.run_concurrently(*[lambda u=u: redeem(u) for u in users])
        self.assertEqual(sorted(map(str, results)), ["exhausted"] * 5 + ["redeemed"] * 3)
        counts = self.conn.execute(
            "SELECT use_count, (SELECT count(*) FROM project_invitation_redemptions) "
            "FROM project_invite_links WHERE id = %s",
            (link,),
        ).fetchone()
        self.assertEqual(counts, (3, 3))
        # An unguarded increment past the limit is refused by the table itself.
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "UPDATE project_invite_links SET use_count = use_count + 1 WHERE id = %s",
                (link,),
            )

    def test_one_redemption_per_user_and_revocation_is_recorded(self):
        link = self.link()
        user = self.user()
        self.conn.execute(
            "INSERT INTO project_invitation_redemptions (invite_link_id, user_id) VALUES (%s, %s)",
            (link, user),
        )
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "INSERT INTO project_invitation_redemptions (invite_link_id, user_id) "
                "VALUES (%s, %s)",
                (link, user),
            )
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "UPDATE project_invite_links SET revoked_at = now() WHERE id = %s", (link,)
            )
        self.conn.execute(
            "UPDATE project_invite_links SET revoked_at = now(), revoked_by_user_id = %s "
            "WHERE id = %s",
            (self.owner, link),
        )
        exhausted = self.conn.execute(
            "UPDATE project_invite_links SET use_count = use_count + 1 WHERE id = %s "
            "AND use_count < max_uses AND revoked_at IS NULL",
            (link,),
        )
        self.assertEqual(exhausted.rowcount, 0)
