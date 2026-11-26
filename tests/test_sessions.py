"""Browser session management: short-lived access tokens and rotating, revocable
refresh sessions (B04).

The store tests run against a real PostgreSQL (see `postgres_fixture`). The HTTP
tests sign in through `fake_oidc.FakeGoogle` and drive the Controller's auth
listener over real HTTP.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import unittest
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from fake_oidc import CLIENT_ID, CLIENT_SECRET, FakeGoogle
from postgres_fixture import PostgresTestCase, connect
from test_google_auth import Browser, free_port

from nest.auth import (
    INSECURE_REFRESH_COOKIE,
    INSECURE_SESSION_COOKIE,
    LOGIN_PATH,
    LOGOUT_PATH,
    REFRESH_PATH,
    SESSION_PATH,
    GoogleSignIn,
    start_auth_service,
)
from nest.config import (
    ConfigError,
    SessionPolicy,
    load_controller_config,
    load_database_config,
)
from nest.db import (
    Database,
    DatabaseError,
    IdentityStore,
    SessionRefreshError,
    SessionStore,
    hash_token,
    load_migrations,
    upgrade,
)

GOOGLE = "https://accounts.google.com"


def google_env(**overrides: str) -> dict[str, str]:
    env = {
        "NEST_DATABASE_URL": "postgresql://nest@db.example.internal:5432/nest",
        "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
        "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        "NEST_GOOGLE_REDIRECT_URL": "https://nest.example.com/auth/google/callback",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v != ""}


def config_problems(env: dict[str, str]) -> str:
    with unittest.TestCase().assertRaises(ConfigError) as caught:
        load_controller_config(env)
    return str(caught.exception)


class SessionPolicyConfigTests(unittest.TestCase):
    def test_defaults(self):
        google = load_controller_config(google_env()).google
        assert google is not None
        self.assertEqual(google.sessions, SessionPolicy(900, 86_400, 604_800))
        self.assertEqual(google.summary()["sessions"], google.sessions.summary())

    def test_custom_lifetimes(self):
        google = load_controller_config(
            google_env(
                NEST_AUTH_ACCESS_TOKEN_TTL="300",
                NEST_AUTH_REFRESH_TTL="3600",
                NEST_AUTH_SESSION_MAX_LIFETIME="86400",
            )
        ).google
        assert google is not None
        self.assertEqual(google.sessions, SessionPolicy(300, 3600, 86_400))

    def test_invalid_lifetimes(self):
        for overrides, expected in [
            ({"NEST_AUTH_ACCESS_TOKEN_TTL": "0"}, "positive number of seconds"),
            ({"NEST_AUTH_ACCESS_TOKEN_TTL": "soon"}, "positive number of seconds"),
            ({"NEST_AUTH_ACCESS_TOKEN_TTL": "30"}, "between 60 and 3600"),
            ({"NEST_AUTH_ACCESS_TOKEN_TTL": "7200"}, "between 60 and 3600"),
            (
                {"NEST_AUTH_ACCESS_TOKEN_TTL": "900", "NEST_AUTH_REFRESH_TTL": "600"},
                "NEST_AUTH_REFRESH_TTL (600 seconds) must be at least",
            ),
            (
                {"NEST_AUTH_REFRESH_TTL": "7200", "NEST_AUTH_SESSION_MAX_LIFETIME": "3600"},
                "NEST_AUTH_SESSION_MAX_LIFETIME (3600 seconds) must be at least",
            ),
            ({"NEST_AUTH_SESSION_MAX_LIFETIME": "99999999"}, "at most 7776000 seconds"),
        ]:
            with self.subTest(overrides=overrides):
                self.assertIn(expected, config_problems(google_env(**overrides)))

    def test_lifetimes_need_google_sign_in(self):
        message = config_problems({"NEST_AUTH_REFRESH_TTL": "3600"})
        self.assertIn("NEST_AUTH_REFRESH_TTL needs NEST_GOOGLE_CLIENT_ID", message)

    def test_redirect_url_cannot_be_a_session_route(self):
        for path in (SESSION_PATH, REFRESH_PATH, LOGOUT_PATH):
            with self.subTest(path=path):
                message = config_problems(
                    google_env(NEST_GOOGLE_REDIRECT_URL=f"https://nest.example.com{path}")
                )
                self.assertIn("a browser session route", message)

    def test_policy_rejects_lifetimes_that_do_not_nest(self):
        with self.assertRaises(ValueError):
            SessionPolicy(access_token_ttl=600, refresh_ttl=60, max_lifetime=3600)


class _StoreBase(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = Database(load_database_config({"NEST_DATABASE_URL": self.url}))
        self.db.open()
        self.addCleanup(self.db.close)
        self.identities = IdentityStore(self.db)
        self.sessions = SessionStore(self.db)

    def user(self, subject: str = "sub-1") -> uuid.UUID:
        user, _, _ = self.identities.sign_in(
            issuer=GOOGLE, subject=subject, email=f"{subject}@example.org", email_verified=True
        )
        return user.id

    def sign_in(self, user_id, *, refresh_hours=24, family_hours=24 * 7, access_ttl=900):
        """Creates a session family with an access token. Returns (session, refresh, access)."""
        now = datetime.now(UTC)
        refresh, access = f"refresh-{uuid.uuid4()}", f"access-{uuid.uuid4()}"
        session = self.identities.create_session(
            user_id,
            kind="browser",
            token_hash=hash_token(refresh),
            expires_at=now + timedelta(hours=refresh_hours),
            family_expires_at=now + timedelta(hours=family_hours),
        )
        self.sessions.issue_access_token(
            session.id, token_hash=hash_token(access), ttl_seconds=access_ttl
        )
        return session, refresh, access

    def rotate(self, refresh: str, *, refresh_ttl=86_400.0, access_ttl=900.0):
        new_refresh, new_access = f"refresh-{uuid.uuid4()}", f"access-{uuid.uuid4()}"
        rotation = self.sessions.rotate(
            hash_token(refresh),
            new_refresh_hash=hash_token(new_refresh),
            new_access_hash=hash_token(new_access),
            refresh_ttl=refresh_ttl,
            access_ttl=access_ttl,
        )
        return rotation, new_refresh, new_access

    def refused(self, refresh: str) -> str:
        with self.assertRaises(SessionRefreshError) as caught:
            self.rotate(refresh)
        return caught.exception.reason

    def authenticated(self, access: str) -> bool:
        return self.sessions.authenticate(hash_token(access)) is not None

    def sql(self, query: str, *params):
        with connect(self.url) as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else []


class SessionStoreTests(_StoreBase):
    def test_access_token_authenticates_its_session(self):
        alice = self.user()
        session, _, access = self.sign_in(alice)
        grant = self.sessions.authenticate(hash_token(access))
        assert grant is not None
        self.assertEqual((grant.session.id, grant.session.user_id), (session.id, alice))
        self.assertEqual(grant.session.family_id, session.id)
        self.assertLessEqual(grant.access_expires_at, datetime.now(UTC) + timedelta(seconds=901))
        self.assertIsNone(self.sessions.authenticate(hash_token("unknown")))
        self.assertIsNone(self.sessions.authenticate(b"short"))

    def test_rotation_replaces_both_secrets(self):
        alice = self.user()
        first, refresh, access = self.sign_in(alice)
        rotation, new_refresh, new_access = self.rotate(refresh)
        self.assertEqual(rotation.replaced.id, first.id)
        self.assertEqual(rotation.replaced.revoked_reason, "rotated")
        self.assertEqual(rotation.replaced.replaced_by_session_id, rotation.session.id)
        self.assertEqual(rotation.session.family_id, first.family_id)
        self.assertEqual(rotation.session.family_expires_at, first.family_expires_at)
        # The old access token stops working at once; the new pair works.
        self.assertFalse(self.authenticated(access))
        self.assertTrue(self.authenticated(new_access))
        self.assertIsNone(self.identities.active_session(hash_token(refresh)))
        self.assertIsNotNone(self.identities.active_session(hash_token(new_refresh)))
        # Rotating again continues the same family.
        again, _, _ = self.rotate(new_refresh)
        self.assertEqual(again.session.family_id, first.family_id)
        self.assertEqual(len(self.sessions.active_sessions(alice)), 1)

    def test_reused_refresh_secret_revokes_the_family(self):
        alice = self.user()
        _, stolen, _ = self.sign_in(alice)
        other, other_refresh, other_access = self.sign_in(alice)
        _, legitimate, legitimate_access = self.rotate(stolen)
        self.assertEqual(self.refused(stolen), "reused")
        # The thief's replay ended the legitimate session too, and stays committed.
        self.assertFalse(self.authenticated(legitimate_access))
        self.assertEqual(self.refused(legitimate), "revoked")
        reasons = self.sql(
            "SELECT revoked_reason FROM user_sessions WHERE token_hash = %s",
            hash_token(legitimate),
        )
        self.assertEqual(reasons, [("refresh_reuse",)])
        # Another sign-in of the same User is untouched.
        self.assertTrue(self.authenticated(other_access))
        self.assertEqual([s.id for s in self.sessions.active_sessions(alice)], [other.id])
        self.rotate(other_refresh)

    def test_expired_sessions_and_tokens_are_rejected(self):
        alice = self.user()
        session, refresh, access = self.sign_in(alice)
        self.sql(
            "UPDATE session_access_tokens SET created_at = now() - interval '1 hour', "
            "expires_at = now() - interval '1 second'"
        )
        self.assertFalse(self.authenticated(access))
        # An expired access token does not stop the refresh.
        _, refresh, access = self.rotate(refresh)
        self.assertTrue(self.authenticated(access))
        self.sql(
            "UPDATE user_sessions SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 second' WHERE revoked_at IS NULL"
        )
        self.assertFalse(self.authenticated(access))
        self.assertEqual(self.refused(refresh), "expired")
        self.assertEqual(self.refused("never-issued"), "unknown")
        self.assertEqual(session.kind, "browser")

    def test_rotation_never_extends_the_family(self):
        alice = self.user()
        session, refresh, _ = self.sign_in(alice, refresh_hours=1, family_hours=2)
        rotation, _, _ = self.rotate(refresh, refresh_ttl=86_400, access_ttl=86_400)
        self.assertEqual(rotation.session.expires_at, session.family_expires_at)
        self.assertEqual(rotation.access_expires_at, session.family_expires_at)

    def test_access_token_never_outlives_its_session(self):
        alice = self.user()
        session, _, _ = self.sign_in(alice, refresh_hours=1)
        expires = self.sessions.issue_access_token(
            session.id, token_hash=hash_token("long"), ttl_seconds=86_400
        )
        self.assertEqual(expires, session.expires_at)
        self.identities.revoke_session(session.id)
        with self.assertRaises(LookupError):
            self.sessions.issue_access_token(
                session.id, token_hash=hash_token("late"), ttl_seconds=60
            )

    def test_logout_revokes_the_family_from_any_member(self):
        alice = self.user()
        first, refresh, _ = self.sign_in(alice)
        _, refresh, access = self.rotate(refresh)
        # The rotated (first) session's ID still names the family.
        self.assertTrue(self.identities.revoke_session(first.id, reason="logout"))
        self.assertFalse(self.authenticated(access))
        self.assertEqual(self.refused(refresh), "revoked")
        self.assertFalse(self.identities.revoke_session(first.id))
        self.assertEqual(self.sql("SELECT count(*) FROM session_access_tokens"), [(0,)])

    def test_administrative_revocation_and_disable(self):
        alice, bob = self.user("alice"), self.user("bob")
        _, alice_refresh, alice_access = self.sign_in(alice)
        _, _, alice_other = self.sign_in(alice)
        _, bob_refresh, bob_access = self.sign_in(bob)
        self.assertEqual(self.identities.revoke_user_sessions(alice), 2)
        self.assertFalse(self.authenticated(alice_access))
        self.assertFalse(self.authenticated(alice_other))
        self.assertEqual(self.refused(alice_refresh), "revoked")
        self.assertTrue(self.authenticated(bob_access))
        self.identities.set_user_status(bob, "disabled")
        self.assertFalse(self.authenticated(bob_access))
        self.assertEqual(self.refused(bob_refresh), "revoked")

    def test_disabled_user_cannot_use_an_unrevoked_session(self):
        alice = self.user()
        _, refresh, access = self.sign_in(alice)
        # Bypass set_user_status, which would also revoke: the User check alone refuses.
        self.sql("UPDATE users SET status = 'disabled', disabled_at = now() WHERE id = %s", alice)
        self.assertFalse(self.authenticated(access))
        self.assertEqual(self.refused(refresh), "user_disabled")

    def test_revoked_session_cannot_be_restored(self):
        alice = self.user()
        session, _, _ = self.sign_in(alice)
        self.identities.revoke_session(session.id)
        for statement in (
            "UPDATE user_sessions SET revoked_at = NULL, revoked_reason = NULL",
            "UPDATE user_sessions SET revoked_reason = 'admin_revoked'",
            "UPDATE user_sessions SET token_hash = sha256('x')",
            "UPDATE user_sessions SET family_id = gen_random_uuid()",
        ):
            with self.subTest(statement=statement), self.assertRaises(psycopg.Error):
                self.sql(statement)

    def test_concurrent_refreshes_rotate_once(self):
        alice = self.user()
        _, refresh, _ = self.sign_in(alice)
        workers = 6
        barrier = threading.Barrier(workers)
        results: list[object] = [None] * workers

        def run(index: int):
            barrier.wait()
            try:
                results[index] = self.rotate(refresh)[0]
            except SessionRefreshError as exc:
                results[index] = exc.reason

        threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        rotated = [r for r in results if not isinstance(r, str)]
        self.assertEqual(len(rotated), 1, results)
        self.assertEqual(sorted(r for r in results if isinstance(r, str)), ["reused"] * 5)

    def _revoke_during_rotation(self, revoke):
        """
        Runs `revoke` while a rotation of a fresh sign-in is uncommitted, commits the
        rotation, and returns (revoke's result, the new refresh, the new access).
        """
        alice = self.user()
        first, refresh, _ = self.sign_in(alice)
        result: list[object] = []
        with self.db.connection() as rotating:
            new_refresh, new_access = "raced-refresh", "raced-access"
            self.sessions.rotate(
                hash_token(refresh),
                new_refresh_hash=hash_token(new_refresh),
                new_access_hash=hash_token(new_access),
                refresh_ttl=3600,
                access_ttl=900,
                conn=rotating,
            )
            revoker = threading.Thread(target=lambda: result.append(revoke(alice, first)))
            revoker.start()
            time.sleep(0.5)
            self.assertTrue(revoker.is_alive(), "the revocation should wait for the rotation")
            rotating.commit()
        revoker.join(timeout=30)
        self.assertFalse(revoker.is_alive())
        return result[0], new_refresh, new_access

    def test_logout_during_a_refresh_revokes_the_replacement(self):
        revoked, new_refresh, new_access = self._revoke_during_rotation(
            lambda _, first: self.identities.revoke_session(first.id, reason="logout")
        )
        self.assertTrue(revoked)
        self.assertFalse(self.authenticated(new_access))
        self.assertEqual(self.refused(new_refresh), "revoked")

    def test_user_revocation_during_a_refresh_revokes_the_replacement(self):
        count, new_refresh, new_access = self._revoke_during_rotation(
            lambda alice, _: self.identities.revoke_user_sessions(alice)
        )
        self.assertEqual(count, 1)
        self.assertFalse(self.authenticated(new_access))
        self.assertEqual(self.refused(new_refresh), "revoked")

    def test_disable_during_a_refresh_revokes_the_replacement(self):
        _, new_refresh, new_access = self._revoke_during_rotation(
            lambda alice, _: self.identities.set_user_status(alice, "disabled")
        )
        self.assertFalse(self.authenticated(new_access))
        self.assertEqual(self.refused(new_refresh), "revoked")

    def test_refresh_after_a_concurrent_logout_is_refused(self):
        alice = self.user()
        first, refresh, _ = self.sign_in(alice)
        result: list[object] = []
        with self.db.connection() as revoking:
            self.identities.revoke_session(first.id, reason="logout", conn=revoking)
            refresher = threading.Thread(target=lambda: result.append(self.refused(refresh)))
            refresher.start()
            time.sleep(0.5)
            self.assertTrue(refresher.is_alive(), "the refresh should wait for the logout")
            revoking.commit()
        refresher.join(timeout=30)
        self.assertEqual(result, ["revoked"])
        self.assertEqual(self.sessions.active_sessions(alice), [])

    def test_invalid_arguments(self):
        alice = self.user()
        session, refresh, _ = self.sign_in(alice)
        with self.assertRaises(ValueError):
            self.sessions.issue_access_token(session.id, token_hash=b"x", ttl_seconds=60)
        with self.assertRaises(ValueError):
            self.sessions.issue_access_token(session.id, token_hash=hash_token("a"), ttl_seconds=0)
        with self.assertRaises(ValueError):
            self.sessions.rotate(
                hash_token(refresh),
                new_refresh_hash=b"x",
                new_access_hash=hash_token("a"),
                refresh_ttl=60,
                access_ttl=60,
            )
        now = datetime.now(UTC)
        with self.assertRaises(ValueError):
            self.identities.create_session(
                alice,
                kind="browser",
                token_hash=hash_token("b"),
                expires_at=now + timedelta(hours=2),
                family_expires_at=now + timedelta(hours=1),
            )
        with self.assertRaises(DatabaseError):
            self.identities.revoke_session(session.id, reason="Not A Reason")


class SessionMigrationTests(PostgresTestCase):
    def test_existing_sessions_become_their_own_family(self):
        url = self.database()
        migrations = load_migrations()
        before = [m for m in migrations if m.version < 6]
        with connect(url) as conn:
            upgrade(conn, before)
            user = conn.execute("INSERT INTO users DEFAULT VALUES RETURNING id").fetchone()
            assert user is not None
            session = conn.execute(
                "INSERT INTO user_sessions (user_id, kind, token_hash, expires_at) "
                "VALUES (%s, 'browser', sha256('old'), now() + interval '1 hour') "
                "RETURNING id, expires_at",
                (user[0],),
            ).fetchone()
            assert session is not None
            conn.commit()
            upgrade(conn)
            row = conn.execute(
                "SELECT family_id, family_expires_at FROM user_sessions WHERE id = %s",
                (session[0],),
            ).fetchone()
        self.assertEqual(row, (session[0], session[1]))


class BrowserSessionHTTPTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = Database(load_database_config({"NEST_DATABASE_URL": self.url}))
        self.db.open()
        self.addCleanup(self.db.close)
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
                "NEST_AUTH_ACCESS_TOKEN_TTL": "120",
                "NEST_AUTH_REFRESH_TTL": "3600",
                "NEST_AUTH_SESSION_MAX_LIFETIME": "7200",
            }
        ).google
        assert config is not None
        sign_in = GoogleSignIn(config, self.db, endpoints=self.fake.endpoints, timeout=5)
        self.service = start_auth_service(config, self.db, sign_in=sign_in)
        self.addCleanup(self.service.stop)
        self.identities = IdentityStore(self.db)

    def login(self) -> Browser:
        browser = Browser()
        _, headers, _ = browser.get(self.base + LOGIN_PATH)
        _, headers, _ = browser.get(headers["Location"])
        status, _, body = browser.get(headers["Location"])
        self.assertEqual(status, 200, body)
        return browser

    def post(self, browser: Browser, path: str, **headers: str):
        return browser.get(self.base + path, method="POST", headers=headers)

    def describe(self, browser: Browser):
        status, headers, body = browser.get(self.base + SESSION_PATH)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        return status, json.loads(body)

    def same_origin(self) -> dict[str, str]:
        return {"Origin": self.base, "Sec-Fetch-Site": "same-origin"}

    def test_sign_in_sets_configured_lifetimes(self):
        browser = self.login()
        self.assertEqual(browser.cookies[INSECURE_SESSION_COOKIE][2]["max-age"], "120")
        self.assertEqual(browser.cookies[INSECURE_REFRESH_COOKIE][2]["max-age"], "3600")
        status, body = self.describe(browser)
        self.assertEqual(status, 200)
        self.assertEqual(
            set(body),
            {
                "user_id",
                "display_name",
                "email",
                "access_expires_at",
                "refresh_expires_at",
                "session_expires_at",
            },
        )

    def test_refresh_rotates_both_cookies(self):
        browser = self.login()
        old_access = browser.cookies[INSECURE_SESSION_COOKIE][0]
        old_refresh = browser.cookies[INSECURE_REFRESH_COOKIE][0]
        status, _, body = self.post(browser, REFRESH_PATH, **self.same_origin())
        self.assertEqual(status, 200, body)
        new_access = browser.cookies[INSECURE_SESSION_COOKIE][0]
        new_refresh = browser.cookies[INSECURE_REFRESH_COOKIE][0]
        self.assertNotEqual(new_access, old_access)
        self.assertNotEqual(new_refresh, old_refresh)
        self.assertEqual(self.describe(browser)[0], 200)
        stale = Browser()
        stale.cookies[INSECURE_SESSION_COOKIE] = (old_access, "/", {})
        self.assertEqual(self.describe(stale), (401, {"error": "unauthenticated"}))

    def test_replayed_refresh_cookie_ends_the_session(self):
        browser = self.login()
        thief = Browser()
        thief.cookies = dict(browser.cookies)
        status, _, _ = self.post(browser, REFRESH_PATH)
        self.assertEqual(status, 200)
        with self.assertLogs("nest", logging.WARNING) as logs:
            status, _, body = self.post(thief, REFRESH_PATH)
        self.assertEqual((status, json.loads(body)), (401, {"error": "unauthenticated"}))
        self.assertIn("Refresh secret reused", "\n".join(logs.output))
        self.assertNotIn(INSECURE_REFRESH_COOKIE, thief.cookies)
        # The legitimate browser is signed out as well.
        self.assertEqual(self.describe(browser)[0], 401)
        self.assertEqual(self.post(browser, REFRESH_PATH)[0], 401)

    def test_logout_invalidates_and_clears(self):
        browser = self.login()
        copy = Browser()
        copy.cookies = dict(browser.cookies)
        status, _, body = self.post(browser, LOGOUT_PATH, **self.same_origin())
        self.assertEqual((status, json.loads(body)), (200, {"signed_out": True}))
        self.assertNotIn(INSECURE_SESSION_COOKIE, browser.cookies)
        self.assertNotIn(INSECURE_REFRESH_COOKIE, browser.cookies)
        # Copies of the cookies no longer work either.
        self.assertEqual(self.describe(copy)[0], 401)
        self.assertEqual(self.post(copy, REFRESH_PATH)[0], 401)
        with connect(self.url) as conn:
            reasons = conn.execute("SELECT revoked_reason FROM user_sessions").fetchall()
        self.assertEqual(reasons, [("logout",)])
        # Logging out again, or without cookies, is harmless.
        self.assertEqual(self.post(browser, LOGOUT_PATH)[0], 200)

    def test_logout_with_only_the_access_cookie(self):
        browser = self.login()
        del browser.cookies[INSECURE_REFRESH_COOKIE]
        refresh_holder = self.login()
        self.assertEqual(self.post(browser, LOGOUT_PATH)[0], 200)
        with connect(self.url) as conn:
            revoked = conn.execute(
                "SELECT count(*) FROM user_sessions WHERE revoked_reason = 'logout'"
            ).fetchone()
        self.assertEqual(revoked, (1,))
        self.assertEqual(self.describe(refresh_holder)[0], 200)

    def test_admin_revocation_is_immediate(self):
        browser = self.login()
        _, body = self.describe(browser)
        self.identities.revoke_user_sessions(body["user_id"])
        self.assertEqual(self.describe(browser)[0], 401)
        self.assertEqual(self.post(browser, REFRESH_PATH)[0], 401)

    def test_cross_site_requests_are_refused(self):
        browser = self.login()
        refresh = browser.cookies[INSECURE_REFRESH_COOKIE][0]
        for headers in (
            {"Origin": "https://attacker.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
            {"Sec-Fetch-Site": "same-site"},
        ):
            for path in (REFRESH_PATH, LOGOUT_PATH):
                with self.subTest(headers=headers, path=path):
                    status, _, body = self.post(browser, path, **headers)
                    self.assertEqual((status, json.loads(body)), (403, {"error": "cross_origin"}))
        # Nothing was rotated or revoked.
        self.assertEqual(browser.cookies[INSECURE_REFRESH_COOKIE][0], refresh)
        self.assertEqual(self.describe(browser)[0], 200)

    def test_methods_and_missing_cookies(self):
        browser = Browser()
        for path in (REFRESH_PATH, LOGOUT_PATH):
            status, headers, _ = browser.get(self.base + path)
            self.assertEqual((status, headers["Allow"]), (405, "POST"))
        status, headers, _ = self.post(browser, SESSION_PATH)
        self.assertEqual((status, headers["Allow"]), (405, "GET"))
        self.assertEqual(self.describe(browser), (401, {"error": "unauthenticated"}))
        self.assertEqual(self.post(browser, REFRESH_PATH)[0], 401)
        browser.cookies[INSECURE_REFRESH_COOKIE] = ("forged", "/auth/session", {})
        self.assertEqual(self.post(browser, REFRESH_PATH)[0], 401)

    def test_logs_contain_no_session_credentials(self):
        with self.assertLogs("nest", logging.DEBUG) as logs:
            browser = self.login()
            seen = [browser.cookies[INSECURE_SESSION_COOKIE][0]]
            seen.append(browser.cookies[INSECURE_REFRESH_COOKIE][0])
            replay = dict(browser.cookies)
            self.post(browser, REFRESH_PATH)
            seen.append(browser.cookies[INSECURE_SESSION_COOKIE][0])
            seen.append(browser.cookies[INSECURE_REFRESH_COOKIE][0])
            thief = Browser()
            thief.cookies = replay
            self.post(thief, REFRESH_PATH)
            self.describe(browser)
            self.post(browser, LOGOUT_PATH)
        output = "\n".join(logs.output)
        self.assertIn("Session refreshed", output)
        self.assertIn("Signed out", output)
        for secret in seen:
            self.assertNotIn(secret, output)
            self.assertNotIn(hash_token(secret).hex(), output)


class SessionCLITests(_StoreBase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("NEST_DATABASE_")}
        env["NEST_DATABASE_URL"] = self.url
        result = subprocess.run(
            [sys.executable, "-m", "nest.db.cli", "sessions", *argv],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr

    def test_list_and_revoke(self):
        alice, bob = self.user("alice"), self.user("bob")
        first, refresh, _ = self.sign_in(alice)
        _, _, other_access = self.sign_in(alice)
        _, _, bob_access = self.sign_in(bob)
        rotation, _, access = self.rotate(refresh)

        code, out, _ = self.run_cli("list", "--user-id", str(alice))
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn(f"family={first.id}", out)
        self.assertNotIn(refresh, out)

        code, out, _ = self.run_cli("revoke", "--session-id", str(rotation.session.id))
        self.assertEqual(code, 0, out)
        self.assertFalse(self.authenticated(access))
        self.assertTrue(self.authenticated(other_access))
        code, _, err = self.run_cli("revoke", "--session-id", str(first.id))
        self.assertEqual(code, 1)
        self.assertIn("No change", err)

        code, out, _ = self.run_cli("revoke", "--user-id", str(alice))
        self.assertEqual((code, out.strip()), (0, f"Revoked 1 session(s) of {alice}"))
        self.assertFalse(self.authenticated(other_access))
        self.assertTrue(self.authenticated(bob_access))
        self.assertEqual(self.run_cli("revoke", "--user-id", str(alice))[0], 1)
        reasons = self.sql(
            "SELECT DISTINCT revoked_reason FROM user_sessions WHERE user_id = %s "
            "AND revoked_reason <> 'rotated'",
            alice,
        )
        self.assertEqual(reasons, [("admin_revoked",)])

    def test_revoke_needs_one_target(self):
        self.assertEqual(self.run_cli("revoke")[0], 2)
        user = str(uuid.uuid4())
        self.assertEqual(self.run_cli("revoke", "--user-id", user, "--session-id", user)[0], 2)


if __name__ == "__main__":
    unittest.main()
