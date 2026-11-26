"""Platform-aware durable execution registry (C01).

Every test runs against a real PostgreSQL named by `NEST_TEST_DATABASE_URL` (see
`postgres_fixture`). Restarts are simulated by closing the connection pool and
opening a new one on the same database, so only what was committed survives.
"""

import threading
import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import psycopg
from postgres_fixture import PostgresTestCase, connect

from nest.config import load_database_config
from nest.db import (
    EXECUTION_AUTHORITIES,
    AccountNotPermittedError,
    AttemptInFlightError,
    AuthorityPin,
    Database,
    DatabaseError,
    EndpointNotApprovedError,
    ExecutionClosedError,
    ExecutionNotFoundError,
    ExecutionRegistry,
    HPCAccessStore,
    HPCPrincipal,
    IdempotencyConflictError,
    IdentityStore,
    InvalidTransitionError,
    PlatformStore,
    PlatformUnavailableError,
    ProjectStore,
    ResolvedOption,
    SlurmAccountStore,
    load_migrations,
    upgrade,
    verify,
)
from nest.db.platforms import endpoint_for

DIGEST = "sha256:" + "ab" * 32
PIN = "SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
OPS: dict[str, Any] = {"operator": "ops-test", "reason": "execution registry tests"}
# The owner's own HPC identity: a user_owned attempt (D01) under a verified binding (D02).
USER_OWNED = AuthorityPin("user_owned", 1, 1)
# Columns a raw attempt INSERT needs since 0019 for a controller_owned attempt.
CONTROLLER_PIN_COLUMNS = (
    ", execution_authority, platform_authority_policy_version, project_authority_policy_version"
)
CONTROLLER_PIN_VALUES = ", 'controller_owned', 1, 1"
# An attempt recorded at schema 0014, before bindings existed (no authority pin).
ALICE = HPCPrincipal("alice", 1001, binding_id="binding-1", binding_version=3)


class _Base(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = self.open_database()
        self.registry = ExecutionRegistry(self.db)
        identities = IdentityStore(self.db)
        projects = ProjectStore(self.db)
        self.projects = projects
        self.owner = identities.create_user(display_name="owner").id
        self.other_user = identities.create_user(display_name="other").id
        self.project = projects.create_project("p", created_by_user_id=self.owner).id
        self.other_project = projects.create_project("q", created_by_user_id=self.owner).id
        # Two Platforms of the D07 catalog, both granted to self.project.
        platforms = PlatformStore(self.db)
        self.platforms = platforms
        self.alpha = platforms.create_platform("alpha", "Alpha", **OPS)
        self.beta = platforms.create_platform("beta", "Beta", **OPS)
        a1 = platforms.add_login_node(
            self.alpha.id, "login1.alpha.example", transport_modes=["A"], **OPS
        )
        a2 = platforms.add_login_node(
            self.alpha.id,
            "login2.alpha.example",
            port=2222,
            transport_modes=["A", "B"],
            host_key_pin_ref=PIN,
            **OPS,
        )
        b1 = platforms.add_login_node(
            self.beta.id, "login.beta.example", transport_modes=["B"], host_key_pin_ref=PIN, **OPS
        )
        for platform in (self.alpha, self.beta):
            platforms.grant_project(platform.id, self.project, **OPS)
        # D08 accounts: "proj" on alpha only; "shared" exists on both Platforms.
        accounts = SlurmAccountStore(self.db)
        self.accounts = accounts
        self.account_ids = {}
        for platform, name, modes in (
            (self.alpha, "proj", ["A", "B"]),
            (self.alpha, "shared", ["A", "B"]),
            (self.beta, "shared", ["B"]),
        ):
            registered = accounts.register_account(platform.id, name, **OPS)
            accounts.link_project(self.project, platform.id, name, modes=modes, **OPS)
            self.account_ids[platform.id, name] = registered.id
        # Both authorities permitted for both Projects, and the owner's verified
        # binding to UNIX user "alice" (uid 1001) on each Platform.
        access = HPCAccessStore(self.db)
        self.bindings = {}
        for platform in (self.alpha, self.beta):
            access.set_platform_policy(platform.id, EXECUTION_AUTHORITIES, **OPS)
            for project in (self.project, self.other_project):
                access.set_project_policy(project, platform.id, EXECUTION_AUTHORITIES, **OPS)
            self.bindings[platform.id] = access.attest(
                self.owner,
                platform.id,
                "alice",
                method="operator_attestation",
                evidence_ref="TICKET-1",
                unix_uid=1001,
                **OPS,
            )
        self.A1 = endpoint_for(self.alpha, a1, "A")
        self.A2 = endpoint_for(self.alpha, a2, "B")
        self.B1 = endpoint_for(self.beta, b1, "B")
        self.conn = connect(self.url)
        self.conn.autocommit = True
        self.addCleanup(self.conn.close)

    def open_database(self) -> Database:
        db = Database(
            load_database_config(
                {"NEST_DATABASE_URL": self.url, "NEST_DATABASE_POOL_MAX_SIZE": "4"}
            )
        )
        db.open()
        self.addCleanup(db.close)
        return db

    def register(self, key=None, options=None, project=None):
        return self.registry.register(
            project or self.project,
            self.owner,
            "execution",
            options if options is not None else {"gpu": 1},
            idempotency_key=key,
        )

    def account(self, endpoint, name="proj", origin="explicit"):
        """The `job_fields()` of an account resolution for `name` on the endpoint's Platform."""
        return {
            "account": name,
            "slurm_account_id": self.account_ids.get((endpoint.platform_id, name)),
            "slurm_account_origin": origin,
        }

    def alice(self, endpoint=None):
        """The owner's verified HPC identity on the endpoint's Platform."""
        binding = self.bindings[(endpoint or self.A1).platform_id]
        return HPCPrincipal("alice", 1001, binding_id=str(binding.id), binding_version=1)

    def begin(self, execution_id, endpoint=None, account="proj", **kw):
        endpoint = endpoint or self.A1
        return self.registry.begin_attempt(
            execution_id,
            endpoint=endpoint,
            principal=kw.pop("principal", self.alice(endpoint)),
            authority=kw.pop("authority", USER_OWNED),
            **kw.pop("account_fields", self.account(endpoint, account)),
            options=kw.pop(
                "options",
                {
                    "gpu": ResolvedOption.requested(1, default=0, default_source="platform"),
                    "partition": ResolvedOption.defaulted("gpu", "platform"),
                },
            ),
            source_digest=DIGEST,
            environment_digest=DIGEST,
            runtime_digest=DIGEST,
            **kw,
        )


class RegistrationTests(_Base):
    def test_execution_ids_are_stable_and_scoped_to_their_project(self):
        execution, created = self.register()
        self.assertTrue(created)
        self.assertRegex(execution.execution_id, "^[0-9a-f]{32}$")
        self.assertEqual(execution.status, "pending")
        self.assertEqual(execution.requested_options, {"gpu": 1})
        self.assertEqual(
            self.registry.get(execution.execution_id, project_id=self.project), execution
        )
        # Another Project never sees it, by any read.
        self.assertIsNone(self.registry.get(execution.execution_id, project_id=self.other_project))
        self.assertEqual(
            self.registry.attempts(execution.execution_id, project_id=self.other_project), []
        )
        self.assertEqual(self.registry.list_for_project(self.other_project), [])
        self.assertEqual(self.registry.list_for_project(self.project), [execution])

    def test_duplicate_registration_with_a_key_returns_the_same_execution(self):
        first, created = self.register(key="client-req-1")
        again, created_again = self.register(key="client-req-1")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(again.execution_id, first.execution_id)
        # The same key in another Project, or by another owner, is a new execution.
        elsewhere, _ = self.register(key="client-req-1", project=self.other_project)
        self.assertNotEqual(elsewhere.execution_id, first.execution_id)
        by_other, _ = self.registry.register(
            self.project, self.other_user, "execution", {"gpu": 1}, idempotency_key="client-req-1"
        )
        self.assertNotEqual(by_other.execution_id, first.execution_id)

    def test_reusing_a_key_for_a_different_request_is_refused(self):
        self.register(key="k", options={"gpu": 1})
        with self.assertRaises(IdempotencyConflictError):
            self.register(key="k", options={"gpu": 2})
        count = self.conn.execute("SELECT count(*) FROM executions").fetchone()
        self.assertEqual(count, (1,))

    def test_concurrent_duplicate_registrations_create_one_execution(self):
        results, errors = [], []
        barrier = threading.Barrier(4)

        def run():
            barrier.wait()
            try:
                results.append(self.register(key="race"))
            except (DatabaseError, IdempotencyConflictError) as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({execution.execution_id for execution, _ in results}), 1)
        self.assertEqual(sum(created for _, created in results), 1)

    def test_registration_survives_a_controller_restart(self):
        execution, _ = self.register(key="restart")
        attempt = self.begin(execution.execution_id)
        self.db.close()
        restarted = ExecutionRegistry(self.open_database())
        again, created = restarted.register(
            self.project, self.owner, "execution", {"gpu": 1}, idempotency_key="restart"
        )
        self.assertFalse(created)
        self.assertEqual(again.execution_id, execution.execution_id)
        self.assertEqual(again.status, "active")
        live = restarted.live_attempts()
        self.assertEqual([a.id for a in live], [attempt.id])
        self.assertEqual(live[0].options, attempt.options)


class AttemptTests(_Base):
    def test_attempt_is_durable_before_submission_with_its_platform_snapshot(self):
        execution, _ = self.register()
        attempt = self.begin(execution.execution_id)
        # Committed: visible from an unrelated connection before any submission.
        row = self.conn.execute(
            "SELECT status, platform_id, login_node_host, login_node_port, transport_mode, "
            "login_node_id, unix_user, unix_uid, hpc_binding_id, hpc_binding_version, "
            "slurm_account, slurm_account_id, slurm_account_origin "
            "FROM execution_attempts WHERE id = %s",
            (attempt.id,),
        ).fetchone()
        self.assertEqual(
            row,
            (
                "submitting",
                self.alpha.id,
                "login1.alpha.example",
                None,
                "A",
                self.A1.node_id,
                "alice",
                1001,
                str(self.bindings[self.alpha.id].id),
                1,
                "proj",
                self.account_ids[self.alpha.id, "proj"],
                "explicit",
            ),
        )
        self.assertEqual(attempt.attempt_number, 1)
        self.assertEqual(attempt.endpoint, self.A1)
        self.assertEqual(attempt.platform_id, self.alpha.id)
        self.assertEqual(attempt.principal, self.alice())
        self.assertEqual(attempt.authority, USER_OWNED)

    def test_resolved_options_keep_value_and_provenance(self):
        execution, _ = self.register()
        attempt = self.begin(execution.execution_id)
        [loaded] = self.registry.attempts(execution.execution_id, project_id=self.project)
        self.assertEqual(loaded.options, attempt.options)
        self.assertEqual(
            loaded.options["gpu"],
            ResolvedOption(1, "request", default_value=0, default_source="platform"),
        )
        self.assertEqual(
            loaded.options["partition"],
            ResolvedOption("gpu", "default", default_value="gpu", default_source="platform"),
        )
        # History is append-only.
        for statement in (
            "UPDATE execution_attempt_options SET value = '2' WHERE name = 'gpu'",
            "DELETE FROM execution_attempt_options",
        ):
            with (
                self.subTest(statement=statement),
                self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            ):
                self.conn.execute(statement)
        # A defaulted option must carry its source and equal its default.
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "INSERT INTO execution_attempt_options (attempt_id, name, value, origin) "
                "VALUES (%s, 'cpu', '4', 'default')",
                (attempt.id,),
            )

    def test_retry_creates_a_new_attempt_on_another_login_node(self):
        execution, _ = self.register()
        first = self.begin(execution.execution_id)
        with self.assertRaises(AttemptInFlightError):
            self.begin(execution.execution_id, endpoint=self.A2)
        self.registry.finish_attempt(first.id, "failed", error_kind="ssh_unreachable")
        failed = self.registry.get(execution.execution_id, project_id=self.project)
        assert failed is not None
        self.assertEqual(failed.status, "failed")
        second = self.begin(execution.execution_id, endpoint=self.A2)
        self.assertEqual(second.attempt_number, 2)
        attempts = self.registry.attempts(execution.execution_id, project_id=self.project)
        self.assertEqual(
            [(a.attempt_number, a.status, a.endpoint.host) for a in attempts],
            [(1, "failed", "login1.alpha.example"), (2, "submitting", "login2.alpha.example")],
        )
        self.assertEqual(attempts[0].error_kind, "ssh_unreachable")

    def test_full_lifecycle_records_slurm_ids_nodes_and_references(self):
        execution, _ = self.register()
        attempt = self.begin(execution.execution_id)
        submitted = self.registry.record_submission(attempt.id, 4242, slurm_state="PENDING")
        self.assertEqual((submitted.status, submitted.slurm_job_id), ("submitted", 4242))
        self.assertIsNotNone(submitted.submitted_at)
        running = self.registry.record_running(attempt.id, ["gpu02", "gpu01", "gpu01"])
        self.assertEqual(running.nodes, ("gpu01", "gpu02"))
        done = self.registry.finish_attempt(
            attempt.id,
            "succeeded",
            slurm_state="COMPLETED",
            output_ref="/w/jobs/x/output.pkl",
            usage_ref="sacct:alpha:4242",
        )
        self.assertEqual(
            (done.status, done.slurm_state, done.output_ref, done.usage_ref),
            ("succeeded", "COMPLETED", "/w/jobs/x/output.pkl", "sacct:alpha:4242"),
        )
        closed = self.registry.get(execution.execution_id, project_id=self.project)
        assert closed is not None
        self.assertEqual(closed.status, "succeeded")
        self.assertIsNotNone(closed.finished_at)
        with self.assertRaises(ExecutionClosedError):
            self.begin(execution.execution_id)
        with self.assertRaises(InvalidTransitionError):
            self.registry.finish_attempt(attempt.id, "failed")
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE execution_attempts SET status = 'running' WHERE id = %s", (attempt.id,)
            )

    def test_identity_fields_are_immutable(self):
        execution, _ = self.register()
        attempt = self.begin(execution.execution_id)
        self.registry.record_submission(attempt.id, 7)
        for statement, params in (
            (
                "UPDATE execution_attempts SET platform_id = %s WHERE id = %s",
                (self.beta.id, attempt.id),
            ),
            ("UPDATE execution_attempts SET slurm_account = 'x' WHERE id = %s", (attempt.id,)),
            ("UPDATE execution_attempts SET unix_user = 'root' WHERE id = %s", (attempt.id,)),
            ("UPDATE execution_attempts SET login_node_host = 'h' WHERE id = %s", (attempt.id,)),
            ("UPDATE execution_attempts SET slurm_job_id = 8 WHERE id = %s", (attempt.id,)),
            ("DELETE FROM execution_attempts WHERE id = %s", (attempt.id,)),
            (
                "UPDATE executions SET project_id = %s WHERE execution_id = %s",
                (self.other_project, execution.execution_id),
            ),
            (
                "UPDATE executions SET owner_user_id = %s WHERE execution_id = %s",
                (self.other_user, execution.execution_id),
            ),
            ("DELETE FROM executions WHERE execution_id = %s", (execution.execution_id,)),
        ):
            with (
                self.subTest(statement=statement),
                self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            ):
                self.conn.execute(statement, params)

    def test_the_database_allows_only_one_live_attempt(self):
        execution, _ = self.register()
        attempt = self.begin(execution.execution_id)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "INSERT INTO execution_attempts (execution_id, attempt_number, platform_id, "
                "login_node_id, login_node_host, transport_mode, unix_user, "
                "slurm_account_id, slurm_account, slurm_account_origin"
                + CONTROLLER_PIN_COLUMNS
                + ") "
                "VALUES (%s, 2, %s, %s, 'h', 'A', 'alice', %s, 'proj', 'explicit'"
                + CONTROLLER_PIN_VALUES
                + ")",
                (
                    execution.execution_id,
                    self.alpha.id,
                    self.A1.node_id,
                    self.account_ids[self.alpha.id, "proj"],
                ),
            )
        self.assertEqual(len(self.registry.live_attempts()), 1)
        self.assertEqual(self.registry.live_attempts()[0].id, attempt.id)

    def test_concurrent_attempts_for_one_execution_start_one(self):
        execution, _ = self.register()
        results, errors = [], []
        barrier = threading.Barrier(3)

        def run():
            barrier.wait()
            try:
                results.append(self.begin(execution.execution_id))
            except AttemptInFlightError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((len(results), len(errors)), (1, 2))

    def test_unknown_execution_and_attempt(self):
        with self.assertRaises(ExecutionNotFoundError):
            self.begin("0" * 32)
        with self.assertRaises(ExecutionNotFoundError):
            self.registry.record_submission(999999, 1)
        with self.assertRaises(ValueError):
            self.begin(
                self.register()[0].execution_id,
                principal=HPCPrincipal("alice", binding_id="b"),
            )
        with self.assertRaises(ValueError):
            self.begin(self.register()[0].execution_id, account="unregistered")


class RecoveryTests(_Base):
    def test_interrupted_submissions_become_unknown_and_block_blind_retry(self):
        execution, _ = self.register()
        attempt = self.begin(execution.execution_id)
        # A recent attempt may still be in progress on another Controller.
        self.assertEqual(self.registry.mark_interrupted(timedelta(minutes=5)), [])
        self.conn.execute(
            "UPDATE execution_attempts SET updated_at = now() - interval '1 hour' WHERE id = %s",
            (attempt.id,),
        )
        self.db.close()
        restarted = ExecutionRegistry(self.open_database())
        [interrupted] = restarted.mark_interrupted(timedelta(minutes=5))
        self.assertEqual((interrupted.id, interrupted.status), (attempt.id, "unknown"))
        self.assertEqual(interrupted.error_kind, "controller_interrupted")
        # sbatch may have run: no new attempt until the unknown one is reconciled.
        with self.assertRaises(AttemptInFlightError):
            restarted.begin_attempt(
                execution.execution_id,
                endpoint=self.A2,
                principal=self.alice(),
                **self.account(self.A2),
                options={},
                authority=USER_OWNED,
            )
        # Reconciliation found the job after all.
        found = restarted.record_submission(attempt.id, 99)
        self.assertEqual(found.status, "submitted")
        self.assertEqual([a.id for a in restarted.live_attempts(self.alpha.id)], [attempt.id])
        self.assertEqual(restarted.live_attempts(self.beta.id), [])

    def test_mark_interrupted_needs_a_positive_age(self):
        with self.assertRaises(ValueError):
            self.registry.mark_interrupted(timedelta(0))


class PlatformNamespaceTests(_Base):
    def test_same_account_and_job_id_on_two_platforms_stay_distinct(self):
        a, _ = self.register()
        b, _ = self.register()
        on_alpha = self.begin(a.execution_id, account="shared")
        on_beta = self.begin(b.execution_id, self.B1, account="shared")
        self.registry.record_submission(on_alpha.id, 1000)
        self.registry.record_submission(on_beta.id, 1000)
        self.registry.record_running(on_alpha.id, ["node01"])
        self.registry.record_running(on_beta.id, ["node01"])
        alpha_job = self.registry.find_by_slurm_job(self.alpha.id, 1000, project_id=self.project)
        beta_job = self.registry.find_by_slurm_job(self.beta.id, 1000, project_id=self.project)
        assert alpha_job is not None and beta_job is not None
        self.assertEqual(
            (alpha_job.execution_id, beta_job.execution_id), (a.execution_id, b.execution_id)
        )
        self.assertIsNone(
            self.registry.find_by_slurm_job(uuid.uuid4(), 1000, project_id=self.project)
        )
        self.assertEqual(
            [
                x.id
                for x in self.registry.attempts_for_account(
                    self.alpha.id, "shared", project_id=self.project
                )
            ],
            [on_alpha.id],
        )
        self.assertEqual(
            [
                x.id
                for x in self.registry.attempts_for_account(
                    self.beta.id, "shared", project_id=self.project
                )
            ],
            [on_beta.id],
        )
        self.assertEqual(
            [
                x.id
                for x in self.registry.attempts_on_node(
                    self.alpha.id, "node01", project_id=self.project
                )
            ],
            [on_alpha.id],
        )
        self.assertEqual(
            [
                x.id
                for x in self.registry.attempts_on_node(
                    self.beta.id, "node01", project_id=self.project
                )
            ],
            [on_beta.id],
        )
        self.assertEqual(beta_job.endpoint, self.B1)

    def test_a_reused_slurm_job_id_finds_the_latest_attempt(self):
        old, _ = self.register()
        new, _ = self.register()
        first = self.begin(old.execution_id)
        self.registry.record_submission(first.id, 5)
        self.registry.finish_attempt(first.id, "succeeded")
        second = self.begin(new.execution_id)
        self.registry.record_submission(second.id, 5)
        found = self.registry.find_by_slurm_job(self.alpha.id, 5, project_id=self.project)
        assert found is not None
        self.assertEqual(found.id, second.id)


class PlatformCatalogTests(_Base):
    """Attempts are recorded only on endpoints the D07 catalog still approves."""

    def test_an_ungranted_platform_is_refused(self):
        execution, _ = self.register(project=self.other_project)
        with self.assertRaises(PlatformUnavailableError):
            self.begin(execution.execution_id)
        execution2, _ = self.register()
        self.platforms.revoke_project(self.beta.id, self.project, **OPS)
        with self.assertRaises(PlatformUnavailableError):
            self.begin(execution2.execution_id, self.B1)
        # Nothing was recorded and the execution can still start elsewhere.
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM execution_attempts").fetchone(), (0,)
        )
        self.assertEqual(self.begin(execution2.execution_id).attempt_number, 1)

    def test_a_disabled_platform_is_refused(self):
        execution, _ = self.register()
        self.platforms.set_status(self.beta.id, "disabled", **OPS)
        with self.assertRaises(PlatformUnavailableError):
            self.begin(execution.execution_id, self.B1)

    def test_an_endpoint_that_no_longer_matches_the_registry_is_refused(self):
        execution, _ = self.register()
        for endpoint in (
            replace(self.A1, host="evil.example"),
            replace(self.A2, port=22),
            replace(self.A2, host_key_pin_ref="SHA256:other"),
            replace(self.A1, mode="B", port=22, host_key_pin_ref=PIN),
            replace(self.B1, platform_id=self.alpha.id, platform_slug="alpha"),
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(EndpointNotApprovedError):
                self.begin(execution.execution_id, endpoint)
        self.platforms.update_login_node(self.A1.node_id, status="disabled", **OPS)
        with self.assertRaises(EndpointNotApprovedError):
            self.begin(execution.execution_id)

    def test_history_outlives_a_removed_login_node(self):
        execution, _ = self.register()
        attempt = self.begin(execution.execution_id)
        self.registry.finish_attempt(attempt.id, "failed")
        self.platforms.remove_login_node(self.A1.node_id, **OPS)
        [kept] = self.registry.attempts(execution.execution_id, project_id=self.project)
        self.assertEqual(kept.endpoint, self.A1)


class AccountRegistryTests(_Base):
    """Attempts charge only D08 accounts linked to the Project for the mode."""

    def test_an_account_of_another_platform_or_unlinked_is_refused(self):
        execution, _ = self.register()
        alpha_shared = self.account_ids[self.alpha.id, "shared"]
        for fields in (
            # The beta "shared" account presented on an alpha endpoint.
            {
                **self.account(self.A1, "shared"),
                "slurm_account_id": self.account_ids[self.beta.id, "shared"],
            },
            # A name that disagrees with the registered account.
            {**self.account(self.A1, "shared"), "account": "proj"},
        ):
            with self.subTest(fields=fields), self.assertRaises(AccountNotPermittedError):
                self.begin(execution.execution_id, account_fields=fields)
        self.accounts.unlink_project(self.project, self.alpha.id, "shared", **OPS)
        with self.assertRaises(AccountNotPermittedError):
            self.begin(execution.execution_id, account="shared")
        self.assertEqual(alpha_shared, self.account_ids[self.alpha.id, "shared"])

    def test_an_account_not_permitted_for_the_mode_is_refused(self):
        execution, _ = self.register()
        self.accounts.link_project(self.project, self.alpha.id, "proj", modes=["B"], **OPS)
        with self.assertRaises(AccountNotPermittedError):
            self.begin(execution.execution_id)  # A1 is Mode A
        self.assertEqual(self.begin(execution.execution_id, self.A2).endpoint.mode, "B")

    def test_a_disabled_account_is_refused(self):
        execution, _ = self.register()
        self.accounts.update_account(self.alpha.id, "proj", status="disabled", **OPS)
        with self.assertRaises(AccountNotPermittedError):
            self.begin(execution.execution_id)

    def test_native_default_needs_mode_b_and_the_projects_permission(self):
        execution, _ = self.register()
        native = {
            "account": None,
            "slurm_account_id": None,
            "slurm_account_origin": "native_default",
        }
        with self.assertRaises(AccountNotPermittedError):
            self.begin(execution.execution_id, self.B1, account_fields=native)
        self.accounts.set_native_default(self.project, self.beta.id, True, **OPS)
        with self.assertRaises(AccountNotPermittedError):
            self.begin(execution.execution_id, self.A1, account_fields=native)
        attempt = self.begin(execution.execution_id, self.B1, account_fields=native)
        self.assertEqual(
            (attempt.slurm_account, attempt.slurm_account_id, attempt.slurm_account_origin),
            (None, None, "native_default"),
        )

    def test_the_database_keeps_the_account_on_the_attempts_platform(self):
        execution, _ = self.register()
        attempt = self.begin(execution.execution_id)
        self.registry.finish_attempt(attempt.id, "failed")
        insert = (
            "INSERT INTO execution_attempts (execution_id, attempt_number, platform_id, "
            "login_node_id, login_node_host, transport_mode, unix_user, "
            "slurm_account_id, slurm_account, slurm_account_origin" + CONTROLLER_PIN_COLUMNS + ") "
            "VALUES (%s, 2, %s, %s, 'h', 'A', 'alice', %s, %s, 'explicit'"
            + CONTROLLER_PIN_VALUES
            + ")"
        )
        for account_id, name, error in (
            (
                self.account_ids[self.beta.id, "shared"],
                "shared",
                psycopg.errors.ForeignKeyViolation,
            ),
            (self.account_ids[self.alpha.id, "proj"], "shared", psycopg.errors.CheckViolation),
        ):
            with self.subTest(name=name), self.assertRaises(error):
                self.conn.execute(
                    insert,
                    (execution.execution_id, self.alpha.id, self.A1.node_id, account_id, name),
                )
        for column in ("slurm_account_id", "slurm_account_origin"):
            with (
                self.subTest(column=column),
                self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            ):
                self.conn.execute(
                    f"UPDATE execution_attempts SET {column} = NULL WHERE id = %s", (attempt.id,)
                )


class TenantScopeTests(_Base):
    """Platform-scoped lookups never return another Project's attempts."""

    def test_lookups_by_job_node_and_account_stay_in_their_project(self):
        self.platforms.grant_project(self.alpha.id, self.other_project, **OPS)
        self.accounts.link_project(self.other_project, self.alpha.id, "shared", modes=["A"], **OPS)
        mine, _ = self.register()
        theirs, _ = self.register(project=self.other_project)
        my_attempt = self.begin(mine.execution_id, account="shared")
        their_attempt = self.begin(theirs.execution_id, account="shared")
        # The same Platform, Slurm job ID, node and account in both Projects.
        for attempt in (my_attempt, their_attempt):
            self.registry.record_submission(attempt.id, 777)
            self.registry.record_running(attempt.id, ["cn01"])
        for project, expected in (
            (self.project, my_attempt.id),
            (self.other_project, their_attempt.id),
        ):
            with self.subTest(project=project):
                found = self.registry.find_by_slurm_job(self.alpha.id, 777, project_id=project)
                assert found is not None
                self.assertEqual(found.id, expected)
                self.assertEqual(
                    [
                        a.id
                        for a in self.registry.attempts_on_node(
                            self.alpha.id, "cn01", project_id=project
                        )
                    ],
                    [expected],
                )
                self.assertEqual(
                    [
                        a.id
                        for a in self.registry.attempts_for_account(
                            self.alpha.id, "shared", project_id=project
                        )
                    ],
                    [expected],
                )
        stranger = self.projects.create_project("r", created_by_user_id=self.owner).id
        self.assertIsNone(self.registry.find_by_slurm_job(self.alpha.id, 777, project_id=stranger))
        self.assertEqual(
            self.registry.attempts_on_node(self.alpha.id, "cn01", project_id=stranger), []
        )
        self.assertEqual(
            self.registry.attempts_for_account(self.alpha.id, "shared", project_id=stranger), []
        )
        self.assertEqual(self.registry.get(mine.execution_id, project_id=self.other_project), None)


class UpgradePathTests(PostgresTestCase):
    def test_upgrade_from_the_account_registry_keeps_records(self):
        """A populated database at 0012, then D08's 0013, upgrades to 0014."""
        url = self.database()
        labels = [m.label for m in load_migrations()]
        accounts_at = labels.index("0013_slurm_account_registry")
        registry_at = labels.index("0014_execution_registry")
        self.assertEqual(labels[accounts_at - 1], "0012_hpc_platform_catalog")
        self.assertEqual(registry_at, accounts_at + 1)
        migrations = load_migrations()
        with connect(url) as conn:
            upgrade(conn, migrations[:accounts_at])
        db = Database(load_database_config({"NEST_DATABASE_URL": url}))
        db.open()
        self.addCleanup(db.close)
        owner = IdentityStore(db).create_user(display_name="owner").id
        project = ProjectStore(db).create_project("p", created_by_user_id=owner).id
        platforms = PlatformStore(db)
        nano4 = platforms.create_platform("nano4", "Nano 4", **OPS)
        node = platforms.add_login_node(nano4.id, "ln01.example", transport_modes=["A"], **OPS)
        platforms.grant_project(nano4.id, project, **OPS)
        with connect(url) as conn:
            self.assertEqual(
                [m.label for m in upgrade(conn, migrations[: accounts_at + 1])],
                ["0013_slurm_account_registry"],
            )
        accounts = SlurmAccountStore(db)
        chem = accounts.register_account(nano4.id, "chem", **OPS)
        accounts.link_project(project, nano4.id, "chem", modes=["A"], **OPS)
        with connect(url) as conn:
            self.assertEqual(
                [m.label for m in upgrade(conn, migrations[: registry_at + 1])],
                ["0014_execution_registry"],
            )
            verify(conn, migrations[: registry_at + 1])
        # The Platform, grant and account written before 0014 are usable at once.
        registry = ExecutionRegistry(db)
        execution, _ = registry.register(project, owner, "batch", {})
        attempt = registry.begin_attempt(
            execution.execution_id,
            endpoint=endpoint_for(nano4, node, "A"),
            principal=ALICE,
            account="chem",
            slurm_account_id=chem.id,
            slurm_account_origin="project_default",
            options={},
        )
        self.assertEqual((attempt.platform_id, attempt.slurm_account_id), (nano4.id, chem.id))
        self.assertEqual(accounts.get_account(nano4.id, "chem"), chem)


class SlurmIdentityTests(_Base):
    """An attempt Slurm accepted always carries its Slurm job ID."""

    def test_accepted_states_need_a_job_id(self):
        execution, _ = self.register()
        attempt = self.begin(execution.execution_id)
        # Through the registry: refused before any write.
        for call in (
            lambda: self.registry.record_running(attempt.id, ["cn01"]),
            lambda: self.registry.finish_attempt(attempt.id, "succeeded"),
        ):
            with self.assertRaises(InvalidTransitionError):
                call()
        # Directly in SQL: refused by the database, from 'submitting' and from an
        # unreconciled 'unknown' (the only states that may reach these).
        direct = (
            "UPDATE execution_attempts SET status = %s, "
            "finished_at = CASE WHEN %s = 'succeeded' THEN now() END WHERE id = %s"
        )
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(direct, ("submitted", "submitted", attempt.id))
        unknown_execution, _ = self.register()
        unknown = self.begin(unknown_execution.execution_id)
        self.registry.finish_attempt(unknown.id, "unknown")
        for status in ("submitted", "running", "succeeded"):
            with (
                self.subTest(status=status),
                self.assertRaises(psycopg.errors.CheckViolation),
            ):
                self.conn.execute(direct, (status, status, unknown.id))
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "UPDATE execution_attempts SET slurm_job_id = 5 WHERE id = %s", (attempt.id,)
            )
        [unchanged] = self.registry.attempts(execution.execution_id, project_id=self.project)
        self.assertEqual((unchanged.status, unchanged.slurm_job_id), ("submitting", None))

    def test_pre_submission_outcomes_may_lack_a_job_id(self):
        for status in ("failed", "cancelled", "unknown"):
            with self.subTest(status=status):
                execution, _ = self.register()
                attempt = self.begin(execution.execution_id)
                done = self.registry.finish_attempt(attempt.id, status, error_kind="ssh")
                self.assertEqual(
                    (done.status, done.slurm_job_id, done.submitted_at), (status, None, None)
                )

    def test_reconciling_an_unknown_attempt_records_when_its_job_was_learned(self):
        execution, _ = self.register()
        attempt = self.begin(execution.execution_id)
        self.registry.finish_attempt(attempt.id, "unknown")
        with self.assertRaises(InvalidTransitionError):
            self.registry.finish_attempt(attempt.id, "succeeded")
        done = self.registry.finish_attempt(attempt.id, "succeeded", slurm_job_id=42)
        self.assertEqual((done.status, done.slurm_job_id), ("succeeded", 42))
        self.assertIsNotNone(done.submitted_at)


class ExecutionLifecycleTests(_Base):
    """The database keeps an execution's status consistent with its attempts."""

    def refused(self, statement, params):
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(statement, params)

    def status(self, execution_id):
        execution = self.registry.get(execution_id, project_id=self.project)
        assert execution is not None
        return execution.status, execution.finished_at

    def test_premature_success_is_refused(self):
        execution, _ = self.register()
        eid = execution.execution_id
        mark = "UPDATE executions SET status = %s, finished_at = now() WHERE execution_id = %s"
        # No attempt yet.
        self.refused(mark, ("succeeded", eid))
        attempt = self.begin(eid)
        self.registry.record_submission(attempt.id, 1)
        # An attempt in flight, or one that failed, is not a success.
        self.refused(mark, ("succeeded", eid))
        self.refused(mark, ("cancelled", eid))
        self.registry.finish_attempt(attempt.id, "failed")
        self.refused(mark, ("succeeded", eid))
        self.assertEqual(self.status(eid)[0], "failed")
        # Inserting an execution that claims to be finished is refused too.
        self.refused(
            "INSERT INTO executions (execution_id, project_id, owner_user_id, kind, "
            "request_digest, requested_options, status, finished_at) "
            "VALUES (%s, %s, %s, 'batch', %s, '{}', 'succeeded', now())",
            ("f" * 32, self.project, self.owner, b"\0" * 32),
        )

    def test_a_finished_execution_cannot_be_reopened(self):
        for final in ("succeeded", "cancelled"):
            with self.subTest(final=final):
                execution, _ = self.register()
                eid = execution.execution_id
                attempt = self.begin(eid)
                self.registry.record_submission(attempt.id, 2)
                self.registry.finish_attempt(attempt.id, final)
                before = self.status(eid)
                for status in ("pending", "active", "failed"):
                    self.refused(
                        "UPDATE executions SET status = %s, finished_at = NULL "
                        "WHERE execution_id = %s",
                        (status, eid),
                    )
                self.refused(
                    "UPDATE executions SET finished_at = now() - interval '1 day' "
                    "WHERE execution_id = %s",
                    (eid,),
                )
                self.assertEqual(self.status(eid), before)

    def test_attempt_and_execution_cannot_drift_apart(self):
        execution, _ = self.register()
        eid = execution.execution_id
        attempt = self.begin(eid)
        # An active execution cannot be marked failed while its attempt is in flight.
        self.refused("UPDATE executions SET status = 'failed' WHERE execution_id = %s", (eid,))
        # Nor can an attempt finish without its execution following.
        self.refused(
            "UPDATE execution_attempts SET status = 'failed', finished_at = now() WHERE id = %s",
            (attempt.id,),
        )
        self.assertEqual(self.status(eid)[0], "active")
        # The registry moves both together, and a retry reopens a failed one.
        self.registry.finish_attempt(attempt.id, "failed")
        self.assertEqual(self.status(eid)[0], "failed")
        self.assertEqual(self.begin(eid, self.A2).attempt_number, 2)
        self.assertEqual(self.status(eid)[0], "active")
