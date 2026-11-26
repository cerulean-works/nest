"""CI workload identity: service principals and the GitHub Actions OIDC exchange (B09).

Workload tokens are minted by `fake_github_oidc.FakeGitHubOIDC`, a synthetic issuer
with throwaway keys; nothing contacts GitHub and no HPC job runs. PostgreSQL tests
run against the server named by `NEST_TEST_DATABASE_URL` (see `postgres_fixture`).
"""

import io
import json
import secrets
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
import uuid
from concurrent import futures
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from typing import Any

import cloudpickle
import grpc
import jwt
import psycopg
from admin_fixture import owned_project
from fake_github_oidc import (
    AUDIENCE,
    DROP,
    OWNER_ID,
    REF,
    REPOSITORY,
    REPOSITORY_ID,
    WORKFLOW,
    FakeGitHubOIDC,
    new_key,
)
from fake_oidc import CLIENT_ID, CLIENT_SECRET, FakeGoogle
from postgres_fixture import PostgresTestCase, connect
from test_google_auth import free_port

from nest.audit import AuditLog
from nest.auth import (
    GoogleSignIn,
    WorkloadExchange,
    WorkloadExchangeError,
    WorkloadTokenVerifier,
    start_auth_service,
)
from nest.authz import (
    HPC_AUTHORITY_REASON,
    AllowedProjects,
    ExecutionAccess,
    Principal,
    ProjectService,
    ServiceAccess,
    ServiceDeniedError,
)
from nest.config import (
    ConfigError,
    LoginNode,
    PlatformProfile,
    WorkloadIdentityConfig,
    load_controller_config,
    load_database_config,
)
from nest.controller import ExecutionService
from nest.db import (
    Database,
    DatabaseError,
    ExchangeRefusedError,
    IdentityStore,
    JobStore,
    PlatformStore,
    ServicePrincipalError,
    ServicePrincipalStore,
    SessionStore,
    hash_token,
    load_migrations,
    upgrade,
)
from nest.db.cli import main as nest_db
from nest.errors import ERROR_KIND_METADATA
from nest.proto import execution_pb2, execution_pb2_grpc

OPS: dict[str, Any] = {"operator": "ops-ci", "reason": "CI workload identity tests"}
DB_URL = "postgresql://nest@db.example.internal:5432/nest"


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "NEST_DATABASE_URL": DB_URL,
        "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
        "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        "NEST_GOOGLE_REDIRECT_URL": "https://nest.example.com/auth/google/callback",
        "NEST_WORKLOAD_OIDC_AUDIENCE": AUDIENCE,
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v != ""}


class WorkloadConfigTests(unittest.TestCase):
    def test_off_by_default_and_enabled_by_the_audience(self):
        self.assertIsNone(load_controller_config(_env(NEST_WORKLOAD_OIDC_AUDIENCE="")).workload)
        config = load_controller_config(_env())
        self.assertEqual(config.workload, WorkloadIdentityConfig(AUDIENCE, 600.0))
        self.assertEqual(config.summary()["workload"], {"audience": AUDIENCE, "token_ttl": 600.0})
        ttl = load_controller_config(_env(NEST_WORKLOAD_TOKEN_TTL="120")).workload
        assert ttl is not None
        self.assertEqual(ttl.token_ttl, 120.0)

    def test_invalid_or_incomplete_settings_fail_at_start(self):
        cases = {
            "ttl too long": _env(NEST_WORKLOAD_TOKEN_TTL="7200"),
            "ttl too short": _env(NEST_WORKLOAD_TOKEN_TTL="10"),
            "ttl alone": _env(NEST_WORKLOAD_OIDC_AUDIENCE="", NEST_WORKLOAD_TOKEN_TTL="60"),
            "audience with spaces": _env(NEST_WORKLOAD_OIDC_AUDIENCE="a b"),
            "no auth listener": _env(
                NEST_GOOGLE_CLIENT_ID="", NEST_GOOGLE_CLIENT_SECRET="", NEST_GOOGLE_REDIRECT_URL=""
            ),
            "no session auth": _env(NEST_CONTROLLER_AUTH="none"),
        }
        for name, env in cases.items():
            with self.subTest(name), self.assertRaises(ConfigError):
                load_controller_config(env)


class _Base(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = Database(
            load_database_config(
                {"NEST_DATABASE_URL": self.url, "NEST_DATABASE_POOL_MAX_SIZE": "12"}
            )
        )
        self.db.open()
        self.addCleanup(self.db.close)
        self.audit = AuditLog(self.db)
        self.identities = IdentityStore(self.db)
        self.store = ServicePrincipalStore(self.db)
        self.projects = ProjectService(self.db)
        self.owner = self.identities.create_user(display_name="owner").id
        self.project = owned_project(self.projects, self.owner, "ci-project").id
        self.other = owned_project(self.projects, self.owner, "other-project").id
        self.platforms = PlatformStore(self.db)
        profile = PlatformProfile("nano", "A", (LoginNode("login"),), "acct")
        self.platform_id = self.platforms.sync_environment({"nano": profile})["nano"]
        for project in (self.project, self.other):
            self.platforms.grant_project(self.platform_id, project, **OPS)
        self.issuer = FakeGitHubOIDC()
        self.addCleanup(self.issuer.close)
        self.principal = self.store.create_principal("nest-ci", max_active_sessions=10, **OPS)
        self.policy = self.store.add_trust_policy(
            self.principal.id,
            audience=AUDIENCE,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            repository_owner_id=OWNER_ID,
            refs=[REF],
            workflows=[WORKFLOW],
            **OPS,
        )
        self.grant = self.store.grant(self.principal.id, self.project, ["project.read"], **OPS)
        self.exchanger = self.exchange_service()

    def exchange_service(self, ttl: float = 600.0) -> WorkloadExchange:
        verifier = WorkloadTokenVerifier(AUDIENCE, jwks_url=self.issuer.jwks_url, timeout=5)
        return WorkloadExchange(
            WorkloadIdentityConfig(AUDIENCE, ttl), self.db, verifier=verifier, audit=self.audit
        )

    def exchange(self, token: str | None = None, principal=None, **claims):
        return self.exchanger.exchange(
            token or self.issuer.mint(**claims), str(principal or self.principal.id)
        )

    def refused(self, token: str | None = None, principal=None, **claims) -> str:
        with self.assertRaises(WorkloadExchangeError) as caught:
            self.exchange(token, principal, **claims)
        return caught.exception.reason

    def backdated_use(self, jti: str, expired_seconds_ago: int) -> None:
        """Records a token use from two hours ago, as though it had been exchanged
        then. The schema refuses such an insert (its deadline has passed), so the
        insert check is turned off for this one statement, as a superuser."""
        with connect(self.url) as conn:
            conn.execute(
                "ALTER TABLE workload_token_uses DISABLE TRIGGER workload_token_uses_unexpired"
            )
            conn.execute(
                "INSERT INTO workload_token_uses (issuer, jti, service_principal_id, "
                "trust_policy_id, token_expires_at, used_at) VALUES "
                "('https://token.actions.githubusercontent.com', %s, %s, %s, "
                "now() - make_interval(secs => %s), now() - interval '2 hours')",
                (jti, self.principal.id, self.policy.id, expired_seconds_ago),
            )
            conn.execute(
                "ALTER TABLE workload_token_uses ENABLE TRIGGER workload_token_uses_unexpired"
            )

    def sql(self, query: str, *params):
        with connect(self.url) as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else []

    def events(self, event: str):
        return list(reversed(self.audit.events(limit=1000, event=event)))

    def audit_text(self) -> str:
        return "\n".join(
            row[0] for row in self.sql("SELECT to_jsonb(e)::text FROM security_audit_events e")
        )


class ExchangeTests(_Base):
    def test_a_verified_workload_token_gets_a_short_lived_service_session(self):
        workload_token = self.issuer.mint()
        issued = self.exchange(workload_token)
        self.assertTrue(issued.token.startswith("nest_svc_"))
        session = issued.session
        self.assertEqual(
            (session.service_principal_id, session.trust_policy_id),
            (self.principal.id, self.policy.id),
        )
        lifetime = session.expires_at - session.created_at
        self.assertLessEqual(lifetime, timedelta(seconds=601))
        self.assertGreater(lifetime, timedelta(seconds=590))
        self.assertEqual(session.provenance["repository_id"], REPOSITORY_ID)
        self.assertEqual(session.provenance["run_id"], "3000001")
        caller = ServiceAccess(self.db).authenticate(issued.token)
        assert caller is not None
        self.assertEqual(caller.service_principal_id, self.principal.id)
        # Only the digest is stored, and neither token reaches the audit log.
        [row] = self.sql("SELECT token_hash FROM service_sessions")
        self.assertEqual(bytes(row[0]), hash_token(issued.token))
        text = self.audit_text()
        for secret in (issued.token, workload_token, hash_token(issued.token).hex()):
            self.assertNotIn(secret, text)
        [created] = self.events("service_session.created")
        self.assertEqual(created.session_id, session.id)
        self.assertIsNone(created.actor_user_id)
        self.assertEqual(created.details["service_principal_id"], str(self.principal.id))

    def test_forged_expired_and_malformed_tokens_fail_closed(self):
        other_key = new_key()
        now = int(datetime.now(UTC).timestamp())
        cases: dict[str, dict[str, Any]] = {
            "other key": {"key": other_key},
            "unknown kid": {"key": other_key, "kid": "made-up"},
            "hmac forgery": {"algorithm": "HS256"},
            "expired": {"exp": now - 120, "iat": now - 600, "nbf": now - 600},
            "not yet valid": {"nbf": now + 600},
            "issued in the future": {"iat": now + 600, "exp": now + 900},
            "lives too long": {"exp": now + 7200},
            "wrong audience": {"aud": "https://other.example/workload"},
            "several audiences": {"aud": [AUDIENCE, "other"]},
            "wrong issuer": {"iss": "https://token.actions.example.com"},
            "no jti": {"jti": DROP},
            "no repository_id": {"repository_id": DROP},
            "no workflow_ref": {"workflow_ref": DROP},
            "no nbf": {"nbf": DROP},
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                self.assertEqual(self.refused(**overrides), "invalid_token")
        for token in ("", "not.a.jwt", "x" * 10_000):
            with self.subTest(token=token[:10]):
                self.assertEqual(self.refused(token or "e"), "invalid_token")
        unsigned = jwt.encode(self.issuer.claims(), "", algorithm="none")
        self.assertEqual(self.refused(unsigned), "invalid_token")
        self.assertEqual(self.sql("SELECT count(*) FROM service_sessions"), [(0,)])
        # Unverified tokens cannot fill the database.
        self.assertEqual(self.events("workload.exchange"), [])

    def test_only_exact_repository_ref_workflow_and_environment_match(self):
        other_repo = "example-org/other"
        cases: dict[str, dict[str, Any]] = {
            "cross-repository": {
                "repository": other_repo,
                "repository_id": "100000002",
                "workflow_ref": f"{other_repo}/{WORKFLOW}@{REF}",
            },
            "same name, recreated repository": {"repository_id": "100000003"},
            "transferred owner": {"repository_owner_id": "200000002"},
            "renamed to a trusted name": {"repository": "example-org/renamed"},
            "other branch": {
                "ref": "refs/heads/feature",
                "workflow_ref": f"{REPOSITORY}/{WORKFLOW}@refs/heads/feature",
            },
            "pull request": {
                "ref": "refs/pull/1/merge",
                "workflow_ref": f"{REPOSITORY}/{WORKFLOW}@refs/pull/1/merge",
                "event_name": "pull_request",
            },
            "other workflow": {"workflow_ref": f"{REPOSITORY}/.github/workflows/evil.yml@{REF}"},
            "workflow of another ref": {
                "workflow_ref": f"{REPOSITORY}/{WORKFLOW}@refs/heads/feature"
            },
            "workflow of another repository": {"workflow_ref": f"{other_repo}/{WORKFLOW}@{REF}"},
            "prefix of the workflow": {"workflow_ref": f"{REPOSITORY}/{WORKFLOW}.bak/x.yml@{REF}"},
            "unexpected environment": {"environment": "production"},
            # Fork-triggerable events run in the base repository's context, on its
            # default branch, with exactly the trusted ref and workflow_ref.
            "pull_request_target": {"event_name": "pull_request_target"},
            "workflow_run": {"event_name": "workflow_run"},
            "issue_comment": {"event_name": "issue_comment"},
            "pull_request on a branch ref": {"event_name": "pull_request"},
            "event not listed": {"event_name": "workflow_dispatch"},
            "no event": {"event_name": DROP},
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                self.assertEqual(self.refused(**overrides), "not_trusted")
        # An environment-bound policy needs exactly its environment.
        deploy = self.store.create_principal("nest-deploy", **OPS)
        self.store.add_trust_policy(
            deploy.id,
            audience=AUDIENCE,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            repository_owner_id=OWNER_ID,
            refs=[REF],
            workflows=[WORKFLOW],
            environments=["production"],
            **OPS,
        )
        self.assertEqual(self.refused(principal=deploy.id), "not_trusted")
        self.assertEqual(self.refused(principal=deploy.id, environment="staging"), "not_trusted")
        self.exchange(principal=deploy.id, environment="production")
        # A token of the trusted repository names the other principal: no policy of
        # it trusts this exact combination, so a policy never lends its trust.
        self.assertEqual(self.refused(principal=uuid.uuid4()), "not_trusted")
        self.assertEqual(self.refused(principal="not-a-uuid"), "invalid_request")
        self.assertEqual(self.sql("SELECT count(*) FROM service_sessions"), [(1,)])
        # Mismatches from the trusted repository are audited, by check name only.
        denied = self.events("workload.exchange")
        self.assertTrue(denied)
        self.assertTrue(all(e.outcome == "denied" for e in denied))
        self.assertIn("ref", {c for e in denied for c in str(e.details.get("checks")).split(",")})

    def test_only_listed_events_that_a_fork_cannot_trigger_are_trusted(self):
        manual = self.store.create_principal("nest-manual", **OPS)
        self.store.add_trust_policy(
            manual.id,
            audience=AUDIENCE,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            repository_owner_id=OWNER_ID,
            refs=[REF],
            workflows=[WORKFLOW],
            event_names=["workflow_dispatch", "schedule"],
            **OPS,
        )
        self.exchange(principal=manual.id, event_name="workflow_dispatch")
        self.exchange(principal=manual.id, event_name="schedule")
        self.assertEqual(self.refused(principal=manual.id), "not_trusted")  # push
        policy: dict[str, Any] = dict(
            audience=AUDIENCE,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            repository_owner_id=OWNER_ID,
            refs=[REF],
            workflows=[WORKFLOW],
            **OPS,
        )
        for event in ("pull_request_target", "workflow_run", "issue_comment", "pull_request"):
            with self.subTest(event), self.assertRaises(ValueError):
                self.store.add_trust_policy(manual.id, event_names=[event], **policy)
        with self.assertRaises(ValueError):
            self.store.add_trust_policy(manual.id, event_names=[], **policy)
        # The schema refuses them too.
        with (
            self.assertRaises(psycopg.errors.CheckViolation),
            connect(self.url) as conn,
        ):
            conn.execute("SELECT set_config('nest.audit_operator', 'x', false)")
            conn.execute(
                "INSERT INTO workload_trust_policies (service_principal_id, issuer, audience, "
                "repository, repository_id, repository_owner_id, refs, workflows, event_names) "
                "VALUES (%s, 'https://token.actions.githubusercontent.com', 'a', 'o/r', '1', "
                "'2', ARRAY['refs/heads/main'], ARRAY['.github/workflows/a.yml'], "
                "ARRAY['pull_request_target'])",
                (manual.id,),
            )

    def test_token_uses_outlive_the_verifiers_clock_skew(self):
        claims = self.issuer.claims()
        issued = self.exchange(self.issuer.mint(jti=claims["jti"], exp=claims["exp"]))
        [(expires,)] = self.sql(
            "SELECT token_expires_at FROM workload_token_uses WHERE jti = %s", claims["jti"]
        )
        # Kept until exp plus the verifier's 60 seconds of skew, then 60 more.
        self.assertEqual(int(expires.timestamp()), claims["exp"] + 60)
        self.assertIsNotNone(issued)
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.sql("DELETE FROM workload_token_uses WHERE jti = %s", claims["jti"])
        # Boundary: a use whose token stopped being accepted 30 seconds ago, an hour
        # after it was used, is still kept; one from 2 minutes ago may be pruned.
        for jti, ago in (("inside-margin", 30), ("past-margin", 120)):
            self.backdated_use(jti, ago)
        self.assertEqual(self.store.prune_token_uses(), 1)
        remaining = {row[0] for row in self.sql("SELECT jti FROM workload_token_uses")}
        self.assertEqual(remaining, {claims["jti"], "inside-margin"})
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.sql("DELETE FROM workload_token_uses WHERE jti = 'inside-margin'")

    def hold_principal_lock(self, seconds: float) -> threading.Thread:
        """Holds the principal's row lock from another connection for `seconds`, so
        an exchange started meanwhile waits for it."""
        held = threading.Event()

        def hold():
            with connect(self.url) as conn, conn.transaction():
                conn.execute(
                    "SELECT 1 FROM service_principals WHERE id = %s FOR UPDATE",
                    (self.principal.id,),
                )
                held.set()
                time.sleep(seconds)

        thread = threading.Thread(target=hold)
        thread.start()
        self.assertTrue(held.wait(10))
        return thread

    def test_a_token_whose_deadline_passes_during_a_lock_wait_is_refused(self):
        # Hold the lock first, then mint a token whose exp was 55 seconds ago: still
        # inside the verifier's skew, so it verifies even on a slow runner, but its
        # acceptance deadline (exp + 60 s) passes while the exchange waits 7 s.
        holder = self.hold_principal_lock(7.0)
        now = int(time.time())
        token = self.issuer.mint(exp=now - 55, iat=now - 300, nbf=now - 300)
        try:
            self.assertEqual(self.refused(token), "invalid_token")
        finally:
            holder.join()
        self.assertEqual(self.sql("SELECT count(*) FROM workload_token_uses"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM service_sessions"), [(0,)])
        [denied] = [e for e in self.events("workload.exchange") if e.reason == "expired"]
        self.assertEqual(denied.outcome, "denied")

    def test_issuance_uses_the_clock_after_the_lock_wait(self):
        holder = self.hold_principal_lock(2.0)
        started = datetime.now(UTC)
        try:
            issued = self.exchange()
        finally:
            holder.join()
        session = issued.session
        # Created when it was issued, not when its transaction began, and its
        # lifetime counts from then.
        self.assertGreaterEqual(session.created_at, started + timedelta(seconds=1.5))
        self.assertEqual(session.expires_at - session.created_at, timedelta(seconds=600))
        [(used_at,)] = self.sql(
            "SELECT used_at FROM workload_token_uses WHERE jti = %s", session.jti
        )
        # The use is recorded just before the session is issued, after the wait.
        self.assertLessEqual(used_at, session.created_at)
        self.assertLess(session.created_at - used_at, timedelta(seconds=1))
        self.assertGreaterEqual(used_at, started + timedelta(seconds=1.5))

    def test_a_deadline_passing_while_the_replay_key_insert_waits_rolls_everything_back(self):
        """A writer that takes no advisory lock holds the same (issuer, jti)
        uncommitted: the exchange passes its locks, its deadline check and the
        insert trigger, then waits on the unique index. The writer rolls back after
        the deadline, so the insert succeeds; the exchange must still refuse and
        leave neither the token use nor a session."""
        issuer = "https://token.actions.githubusercontent.com"
        other = self.store.create_principal("nest-other", **OPS)
        other_policy = self.store.add_trust_policy(
            other.id,
            audience=AUDIENCE,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            repository_owner_id=OWNER_ID,
            refs=[REF],
            workflows=[WORKFLOW],
            **OPS,
        )
        holding = threading.Event()
        release = threading.Event()

        def writer():
            with connect(self.url) as conn, conn.transaction(force_rollback=True):
                conn.execute(
                    "INSERT INTO workload_token_uses (issuer, jti, service_principal_id, "
                    "trust_policy_id, token_expires_at) VALUES (%s, 'contended', %s, %s, "
                    "clock_timestamp() + interval '1 hour')",
                    (issuer, other.id, other_policy.id),
                )
                holding.set()
                release.wait(20)

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(holding.wait(10))
        result: dict[str, object] = {}
        # Far enough ahead that a slow runner still reaches the unique-index wait
        # before it; the writer is released only once it has passed.
        deadline = datetime.now(UTC) + timedelta(seconds=5)

        def exchange():
            try:
                self.store.exchange(
                    self.principal.id,
                    self.policy.id,
                    issuer=issuer,
                    jti="contended",
                    token_expires_at=deadline,
                    token_hash=hash_token(secrets.token_urlsafe()),
                    ttl_seconds=600,
                    provenance={},
                )
                result["outcome"] = "issued"
            except ExchangeRefusedError as exc:
                result["outcome"] = exc.reason

        exchanger = threading.Thread(target=exchange)
        exchanger.start()
        # Wait until the exchange is blocked on the unique index, then past its
        # deadline, then let the writer roll back.
        for _ in range(200):
            waiting = self.sql(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND query LIKE 'INSERT INTO workload_token_uses%%'"
            )
            if waiting[0][0]:
                break
            time.sleep(0.05)
        self.assertEqual(waiting[0][0], 1, "the exchange should be waiting on the index")
        time.sleep(max(0.0, (deadline - datetime.now(UTC)).total_seconds()) + 0.5)
        release.set()
        thread.join()
        exchanger.join(20)
        self.assertEqual(result.get("outcome"), "expired")
        self.assertEqual(self.sql("SELECT count(*) FROM workload_token_uses"), [(0,)])
        self.assertEqual(self.sql("SELECT count(*) FROM service_sessions"), [(0,)])

    def test_a_pruned_jti_past_its_deadline_is_never_recorded_again(self):
        self.backdated_use("pruned-jti", 120)
        self.assertEqual(self.store.prune_token_uses(), 1)
        with self.assertRaises(ExchangeRefusedError) as caught:
            self.store.exchange(
                self.principal.id,
                self.policy.id,
                issuer="https://token.actions.githubusercontent.com",
                jti="pruned-jti",
                token_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                token_hash=hash_token(secrets.token_urlsafe()),
                ttl_seconds=600,
                provenance={},
            )
        self.assertEqual(caught.exception.reason, "expired")
        self.assertEqual(self.sql("SELECT count(*) FROM workload_token_uses"), [(0,)])
        # The schema refuses it even if the application did not check.
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.sql(
                "INSERT INTO workload_token_uses (issuer, jti, service_principal_id, "
                "trust_policy_id, token_expires_at) VALUES (%s, 'pruned-jti', %s, %s, "
                "clock_timestamp() - interval '1 second')",
                "https://token.actions.githubusercontent.com",
                self.principal.id,
                self.policy.id,
            )

    def test_a_token_is_exchanged_once(self):
        token = self.issuer.mint()
        self.exchange(token)
        self.assertEqual(self.refused(token), "replayed")
        # A replay naming another principal is refused too.
        other = self.store.create_principal("nest-ci-2", **OPS)
        self.store.add_trust_policy(
            other.id,
            audience=AUDIENCE,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            repository_owner_id=OWNER_ID,
            refs=[REF],
            workflows=[WORKFLOW],
            **OPS,
        )
        self.assertEqual(self.refused(token, principal=other.id), "replayed")
        [denied] = [e for e in self.events("workload.exchange") if e.reason == "replayed"][:1]
        self.assertEqual(denied.outcome, "denied")

    def test_concurrent_replays_issue_exactly_one_session(self):
        token = self.issuer.mint()
        results: list[str] = []
        barrier = threading.Barrier(8)

        def attempt():
            barrier.wait()
            try:
                self.exchange(token)
                results.append("issued")
            except WorkloadExchangeError as exc:
                results.append(exc.reason)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("issued"), 1, results)
        self.assertEqual(set(results) - {"issued"}, {"replayed"})

    def test_quotas_bound_sessions_and_exchanges_even_concurrently(self):
        small = self.store.create_principal(
            "nest-small", max_active_sessions=2, max_exchanges_per_hour=3, **OPS
        )
        self.store.add_trust_policy(
            small.id,
            audience=AUDIENCE,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            repository_owner_id=OWNER_ID,
            refs=[REF],
            workflows=[WORKFLOW],
            **OPS,
        )
        tokens = [self.issuer.mint() for _ in range(6)]
        results: list[str] = []
        barrier = threading.Barrier(len(tokens))

        def attempt(token):
            barrier.wait()
            try:
                issued = self.exchange(token, principal=small.id)
                results.append(f"issued:{issued.session.id}")
            except WorkloadExchangeError as exc:
                results.append(exc.reason)

        threads = [threading.Thread(target=attempt, args=(t,)) for t in tokens]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        issued = [r.split(":", 1)[1] for r in results if r.startswith("issued:")]
        self.assertEqual(len(issued), 2, results)
        self.assertEqual(set(results) - {f"issued:{i}" for i in issued}, {"session_quota"})
        # Logging out frees a session slot; the hourly exchange quota still holds.
        self.store.revoke_session(issued[0], reason="logout")
        self.exchange(principal=small.id)
        self.store.revoke_session(
            self.store.active_sessions(small.id)[0].id, operator="ops", note="tests"
        )
        self.assertEqual(self.refused(principal=small.id), "exchange_quota")

    def test_revocation_and_expiry_fail_closed_from_the_next_request(self):
        access = ServiceAccess(self.db)
        cases = {}
        for name in ("session", "logout", "policy"):
            cases[name] = self.exchange().token
        # A second policy, so that revoking the first leaves this session to the
        # principal's own revocation.
        self.store.add_trust_policy(
            self.principal.id,
            audience=AUDIENCE,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            repository_owner_id=OWNER_ID,
            refs=[REF],
            workflows=[WORKFLOW],
            environments=["ci"],
            **OPS,
        )
        cases["principal"] = self.exchange(environment="ci").token
        # Expiry: a session cannot be extended, even in the database (see SchemaTests).
        cases["expired"] = (
            self.exchange_service(ttl=1).exchange(self.issuer.mint(), str(self.principal.id)).token
        )
        session = access.authenticate(cases["session"])
        logout = access.authenticate(cases["logout"])
        assert session is not None and logout is not None
        self.store.revoke_session(session.session_id, operator="ops", note="leaked")
        self.store.revoke_session(logout.session_id, reason="logout")
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.sql(
                "UPDATE service_sessions SET expires_at = expires_at + interval '1 hour' "
                "WHERE token_hash = %s",
                hash_token(cases["expired"]),
            )
        time.sleep(1.2)
        self.assertIsNone(access.authenticate(cases["session"]))
        self.assertIsNone(access.authenticate(cases["logout"]))
        self.assertIsNone(access.authenticate(cases["expired"]))
        self.assertIsNotNone(access.authenticate(cases["policy"]))
        self.store.revoke_policy(self.policy.id, **OPS)
        self.assertIsNone(access.authenticate(cases["policy"]))
        self.assertIsNotNone(access.authenticate(cases["principal"]))
        self.assertEqual(self.refused(), "not_trusted")
        self.store.revoke_principal(self.principal.id, **OPS)
        self.assertIsNone(access.authenticate(cases["principal"]))
        self.assertEqual(self.refused(), "not_trusted")
        # Revoking the principal revoked its grants and every session, in one go.
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM service_sessions WHERE revoked_at IS NULL "
                "AND service_principal_id = %s",
                self.principal.id,
            ),
            [(0,)],
        )
        self.assertEqual(self.store.effective_grants(self.principal.id), [])
        reasons = {e.reason for e in self.events("service_session.revoked")}
        self.assertEqual(reasons, {"revoked", "logout", "policy_revoked", "principal_revoked"})
        self.assertTrue(self.events("service_principal.revoked"))


class SchemaTests(_Base):
    def test_principals_policies_and_grants_are_operator_managed_and_final(self):
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.sql("INSERT INTO service_principals (name) VALUES ('sneaky')")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.sql(
                "INSERT INTO service_principal_grants (service_principal_id, project_id, "
                "capabilities) VALUES (%s, %s, ARRAY['project.read'])",
                self.principal.id,
                self.other,
            )
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.sql(
                "SELECT set_config('nest.audit_operator', 'x', false); "
                "UPDATE workload_trust_policies SET refs = ARRAY['refs/heads/dev']"
            )
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.sql("DELETE FROM service_principals")
        self.store.revoke_principal(self.principal.id, **OPS)
        with (
            self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            connect(self.url) as conn,
        ):
            conn.execute("SELECT set_config('nest.audit_operator', 'x', false)")
            conn.execute("UPDATE service_principals SET status = 'active', revoked_at = NULL")
        with self.assertRaises(ServicePrincipalError):
            self.store.grant(self.principal.id, self.other, ["project.read"], **OPS)

    def test_policies_accept_exact_values_only(self):
        base: dict[str, Any] = {
            "audience": AUDIENCE,
            "repository": REPOSITORY,
            "repository_id": REPOSITORY_ID,
            "repository_owner_id": OWNER_ID,
            "refs": [REF],
            "workflows": [WORKFLOW],
            **OPS,
        }
        bad: dict[str, dict[str, Any]] = {
            "wildcard ref": {"refs": ["refs/heads/*"]},
            "pull request ref": {"refs": ["refs/pull/1/merge"]},
            "bare branch": {"refs": ["main"]},
            "no ref": {"refs": []},
            "wildcard workflow": {"workflows": [".github/workflows/*.yml"]},
            "other path": {"workflows": ["ci.yml"]},
            "wildcard environment": {"environments": ["prod*"]},
            "fork-triggerable event": {"event_names": ["pull_request_target"]},
            "repository pattern": {"repository": "example-org/*"},
            "repository name as id": {"repository_id": "pipeline"},
        }
        for name, change in bad.items():
            with self.subTest(name), self.assertRaises(ValueError):
                self.store.add_trust_policy(self.principal.id, **{**base, **change})
        # The schema refuses them too, whatever writes the row.
        with self.assertRaises(psycopg.errors.CheckViolation), connect(self.url) as conn:
            conn.execute("SELECT set_config('nest.audit_operator', 'x', false)")
            conn.execute(
                "INSERT INTO workload_trust_policies (service_principal_id, issuer, "
                "audience, repository, repository_id, repository_owner_id, refs, workflows) "
                "VALUES (%s, 'https://token.actions.githubusercontent.com', 'a', 'o/r', '1', "
                "'2', ARRAY['refs/heads/*'], ARRAY['.github/workflows/a.yml'])",
                (self.principal.id,),
            )

    def test_grants_need_a_live_project_and_a_granted_platform(self):
        with self.assertRaises(ValueError):
            self.store.grant(self.principal.id, self.other, ["run.submit"], **OPS)
        with self.assertRaises(ValueError):
            self.store.grant(self.principal.id, self.other, ["admin"], **OPS)
        platform = self.platforms.create_platform("ungranted", "U", **OPS)
        with self.assertRaises(ServicePrincipalError):
            self.store.grant(
                self.principal.id, self.other, ["platform.read"], platform_id=platform.id, **OPS
            )
        with self.assertRaises(ServicePrincipalError):
            self.store.grant(self.principal.id, self.project, ["project.read"], **OPS)
        grant = self.store.grant(
            self.principal.id,
            self.project,
            ["platform.read", "run.submit"],
            platform_id=self.platform_id,
            **OPS,
        )
        self.assertIn(grant, self.store.effective_grants(self.principal.id))
        # Revoking the Project's Platform grant takes the Platform grant out of force.
        self.platforms.revoke_project(self.platform_id, self.project, **OPS)
        self.assertNotIn(grant.id, {g.id for g in self.store.effective_grants(self.principal.id)})

    def test_migration_keeps_human_sessions_and_needs_no_service_rows(self):
        """Upgrading a database at 0019 changes nothing a User relies on."""
        url = self.database()
        migrations = load_migrations()
        at_0019 = [m for m in migrations if m.version <= 19]
        with connect(url) as conn:
            upgrade(conn, migrations=at_0019)
        db = Database(load_database_config({"NEST_DATABASE_URL": url}))
        db.open()
        self.addCleanup(db.close)
        identities = IdentityStore(db)
        user = identities.create_user(display_name="before").id
        session = identities.create_session(
            user,
            kind="cli",
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="jobs:run",
        )
        access = secrets.token_urlsafe(32)
        SessionStore(db).issue_access_token(
            session.id, token_hash=hash_token(access), ttl_seconds=600
        )
        with connect(url) as conn:
            applied = upgrade(conn)
        self.assertEqual([m.label for m in applied], ["0020_service_principals"])
        grant = SessionStore(db).authenticate(hash_token(access), kind="cli")
        assert grant is not None
        self.assertEqual(grant.session.user_id, user)
        # No User or human session became a service principal or session.
        with connect(url) as conn:
            counts = conn.execute(
                "SELECT (SELECT count(*) FROM service_principals), "
                "(SELECT count(*) FROM service_sessions)"
            ).fetchone()
        self.assertEqual(counts, (0, 0))

    def test_a_human_token_is_never_a_service_session_and_the_reverse(self):
        issued = self.exchange()
        self.assertIsNone(SessionStore(self.db).authenticate(hash_token(issued.token)))
        session = self.identities.create_session(
            self.owner,
            kind="cli",
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="jobs:run",
        )
        human = "nest_svc_" + secrets.token_urlsafe(32)
        SessionStore(self.db).issue_access_token(
            session.id, token_hash=hash_token(human), ttl_seconds=600
        )
        self.assertIsNone(ServiceAccess(self.db).authenticate(human))


class ServiceAuthorizationTests(_Base):
    def caller(self):
        caller = ServiceAccess(self.db).authenticate(self.exchange().token)
        assert caller is not None
        return caller

    def test_capabilities_are_explicit_and_project_scoped(self):
        access = ServiceAccess(self.db, self.audit)
        caller = self.caller()
        access.require(caller, "project.read", self.project)
        for project, capability in (
            (self.other, "project.read"),
            (uuid.uuid4(), "project.read"),
            (self.project, "platform.read"),
        ):
            with self.subTest(project=project, capability=capability):
                with self.assertRaises(ServiceDeniedError) as caught:
                    access.require(caller, capability, project, platform_id=self.platform_id)
                self.assertEqual(caught.exception.reason, "not_found")
        # Revoking the grant applies to the very next check.
        self.store.revoke_grant(self.grant.id, **OPS)
        with self.assertRaises(ServiceDeniedError):
            access.require(caller, "project.read", self.project)
        denied = [e for e in self.events("project.read") if e.outcome == "denied"]
        self.assertTrue(denied)
        for event in denied:
            self.assertIsNone(event.actor_user_id)
            self.assertEqual(event.session_id, caller.session_id)
            self.assertEqual(event.details["principal_type"], "service")

    def test_execution_capabilities_never_give_hpc_authority(self):
        self.store.grant(
            self.principal.id,
            self.project,
            ["platform.read", "run.read", "run.submit", "run.cancel"],
            platform_id=self.platform_id,
            **OPS,
        )
        access = ServiceAccess(self.db, self.audit)
        caller = self.caller()
        for capability in ("run.submit", "run.read", "run.cancel"):
            with self.subTest(capability), self.assertRaises(ServiceDeniedError) as caught:
                access.require(caller, capability, self.project, platform_id=self.platform_id)
            self.assertEqual(caught.exception.reason, HPC_AUTHORITY_REASON)

    def test_a_service_principal_is_no_project_member(self):
        principal = Principal(self.principal.id, "cli", frozenset({"jobs:run", "projects:read"}))
        self.assertEqual(self.projects.list_projects(principal), [])
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM project_memberships WHERE user_id = %s", self.principal.id
            ),
            [(0,)],
        )


class FakeSlurm:
    def __init__(self):
        self.calls: list[str] = []

    def submit(self, *, script: str, **options) -> int:
        self.calls.append("submit")
        return 4001

    def status(self, job_id: int) -> str:
        self.calls.append("status")
        return "RUNNING"

    def cancel(self, job_id: int) -> None:
        self.calls.append("cancel")


class GrpcTests(_Base):
    def setUp(self):
        super().setUp()
        self.store.grant(
            self.principal.id,
            self.project,
            ["platform.read", "run.read", "run.submit", "run.cancel"],
            platform_id=self.platform_id,
            **OPS,
        )
        self.sql(
            "INSERT INTO provider_profiles (id, ssh_mode, login_nodes, account, partition, "
            "time_limit) VALUES ('nano', 'A', ARRAY['login'], 'acct', 'dev', '00:05:00')"
        )
        self.slurm = FakeSlurm()
        access = ExecutionAccess(
            self.db,
            AllowedProjects([str(self.project)]),
            provider_id="nano",
            platform_id=self.platform_id,
            audit=self.audit,
            workload=True,
        )
        jobs = JobStore(self.db, "nano", platform_id=self.platform_id)
        service = ExecutionService(self.slurm, executor="local", jobs=jobs, access=access)
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(service, self.server)
        port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.addCleanup(self.server.stop, None)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        self.stub = execution_pb2_grpc.ExecutionServiceStub(channel)

    def calls(self, token: str):
        metadata = [
            ("authorization", f"Bearer {token}"),
            ("x-nest-project-id", str(self.project)),
            # Nothing a client sends can select Controller SSH authority.
            ("x-nest-platform", "nano"),
            ("x-nest-ssh-mode", "A"),
            ("x-nest-execution-authority", "controller_owned"),
        ]
        payload = cloudpickle.dumps((lambda: 42, (), {}))
        return {
            "Execute": lambda: self.stub.Execute(
                execution_pb2.ExecuteRequest(payload=payload), timeout=10, metadata=metadata
            ),
            "SubmitJob": lambda: self.stub.SubmitJob(
                execution_pb2.SubmitJobRequest(script="#!/bin/bash\ntrue\n"),
                timeout=10,
                metadata=metadata,
            ),
            "GetJobStatus": lambda: self.stub.GetJobStatus(
                execution_pb2.JobStatusRequest(job_id=4001), timeout=10, metadata=metadata
            ),
            "CancelJob": lambda: self.stub.CancelJob(
                execution_pb2.CancelJobRequest(job_id=4001), timeout=10, metadata=metadata
            ),
            "ListEligibleProjects": lambda: self.stub.ListEligibleProjects(
                execution_pb2.ListEligibleProjectsRequest(), timeout=10, metadata=metadata
            ),
        }

    def test_every_execution_rpc_refuses_a_service_principal(self):
        token = self.exchange().token
        for rpc, call in self.calls(token).items():
            with self.subTest(rpc), self.assertRaises(grpc.RpcError) as caught:
                call()
            error: Any = caught.exception
            self.assertEqual(error.code(), grpc.StatusCode.PERMISSION_DENIED)
            kinds = dict(error.trailing_metadata() or ())
            self.assertEqual(kinds.get(ERROR_KIND_METADATA), HPC_AUTHORITY_REASON)
            self.assertIn(HPC_AUTHORITY_REASON, error.details())
        self.assertEqual(self.slurm.calls, [])
        self.assertEqual(self.sql("SELECT count(*) FROM jobs"), [(0,)])
        denied = [
            e
            for name in ("run.submit", "run.read", "run.cancel")
            for e in self.events(name)
            if e.reason == HPC_AUTHORITY_REASON
        ]
        self.assertEqual(len(denied), 5)
        for event in denied:
            self.assertIsNone(event.actor_user_id)
            self.assertEqual(event.project_id, self.project)
            self.assertEqual(event.details["service_principal_id"], str(self.principal.id))
        self.assertNotIn(token, self.audit_text())

    def test_unknown_or_revoked_service_tokens_are_unauthenticated(self):
        issued = self.exchange()
        self.store.revoke_session(issued.session.id, reason="logout")
        for token in (issued.token, "nest_svc_" + secrets.token_urlsafe(32)):
            with self.subTest(token=token[:12]), self.assertRaises(grpc.RpcError) as caught:
                self.calls(token)["Execute"]()
            self.assertEqual(
                caught.exception.code(),  # ty: ignore[missing-attribute]
                grpc.StatusCode.UNAUTHENTICATED,
            )

    def test_with_the_exchange_off_service_tokens_are_unauthenticated(self):
        token = self.exchange().token
        before = len(self.audit.events(limit=1000))
        access = ExecutionAccess(
            self.db,
            AllowedProjects([str(self.project)]),
            provider_id="nano",
            platform_id=self.platform_id,
            audit=self.audit,
        )
        service = ExecutionService(self.slurm, executor="local", access=access)
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, None)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        with self.assertRaises(grpc.RpcError) as caught:
            stub.ListEligibleProjects(
                execution_pb2.ListEligibleProjectsRequest(),
                timeout=10,
                metadata=[("authorization", f"Bearer {token}")],
            )
        self.assertEqual(
            caught.exception.code(),  # ty: ignore[missing-attribute]
            grpc.StatusCode.UNAUTHENTICATED,
        )
        # Like any unauthenticated call, it writes nothing to the database.
        self.assertEqual(len(self.audit.events(limit=1000)), before)

    def test_a_member_still_runs_mode_a(self):
        """Human CLI sessions and Mode A are unchanged by B09."""
        member = self.identities.create_user(display_name="member").id
        self.projects.add_member(Principal(self.owner, "browser"), self.project, member, "member")
        session = self.identities.create_session(
            member,
            kind="cli",
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="jobs:run",
        )
        token = secrets.token_urlsafe(32)
        SessionStore(self.db).issue_access_token(
            session.id, token_hash=hash_token(token), ttl_seconds=600
        )
        response = self.calls(token)["Execute"]()
        self.assertEqual(cloudpickle.loads(response.result), 42)


class HTTPTests(_Base):
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
        self.http = start_auth_service(config, self.db, sign_in=sign_in, workload=self.exchanger)
        self.addCleanup(self.http.stop)
        self.secrets: list[str] = []

    def call(self, method, path, *, bearer=None, body=None, headers=None):
        sent = dict(headers or {})
        if bearer:
            sent["Authorization"] = f"Bearer {bearer}"
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            sent.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=sent)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as exc:
            with exc:
                raw, status = exc.read(), exc.code
        for secret in self.secrets:
            self.assertNotIn(secret.encode(), raw, "a response must never echo a workload token")
        return status, json.loads(raw or b"{}")

    def token_exchange(self, **claims):
        workload = self.issuer.mint(**claims)
        self.secrets.append(workload)
        return self.call(
            "POST",
            "/auth/workload/token",
            body={"token": workload, "service_principal_id": str(self.principal.id)},
        )

    def test_ci_calls_protected_apis_without_a_browser(self):
        status, issued = self.token_exchange()
        self.assertEqual(status, 200, issued)
        token = issued["access_token"]
        self.assertEqual(issued["token_type"], "Bearer")
        self.assertEqual(issued["principal_type"], "service")
        self.assertLessEqual(issued["expires_in"], 600)
        status, described = self.call("GET", "/auth/workload/session", bearer=token)
        self.assertEqual((status, described["session_id"]), (200, issued["session_id"]))
        status, listed = self.call("GET", "/api/workload/projects", bearer=token)
        self.assertEqual(status, 200)
        self.assertEqual([p["id"] for p in listed["projects"]], [str(self.project)])
        self.assertFalse(listed["projects"][0]["hpc_authority"])
        status, project = self.call("GET", f"/api/workload/projects/{self.project}", bearer=token)
        self.assertEqual((status, project["name"]), (200, "ci-project"))
        self.assertEqual(project["grants"][0]["capabilities"], ["project.read"])
        # Cross-project and unknown Projects look the same: not found.
        for pid in (self.other, uuid.uuid4(), "junk"):
            with self.subTest(project=pid):
                status, _ = self.call("GET", f"/api/workload/projects/{pid}", bearer=token)
                self.assertEqual(status, 404)
        # Logging out ends the session at once.
        status, out = self.call("POST", "/auth/workload/logout", bearer=token)
        self.assertEqual((status, out), (200, {"signed_out": True}))
        status, _ = self.call("GET", "/api/workload/projects", bearer=token)
        self.assertEqual(status, 401)

    def test_service_tokens_reach_no_user_api(self):
        _, issued = self.token_exchange()
        token = issued["access_token"]
        for method, path in (
            ("GET", "/api/projects"),
            ("GET", f"/api/projects/{self.project}"),
            ("GET", "/api/platforms"),
            ("GET", "/api/admin/users"),
            ("GET", "/api/connect"),
            ("POST", "/api/projects"),
        ):
            with self.subTest(path=path):
                status, answer = self.call(method, path, bearer=token)
                self.assertEqual((status, answer.get("reason")), (403, "service_principal"))
        for path in ("/auth/cli/session", "/auth/session"):
            with self.subTest(path=path):
                status, _ = self.call("GET", path, bearer=token)
                self.assertEqual(status, 401)
        # A User's CLI token is not a service session either.
        session = self.identities.create_session(
            self.owner,
            kind="cli",
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope="projects:read",
        )
        human = secrets.token_urlsafe(32)
        SessionStore(self.db).issue_access_token(
            session.id, token_hash=hash_token(human), ttl_seconds=600
        )
        status, answer = self.call("GET", "/api/workload/projects", bearer=human)
        self.assertEqual((status, answer.get("reason")), (403, "service_only"))
        status, _ = self.call("GET", "/auth/workload/session", bearer=human)
        self.assertEqual(status, 401)

    def test_exchange_refusals(self):
        self.assertEqual(self.token_exchange(aud="other")[0], 401)
        self.assertEqual(self.token_exchange(ref="refs/heads/dev")[0], 403)
        workload = self.issuer.mint()
        body = {"token": workload, "service_principal_id": str(self.principal.id)}
        self.assertEqual(self.call("POST", "/auth/workload/token", body=body)[0], 200)
        status, answer = self.call("POST", "/auth/workload/token", body=body)
        self.assertEqual((status, answer), (401, {"error": "replayed"}))
        # A browser can never drive the exchange.
        status, _ = self.call(
            "POST", "/auth/workload/token", body=body, headers={"Origin": self.base}
        )
        self.assertEqual(status, 403)
        status, _ = self.call(
            "POST", "/auth/workload/token", body=body, headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 415)
        self.assertEqual(self.call("GET", "/auth/workload/token")[0], 405)

    def test_without_the_exchange_service_tokens_are_refused(self):
        _, issued = self.token_exchange()
        self.http.stop()
        port = free_port()
        config = load_controller_config(
            {
                "NEST_DATABASE_URL": self.url,
                "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
                "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
                "NEST_GOOGLE_REDIRECT_URL": f"http://127.0.0.1:{port}/auth/google/callback",
                "NEST_AUTH_HTTP_BIND": f"127.0.0.1:{port}",
            }
        ).google
        assert config is not None
        sign_in = GoogleSignIn(config, self.db, endpoints=self.fake.endpoints, timeout=5)
        http = start_auth_service(config, self.db, sign_in=sign_in)
        self.addCleanup(http.stop)
        self.base = f"http://127.0.0.1:{port}"
        status, _ = self.call("GET", "/api/workload/projects", bearer=issued["access_token"])
        self.assertEqual(status, 401)
        body = {"token": self.issuer.mint(), "service_principal_id": str(self.principal.id)}
        self.assertEqual(self.call("POST", "/auth/workload/token", body=body)[0], 404)


class CLITests(_Base):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        env = {"NEST_DATABASE_URL": self.url}
        with (
            unittest.mock.patch.dict("os.environ", env),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = nest_db(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_operator_lifecycle(self):
        ops = ("--operator", "ops-cli", "--reason", "onboard CI")
        code, out, err = self.run_cli("service-principal", "create", "release-ci", *ops)
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(
            "service-principal",
            "trust",
            "release-ci",
            "--audience",
            AUDIENCE,
            "--repository",
            REPOSITORY,
            "--repository-id",
            REPOSITORY_ID,
            "--repository-owner-id",
            OWNER_ID,
            "--ref",
            "refs/tags/v1.0.0",
            "--workflow",
            ".github/workflows/release.yml",
            "--environment",
            "release",
            *ops,
        )
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(
            "service-principal",
            "grant",
            "release-ci",
            "--project-id",
            str(self.project),
            "--platform",
            "nano",
            "--capabilities",
            "platform.read,run.submit",
            *ops,
        )
        self.assertEqual(code, 0, err)
        code, out, _ = self.run_cli("service-principal", "show", "release-ci")
        self.assertIn("refs/tags/v1.0.0", out)
        self.assertIn("capabilities=platform.read,run.submit", out)
        code, _, err = self.run_cli(
            "service-principal", "trust", "release-ci", "--audience", AUDIENCE,
            "--repository", REPOSITORY, "--repository-id", REPOSITORY_ID,
            "--repository-owner-id", OWNER_ID, "--ref", "refs/heads/*",
            "--workflow", WORKFLOW, *ops,
        )  # fmt: skip
        self.assertEqual(code, 2, err)
        code, out, err = self.run_cli("service-principal", "revoke", "release-ci", *ops)
        self.assertEqual((code, "status=revoked" in out), (0, True), err)
        events = {e.event for e in self.audit.events(limit=100)}
        self.assertTrue(
            {
                "service_principal.created",
                "workload_policy.created",
                "service_grant.created",
                "service_grant.revoked",
                "workload_policy.revoked",
                "service_principal.revoked",
            }
            <= events
        )
        operators = {
            e.actor_operator for e in self.audit.events(limit=100) if e.event.startswith("serv")
        }
        self.assertIn("ops-cli", operators)

    def test_changes_need_an_operator_and_reason(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            nest_db(["service-principal", "create", "nameless"])


class ExchangeDatabaseFailureTests(_Base):
    def test_a_database_failure_issues_nothing(self):
        token = self.issuer.mint()
        self.db.close()
        with self.assertRaises(DatabaseError):
            self.exchanger.exchange(token, str(self.principal.id))


if __name__ == "__main__":
    unittest.main()
