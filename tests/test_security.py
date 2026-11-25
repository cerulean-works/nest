"""Authenticated, authorized execution APIs and the security audit log (B07).

`SecureTransportConfigTests`, `RedactionTests` and `ClientMetadataTests` need no
database. The other tests run against a real PostgreSQL (see `postgres_fixture`):
`ExecutionAccessTests` serve the real gRPC `ExecutionService` in process with
session authentication and check cross-user and cross-project isolation, and
`AuditTriggerTests` and `AuditChainTests` check the trigger-written events,
their redaction and the tamper-evident hash chain.
"""

import io
import json
import logging
import os
import secrets
import tempfile
import types
import unittest
import unittest.mock
import uuid
from concurrent import futures
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from typing import Any

import cloudpickle
import grpc
import psycopg
from admin_fixture import owned_project
from postgres_fixture import PostgresTestCase, connect

from nest import _logging
from nest.audit import AuditLog, audit_interface, redact_details
from nest.authz import (
    AccessDeniedError,
    AllowedProjects,
    ExecutionAccess,
    Principal,
    ProjectService,
)
from nest.client import _controller_error, call_metadata, execute_remote
from nest.config import (
    ClientConfig,
    ConfigError,
    LoginNode,
    PlatformProfile,
    load_client_config,
    load_controller_config,
    load_database_config,
)
from nest.controller import ExecutionService, build_access, record_start
from nest.db import (
    Database,
    IdentityStore,
    JobStore,
    PlatformStore,
    SessionStore,
    hash_token,
    upgrade,
)
from nest.db.cli import main as nest_db
from nest.errors import (
    AuthenticationRequiredError,
    DatabaseUnavailableError,
    RemoteAccessDeniedError,
)
from nest.login import CredentialStore, NestSession
from nest.proto import execution_pb2, execution_pb2_grpc

DB_URL = "postgresql://nest@db.example.internal:5432/nest"
SECRET_EMAIL = "invitee.private@example.org"
OPS: dict[str, Any] = {"operator": "ops-test", "reason": "security tests"}


def browser(user_id: uuid.UUID) -> Principal:
    return Principal(user_id, "browser")


class SecureTransportConfigTests(unittest.TestCase):
    def load(self, **settings):
        return load_controller_config(settings)

    def problems(self, **settings) -> str:
        with self.assertRaises(ConfigError) as caught:
            self.load(**settings)
        return str(caught.exception)

    def test_session_auth_is_the_default_with_a_database(self):
        self.assertEqual(self.load(NEST_DATABASE_URL=DB_URL).auth, "session")
        self.assertEqual(self.load().auth, "none")

    def test_session_auth_needs_a_database(self):
        self.assertIn("needs NEST_DATABASE_URL", self.problems(NEST_CONTROLLER_AUTH="session"))

    def test_tokens_are_never_accepted_over_unapproved_plaintext(self):
        public = {"NEST_DATABASE_URL": DB_URL, "NEST_CONTROLLER_BIND": "0.0.0.0:50051"}
        self.assertIn("NEST_TLS_CERT_FILE", self.problems(**public))
        approved = self.load(**public, NEST_CONTROLLER_ALLOW_PLAINTEXT="true")
        self.assertTrue(approved.allow_plaintext)
        # A loopback listener carries tokens only on the host.
        local = self.load(NEST_DATABASE_URL=DB_URL, NEST_CONTROLLER_BIND="127.0.0.1:50051")
        self.assertEqual(local.auth, "session")
        # Turning authentication off is explicit.
        self.assertEqual(self.load(**public, NEST_CONTROLLER_AUTH="none").auth, "none")

    def test_allowed_projects(self):
        project = uuid.uuid4()
        config = self.load(NEST_DATABASE_URL=DB_URL, NEST_PROVIDER_ALLOWED_PROJECTS=f" {project}, ")
        self.assertEqual(config.allowed_projects, frozenset({str(project)}))
        self.assertEqual(
            self.load(
                NEST_DATABASE_URL=DB_URL, NEST_PROVIDER_ALLOWED_PROJECTS="*"
            ).allowed_projects,
            frozenset({"*"}),
        )
        for bad, message in (
            ("not-a-uuid", "not a Project ID"),
            (f"*,{project}", "cannot combine"),
        ):
            with self.subTest(bad=bad):
                self.assertIn(
                    message,
                    self.problems(NEST_DATABASE_URL=DB_URL, NEST_PROVIDER_ALLOWED_PROJECTS=bad),
                )
        self.assertIn(
            "needs NEST_CONTROLLER_AUTH=session",
            self.problems(NEST_PROVIDER_ALLOWED_PROJECTS="*"),
        )
        # Plaintext approval loosens nothing without authentication.
        self.assertFalse(self.load(NEST_CONTROLLER_ALLOW_PLAINTEXT="true").allow_plaintext)

    def test_summary_names_the_protections(self):
        summary = self.load(NEST_DATABASE_URL=DB_URL).summary()
        self.assertEqual(
            (summary["auth"], summary["allow_plaintext"], summary["allowed_projects"]),
            ("session", False, []),
        )

    def test_client_project_and_auth_url(self):
        project = uuid.uuid4()
        config = load_client_config(
            {"NEST_PROJECT_ID": str(project).upper(), "NEST_AUTH_URL": "https://nest.example.com/"}
        )
        self.assertEqual(config.project_id, str(project))
        self.assertEqual(config.auth_url, "https://nest.example.com")
        with self.assertRaises(ConfigError):
            load_client_config({"NEST_PROJECT_ID": "nope"})
        with self.assertRaises(ConfigError):
            load_client_config({"NEST_AUTH_URL": "http://nest.example.com"})


class RedactionTests(unittest.TestCase):
    def test_secrets_never_reach_details(self):
        token = secrets.token_urlsafe(32)
        registered = "hunter2-registered-secret"
        _logging.register_secret(registered)
        details = redact_details(
            {
                "rpc": "Execute",
                "gpu": 2,
                "ok": True,
                "execution_id": uuid.uuid4().hex,
                "project": str(uuid.uuid4()),
                "note": f"bearer {token}",
                "who": SECRET_EMAIL,
                # Split so the repository credential scan does not match it.
                "pem": "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
                "echo": f"x {registered} y",
                "refresh_token": "short",
                "smtp_password": "short",
                "ssh_key": "short",
                "nested": {"a": 1},
                "Bad Key": "dropped",
            }
        )
        self.assertEqual(details["rpc"], "Execute")
        self.assertEqual((details["gpu"], details["ok"]), (2, True))
        self.assertEqual(len(str(details["execution_id"])), 32)
        self.assertEqual(len(str(details["project"])), 36)
        for key in ("note", "who", "pem", "echo", "refresh_token", "smtp_password", "ssh_key"):
            with self.subTest(key=key):
                self.assertEqual(details[key], "***")
        self.assertEqual(details["nested"], "***")
        self.assertNotIn("Bad Key", details)

    def test_values_are_bounded(self):
        details = redact_details({f"k{i}": "ab " * 200 for i in range(100)})
        self.assertEqual(len(details), 32)
        self.assertTrue(all(len(str(v)) == 200 for v in details.values()))

    def test_log_only_record_is_one_json_line(self):
        log = AuditLog(None)
        with self.assertLogs("nest.audit", logging.INFO) as logs, audit_interface("grpc"):
            log.record("auth.failed", "denied", reason="invalid_token", details={"rpc": "X"})
        line = logs.records[0].getMessage()
        self.assertTrue(line.startswith("Audit {"))
        record = json.loads(line.removeprefix("Audit "))
        self.assertEqual(
            (record["event"], record["interface"], record["reason"]),
            ("auth.failed", "grpc", "invalid_token"),
        )

    def test_names_are_validated(self):
        log = AuditLog(None)
        with self.assertRaises(ValueError):
            log.record("Not An Event", "denied")
        with self.assertRaises(ValueError):
            log.record("run.submit", "maybe")


class ClientMetadataTests(unittest.TestCase):
    def test_metadata_carries_project_and_bearer_only_when_set(self):
        config = ClientConfig(project_id=str(uuid.uuid4()))
        metadata = dict(call_metadata(config, "tok"))
        self.assertEqual(metadata["x-nest-project-id"], config.project_id)
        self.assertEqual(metadata["authorization"], "Bearer tok")
        self.assertEqual(set(dict(call_metadata(ClientConfig(), None))), {"x-nest-request-id"})

    def test_access_errors_are_typed(self):
        class Call(grpc.RpcError):
            def __init__(self, code):
                self._code = code

            def code(self):
                return self._code

            def details(self):
                return "Project not found"

            def trailing_metadata(self):
                return ()

        self.assertIsInstance(
            _controller_error(Call(grpc.StatusCode.UNAUTHENTICATED), "c:1", 1),
            AuthenticationRequiredError,
        )
        for code in (grpc.StatusCode.PERMISSION_DENIED, grpc.StatusCode.NOT_FOUND):
            self.assertIsInstance(_controller_error(Call(code), "c:1", 1), RemoteAccessDeniedError)


class _DatabaseCase(PostgresTestCase):
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
        self.sessions = SessionStore(self.db)
        self.audit = AuditLog(self.db)

    def user(self, name: str = "u") -> uuid.UUID:
        return self.identities.create_user(display_name=name).id

    def token(self, user_id: uuid.UUID, kind: str = "cli", scope: str | None = "jobs:run"):
        """Returns (access token, session) of a new session."""
        session = self.identities.create_session(
            user_id,
            kind=kind,
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=scope if kind == "cli" else None,
        )
        access = secrets.token_urlsafe(32)
        self.sessions.issue_access_token(session.id, token_hash=hash_token(access), ttl_seconds=600)
        return access, session

    def sql(self, query: str, *params):
        with connect(self.url) as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def events(self, event: str | None = None):
        return list(reversed(self.audit.events(limit=1000, event=event)))

    def audit_text(self) -> str:
        """Every column of every audit row, as text, to search for leaks."""
        rows = self.sql("SELECT to_jsonb(e)::text FROM security_audit_events e")
        return "\n".join(row[0] for row in rows)


class FakeSlurm:
    """Answers status and cancellation without SSH; records what it was asked."""

    def __init__(self):
        self.cancelled: list[int] = []
        self.submitted: list[str] = []

    def submit(self, *, script: str, **options) -> int:
        self.submitted.append(script)
        return 4000 + len(self.submitted)

    def status(self, job_id: int) -> str:
        return "RUNNING"

    def cancel(self, job_id: int) -> None:
        self.cancelled.append(job_id)


class ExecutionAccessTests(_DatabaseCase):
    def setUp(self):
        super().setUp()
        projects = ProjectService(self.db)
        self.owner, self.member, self.viewer, self.outsider = (
            self.user(n) for n in ("owner", "member", "viewer", "outsider")
        )
        self.project = owned_project(projects, self.owner, "alpha").id
        projects.add_member(browser(self.owner), self.project, self.member, "member")
        projects.add_member(browser(self.owner), self.project, self.viewer, "viewer")
        # The outsider owns a Project of their own that the Provider has not approved.
        self.other = owned_project(projects, self.outsider, "beta").id
        self.projects = projects
        self.sql(
            "INSERT INTO provider_profiles (id, ssh_mode, login_nodes, account, partition, "
            "time_limit) VALUES ('nano', 'A', ARRAY['login'], 'acct', 'dev', '00:05:00')"
        )
        # The served Platform, granted by the operator to both Projects.
        self.platforms = PlatformStore(self.db)
        profile = PlatformProfile("nano", "A", (LoginNode("login"),), "acct")
        self.platform_id = self.platforms.sync_environment({"nano": profile})["nano"]
        for project in (self.project, self.other):
            self.platforms.grant_project(self.platform_id, project, **OPS)
        self.jobs = JobStore(self.db, "nano", platform_id=self.platform_id)
        self.slurm = FakeSlurm()
        access = ExecutionAccess(
            self.db,
            AllowedProjects([str(self.project)]),
            provider_id="nano",
            platform_id=self.platform_id,
            audit=self.audit,
        )
        self.access = access
        service = ExecutionService(self.slurm, executor="local", jobs=self.jobs, access=access)
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(service, self.server)
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.addCleanup(self.server.stop, None)
        self.channel_address = f"127.0.0.1:{port}"
        self.channel = grpc.insecure_channel(self.channel_address)
        self.addCleanup(self.channel.close)
        self.stub = execution_pb2_grpc.ExecutionServiceStub(self.channel)

    def execute(self, token=None, project=None, **extra):
        metadata = []
        if token is not None:
            metadata.append(("authorization", f"Bearer {token}"))
        if project is not None:
            metadata.append(("x-nest-project-id", str(project)))
        metadata.extend(extra.items())
        payload = cloudpickle.dumps((lambda: 42, (), {}))
        response = self.stub.Execute(
            execution_pb2.ExecuteRequest(payload=payload), timeout=10, metadata=metadata
        )
        return cloudpickle.loads(response.result)

    def code(self, call) -> grpc.StatusCode:
        with self.assertRaises(grpc.RpcError) as caught:
            call()
        return caught.exception.code()  # ty: ignore[missing-attribute]

    def test_execution_requires_a_cli_session(self):
        browser_token, _ = self.token(self.member, kind="browser")
        for token in (None, "not-a-token", browser_token):
            with self.subTest(token=bool(token)):
                status = self.code(lambda t=token: self.execute(t, self.project))
                self.assertEqual(status, grpc.StatusCode.UNAUTHENTICATED)
        # Unauthenticated callers write nothing to the database.
        self.assertEqual(self.events("auth.failed"), [])
        self.assertEqual(self.events("run.submit"), [])

    def test_member_runs_in_an_approved_project_and_is_audited(self):
        token, session = self.token(self.member)
        request_id = uuid.uuid4().hex
        self.assertEqual(self.execute(token, self.project, **{"x-nest-request-id": request_id}), 42)
        [event] = self.events("run.submit")
        self.assertEqual(
            (event.outcome, event.interface, event.actor_user_id, event.session_id),
            ("succeeded", "grpc", self.member, session.id),
        )
        self.assertEqual((event.project_id, event.request_id), (self.project, request_id))
        self.assertEqual(event.resource_type, "job")
        self.assertEqual(event.details["rpc"], "Execute")
        self.assertNotIn(token, self.audit_text())
        self.assertNotIn(hash_token(token).hex(), self.audit_text())

    def test_cross_user_and_cross_project_access_is_denied(self):
        member, _ = self.token(self.member)
        viewer, _ = self.token(self.viewer)
        outsider, _ = self.token(self.outsider)
        read_only, _ = self.token(self.member, scope="projects:read")
        cases = {
            # Not a member: answered as if the Project did not exist.
            "outsider in alpha": (outsider, self.project, grpc.StatusCode.NOT_FOUND),
            "member in beta": (member, self.other, grpc.StatusCode.NOT_FOUND),
            "unknown project": (member, uuid.uuid4(), grpc.StatusCode.NOT_FOUND),
            "viewer": (viewer, self.project, grpc.StatusCode.PERMISSION_DENIED),
            "scope": (read_only, self.project, grpc.StatusCode.PERMISSION_DENIED),
            # The Owner of beta, but the Provider has not approved beta.
            "provider": (outsider, self.other, grpc.StatusCode.PERMISSION_DENIED),
            "no project": (member, None, grpc.StatusCode.INVALID_ARGUMENT),
        }
        for name, (token, project, expected) in cases.items():
            with self.subTest(name):
                status = self.code(lambda t=token, p=project: self.execute(t, p))
                self.assertEqual(status, expected)
        denied = {(e.actor_user_id, e.reason) for e in self.events("run.submit")}
        self.assertEqual(
            denied,
            {
                (self.outsider, "not_a_member"),
                (self.member, "not_a_member"),
                (self.viewer, "role"),
                (self.member, "scope"),
                (self.outsider, "provider_denied"),
            },
        )
        self.assertTrue(all(e.outcome == "denied" for e in self.events("run.submit")))

    def test_revocation_and_removal_apply_to_the_next_call(self):
        token, session = self.token(self.member)
        self.assertEqual(self.execute(token, self.project), 42)
        self.projects.remove_member(browser(self.owner), self.project, self.member)
        self.assertEqual(
            self.code(lambda: self.execute(token, self.project)), grpc.StatusCode.NOT_FOUND
        )
        self.identities.revoke_session(session.id, reason="logout")
        self.assertEqual(
            self.code(lambda: self.execute(token, self.project)),
            grpc.StatusCode.UNAUTHENTICATED,
        )

    def job(self, project, submitter, slurm_job_id: int) -> None:
        record = self.jobs.begin(
            "batch", uuid.uuid4().hex, project_id=project, submitted_by_user_id=submitter
        )
        self.jobs.submitted(record, slurm_job_id)

    def status(self, token, job_id):
        return self.stub.GetJobStatus(
            execution_pb2.JobStatusRequest(job_id=job_id),
            timeout=10,
            metadata=[("authorization", f"Bearer {token}")],
        ).state

    def cancel(self, token, job_id):
        self.stub.CancelJob(
            execution_pb2.CancelJobRequest(job_id=job_id),
            timeout=10,
            metadata=[("authorization", f"Bearer {token}")],
        )

    def test_job_status_and_cancellation_follow_the_job_project(self):
        self.job(self.project, self.member, 101)
        self.job(self.other, self.outsider, 202)
        jobs = self.jobs.begin("batch", uuid.uuid4().hex)  # recorded before authentication
        self.jobs.submitted(jobs, 303)
        member, _ = self.token(self.member)
        owner, _ = self.token(self.owner)
        viewer, _ = self.token(self.viewer)
        outsider, _ = self.token(self.outsider)

        self.assertEqual(self.status(viewer, 101), "RUNNING")
        self.assertEqual(self.status(outsider, 202), "RUNNING")
        for token, job in ((outsider, 101), (member, 202), (member, 303), (member, 999)):
            with self.subTest(job=job):
                status = self.code(lambda t=token, j=job: self.status(t, j))
                self.assertEqual(status, grpc.StatusCode.NOT_FOUND)
                status = self.code(lambda t=token, j=job: self.cancel(t, j))
                self.assertEqual(status, grpc.StatusCode.NOT_FOUND)
        # Only the submitter cancels a Run, even the Owner may not.
        self.assertEqual(
            self.code(lambda: self.cancel(owner, 101)), grpc.StatusCode.PERMISSION_DENIED
        )
        self.assertEqual(
            self.code(lambda: self.cancel(viewer, 101)), grpc.StatusCode.PERMISSION_DENIED
        )
        self.assertEqual(self.slurm.cancelled, [])
        self.cancel(member, 101)
        self.assertEqual(self.slurm.cancelled, [101])
        outcomes = [(e.actor_user_id, e.outcome, e.reason) for e in self.events("run.cancel")]
        self.assertIn((self.member, "succeeded", None), outcomes)
        self.assertIn((self.owner, "denied", "not_own_run"), outcomes)
        self.assertIn((self.outsider, "denied", "not_a_member"), outcomes)
        [cancelled] = [e for e in self.events("run.cancel") if e.outcome == "succeeded"]
        self.assertEqual((cancelled.resource_type, cancelled.resource_id), ("slurm_job", "101"))

    def submit_job(self, token, project):
        return self.stub.SubmitJob(
            execution_pb2.SubmitJobRequest(workdir="/w", script="run.sbatch"),
            timeout=10,
            metadata=[("authorization", f"Bearer {token}"), ("x-nest-project-id", str(project))],
        ).job_id

    def platform_denials(self, event: str):
        return [
            (e.actor_user_id, e.project_id, e.details.get("platform_id"))
            for e in self.events(event)
            if e.reason == "platform_not_authorized"
        ]

    def test_slurm_submission_needs_the_served_platform_granted_to_the_project(self):
        member, _ = self.token(self.member)
        self.assertEqual(self.submit_job(member, self.project), 4001)
        [allowed] = [e for e in self.events("run.submit") if e.outcome == "succeeded"]
        self.assertEqual(allowed.details["platform_id"], str(self.platform_id))

        # A revoked grant is refused from the next call on, as not found, and no
        # trusted Mode A credential runs anything.
        self.platforms.revoke_project(self.platform_id, self.project, **OPS)
        status = self.code(lambda: self.submit_job(member, self.project))
        self.assertEqual(status, grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.slurm.submitted, ["run.sbatch"])
        self.assertEqual(
            self.platform_denials("run.submit"),
            [(self.member, self.project, str(self.platform_id))],
        )
        # A disabled Platform is refused even with the grant restored.
        self.platforms.grant_project(self.platform_id, self.project, **OPS)
        self.platforms.sync_environment({})  # The operator removed the profile.
        status = self.code(lambda: self.submit_job(member, self.project))
        self.assertEqual(status, grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.slurm.submitted, ["run.sbatch"])

    def test_platform_check_comes_after_the_project_check(self):
        # A non-member learns nothing about the Project's Platforms: the refusal is
        # the Project's, and no Platform decision is recorded.
        outsider, _ = self.token(self.outsider)
        self.platforms.revoke_project(self.platform_id, self.project, **OPS)
        status = self.code(lambda: self.submit_job(outsider, self.project))
        self.assertEqual(status, grpc.StatusCode.NOT_FOUND)
        reasons = [e.reason for e in self.events("run.submit")]
        self.assertEqual(reasons, ["not_a_member"])

    def test_controller_without_a_catalog_platform_refuses_platform_bound_calls(self):
        access = ExecutionAccess(
            self.db, AllowedProjects([str(self.project)]), provider_id="nano", audit=self.audit
        )
        member = Principal(self.member, "cli", frozenset({"jobs:run"}))
        with self.assertRaises(AccessDeniedError) as caught:
            access.authorize_submit(member, self.project, platform_bound=True)
        self.assertEqual(caught.exception.reason, "platform_not_authorized")
        self.assertFalse(caught.exception.visible)
        self.assertEqual([e.outcome for e in self.events("run.submit")], ["denied"])

    def test_job_status_and_cancellation_fail_closed_on_revoked_platform_access(self):
        self.job(self.project, self.member, 101)
        member, _ = self.token(self.member)
        self.assertEqual(self.status(member, 101), "RUNNING")
        self.platforms.revoke_project(self.platform_id, self.project, **OPS)
        for call in (self.status, self.cancel):
            with self.subTest(call=call.__name__):
                status = self.code(lambda c=call: c(member, 101))
                self.assertEqual(status, grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.slurm.cancelled, [])
        self.assertEqual(
            self.platform_denials("run.read") + self.platform_denials("run.cancel"),
            [(self.member, self.project, str(self.platform_id))] * 2,
        )
        # Restoring the grant restores access; disabling the Platform ends it again.
        self.platforms.grant_project(self.platform_id, self.project, **OPS)
        self.assertEqual(self.status(member, 101), "RUNNING")
        self.platforms.sync_environment({})  # The operator removed the profile.
        status = self.code(lambda: self.cancel(member, 101))
        self.assertEqual(status, grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.slurm.cancelled, [])

    def test_jobs_of_another_platform_are_not_found(self):
        f1 = self.platforms.create_platform("f1", "F1", **OPS)
        self.platforms.grant_project(f1.id, self.project, **OPS)
        record = JobStore(self.db, "nano", platform_id=f1.id).begin(
            "batch", uuid.uuid4().hex, project_id=self.project, submitted_by_user_id=self.member
        )
        self.jobs.submitted(record, 515)
        member, _ = self.token(self.member)
        for call in (self.status, self.cancel):
            with self.subTest(call=call.__name__):
                status = self.code(lambda c=call: c(member, 515))
                self.assertEqual(status, grpc.StatusCode.NOT_FOUND)
        self.assertEqual(self.slurm.cancelled, [])
        [denied] = [e for e in self.events("run.cancel") if e.outcome == "denied"]
        self.assertEqual(
            (denied.reason, denied.details["job_platform_id"]),
            ("platform_not_authorized", str(f1.id)),
        )

    def test_sdk_sends_the_stored_session_and_project(self):
        token, session = self.token(self.member, scope="jobs:run projects:read")
        now = datetime.now(UTC)
        auth_url = "http://127.0.0.1:9"  # never contacted: the stored token is fresh
        with tempfile.TemporaryDirectory() as home:
            os.chmod(home, 0o700)
            credentials = os.path.join(home, "nest", "credentials.json")
            CredentialStore(credentials).save(
                NestSession(
                    auth_url=auth_url,
                    user_id=self.member,
                    session_id=session.id,
                    scope="jobs:run projects:read",
                    access_token=token,
                    refresh_token="refresh-" + secrets.token_urlsafe(32),
                    access_expires_at=now + timedelta(minutes=10),
                    refresh_expires_at=now + timedelta(hours=1),
                    session_expires_at=now + timedelta(days=1),
                )
            )
            environment = {
                "NEST_CONTROLLER_ADDRESS": self.channel_address,
                "NEST_AUTH_URL": auth_url,
                "NEST_CLI_CREDENTIALS_FILE": credentials,
                "NEST_PROJECT_ID": str(self.project),
            }
            resources = types.SimpleNamespace(gpu=0, cpu=0)
            with unittest.mock.patch.dict("os.environ", environment):
                self.assertEqual(execute_remote(lambda x: x * 2, resources, None, 21), 42)
                other = {"NEST_PROJECT_ID": str(self.other)}
                with (
                    unittest.mock.patch.dict("os.environ", other),
                    self.assertRaises(RemoteAccessDeniedError),
                ):
                    execute_remote(lambda: 1, resources, None)
            with (
                unittest.mock.patch.dict(
                    "os.environ", {"NEST_CONTROLLER_ADDRESS": self.channel_address}
                ),
                self.assertRaises(AuthenticationRequiredError),
            ):
                execute_remote(lambda: 1, resources, None)
        [event] = [e for e in self.events("run.submit") if e.outcome == "succeeded"]
        self.assertEqual((event.actor_user_id, event.project_id), (self.member, self.project))

    def test_submission_is_refused_when_its_record_cannot_be_sealed(self):
        token, _ = self.token(self.member)
        ran = os.path.join(tempfile.mkdtemp(), "ran")

        def touch():
            open(ran, "w").close()

        failure = psycopg.OperationalError("sealing failed")
        with unittest.mock.patch.object(AuditLog, "_seal_or_raise", side_effect=failure):
            payload = cloudpickle.dumps((touch, (), {}))
            with self.assertRaises(grpc.RpcError) as caught:
                self.stub.Execute(
                    execution_pb2.ExecuteRequest(payload=payload),
                    timeout=10,
                    metadata=[
                        ("authorization", f"Bearer {token}"),
                        ("x-nest-project-id", str(self.project)),
                    ],
                )
        self.assertEqual(caught.exception.code(), grpc.StatusCode.UNAVAILABLE)  # ty: ignore[missing-attribute]
        self.assertFalse(os.path.exists(ran))
        # The unsealed record is kept, and sealed by the next seal.
        [event] = self.events("run.submit")
        self.assertIsNone(event.chain_seq)
        self.assertGreaterEqual(self.audit.seal(), 1)
        status = self.audit.verify()
        self.assertEqual((status.intact, status.unsealed), (True, 0))

    def test_build_access_follows_the_configuration(self):
        settings = {"NEST_DATABASE_URL": self.url}
        self.assertIsInstance(
            build_access(load_controller_config(settings), self.db), ExecutionAccess
        )
        none = load_controller_config({**settings, "NEST_CONTROLLER_AUTH": "none"})
        self.assertIsNone(build_access(none, self.db))

    def test_project_api_refusals_are_audited_although_rolled_back(self):
        with self.assertRaises(AccessDeniedError):
            self.projects.rename_project(browser(self.member), self.project, "mine")
        [event] = self.events("project.update")
        self.assertEqual(
            (event.outcome, event.actor_user_id, event.project_id, event.reason),
            ("denied", self.member, self.project, "role"),
        )


class AuditTriggerTests(_DatabaseCase):
    def test_sessions_and_user_status_are_audited(self):
        alice = self.user("alice")
        _, session = self.token(alice, kind="browser")
        self.identities.revoke_session(session.id, reason="logout")
        self.token(alice)
        self.identities.set_user_status(alice, "disabled")
        events = [(e.event, e.reason) for e in self.events()]
        self.assertEqual(
            events,
            [
                ("session.created", None),
                ("session.revoked", "logout"),
                ("session.created", None),
                ("user.disabled", None),
                ("session.revoked", "user_disabled"),
            ],
        )
        created = self.events("session.created")[1]
        self.assertEqual(created.details, {"kind": "cli", "scope": "jobs:run"})
        self.assertTrue(all(e.interface == "database" for e in self.events()))

    def test_operator_changes_name_the_operator(self):
        alice = self.user("alice")
        with connect(self.url) as conn, conn.transaction():
            conn.execute(
                "SELECT set_config('nest.audit_operator', 'ops-oncall', true), "
                "set_config('nest.audit_reason', 'compromised laptop', true)"
            )
            self.identities.set_user_status(alice, "disabled", conn=conn)
        [event] = self.events("user.disabled")
        self.assertEqual(
            (event.actor_operator, event.note, event.target_user_id),
            ("ops-oncall", "compromised laptop", alice),
        )
        self.assertTrue(event.database_role)

    def test_invitation_lifecycle_is_audited_without_addresses_or_tokens(self):
        owner, invitee = self.user("owner"), self.user("invitee")
        project = owned_project(ProjectService(self.db), owner, "p").id
        token = secrets.token_urlsafe(32)
        link_token = secrets.token_urlsafe(32)
        smtp_password = "smtp-password-" + secrets.token_hex(8)
        expires = datetime.now(UTC) + timedelta(days=1)
        [(invitation,)] = self.sql(
            "INSERT INTO project_email_invitations (project_id, invited_email_normalized, role, "
            "invited_by_user_id, token_hash, expires_at) VALUES (%s, %s, 'member', %s, %s, %s) "
            "RETURNING id",
            project,
            SECRET_EMAIL,
            owner,
            hash_token(token),
            expires,
        )
        for attempt in (1, 2):  # sent, then resent
            self.sql(
                "INSERT INTO email_outbox (message_key, kind, subject_ref, recipient, subject, "
                "body_text, message_id, max_attempts) VALUES (%s, 'project_invitation', %s, %s, "
                "'Join', %s, %s, 5)",
                f"invitation:{invitation}:send:{attempt}",
                f"invitation:{invitation}",
                SECRET_EMAIL,
                f"Accept: https://nest.example.com/i/{token} (smtp {smtp_password})",
                f"<{uuid.uuid4().hex}@nest>",
            )
        self.sql(
            "UPDATE project_email_invitations SET status = 'accepted', accepted_at = now(), "
            "accepted_by_user_id = %s WHERE id = %s",
            invitee,
            invitation,
        )
        [(revoked,)] = self.sql(
            "INSERT INTO project_email_invitations (project_id, invited_email_normalized, role, "
            "invited_by_user_id, expires_at) VALUES (%s, 'other@example.org', 'viewer', %s, %s) "
            "RETURNING id",
            project,
            owner,
            expires,
        )
        self.sql(
            "UPDATE project_email_invitations SET status = 'revoked', revoked_at = now(), "
            "revoked_by_user_id = %s WHERE id = %s",
            owner,
            revoked,
        )
        [(link,)] = self.sql(
            "INSERT INTO project_invite_links (project_id, role, created_by_user_id, token_hash, "
            "expires_at, max_uses) VALUES (%s, 'viewer', %s, %s, %s, 3) RETURNING id",
            project,
            owner,
            hash_token(link_token),
            expires,
        )
        guest = self.user("guest")
        self.sql(
            "INSERT INTO project_invitation_redemptions (invite_link_id, user_id) VALUES (%s, %s)",
            link,
            guest,
        )
        self.sql(
            "UPDATE project_invite_links SET revoked_at = now(), revoked_by_user_id = %s "
            "WHERE id = %s",
            owner,
            link,
        )
        events = [
            (e.event, e.actor_user_id, e.resource_id)
            for e in self.events()
            if e.event.startswith(("invitation.", "invite_link."))
        ]
        self.assertEqual(
            events,
            [
                ("invitation.created", owner, str(invitation)),
                ("invitation.email_queued", None, str(invitation)),
                ("invitation.email_queued", None, str(invitation)),
                ("invitation.accepted", invitee, str(invitation)),
                ("invitation.created", owner, str(revoked)),
                ("invitation.revoked", owner, str(revoked)),
                ("invite_link.created", owner, str(link)),
                ("invite_link.redeemed", guest, str(link)),
                ("invite_link.revoked", owner, str(link)),
            ],
        )
        self.assertTrue(
            all(e.project_id == project for e in self.events() if e.event.startswith("invit"))
        )
        text = self.audit_text()
        for leaked in (
            SECRET_EMAIL,
            "other@example.org",
            token,
            link_token,
            hash_token(token).hex(),
            hash_token(link_token).hex(),
            smtp_password,
            "Accept:",
        ):
            with self.subTest(leaked=leaked[:12]):
                self.assertNotIn(leaked, text)

    def test_provider_profile_changes_are_audited_but_restarts_are_not(self):
        insert = (
            "INSERT INTO provider_profiles (id, ssh_mode, login_nodes, account, partition, "
            "time_limit) VALUES ('nano', 'A', ARRAY['login'], 'acct', 'dev', '00:05:00')"
        )
        self.sql(insert)
        self.sql("UPDATE provider_profiles SET updated_at = now(), is_default = true")
        self.sql("UPDATE provider_profiles SET ssh_mode = 'B', login_nodes = ARRAY['l2']")
        details = [e.details for e in self.events("provider.profile_changed")]
        self.assertEqual([d["changed"] for d in details], ["created", "login_nodes,ssh_mode"])

    def test_controller_start_records_its_protections(self):
        config = load_controller_config({"NEST_DATABASE_URL": self.url})
        record_start(config, self.db)
        [event] = self.events("controller.started")
        self.assertEqual(event.interface, "controller")
        self.assertEqual(
            (event.details["auth"], event.details["tls"], event.details["allowed_projects"]),
            ("session", False, 0),
        )
        self.assertNotIn("postgresql://", self.audit_text())


class AuditChainTests(_DatabaseCase):
    def fill(self, count: int = 4) -> None:
        for _ in range(count):
            self.audit.record("run.read", "denied", reason="not_a_member")

    def nest_db(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            unittest.mock.patch.dict("os.environ", {"NEST_DATABASE_URL": self.url}),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            status = nest_db(list(argv))
        return status, out.getvalue(), err.getvalue()

    def test_required_records_are_sealed_or_refused(self):
        row = self.audit.record("run.submit", "succeeded", required=True)
        self.assertIsNotNone(self.audit.events(limit=1)[0].chain_seq)
        self.assertEqual(self.audit.events(limit=1)[0].id, row)
        failure = psycopg.OperationalError("sealing failed")
        with unittest.mock.patch.object(AuditLog, "_seal_or_raise", side_effect=failure):
            # Not required: logged and left for the next seal.
            self.assertIsNotNone(self.audit.record("run.read", "denied"))
            with self.assertRaises(DatabaseUnavailableError):
                self.audit.record("run.cancel", "succeeded", required=True)
        # Sealing that succeeds without sealing the row (it was not committed in
        # time, say) is refused too.
        with (
            unittest.mock.patch.object(AuditLog, "_seal_or_raise", return_value=0),
            self.assertRaises(DatabaseUnavailableError),
        ):
            self.audit.record("run.cancel", "succeeded", required=True)
        self.assertEqual(self.audit.seal(), 3)
        self.assertTrue(self.audit.verify().intact)

    def test_records_are_append_only(self):
        self.fill(1)
        for statement in (
            "UPDATE security_audit_events SET reason = 'allowed'",
            "UPDATE security_audit_events SET chain_seq = 99",
            "DELETE FROM security_audit_events",
            "TRUNCATE security_audit_events",
        ):
            with self.subTest(statement=statement), self.assertRaises(Exception) as caught:
                self.sql(statement)
            self.assertIn("append-only", str(caught.exception))

    def test_chain_is_intact_and_sealed_in_order(self):
        self.fill()
        # Trigger-written rows, such as a new sign-in's, are sealed by the next seal.
        self.token(self.user("late"))
        self.assertEqual(self.audit.verify().unsealed, 1)
        self.assertEqual(self.audit.seal(), 1)
        status = self.audit.verify()
        self.assertEqual((status.intact, status.sealed, status.unsealed), (True, 5, 0))
        self.assertEqual(self.nest_db("audit", "verify")[0], 0)

    @contextmanager
    def tampering(self):
        """What someone with the table owner's credentials could do."""
        with connect(self.url) as conn:
            conn.execute(
                "ALTER TABLE security_audit_events DISABLE TRIGGER security_audit_events_append_only"
            )
            try:
                yield conn
            finally:
                conn.execute(
                    "ALTER TABLE security_audit_events "
                    "ENABLE TRIGGER security_audit_events_append_only"
                )

    def test_a_changed_record_is_detected(self):
        self.fill()
        with self.tampering() as conn:
            conn.execute("UPDATE security_audit_events SET reason = 'allowed' WHERE chain_seq = 2")
        status = self.audit.verify()
        self.assertEqual((status.intact, status.first_broken), (False, 2))
        code, _, err = self.nest_db("audit", "verify")
        self.assertEqual(code, 1)
        self.assertIn("BROKEN at chain_seq=2", err)

    def test_a_removed_record_is_detected(self):
        self.fill()
        with self.tampering() as conn:
            conn.execute("DELETE FROM security_audit_events WHERE chain_seq = 3")
        status = self.audit.verify()
        self.assertEqual((status.intact, status.first_broken), (False, 4))

    def test_nest_db_lists_records(self):
        self.audit.record(
            "run.submit", "denied", actor_user_id=uuid.uuid4(), reason="role", details={"gpu": 1}
        )
        code, out, _ = self.nest_db("audit", "list", "--event", "run.submit")
        self.assertEqual(code, 0)
        self.assertIn("run.submit outcome=denied", out)
        self.assertIn('details={"gpu": 1}', out)


if __name__ == "__main__":
    unittest.main()
