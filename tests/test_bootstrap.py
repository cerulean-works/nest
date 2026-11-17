"""One-time platform administrator bootstrap, platform audit log and recovery (B03).

Database tests run against a real PostgreSQL named by `NEST_TEST_DATABASE_URL` (see
`postgres_fixture`); the concurrency tests race real connections.
"""

import dataclasses
import os
import subprocess
import sys
import threading
import unittest
import uuid
from typing import ClassVar

import psycopg
from postgres_fixture import PostgresTestCase, connect

from nest.config import ConfigError, load_controller_config, load_database_config
from nest.db import (
    BootstrapOutcome,
    Database,
    IdentityStore,
    PlatformAdminBootstrap,
    audit_events,
    bootstrap_state,
    load_migrations,
    upgrade,
)

GOOGLE = "https://accounts.google.com"
ADMIN_EMAIL = "first.admin@example.org"
OTHER_EMAIL = "someone.else@example.org"


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
        self.identities = IdentityStore(self.db)
        self.bootstrap = PlatformAdminBootstrap(self.db, ADMIN_EMAIL)
        self.conn = connect(self.url)
        self.addCleanup(self.conn.close)

    def sign_in(self, subject="admin-sub", email=ADMIN_EMAIL, verified=True, issuer=GOOGLE):
        user, identity, _ = self.identities.sign_in(
            issuer=issuer, subject=subject, email=email, email_verified=verified
        )
        return user, identity

    def admins(self) -> set[uuid.UUID]:
        return {user_id for user_id, _ in self.identities.platform_admins()}

    def count(self, table: str) -> int:
        row = self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert row is not None
        return row[0]

    def events(self, event: str | None = None):
        return [e for e in reversed(audit_events(self.db)) if event is None or e.event == event]

    def run_concurrently(self, *calls):
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


class ClaimTests(_Base):
    def test_migration_is_part_of_the_one_chain(self):
        self.assertIn("0005_platform_admin_bootstrap", [m.label for m in load_migrations()])

    def test_configured_verified_google_account_claims_once(self):
        self.assertIsNone(bootstrap_state(self.db))
        user, identity = self.sign_in()
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.CLAIMED)
        self.assertEqual(self.admins(), {user.id})
        state = bootstrap_state(self.db)
        assert state is not None
        self.assertEqual(
            (state.method, state.user_id, state.issuer, state.subject),
            ("google_first_login", user.id, GOOGLE, "admin-sub"),
        )
        # Signing in again changes nothing and records nothing new.
        before = len(self.events())
        _, identity = self.sign_in()
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.ALREADY_COMPLETED)
        self.assertEqual(len(self.events()), before)
        self.assertEqual(self.count("platform_role_assignments"), 1)

    def test_claim_is_audited_without_the_address(self):
        user, identity = self.sign_in()
        self.bootstrap.claim(identity)
        completed, granted = self.events()
        self.assertEqual(
            (completed.event, completed.target_user_id), ("bootstrap_completed", user.id)
        )
        self.assertEqual(
            completed.details,
            {"method": "google_first_login", "issuer": GOOGLE, "subject": "admin-sub"},
        )
        self.assertEqual(
            (granted.event, granted.target_user_id), ("platform_role_granted", user.id)
        )
        self.assertIn("NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL", granted.reason or "")
        self.assertEqual(granted.database_role, completed.database_role)
        self.assertTrue(granted.database_role)
        rows = self.conn.execute("SELECT to_jsonb(e)::text FROM platform_audit_events e").fetchall()
        self.assertFalse([r for r in rows if ADMIN_EMAIL in r[0]])

    def test_not_configured_grants_nothing(self):
        _, identity = self.sign_in()
        outcome = PlatformAdminBootstrap(self.db, None).claim(identity)
        self.assertEqual(outcome, BootstrapOutcome.NOT_CONFIGURED)
        self.assertEqual(self.admins(), set())
        self.assertIsNone(bootstrap_state(self.db))

    def test_only_the_configured_address_matches_ignoring_case(self):
        _, other = self.sign_in("other-sub", OTHER_EMAIL)
        self.assertEqual(self.bootstrap.claim(other), BootstrapOutcome.NOT_CONFIGURED_ACCOUNT)
        self.assertEqual(self.events(), [])  # ordinary sign-ins are not audited
        user, identity = self.sign_in(email=" First.Admin@Example.ORG ")
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.CLAIMED)
        self.assertEqual(self.admins(), {user.id})

    def test_unverified_address_is_refused_and_audited(self):
        user, identity = self.sign_in(verified=False)
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.EMAIL_NOT_VERIFIED)
        self.assertEqual(self.admins(), set())
        self.assertIsNone(bootstrap_state(self.db))
        (refused,) = self.events("bootstrap_refused")
        self.assertEqual(refused.target_user_id, user.id)
        self.assertEqual(refused.details["outcome"], "email_not_verified")
        # A later verified sign-in of the same account still claims.
        _, identity = self.sign_in(verified=True)
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.CLAIMED)

    def test_other_identity_providers_cannot_claim(self):
        _, identity = self.sign_in(issuer="https://login.example.com")
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.NOT_GOOGLE)
        self.assertEqual(self.admins(), set())
        self.assertEqual(self.events("bootstrap_refused")[0].details["outcome"], "not_google")

    def test_disabled_user_cannot_claim(self):
        user, identity = self.sign_in()
        self.identities.set_user_status(user.id, "disabled")
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.USER_NOT_ACTIVE)
        self.assertEqual(self.admins(), set())

    def test_only_stored_claims_count_not_the_callers_values(self):
        _, identity = self.sign_in(verified=False)
        forged = dataclasses.replace(identity, email_verified=True)
        self.assertEqual(self.bootstrap.claim(forged), BootstrapOutcome.EMAIL_NOT_VERIFIED)
        _, other = self.sign_in("other-sub", OTHER_EMAIL)
        forged = dataclasses.replace(other, email=ADMIN_EMAIL, email_verified=True)
        self.assertEqual(self.bootstrap.claim(forged), BootstrapOutcome.NOT_CONFIGURED_ACCOUNT)
        self.assertEqual(self.admins(), set())

    def test_claim_joins_the_callers_transaction(self):
        with self.db.connection() as conn:
            _, identity, _ = self.identities.sign_in(
                issuer=GOOGLE,
                subject="admin-sub",
                email=ADMIN_EMAIL,
                email_verified=True,
                conn=conn,
            )
            self.assertEqual(self.bootstrap.claim(identity, conn=conn), BootstrapOutcome.CLAIMED)
            conn.rollback()
        self.assertIsNone(bootstrap_state(self.db))
        self.assertEqual((self.admins(), self.events()), (set(), []))
        _, identity = self.sign_in()
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.CLAIMED)


class ConcurrencyTests(_Base):
    def test_concurrent_claims_by_one_account_grant_once(self):
        user, identity = self.sign_in()
        results = self.run_concurrently(*[lambda: self.bootstrap.claim(identity)] * 10)
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        self.assertEqual(results.count(BootstrapOutcome.CLAIMED), 1, results)
        self.assertEqual(self.admins(), {user.id})
        self.assertEqual(len(self.events("bootstrap_completed")), 1)

    def test_concurrent_claims_by_different_accounts_grant_once(self):
        # Two Google subjects reporting the configured, verified address, such as
        # a deleted and recreated account, race for the bootstrap.
        identities = [self.sign_in(f"sub-{i}")[1] for i in range(6)]
        results = self.run_concurrently(*[lambda i=i: self.bootstrap.claim(i) for i in identities])
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        self.assertEqual(results.count(BootstrapOutcome.CLAIMED), 1, results)
        self.assertEqual(len(self.admins()), 1)
        self.assertEqual(self.count("platform_bootstrap"), 1)
        self.assertEqual(len(self.events("bootstrap_refused")), 5)

    def test_concurrent_claim_and_direct_grant_leave_one_bootstrap(self):
        _, identity = self.sign_in()
        operator_choice = self.identities.create_user(display_name="op").id
        results = self.run_concurrently(
            lambda: self.bootstrap.claim(identity),
            lambda: self.identities.grant_platform_role(operator_choice, operator="ops"),
        )
        self.assertFalse([r for r in results if isinstance(r, Exception)], results)
        self.assertEqual(self.count("platform_bootstrap"), 1)
        state = bootstrap_state(self.db)
        assert state is not None
        if results[0] is BootstrapOutcome.CLAIMED:
            self.assertEqual(state.method, "google_first_login")
            self.assertEqual(len(self.admins()), 2)
        else:
            self.assertEqual(results[0], BootstrapOutcome.ALREADY_COMPLETED)
            self.assertEqual((state.method, self.admins()), ("direct_grant", {operator_choice}))


class OneTimeTests(_Base):
    def test_changing_the_setting_later_grants_nothing(self):
        first, identity = self.sign_in()
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.CLAIMED)
        changed = PlatformAdminBootstrap(self.db, OTHER_EMAIL)
        other, other_identity = self.sign_in("other-sub", OTHER_EMAIL)
        self.assertEqual(changed.claim(other_identity), BootstrapOutcome.ALREADY_COMPLETED)
        self.assertEqual(self.admins(), {first.id})
        refused = self.events("bootstrap_refused")
        self.assertEqual(
            [(e.target_user_id, e.details["outcome"]) for e in refused],
            [(other.id, "already_completed")],
        )
        # Removing every administrator does not reopen the bootstrap either.
        self.identities.revoke_platform_role(first.id, operator="ops", reason="test")
        self.assertEqual(changed.claim(other_identity), BootstrapOutcome.ALREADY_COMPLETED)
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.ALREADY_COMPLETED)
        self.assertEqual(self.admins(), set())

    def test_any_earlier_grant_closes_the_bootstrap(self):
        chosen = self.identities.create_user(display_name="chosen").id
        self.identities.grant_platform_role(chosen, operator="ops", reason="initial setup")
        _, identity = self.sign_in()
        self.assertEqual(self.bootstrap.claim(identity), BootstrapOutcome.ALREADY_COMPLETED)
        state = bootstrap_state(self.db)
        assert state is not None
        self.assertEqual((state.method, state.user_id), ("direct_grant", chosen))
        self.assertEqual(self.admins(), {chosen})

    def test_raw_sql_grant_also_closes_and_is_audited(self):
        chosen = self.identities.create_user(display_name="chosen").id
        self.conn.execute(
            "INSERT INTO platform_role_assignments (user_id, role) VALUES (%s, 'platform_admin')",
            (chosen,),
        )
        state = bootstrap_state(self.db)
        assert state is not None
        self.assertEqual(state.method, "direct_grant")
        (granted,) = self.events("platform_role_granted")
        self.assertEqual((granted.target_user_id, granted.actor_operator), (chosen, None))
        self.assertTrue(granted.database_role)
        self.conn.execute("DELETE FROM platform_role_assignments")
        self.assertEqual(len(self.events("platform_role_revoked")), 1)

    def test_existing_administrator_counts_as_bootstrapped(self):
        url = self.database()
        with connect(url) as conn:
            upgrade(conn, [m for m in load_migrations() if m.version < 5])
            user = conn.execute("INSERT INTO users DEFAULT VALUES RETURNING id").fetchone()
            assert user is not None
            conn.execute(
                "INSERT INTO platform_role_assignments (user_id, role) VALUES (%s, 'platform_admin')",
                (user[0],),
            )
            upgrade(conn)
            row = conn.execute("SELECT method, user_id FROM platform_bootstrap").fetchone()
            self.assertEqual(row, ("preexisting_admin", user[0]))
            event = conn.execute(
                "SELECT event, target_user_id FROM platform_audit_events"
            ).fetchall()
            self.assertEqual(event, [("bootstrap_completed", user[0])])

    def test_bootstrap_audit_and_role_rows_cannot_be_rewritten(self):
        user, identity = self.sign_in()
        self.bootstrap.claim(identity)
        other = self.identities.create_user().id
        for statement in (
            "UPDATE platform_bootstrap SET user_id = gen_random_uuid()",
            "DELETE FROM platform_bootstrap",
            "TRUNCATE platform_bootstrap",
            "UPDATE platform_audit_events SET reason = 'edited'",
            "DELETE FROM platform_audit_events",
            "TRUNCATE platform_audit_events",
            "TRUNCATE platform_role_assignments",
            f"UPDATE platform_role_assignments SET user_id = '{other}'",
            "INSERT INTO platform_bootstrap (method, user_id) VALUES ('direct_grant', gen_random_uuid())",
        ):
            with self.subTest(statement=statement), self.assertRaises(psycopg.Error):
                self.conn.execute(statement)
        self.assertEqual(self.admins(), {user.id})
        self.assertEqual(self.count("platform_audit_events"), 2)

    def test_deleting_a_grantor_keeps_the_grant_and_its_history(self):
        grantor, admin = self.identities.create_user().id, self.identities.create_user().id
        self.identities.grant_platform_role(admin, granted_by_user_id=grantor)
        # While the grantor exists, the grantor cannot be cleared or replaced directly.
        for value in (None, admin):
            with self.subTest(value=value), self.assertRaises(psycopg.Error):
                self.conn.execute(
                    "UPDATE platform_role_assignments SET granted_by_user_id = %s "
                    "WHERE user_id = %s",
                    (value, admin),
                )
        row = self.conn.execute(
            "SELECT granted_by_user_id FROM platform_role_assignments WHERE user_id = %s",
            (admin,),
        ).fetchone()
        self.assertEqual(row, (grantor,))
        # The 0003 foreign key clears granted_by_user_id; the deletion must not fail.
        self.conn.execute("DELETE FROM users WHERE id = %s", (grantor,))
        row = self.conn.execute(
            "SELECT granted_by_user_id FROM platform_role_assignments WHERE user_id = %s",
            (admin,),
        ).fetchone()
        self.assertEqual(row, (None,))
        self.assertEqual(self.admins(), {admin})
        (granted,) = self.events("platform_role_granted")
        self.assertEqual((granted.target_user_id, granted.actor_user_id), (admin, grantor))
        self.assertEqual(self.events("platform_role_revoked"), [])
        # Once cleared, the grantor can never be set again.
        with self.assertRaises(psycopg.Error):
            self.conn.execute(
                "UPDATE platform_role_assignments SET granted_by_user_id = %s WHERE user_id = %s",
                (admin, admin),
            )

    def test_deleting_a_self_granted_admin_removes_and_audits_the_grant(self):
        admin = self.identities.create_user().id
        self.identities.grant_platform_role(admin, granted_by_user_id=admin)
        self.conn.execute("DELETE FROM users WHERE id = %s", (admin,))
        self.assertEqual(self.admins(), set())
        (revoked,) = self.events("platform_role_revoked")
        self.assertEqual(revoked.target_user_id, admin)


class AuditContextTests(_Base):
    def test_role_changes_record_actor_operator_and_reason(self):
        admin, actor = self.identities.create_user().id, self.identities.create_user().id
        self.identities.grant_platform_role(admin, granted_by_user_id=actor, reason="promoted")
        self.identities.revoke_platform_role(admin, operator="alice-ops", reason="left the team")
        granted, revoked = (e for e in self.events() if e.event.startswith("platform_role"))
        self.assertEqual(
            (granted.actor_user_id, granted.actor_operator, granted.reason),
            (actor, None, "promoted"),
        )
        self.assertEqual(
            (revoked.actor_user_id, revoked.actor_operator, revoked.reason),
            (None, "alice-ops", "left the team"),
        )

    def test_context_does_not_leak_to_later_changes_in_the_transaction(self):
        a, b = self.identities.create_user().id, self.identities.create_user().id
        with self.db.connection() as conn:
            self.identities.grant_platform_role(a, operator="ops", reason="why", conn=conn)
            conn.execute(
                "INSERT INTO platform_role_assignments (user_id, role) VALUES (%s, 'platform_admin')",
                (b,),
            )
        by_target = {e.target_user_id: e for e in self.events("platform_role_granted")}
        self.assertEqual((by_target[a].actor_operator, by_target[a].reason), ("ops", "why"))
        self.assertEqual((by_target[b].actor_operator, by_target[b].reason), (None, None))

    def test_grant_to_a_missing_user_is_refused(self):
        with self.assertRaises(LookupError):
            self.identities.grant_platform_role(uuid.uuid4(), operator="ops")
        self.assertIsNone(bootstrap_state(self.db))

    def test_invalid_operator_or_reason_is_refused(self):
        user = self.identities.create_user().id
        with self.assertRaises(ValueError):
            self.identities.grant_platform_role(user, operator=" ")
        with self.assertRaises(ValueError):
            self.identities.grant_platform_role(user, reason="x" * 1001)
        self.assertEqual(self.admins(), set())


class RecoveryCLITests(_Base):
    def run_cli(self, *argv: str, url: str | None = None) -> tuple[int, str, str]:
        """Runs `nest-db admin` in its own process, as an operator would."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("NEST_DATABASE_")}
        env["NEST_DATABASE_URL"] = url or self.url
        result = subprocess.run(
            [sys.executable, "-m", "nest.db.cli", "admin", *argv],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr

    def test_operator_recovers_after_the_administrator_is_lost(self):
        first, identity = self.sign_in()
        self.bootstrap.claim(identity)
        self.identities.set_user_status(first.id, "disabled")
        replacement, _ = self.sign_in("new-sub", OTHER_EMAIL)

        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("method=google_first_login", out)
        self.assertIn(f"{first.id} granted=", out)
        self.assertIn("status=disabled", out)

        code, out, _ = self.run_cli("find-user", "--email", OTHER_EMAIL.upper())
        self.assertEqual(code, 0)
        self.assertIn(f"{replacement.id} status=active issuer={GOOGLE} subject=new-sub", out)

        code, out, _ = self.run_cli(
            "grant", "--user-id", str(replacement.id), "--operator", "alice-ops",
            "--reason", "first admin account disabled",
        )  # fmt: skip
        self.assertEqual(code, 0, out)
        code, _, _ = self.run_cli(
            "revoke", "--user-id", str(first.id), "--operator", "alice-ops", "--reason", "disabled"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.admins(), {replacement.id})
        # The bootstrap is still the original one: recovery never reopens it.
        state = bootstrap_state(self.db)
        assert state is not None
        self.assertEqual(state.user_id, first.id)

        code, out, _ = self.run_cli("audit")
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(
            [line.split()[1] for line in lines],
            ["bootstrap_completed", "platform_role_granted", "platform_role_granted",
             "platform_role_revoked"],
        )  # fmt: skip
        self.assertIn("actor=alice-ops db_role=postgres", lines[2])
        self.assertIn('reason="first admin account disabled"', lines[2])
        code, out, _ = self.run_cli("audit", "--user-id", str(replacement.id))
        self.assertEqual(len(out.splitlines()), 1)

    def test_changes_that_do_nothing_or_lack_an_operator_fail(self):
        user = self.identities.create_user().id
        code, _, err = self.run_cli(
            "revoke", "--user-id", str(user), "--operator", "o", "--reason", "r"
        )
        self.assertEqual(code, 1)
        self.assertIn("does not have", err)
        code, _, err = self.run_cli(
            "grant", "--user-id", str(uuid.uuid4()), "--operator", "o", "--reason", "r"
        )
        self.assertEqual((code, "does not exist" in err), (1, True))
        code, _, err = self.run_cli("grant", "--user-id", str(user), "--reason", "r")
        self.assertEqual(code, 2)
        self.assertIn("--operator", err)
        code, _, _ = self.run_cli("find-user", "--email", "nobody@example.org")
        self.assertEqual(code, 1)
        self.assertEqual(self.admins(), set())

    def test_schema_must_be_current(self):
        url = self.database()
        with connect(url) as conn:
            upgrade(conn, [m for m in load_migrations() if m.version < 5])
        code, _, err = self.run_cli("status", url=url)
        self.assertEqual(code, 1)
        self.assertIn("nest-db upgrade", err)


class ConfigTests(unittest.TestCase):
    DB: ClassVar[dict[str, str]] = {
        "NEST_DATABASE_URL": "postgresql://nest@db.example.internal:5432/nest",
        "NEST_GOOGLE_CLIENT_ID": "1234-example.apps.googleusercontent.com",
        "NEST_GOOGLE_CLIENT_SECRET": "test-client-secret",
        "NEST_GOOGLE_REDIRECT_URL": "https://nest.example.com/auth/google/callback",
    }

    def test_unset_by_default(self):
        config = load_controller_config({})
        self.assertIsNone(config.bootstrap_admin_email)
        self.assertFalse(config.summary()["bootstrap_admin_configured"])

    def test_address_is_normalized_and_not_logged(self):
        config = load_controller_config(
            {**self.DB, "NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL": "  First.Admin@Example.ORG "}
        )
        self.assertEqual(config.bootstrap_admin_email, ADMIN_EMAIL)
        self.assertTrue(config.summary()["bootstrap_admin_configured"])
        self.assertNotIn(ADMIN_EMAIL, repr(config.summary()).lower())

    def test_invalid_address_or_missing_prerequisites_are_rejected_without_echoing(self):
        for settings, expected in (
            (
                {**self.DB, "NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL": "secret-not-an-address"},
                "must be one",
            ),
            ({**self.DB, "NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL": "a@b.com, c@d.com"}, "must be one"),
            ({"NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL": ADMIN_EMAIL}, "needs NEST_DATABASE_URL"),
            (
                {
                    "NEST_DATABASE_URL": self.DB["NEST_DATABASE_URL"],
                    "NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL": ADMIN_EMAIL,
                },
                "without Google sign-in",
            ),
        ):
            with self.subTest(settings=settings):
                with self.assertRaises(ConfigError) as caught:
                    load_controller_config(settings)
                text = str(caught.exception)
                self.assertIn(expected, text)
                self.assertNotIn(settings["NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL"], text)
