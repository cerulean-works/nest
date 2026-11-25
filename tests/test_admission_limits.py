"""Trusted-pilot admission limits and durable reservations (F02a).

Every test serves the real `ExecutionService` with session authentication and a
`RegistryAdmission` that enforces `AdmissionLimits`, against a real PostgreSQL
(`NEST_TEST_DATABASE_URL`, see `postgres_fixture`) and the `FakeHpc` login node,
reusing `test_registry_admission._Base`.

The central assertions: a refused submission leaves nothing registered and sends
nothing to the cluster; concurrent submissions on two Controller replicas never
oversubscribe a limit; and a reservation is released only by its attempt reaching
a final state, never by a timeout, a lost RPC, a restart or an unknown outcome.
"""

import dataclasses
import os
import secrets
import threading
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import cast
from unittest import mock

import cloudpickle
import grpc
import psycopg
from fake_hpc import FakeHpc
from test_registry_admission import OPS, _Base, browser

from nest.admission import (
    AdmissionRefusedError,
    RecoverySweeper,
    RegistryAdmission,
    SubmissionRequest,
)
from nest.authz import Principal
from nest.backends.slurm import (
    ENVIRONMENT_BUILD_CPUS,
    ENVIRONMENT_BUILD_GPUS,
    ENVIRONMENT_BUILD_TIME,
    WORKER_DEFAULT_CPUS,
    WORKER_DEFAULT_GPUS,
    SlurmBackend,
    SlurmConfig,
)
from nest.config import AdmissionLimits, ConfigError, load_controller_config, slurm_time_seconds
from nest.db import executions as executions_module
from nest.db import hash_token
from nest.db.cli import main as nest_db_main
from nest.db.executions import (
    AdmissionLimitError,
    ExecutionRegistry,
    Reservation,
    check_request_limits,
)
from nest.db.identity import lock_session_family
from nest.errors import (
    ExecutionTimeoutError,
    RemoteCommandError,
    ResultTooLargeError,
    SlurmJobError,
    SSHError,
    SubmissionUnknownError,
)
from nest.proto import execution_pb2

LIMITS = AdmissionLimits(
    active_per_user=2,
    active_per_project=3,
    active_per_platform=4,
    cpu_per_request=16,  # an environment build holds 12
    gpu_per_request=2,
    cpu_per_project=12,
    gpu_per_project=3,
    walltime_seconds=3600,
    submissions_per_minute=50,
    environment_builds=1,
)


class _LimitsBase(_Base):
    limits = LIMITS

    def setUp(self):
        super().setUp()
        self.admission = self.limited_admission()
        self.stub = self.serve(self.admission)
        # The owner of `alpha` also submits, to share the Project's limits.
        self.token_owner = self.token(self.owner)

    def limited_admission(self, db=None, limits=None) -> RegistryAdmission:
        return RegistryAdmission(
            db or self.db,
            self.backend,
            self.profile,
            self.platform_id,
            audit=self.audit,
            limits=limits or self.limits,
        )

    def session(self, user_id):
        """A CLI session for `user_id`: its access token and session ID."""
        session = self.identities.create_session(
            user_id,
            kind="cli",
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="jobs:run",
        )
        access = secrets.token_urlsafe(32)
        self.sessions.issue_access_token(session.id, token_hash=hash_token(access), ttl_seconds=600)
        return access, session.id

    def batch(self, stub=None, token=None, project=None, gpu=0, cpu=0, time="", **extra):
        return (stub or self.stub).SubmitJob(
            execution_pb2.SubmitJobRequest(
                workdir=self.batch_dir(),
                script="hello.sbatch",
                gpu=gpu,
                cpu=cpu,
                time=time,
            ),
            timeout=60,
            metadata=self.metadata(token, project, **extra),
        )

    def held(self):
        return self.sql(
            "SELECT r.*, a.status FROM execution_reservations r "
            "JOIN execution_attempts a ON a.id = r.attempt_id "
            "WHERE a.status IN ('submitting', 'submitted', 'running', 'unknown') ORDER BY r.attempt_id"
        )

    def finish(self, job_id, state="COMPLETED"):
        (self.hpc.state / f"job-{job_id}.state").write_text(state)

    def denials(self):
        return [e for e in self.audit.events(event="execution.admit") if e.outcome == "denied"]

    def nest_db(self, *args):
        argv = [*args, "--operator", "ops", "--reason", "sacct has no such job"]
        out, err = StringIO(), StringIO()
        with (
            mock.patch.dict(os.environ, {"NEST_DATABASE_URL": self.url}),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = nest_db_main(argv)
        return code, out.getvalue(), err.getvalue()


class LimitTests(_LimitsBase):
    def test_active_per_user_refuses_with_nothing_sent_and_releases_on_completion(self):
        self.hpc.hold_jobs("RUNNING")
        first = self.batch()
        self.batch()
        sent = len(self.sbatch_calls())
        error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertIn("limit_active_per_user", error.details())
        self.assertIn("Nothing was submitted", error.details())
        self.assertEqual(len(self.sbatch_calls()), sent)
        # The refused request left no attempt and no reservation; its registration
        # stays pending, so a retry with the same idempotency key can proceed later.
        self.assertEqual(len(self.attempts()), 2)
        self.assertEqual(
            self.sql("SELECT status, count(*) AS n FROM executions GROUP BY status ORDER BY 1"),
            [{"status": "active", "n": 2}, {"status": "pending", "n": 1}],
        )
        [denied] = self.denials()
        self.assertEqual(denied.reason, "limit_active_per_user")
        self.assertEqual(denied.actor_user_id, self.member)
        self.assertEqual(denied.project_id, self.project)
        self.assertEqual(
            {k: denied.details[k] for k in ("limit", "current", "requested")},
            {"limit": 2, "current": 2, "requested": 1},
        )
        self.assertNotIn(self.token_member, str(denied.details))

        # A status query that observes the job finished releases its reservation.
        self.finish(first.job_id)
        state = self.stub.GetJobStatus(
            execution_pb2.JobStatusRequest(job_id=first.job_id),
            timeout=30,
            metadata=self.metadata(),
        ).state
        self.assertEqual(state, "COMPLETED")
        self.assertEqual(len(self.held()), 1)
        self.batch()
        self.assertEqual(len(self.held()), 2)

    def test_project_and_platform_limits_span_users(self):
        self.hpc.hold_jobs("RUNNING")
        self.batch()
        self.batch()
        self.batch(token=self.token_owner)
        error = self.refused(lambda: self.batch(token=self.token_owner))
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertIn("limit_active_per_project", error.details())
        # Another Project still has room until the Platform is full.
        token_outsider = self.token_outsider
        self.batch(token=token_outsider, project=self.other)
        error = self.refused(lambda: self.batch(token=token_outsider, project=self.other))
        self.assertIn("limit_active_per_platform", error.details())
        self.assertEqual(len(self.held()), 4)

    def test_resource_sums_per_project(self):
        self.hpc.hold_jobs("RUNNING")
        self.batch(gpu=2, cpu=8)
        error = self.refused(lambda: self.batch(token=self.token_owner, gpu=2))
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertIn("limit_gpu_per_project", error.details())
        error = self.refused(lambda: self.batch(token=self.token_owner, cpu=5))
        self.assertIn("limit_cpu_per_project", error.details())
        self.batch(token=self.token_owner, gpu=1, cpu=4)
        [first, second] = self.held()
        self.assertEqual((first["gpu"], first["cpu"]), (2, 8))
        self.assertEqual((second["gpu"], second["cpu"], second["walltime_seconds"]), (1, 4, 300))
        self.assertEqual(second["limits"]["gpu_per_project"], 3)

    def test_per_request_bounds_are_refused_before_registration(self):
        for label, kwargs, reason in (
            ("cpu", {"cpu": 17}, "limit_cpu_per_request"),
            ("gpu", {"gpu": 3}, "limit_gpu_per_request"),
            ("walltime", {"time": "02:00:00"}, "limit_walltime"),
            ("unlimited", {"time": "UNLIMITED"}, "limit_walltime_unbounded"),
        ):
            with self.subTest(label):
                error = self.refused(lambda kwargs=kwargs: self.batch(**kwargs))
                self.assertEqual(error.code(), grpc.StatusCode.FAILED_PRECONDITION)
                self.assertIn(reason, error.details())
        self.assert_nothing_sent()
        self.assertEqual(self.sql("SELECT count(*) AS n FROM executions"), [{"n": 0}])
        self.assertEqual(len(self.denials()), 4)

    def test_submissions_per_minute_counts_finished_attempts_too(self):
        self.admission = self.limited_admission(
            limits=AdmissionLimits(submissions_per_minute=2, active_per_user=10)
        )
        self.stub = self.serve(self.admission)
        self.batch()
        self.batch()
        error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertIn("limit_submissions_per_minute", error.details())
        self.assertIn("in the last minute", error.details())

    def test_duplicate_retry_is_answered_without_a_new_reservation(self):
        self.hpc.hold_jobs("RUNNING")
        first = self.batch(x_nest_idempotency_key="key-1")
        self.batch()
        # At the per-User limit, the same request again returns the same job.
        again = self.batch(x_nest_idempotency_key="key-1")
        self.assertEqual(again.job_id, first.job_id)
        self.assertEqual(len(self.sbatch_calls()), 2)
        self.assertEqual(len(self.held()), 2)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM execution_reservations"), [{"n": 2}])

    def test_invalid_limits_are_refused(self):
        with self.assertRaises(ValueError):
            AdmissionLimits(active_per_user=0)
        with self.assertRaises(ValueError):
            ExecutionRegistry(self.db).begin_attempt(
                "0" * 32,
                endpoint=mock.Mock(),
                principal=mock.Mock(binding_id=None, binding_version=None),
                account="proj",
                slurm_account_id=1,
                slurm_account_origin="explicit",
                options={},
                reservation=Reservation(),
            )


class ConcurrencyTests(_LimitsBase):
    def test_concurrent_submissions_on_two_replicas_never_oversubscribe(self):
        self.hpc.hold_jobs("RUNNING")
        replica = self.limited_admission(db=self.open_database())
        stubs = [self.stub, self.serve(replica)]
        tokens = [self.token(self.member) for _ in range(8)]
        barrier = threading.Barrier(len(tokens))
        outcomes: list[object] = [None] * len(tokens)

        def submit(index):
            barrier.wait()
            try:
                outcomes[index] = self.batch(stub=stubs[index % 2], token=tokens[index]).job_id
            except grpc.RpcError as exc:
                outcomes[index] = cast(grpc.Call, exc).code()

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(len(tokens))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        admitted = [o for o in outcomes if isinstance(o, int)]
        self.assertEqual(len(admitted), LIMITS.active_per_user, outcomes)
        self.assertEqual(
            [o for o in outcomes if not isinstance(o, int)],
            [grpc.StatusCode.RESOURCE_EXHAUSTED] * (len(tokens) - LIMITS.active_per_user),
        )
        self.assertEqual(len(self.sbatch_calls()), LIMITS.active_per_user)
        self.assertEqual(len(self.held()), LIMITS.active_per_user)

    def test_concurrent_retries_of_one_key_submit_once(self):
        self.hpc.hold_jobs("RUNNING")
        replica = self.limited_admission(db=self.open_database())
        stubs = [self.stub, self.serve(replica)]
        barrier = threading.Barrier(6)
        results: list[object] = []

        def submit(index):
            barrier.wait()
            try:
                results.append(self.batch(stub=stubs[index % 2], x_nest_idempotency_key="k").job_id)
            except grpc.RpcError as exc:
                results.append(cast(grpc.Call, exc).code())

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(len(self.sbatch_calls()), 1)
        self.assertEqual(len(self.held()), 1)
        # Losers of the race are answered with the job, or told it is in flight.
        job_ids = {r for r in results if isinstance(r, int)}
        self.assertLessEqual(len(job_ids), 1)
        self.assertTrue(
            all(isinstance(r, int) or r == grpc.StatusCode.ABORTED for r in results), results
        )


class OutageTests(_LimitsBase):
    def test_admission_data_unavailable_fails_closed(self):
        with mock.patch(
            "nest.db.executions._reserve", side_effect=psycopg.OperationalError("db gone")
        ):
            error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.UNAVAILABLE)
        self.assertEqual(dict(error.trailing_metadata())["nest-error-kind"], "database_unavailable")
        self.assert_nothing_sent()
        # The attempt was rolled back with its reservation.
        self.assertEqual(self.attempts(), [])
        self.assertEqual(self.sql("SELECT count(*) AS n FROM execution_reservations"), [{"n": 0}])

    def test_policy_outage_at_authentication_fails_closed(self):
        with mock.patch.object(
            self.access.sessions, "authenticate", side_effect=psycopg.OperationalError("down")
        ):
            error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.UNAVAILABLE)
        self.assert_nothing_sent()
        self.assertEqual(self.sql("SELECT count(*) AS n FROM executions"), [{"n": 0}])


class LifecycleTests(_LimitsBase):
    ssh_timeout = 2.0

    def test_unknown_outcome_keeps_its_reservation_until_reconciled(self):
        self.hpc.hang_ssh("sbatch --parsable", after=True)
        error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.UNKNOWN)
        (self.hpc.state / "ssh-fault.json").unlink()
        [held] = self.held()
        self.assertEqual(held["status"], "unknown")
        # A restarted Controller (a new pool and admission) still counts it.
        restarted = self.limited_admission(db=self.open_database())
        RecoverySweeper(
            restarted, timedelta(seconds=1), interval=3600, settle_after=timedelta(0)
        ).sweep()
        self.assertEqual(len(self.held()), 1)
        self.hpc.hold_jobs("RUNNING")
        self.batch()
        error = self.refused(self.batch)
        self.assertIn("limit_active_per_user", error.details())

        # Reconciling it as not submitted releases it.
        code, _, err = self.nest_db(
            "execution", "reconcile", str(held["attempt_id"]), "--not-submitted"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.held()), 1)
        self.batch()

    def test_restart_while_submitting_keeps_the_reservation(self):
        self.hpc.hold_jobs("RUNNING")
        request = SubmissionRequest("batch", workdir=self.batch_dir(), script="hello.sbatch")
        principal = Principal.from_session(self.sessions_for(self.member))
        self.admission.admit(principal, self.project, request, execution_id=uuid.uuid4().hex)
        # The Controller stops before sbatch. Recovery marks the attempt unknown.
        restarted = self.limited_admission(db=self.open_database())
        self.sql("UPDATE execution_attempts SET updated_at = now() - interval '1 hour'")
        restarted.recover(timedelta(minutes=5))
        [held] = self.held()
        self.assertEqual(held["status"], "unknown")

    def sessions_for(self, user_id):
        _, session_id = self.session(user_id)
        [session] = [s for s in self.sessions.active_sessions(user_id) if s.id == session_id]
        return session

    def test_cancellation_releases_only_once_slurm_reports_it(self):
        self.hpc.hold_jobs("RUNNING")
        job = self.batch()
        self.stub.CancelJob(
            execution_pb2.CancelJobRequest(job_id=job.job_id), timeout=30, metadata=self.metadata()
        )
        # The request alone does not release it: the job may still be running.
        self.assertEqual(len(self.held()), 1)
        self.admission.settle(timedelta(0))
        self.assertEqual(self.held(), [])
        [attempt] = self.attempts()
        self.assertEqual(attempt["status"], "cancelled")

    def test_settle_never_releases_on_unknown_or_failed_queries(self):
        self.hpc.hold_jobs("RUNNING")
        lost = self.batch()
        failing = self.batch()
        # Slurm no longer knows `lost`; queries for `failing` cannot reach the cluster.
        (self.hpc.state / f"job-{lost.job_id}.state").unlink()
        self.assertEqual(self.admission.settle(timedelta(0)), 2)
        self.assertEqual(len(self.held()), 2)
        self.hpc.drop_ssh("squeue")
        self.assertEqual(self.admission.settle(timedelta(0)), 2)
        self.assertEqual(len(self.held()), 2)
        (self.hpc.state / "ssh-fault.json").unlink()
        self.finish(failing.job_id, "FAILED")
        self.admission.settle(timedelta(0))
        [left] = self.held()
        self.assertEqual(left["status"], "submitted")
        # A recently changed attempt, or one an RPC here waits on, is left alone.
        self.assertEqual(self.admission.settle(timedelta(hours=1)), 0)

    def test_settle_rotates_past_jobs_that_stay_pending_or_unreadable(self):
        self.admission = self.limited_admission(
            limits=AdmissionLimits(
                active_per_user=100,
                active_per_project=100,
                submissions_per_minute=100,
                gpu_per_project=1000,
            )
        )
        self.stub = self.serve(self.admission)
        self.admission.SETTLE_BATCH = 3
        self.hpc.hold_jobs("PENDING")
        stuck = [self.batch().job_id for _ in range(7)]
        (self.hpc.state / f"job-{stuck[0]}.state").unlink()  # Slurm forgot it: UNKNOWN
        self.hpc.hold_jobs("COMPLETED")
        done = self.batch().job_id  # the newest attempt, behind every stuck one
        self.assertEqual(self.hpc.job_state(done), "COMPLETED")
        sweeps = 0
        while len(self.held()) > len(stuck) and sweeps < 5:
            self.admission.settle(timedelta(0))
            sweeps += 1
        # 8 eligible attempts, 3 per sweep: the completed one is reached in 3 sweeps.
        self.assertLessEqual(sweeps, 3)
        held = self.held()
        self.assertEqual(len(held), len(stuck))  # only the completed job was released
        self.assertTrue(all(r["status"] == "submitted" for r in held))
        [finished] = [a for a in self.attempts() if a["slurm_job_id"] == done]
        self.assertEqual(finished["status"], "succeeded")

    def test_execute_timeout_keeps_the_job_reserved(self):
        self.hpc.hold_jobs("RUNNING")
        backend = SlurmBackend(
            SlurmConfig(
                ssh_host=FakeHpc.host,
                account="legacy-acct",
                partition="dev",
                time="00:05:00",
                work_root=f"{self.hpc.base}/jobs",
                worker_workdir=str(self.hpc.base),
                ssh_user="nestop",
                allowed_hosts=(FakeHpc.host,),
                ssh_timeout=10,
                result_timeout=1,
                poll_interval=0.1,
            )
        )
        admission = RegistryAdmission(
            self.db, backend, self.profile, self.platform_id, audit=self.audit, limits=LIMITS
        )
        self.stub = self.serve(admission, backend)
        error = self.refused(self.execute)
        self.assertIn(error.code(), (grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNKNOWN))
        [held] = self.held()
        self.assertEqual(held["status"], "submitted")

    def test_client_gone_during_payload_staging_never_reaches_sbatch(self):
        gone = [False]
        original = SlurmBackend.stage_payload

        def staging_while_the_client_leaves(backend, *args, **kwargs):
            result = original(backend, *args, **kwargs)
            gone[0] = True  # cancelled, or its deadline passed, during staging
            return result

        with (
            mock.patch("nest.controller._still_wanted", lambda context: not gone[0]),
            mock.patch.object(
                SlurmBackend, "stage_payload", autospec=True,
                side_effect=staging_while_the_client_leaves,
            ),
        ):  # fmt: skip
            error = self.refused(self.execute)
        self.assertEqual(error.code(), grpc.StatusCode.CANCELLED)
        self.assertEqual(dict(error.trailing_metadata())["nest-error-kind"], "client_gone")
        self.assertTrue(gone[0])
        self.assertNotEqual(self.hpc.calls("ssh"), [])  # staging did run
        self.assertEqual(self.sbatch_calls(), [])  # the worker never did
        [attempt] = self.attempts()
        self.assertEqual((attempt["status"], attempt["error_kind"]), ("failed", "client_gone"))
        self.assertIsNone(attempt["slurm_job_id"])
        self.assertEqual(self.held(), [])

    def test_client_gone_before_sbatch_releases_and_sends_nothing(self):
        with mock.patch("nest.controller._still_wanted", return_value=False):
            error = self.refused(self.batch)
        self.assertEqual(error.code(), grpc.StatusCode.CANCELLED)
        self.assert_nothing_sent()
        [attempt] = self.attempts()
        self.assertEqual((attempt["status"], attempt["error_kind"]), ("failed", "client_gone"))
        self.assertEqual(self.held(), [])


class RecheckTests(_LimitsBase):
    """Grants and revocations are checked again in the transaction that reserves."""

    def admit(self, session_id, user_id=None, project=None):
        principal = Principal(user_id or self.member, "cli", frozenset({"jobs:run"}), session_id)
        request = SubmissionRequest("batch", workdir=self.batch_dir(), script="hello.sbatch")
        return self.admission.admit(
            principal, project or self.project, request, execution_id=uuid.uuid4().hex
        )

    def test_revoked_session_is_refused_at_reservation(self):
        _, session_id = self.session(self.member)
        self.identities.revoke_session(session_id)
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit(session_id)
        self.assertEqual(caught.exception.reason, "submitter_not_authorized")
        self.assertEqual(caught.exception.code, grpc.StatusCode.PERMISSION_DENIED)
        self.assertEqual(self.attempts(), [])

    def change_before_reservation(self, change):
        """Applies `change` after the Platform and account checks, just before the attempt."""
        original = self.admission.registry.begin_attempt

        def begin_attempt(*args, **kwargs):
            change()
            return original(*args, **kwargs)

        patcher = mock.patch.object(self.admission.registry, "begin_attempt", begin_attempt)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_removed_member_is_refused_at_reservation(self):
        _, session_id = self.session(self.member)
        self.change_before_reservation(
            lambda: self.projects.remove_member(browser(self.owner), self.project, self.member)
        )
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit(session_id)
        self.assertEqual(caught.exception.reason, "submitter_not_authorized")
        self.assertEqual(self.attempts(), [])

    def test_viewer_is_refused_at_reservation(self):
        _, session_id = self.session(self.member)
        self.change_before_reservation(
            lambda: self.projects.change_role(
                browser(self.owner), self.project, self.member, "viewer"
            )
        )
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit(session_id)
        self.assertEqual(caught.exception.reason, "submitter_not_authorized")

    def test_session_revoked_after_authorization_is_refused(self):
        _, session_id = self.session(self.member)
        self.change_before_reservation(lambda: self.identities.revoke_session(session_id))
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit(session_id)
        self.assertEqual(caught.exception.reason, "submitter_not_authorized")
        self.assertEqual(self.held(), [])

    def race_revocation(self, revoke):
        """
        Holds an attempt's transaction open just after the submitter recheck, starts
        `revoke(session_id)`, and checks that it waits until the attempt commits and
        refuses every later attempt.
        """
        _, session_id = self.session(self.member)
        checked, release = threading.Event(), threading.Event()
        original = executions_module._check_submitter

        def paused(*args, **kwargs):
            original(*args, **kwargs)
            checked.set()  # its locks are held until the attempt commits
            release.wait(30)

        admitted: list[object] = []
        with mock.patch.object(executions_module, "_check_submitter", paused):
            admitter = threading.Thread(target=lambda: admitted.append(self.admit(session_id)))
            admitter.start()
            self.assertTrue(checked.wait(30))
            revoker = threading.Thread(target=revoke, args=(session_id,))
            revoker.start()
            revoker.join(1.0)
            # The revocation waits for the transaction that records the attempt.
            self.assertTrue(revoker.is_alive())
            release.set()
            admitter.join(30)
            revoker.join(30)
        self.assertFalse(revoker.is_alive())
        self.assertEqual(len(admitted), 1)  # fully admitted before the revocation
        # Later attempts are refused before anything is recorded: by this recheck,
        # or earlier when Platform selection already sees the disabled membership.
        before = len(self.attempts())
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit(session_id)
        self.assertIn(caught.exception.reason, ("submitter_not_authorized", "none_authorized"))
        self.assertEqual(len(self.attempts()), before)

    def test_concurrent_session_revocation_waits_for_the_admitted_attempt(self):
        self.race_revocation(self.identities.revoke_session)

    def test_concurrent_user_revocation_waits_for_the_admitted_attempt(self):
        self.race_revocation(lambda _: self.identities.revoke_user_sessions(self.member))

    def test_concurrent_disable_waits_for_the_admitted_attempt(self):
        self.race_revocation(lambda _: self.identities.set_user_status(self.member, "disabled"))

    def test_concurrent_member_removal_waits_for_the_admitted_attempt(self):
        self.race_revocation(
            lambda _: self.projects.remove_member(browser(self.owner), self.project, self.member)
        )

    def test_revocation_committed_first_refuses_the_waiting_attempt(self):
        """An attempt that waits for an in-flight revocation sees it and is refused."""
        _, session_id = self.session(self.member)
        locked, release = threading.Event(), threading.Event()
        family = self.sql("SELECT family_id FROM user_sessions WHERE id = %s", session_id)[0]

        def revoke_slowly():
            with self.db.connection() as conn, conn.transaction(), conn.cursor() as cursor:
                lock_session_family(cursor, family["family_id"])
                locked.set()
                release.wait(30)
                self.identities.revoke_session(session_id, conn=conn)

        revoker = threading.Thread(target=revoke_slowly)
        revoker.start()
        self.assertTrue(locked.wait(30))
        outcome: list[object] = []

        def admit():
            try:
                outcome.append(self.admit(session_id))
            except AdmissionRefusedError as exc:
                outcome.append(exc.reason)

        admitter = threading.Thread(target=admit)
        admitter.start()
        admitter.join(1.0)
        self.assertTrue(admitter.is_alive())  # waiting for the family lock
        release.set()
        revoker.join(30)
        admitter.join(30)
        self.assertEqual(outcome, ["submitter_not_authorized"])
        self.assertEqual(self.attempts(), [])
        self.assert_nothing_sent()

    def test_rotated_session_of_a_live_family_is_legitimate_reuse(self):
        refresh = hash_token(secrets.token_urlsafe())
        session = self.identities.create_session(
            self.member,
            kind="cli",
            token_hash=refresh,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="jobs:run",
        )
        # The SDK refreshes while a call authorized with the old session is in flight.
        successor = self.sessions.rotate(
            refresh,
            new_refresh_hash=hash_token(secrets.token_urlsafe()),
            new_access_hash=hash_token(secrets.token_urlsafe()),
            refresh_ttl=3600,
            access_ttl=600,
        ).session
        session_id = session.id
        self.admit(session_id)
        self.assertEqual(len(self.held()), 1)
        # Once the family is revoked (sign-out), the rotated session no longer counts.
        self.identities.revoke_session(successor.id)
        with self.assertRaises(AdmissionRefusedError):
            self.admit(session_id)

    def test_same_session_may_submit_repeatedly(self):
        token, _ = self.session(self.member)
        self.batch(token=token)
        self.batch(token=token)
        self.assertEqual(len(self.sbatch_calls()), 2)


class EnvironmentBuildTests(_LimitsBase):
    def test_busy_build_slots_refuse_before_registration(self):
        slot = self.admission.environment_slot()
        self.addCleanup(slot.release)
        error = self.refused(
            lambda: self.stub.Execute(
                execution_pb2.ExecuteRequest(
                    payload=b"x",
                    environment=execution_pb2.EnvironmentFiles(
                        pyproject_toml=b"[project]\nname='x'\n", uv_lock=b"version = 1\n"
                    ),
                ),
                timeout=30,
                metadata=self.metadata(),
            )
        )
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertEqual(
            dict(error.trailing_metadata())["nest-error-kind"], "limit_environment_builds"
        )
        self.assert_nothing_sent()
        self.assertEqual(self.sql("SELECT count(*) AS n FROM executions"), [{"n": 0}])
        slot.release()
        slot.release()  # idempotent
        self.admission.environment_slot().release()


class EffectiveAllocationTests(_LimitsBase):
    """Reservations match what Slurm is asked to allocate, not just the RPC's counts."""

    def admit_directly(self, admission=None, **request):
        principal = Principal(self.member, "cli", frozenset({"jobs:run"}), None)
        return (admission or self.admission).admit(
            principal,
            self.project,
            SubmissionRequest("execution", **request),
            execution_id=uuid.uuid4().hex,
        )

    def reservation_of_latest(self) -> dict:
        [attempt] = self.attempts()[-1:]
        reservation = self.registry.reservation(attempt["id"])
        assert reservation is not None
        return reservation

    def test_cpu_only_execute_is_pinned_and_reserves_the_worker_defaults(self):
        self.assertEqual(self.execute(), 42)
        [worker] = self.sbatch_calls()
        self.assertIn("--nodes=1", worker["flags"])
        self.assertIn("--ntasks=1", worker["flags"])
        self.assertIn(f"--cpus-per-task={WORKER_DEFAULT_CPUS}", worker["flags"])
        self.assertFalse(any(f.startswith("--gpus") for f in worker["flags"]))
        reservation = self.reservation_of_latest()
        # The worker script's default GPU is allocated even for a CPU-only call.
        self.assertEqual(
            (reservation["gpu"], reservation["cpu"], reservation["bounded"]),
            (WORKER_DEFAULT_GPUS, WORKER_DEFAULT_CPUS, True),
        )

    def test_explicit_counts_are_passed_and_reserved(self):
        response = self.stub.Execute(
            execution_pb2.ExecuteRequest(
                payload=cloudpickle.dumps((lambda: 1, (), {})), gpu=2, cpu=4
            ),
            timeout=60,
            metadata=self.metadata(),
        )
        self.assertEqual(cloudpickle.loads(response.result), 1)
        [worker] = self.sbatch_calls()
        for flag in ("--nodes=1", "--ntasks=1", "--gpus-per-node=2", "--cpus-per-task=4"):
            self.assertIn(flag, worker["flags"])
        reservation = self.reservation_of_latest()
        self.assertEqual((reservation["gpu"], reservation["cpu"]), (2, 4))

    def test_default_gpus_count_against_the_project_limit(self):
        admission = self.limited_admission(
            limits=AdmissionLimits(gpu_per_project=2, active_per_user=10)
        )
        self.admit_directly(admission)
        self.admit_directly(admission)
        with self.assertRaises(AdmissionRefusedError) as caught:
            self.admit_directly(admission)  # asks for no GPU, but would hold one
        self.assertEqual(caught.exception.reason, "limit_gpu_per_project")

    def test_environment_reserves_the_larger_of_build_and_worker(self):
        admission = self.limited_admission(limits=AdmissionLimits(active_per_user=10))
        self.admit_directly(admission, environment_digest="sha256:" + "a" * 64)
        reservation = self.reservation_of_latest()
        self.assertEqual(
            (reservation["gpu"], reservation["cpu"], reservation["walltime_seconds"]),
            (
                ENVIRONMENT_BUILD_GPUS,
                ENVIRONMENT_BUILD_CPUS,
                slurm_time_seconds(ENVIRONMENT_BUILD_TIME),
            ),
        )
        # A request beyond the build's allocation is reserved as asked.
        self.admit_directly(admission, environment_digest="sha256:" + "b" * 64, gpu=2, cpu=1)
        reservation = self.reservation_of_latest()
        self.assertEqual((reservation["gpu"], reservation["cpu"]), (2, ENVIRONMENT_BUILD_CPUS))

    def test_script_defaults_match_the_reserved_constants(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"

        def directives(name):
            lines = (scripts / name).read_text().splitlines()
            return dict(
                line.removeprefix("#SBATCH --").split("=", 1)
                for line in lines
                if line.startswith("#SBATCH --") and "=" in line
            )

        worker = directives("worker.sbatch")
        self.assertEqual(int(worker["gpus-per-node"]), WORKER_DEFAULT_GPUS)
        self.assertEqual(int(worker["cpus-per-task"]), WORKER_DEFAULT_CPUS)
        build = directives("environment-smoke.sbatch")
        self.assertEqual(int(build["gpus-per-node"]), ENVIRONMENT_BUILD_GPUS)
        self.assertEqual(int(build["cpus-per-task"]), ENVIRONMENT_BUILD_CPUS)
        self.assertEqual(build["time"], ENVIRONMENT_BUILD_TIME)
        self.assertEqual(build["nodes"], "1")
        for script in (worker, build):
            self.assertNotIn("ntasks", script)
            self.assertNotIn("array", script)


class BatchScriptTests(_LimitsBase):
    def write_multi_node_script(self):
        workdir = Path(self.batch_dir())
        (workdir / "wide.sbatch").write_text(
            "#!/bin/bash\n#SBATCH --nodes=4\n#SBATCH --ntasks-per-node=8\n"
            "#SBATCH --gres=gpu:8\necho wide\n"
        )
        return str(workdir)

    def submit_wide(self):
        return self.stub.SubmitJob(
            execution_pb2.SubmitJobRequest(
                workdir=self.write_multi_node_script(), script="wide.sbatch", gpu=1, cpu=1
            ),
            timeout=60,
            metadata=self.metadata(),
        )

    def test_refuse_mode_fails_closed_for_every_batch_script(self):
        self.admission = self.limited_admission(limits=AdmissionLimits(batch_scripts="refuse"))
        self.stub = self.serve(self.admission)
        for call in (self.submit_wide, self.batch):
            error = self.refused(call)
            self.assertEqual(error.code(), grpc.StatusCode.FAILED_PRECONDITION)
            self.assertIn("limit_batch_scripts_unbounded", error.details())
        self.assert_nothing_sent()
        self.assertEqual(self.sql("SELECT count(*) AS n FROM executions"), [{"n": 0}])
        # Execute is still bounded and admitted.
        self.assertEqual(self.execute(), 42)

    def test_trusted_mode_admits_but_never_claims_the_quota(self):
        job = self.submit_wide()
        [sbatch] = self.sbatch_calls()
        # Nothing pretends to pin a script Nest cannot bound.
        self.assertNotIn("--nodes=1", sbatch["flags"])
        self.assertIn("--gpus-per-node=1", sbatch["flags"])
        [attempt] = self.attempts()
        self.assertEqual(attempt["slurm_job_id"], job.job_id)
        reservation = self.registry.reservation(attempt["id"])
        assert reservation is not None
        self.assertFalse(reservation["bounded"])
        self.assertEqual((reservation["gpu"], reservation["cpu"]), (1, 1))
        [admitted] = [
            e for e in self.audit.events(event="execution.admit") if e.outcome == "succeeded"
        ]
        self.assertIs(admitted.details["quota_enforced"], False)

    def test_trusted_batch_jobs_still_count_against_active_limits(self):
        self.hpc.hold_jobs("RUNNING")
        self.submit_wide()
        self.submit_wide()
        error = self.refused(self.submit_wide)
        self.assertIn("limit_active_per_user", error.details())


ENV_ID = "c" * 64


class EnvironmentBuildLifecycleTests(_LimitsBase):
    """A build that may still run keeps its reservation; only terminal evidence frees it."""

    def setUp(self):
        super().setUp()
        self.env_dir = f"{self.hpc.base}/environments/{ENV_ID}"
        self.mocks = {}
        for name, value in (
            ("stage_environment_bytes", self.env_dir),
            ("environment_ready", False),
            ("build_environment", 777),
        ):
            patcher = mock.patch.object(SlurmBackend, name, return_value=value)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def fail_wait(self, error):
        patcher = mock.patch.object(SlurmBackend, "wait_environment", side_effect=error)
        patcher.start()
        self.addCleanup(patcher.stop)

    def execute_with_environment(self):
        return self.refused(
            lambda: self.stub.Execute(
                execution_pb2.ExecuteRequest(
                    payload=b"x",
                    environment=execution_pb2.EnvironmentFiles(
                        pyproject_toml=b"[project]\nname='x'\n", uv_lock=b"version = 1\n"
                    ),
                ),
                timeout=60,
                metadata=self.metadata(),
            )
        )

    def assert_held_unknown(self, build_job_id):
        [held] = self.held()
        self.assertEqual(held["status"], "unknown")
        self.assertEqual(
            (held["gpu"], held["cpu"]), (ENVIRONMENT_BUILD_GPUS, ENVIRONMENT_BUILD_CPUS)
        )
        [attempt] = self.attempts()
        self.assertEqual(attempt["error_kind"], "environment_build_unknown")
        self.assertIsNone(attempt["slurm_job_id"])
        [event] = self.audit.events(event="execution.environment_build_unknown")
        self.assertEqual(event.details["build_job_id"], build_job_id)
        self.assertEqual(self.sbatch_calls(), [])  # the worker was never submitted
        return held

    def test_build_timeout_keeps_the_reservation_across_replicas_and_restart(self):
        self.fail_wait(ExecutionTimeoutError("build still running", job_id=777))
        error = self.execute_with_environment()
        self.assertEqual(error.code(), grpc.StatusCode.DEADLINE_EXCEEDED)
        held = self.assert_held_unknown(777)
        # Another replica (and a restarted Controller) counts it.
        replica = self.limited_admission(db=self.open_database())
        RecoverySweeper(
            replica, timedelta(seconds=1), interval=3600, settle_after=timedelta(0)
        ).sweep()
        self.assertEqual(len(self.held()), 1)
        self.hpc.hold_jobs("RUNNING")
        self.batch(stub=self.serve(replica))
        error = self.refused(lambda: self.batch(stub=self.serve(replica)))
        self.assertIn("limit_active_per_user", error.details())
        # Only an operator's terminal evidence releases it.
        code, _, err = self.nest_db(
            "execution", "reconcile", str(held["attempt_id"]), "--not-submitted"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.held()), 1)

    def test_lost_build_status_keeps_the_reservation(self):
        self.fail_wait(SSHError("Connection closed by remote host"))
        error = self.execute_with_environment()
        self.assertEqual(error.code(), grpc.StatusCode.UNAVAILABLE)
        self.assert_held_unknown(777)

    def test_unknown_build_submission_keeps_the_reservation(self):
        self.mocks["build_environment"].side_effect = SubmissionUnknownError("sbatch timed out")
        error = self.execute_with_environment()
        self.assertEqual(error.code(), grpc.StatusCode.UNKNOWN)
        self.assert_held_unknown(None)

    def test_failed_build_releases(self):
        self.fail_wait(SlurmJobError("build FAILED", job_id=777))
        self.execute_with_environment()
        self.assertEqual(self.held(), [])
        [attempt] = self.attempts()
        self.assertEqual(attempt["status"], "failed")

    def test_client_gone_during_build_staging_never_submits_the_build(self):
        gone = [False]

        def ready_check_while_the_client_leaves(*args, **kwargs):
            gone[0] = True
            return False

        self.mocks["environment_ready"].side_effect = ready_check_while_the_client_leaves
        with mock.patch("nest.controller._still_wanted", lambda context: not gone[0]):
            error = self.execute_with_environment()
        self.assertEqual(error.code(), grpc.StatusCode.CANCELLED)
        self.mocks["build_environment"].assert_not_called()
        self.assertEqual(self.sbatch_calls(), [])
        [attempt] = self.attempts()
        self.assertEqual((attempt["status"], attempt["error_kind"]), ("failed", "client_gone"))
        self.assertEqual(self.held(), [])

    def test_failure_before_any_build_job_releases(self):
        self.mocks["stage_environment_bytes"].side_effect = SSHError("unreachable")
        self.execute_with_environment()
        self.assertEqual(self.held(), [])
        self.mocks["build_environment"].assert_not_called()


class BoundedResultTests(_LimitsBase):
    """The worker's result is never read into the Controller past the RPC bound."""

    LIMIT = 4096

    def bounded_backend(self) -> SlurmBackend:
        return SlurmBackend(dataclasses.replace(self.backend.config, max_result_bytes=self.LIMIT))

    def serve_bounded(self):
        backend = self.bounded_backend()
        admission = RegistryAdmission(
            self.db, backend, self.profile, self.platform_id, audit=self.audit, limits=LIMITS
        )
        self.stub = self.serve(admission, backend)

    def test_oversized_slurm_result_is_refused_unread_and_settled(self):
        self.serve_bounded()
        error = self.refused(lambda: self.execute(lambda: b"x" * 200_000))
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertEqual(dict(error.trailing_metadata())["nest-error-kind"], "result_too_large")
        # The download asked the login node for at most one byte past the bound.
        commands = [c["argv"][-1] for c in self.hpc.calls("ssh")]
        downloads = [c for c in commands if "output.pkl" in c]
        self.assertTrue(downloads)
        self.assertTrue(all(f"head -c {self.LIMIT + 1} " in c for c in downloads), downloads)
        # The job completed, so the attempt is settled and its reservation released.
        [attempt] = self.attempts()
        self.assertEqual((attempt["status"], attempt["error_kind"]), ("failed", "result_too_large"))
        self.assertEqual(self.held(), [])
        # The result stays on the cluster.
        [execution] = self.sql("SELECT execution_id FROM executions")
        output = self.hpc.base / "jobs" / execution["execution_id"] / "output.pkl"
        self.assertGreater(output.stat().st_size, self.LIMIT)

    def test_result_within_the_bound_is_returned(self):
        self.serve_bounded()
        self.assertEqual(self.execute(lambda: b"y" * 1000), b"y" * 1000)

    def test_an_endless_stream_is_cut_at_the_bound(self):
        backend = self.bounded_backend()
        with self.assertRaises(ResultTooLargeError):
            backend._ssh_bounded("yes", self.LIMIT)  # never ends on its own

    def test_error_output_is_bounded_too(self):
        backend = self.bounded_backend()
        with self.assertRaises(RemoteCommandError) as caught:
            backend._ssh_bounded("head -c 20000000 /dev/zero | tr '\\0' e >&2; exit 3", 10)
        self.assertLess(len(str(caught.exception)), 200_000)

    def test_error_after_partial_output_keeps_its_status(self):
        backend = self.bounded_backend()
        with self.assertRaises(RemoteCommandError):
            backend._ssh_bounded("printf abc; exit 4", self.LIMIT)
        self.assertEqual(backend._ssh_bounded("printf abc", self.LIMIT), b"abc")


class BuildAccountTests(_LimitsBase):
    """An environment build is charged to the account authorized for the attempt."""

    def setUp(self):
        super().setUp()
        env_dir = f"{self.hpc.base}/environments/{ENV_ID}"
        for name, value in (("stage_environment_bytes", env_dir), ("environment_ready", False)):
            patcher = mock.patch.object(SlurmBackend, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The build job is recorded failed without running, so no real build runs.
        self.hpc.hold_jobs("FAILED")

    def execute_with_environment(self, **extra):
        return self.refused(
            lambda: self.stub.Execute(
                execution_pb2.ExecuteRequest(
                    payload=b"x",
                    environment=execution_pb2.EnvironmentFiles(
                        pyproject_toml=b"[project]\nname='x'\n", uv_lock=b"version = 1\n"
                    ),
                ),
                timeout=60,
                metadata=self.metadata(**extra),
            )
        )

    def test_cold_cache_build_is_charged_to_the_projects_account(self):
        # The Profile's default account (legacy-acct) is not linked to the Project;
        # its default account is `proj`.
        self.execute_with_environment()
        [build] = self.sbatch_calls()
        self.assertEqual(Path(build["script"]).name, "environment-smoke.sbatch")
        self.assertIn("--account=proj", build["flags"])
        self.assertIn("--partition=dev", build["flags"])
        self.assertNotIn("--account=legacy-acct", build["flags"])
        [attempt] = self.attempts()
        self.assertEqual(attempt["slurm_account"], "proj")

    def test_unlinked_account_refuses_before_any_build(self):
        self.accounts.unlink_project(self.project, self.platform_id, "proj", **OPS)
        self.accounts.unlink_project(self.project, self.platform_id, "spare", **OPS)
        error = self.execute_with_environment()
        self.assertIn(
            error.code(), (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.FAILED_PRECONDITION)
        )
        self.assert_nothing_sent()
        self.assertEqual(self.attempts(), [])


class LimitConfigTests(unittest.TestCase):
    def test_defaults_are_bounded(self):
        limits = load_controller_config({}).limits
        self.assertEqual(limits, AdmissionLimits())
        self.assertEqual(limits.walltime_seconds, 86_400)
        for value in limits.bounds().values():
            self.assertGreater(value, 0)

    def test_settings_are_read_and_validated(self):
        config = load_controller_config(
            {
                "NEST_LIMIT_ACTIVE_PER_USER": "3",
                "NEST_LIMIT_GPU_PER_PROJECT": "7",
                "NEST_LIMIT_WALLTIME": "2-00:00:00",
            }
        )
        self.assertEqual(config.limits.active_per_user, 3)
        self.assertEqual(config.limits.gpu_per_project, 7)
        self.assertEqual(config.limits.walltime_seconds, 2 * 86_400)
        for name, value in (
            ("NEST_LIMIT_ACTIVE_PER_USER", "0"),
            ("NEST_LIMIT_ACTIVE_PER_PROJECT", "-1"),
            ("NEST_LIMIT_CPU_PER_REQUEST", "many"),
            ("NEST_LIMIT_GPU_PER_REQUEST", "99999999"),
            ("NEST_LIMIT_WALLTIME", "UNLIMITED"),
            ("NEST_LIMIT_WALLTIME", "0"),
        ):
            with self.subTest(name=name, value=value), self.assertRaises(ConfigError) as caught:
                load_controller_config({name: value})
            self.assertIn(name, str(caught.exception))

    def test_slurm_time_forms(self):
        for value, seconds in (
            ("30", 1800),
            ("30:15", 1815),
            ("01:02:03", 3723),
            ("2-03", 2 * 86_400 + 3 * 3600),
            ("1-00:30", 86_400 + 1800),
            ("1-00:00:01", 86_401),
        ):
            with self.subTest(value):
                self.assertEqual(slurm_time_seconds(value), seconds)
        for value in ("UNLIMITED", "INFINITE", "", "1:2:3:4", "-1"):
            self.assertIsNone(slurm_time_seconds(value))

    def test_request_limits_without_a_database(self):
        check_request_limits(LIMITS, Reservation(cpu=8, gpu=2, walltime_seconds=3600))
        with self.assertRaises(AdmissionLimitError) as caught:
            check_request_limits(LIMITS, Reservation(walltime_seconds=None))
        self.assertEqual(caught.exception.reason, "walltime_unbounded")
