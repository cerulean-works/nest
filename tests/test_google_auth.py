"""Google sign-in: OAuth code flow with PKCE and OIDC ID-token verification (B02).

Every test talks to `fake_oidc.FakeGoogle`, a local provider with its own signing
keys; nothing contacts Google. The flow tests drive the Controller's auth listener
over real HTTP and store Users and sessions in a real PostgreSQL (see
`postgres_fixture`).
"""

import base64
import json
import logging
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import jwt
from fake_oidc import CLIENT_ID, CLIENT_SECRET, DROP, Account, FakeGoogle, new_key
from postgres_fixture import PostgresTestCase, connect

from nest.auth import (
    GOOGLE_ISSUER,
    INSECURE_SESSION_COOKIE,
    LOGIN_PATH,
    SIGN_IN_COOKIE,
    GoogleSignIn,
    IDTokenVerifier,
    SignInError,
    derive_flow_secrets,
    pkce_challenge,
    start_auth_service,
)
from nest.config import ConfigError, load_controller_config, load_database_config
from nest.db import (
    Database,
    IdentityStore,
    LoginFlowStore,
    TooManyPendingSignInsError,
    hash_token,
    upgrade,
)

DB_URL = "postgresql://nest@db.example.internal:5432/nest"
REDIRECT = "https://nest.example.com/auth/google/callback"


def google_env(**overrides: str) -> dict[str, str]:
    env = {
        "NEST_DATABASE_URL": DB_URL,
        "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
        "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
        "NEST_GOOGLE_REDIRECT_URL": REDIRECT,
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v != ""}


def config_problems(env: dict[str, str]) -> str:
    with unittest.TestCase().assertRaises(ConfigError) as caught:
        load_controller_config(env)
    return str(caught.exception)


class GoogleConfigTests(unittest.TestCase):
    def test_disabled_without_client_id(self):
        self.assertIsNone(load_controller_config({}).google)

    def test_inline_secret(self):
        google = load_controller_config(google_env()).google
        assert google is not None
        self.assertEqual(google.client_id, CLIENT_ID)
        self.assertEqual(google.redirect_url, REDIRECT)
        self.assertEqual(google.read_client_secret(), CLIENT_SECRET)
        self.assertEqual(google.http_bind, "127.0.0.1:8080")
        self.assertTrue(google.secure_cookies)
        self.assertNotIn(CLIENT_SECRET, repr(google))
        summary = load_controller_config(google_env()).summary()
        self.assertEqual(google.summary()["client_secret"], "***")
        self.assertNotIn(CLIENT_SECRET, str(summary))

    def test_secret_file_is_reread(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "secret")
            with open(path, "w") as handle:
                handle.write("first-secret-value\n")
            env = google_env(NEST_GOOGLE_CLIENT_SECRET="", NEST_GOOGLE_CLIENT_SECRET_FILE=path)
            google = load_controller_config(env).google
            assert google is not None
            self.assertIsNone(google.client_secret)
            self.assertEqual(google.read_client_secret(), "first-secret-value")
            with open(path, "w") as handle:
                handle.write("rotated-secret-value")
            self.assertEqual(google.read_client_secret(), "rotated-secret-value")
            with open(path, "w") as handle:
                handle.write("  \n")
            self.assertIn("is empty", config_problems(env))

    def test_secret_is_required_once(self):
        self.assertIn(
            "needs a client secret",
            config_problems(google_env(NEST_GOOGLE_CLIENT_SECRET="")),
        )
        both = google_env(NEST_GOOGLE_CLIENT_SECRET_FILE="/etc/hostname")
        message = config_problems(both)
        self.assertIn("Set only one of NEST_GOOGLE_CLIENT_SECRET", message)
        self.assertNotIn(CLIENT_SECRET, message)

    def test_required_settings_are_named(self):
        message = config_problems(google_env(NEST_GOOGLE_REDIRECT_URL="", NEST_DATABASE_URL=""))
        self.assertIn("NEST_GOOGLE_REDIRECT_URL is required", message)
        self.assertIn("needs NEST_DATABASE_URL", message)

    def test_stray_settings_need_client_id(self):
        message = config_problems(google_env(NEST_GOOGLE_CLIENT_ID=""))
        self.assertIn("NEST_GOOGLE_CLIENT_SECRET, NEST_GOOGLE_REDIRECT_URL needs", message)
        self.assertNotIn(CLIENT_SECRET, message)

    def test_redirect_url_rules(self):
        for url, expected in [
            ("http://nest.example.com/auth/google/callback", "must be an https:// URL"),
            ("https://nest.example.com/cb?x=1", "query string or fragment"),
            ("https://nest.example.com/cb#top", "query string or fragment"),
            ("https://nest.example.com/", "callback path"),
            ("https://nest.example.com", "callback path"),
            ("https://user:pw@nest.example.com/cb", "no credentials"),
            ("https://nest.example.com/auth/google/login", "must not use /auth/google/login"),
            ("nest.example.com/cb", "must be an https:// URL"),
        ]:
            with self.subTest(url=url):
                self.assertIn(expected, config_problems(google_env(NEST_GOOGLE_REDIRECT_URL=url)))
        for url in (
            "http://localhost:8080/auth/google/callback",
            "http://127.0.0.1:8080/cb",
        ):
            with self.subTest(url=url):
                google = load_controller_config(google_env(NEST_GOOGLE_REDIRECT_URL=url)).google
                assert google is not None
                self.assertFalse(google.secure_cookies)

    def test_client_id_and_bind_are_checked(self):
        self.assertIn(
            "NEST_GOOGLE_CLIENT_ID must be the OAuth client ID",
            config_problems(google_env(NEST_GOOGLE_CLIENT_ID="bad id")),
        )
        self.assertIn(
            "NEST_AUTH_HTTP_BIND must be host:port",
            config_problems(google_env(NEST_AUTH_HTTP_BIND="8080")),
        )

    def test_start_auth_service_disabled(self):
        service = start_auth_service(None, None)
        self.assertFalse(service.enabled)
        self.assertIsNone(service.address)
        service.stop()


class PKCETests(unittest.TestCase):
    def test_rfc7636_example(self):
        # RFC 7636 appendix B.
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(pkce_challenge(verifier), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_flow_secrets(self):
        verifier, nonce = derive_flow_secrets("binding-secret")
        self.assertEqual(derive_flow_secrets("binding-secret"), (verifier, nonce))
        self.assertNotEqual(derive_flow_secrets("other-binding")[0], verifier)
        self.assertNotEqual(verifier, nonce)
        # RFC 7636: 43-128 characters from the unreserved set.
        self.assertEqual(len(verifier), 43)
        self.assertRegex(verifier, r"^[A-Za-z0-9._~-]+$")


class IDTokenVerifierTests(unittest.TestCase):
    NONCE = "expected-nonce"

    def setUp(self):
        self.fake = FakeGoogle()
        self.addCleanup(self.fake.close)
        self.verifier = IDTokenVerifier(
            CLIENT_ID, self.fake.endpoints, timeout=5, refetch_cooldown=0
        )

    def token(self, **overrides):
        claims = self.fake.claims(nonce=self.NONCE)
        claims.update(overrides)
        return self.fake.mint(claims)

    def rejected(self, token: str, reason: str = "invalid_token"):
        with self.assertRaises(SignInError) as caught:
            self.verifier.verify(token, nonce=self.NONCE)
        self.assertEqual(caught.exception.reason, reason)

    def test_valid_token(self):
        identity = self.verifier.verify(self.token(), nonce=self.NONCE)
        self.assertEqual(identity.issuer, GOOGLE_ISSUER)
        self.assertEqual(identity.subject, self.fake.account.sub)
        self.assertEqual(identity.email, self.fake.account.email)
        self.assertEqual(identity.name, "Test Researcher")

    def test_short_issuer_form_is_canonicalized(self):
        identity = self.verifier.verify(self.token(iss="accounts.google.com"), nonce=self.NONCE)
        self.assertEqual(identity.issuer, GOOGLE_ISSUER)

    def test_audience_list_needs_matching_azp(self):
        both = [CLIENT_ID, "other-client"]
        self.verifier.verify(self.token(aud=both), nonce=self.NONCE)
        self.rejected(self.token(aud=both, azp=DROP))
        self.rejected(self.token(aud=both, azp="other-client"))
        self.rejected(self.token(azp="other-client"))

    def test_rejected_claims(self):
        now = int(time.time())
        cases = {
            "wrong issuer": {"iss": "https://evil.example.com"},
            "issuer lookalike": {"iss": "https://accounts.google.com.evil.example.com"},
            "no issuer": {"iss": DROP},
            "wrong audience": {"aud": "other-client.apps.googleusercontent.com"},
            "no audience": {"aud": DROP},
            "expired": {"exp": now - 600, "iat": now - 4200},
            "no expiry": {"exp": DROP},
            "issued in the future": {"iat": now + 3600, "exp": now + 7200},
            "no iat": {"iat": DROP},
            "wrong nonce": {"nonce": "another-nonce"},
            "no nonce": {"nonce": DROP},
            "no subject": {"sub": DROP},
            "empty subject": {"sub": ""},
            "numeric subject": {"sub": 1234},
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                self.rejected(self.token(**overrides))

    def test_unverified_email(self):
        for value in (False, "true", DROP, None):
            with self.subTest(email_verified=value):
                self.rejected(self.token(email_verified=value), "unverified_email")
        self.rejected(self.token(email=DROP), "unverified_email")

    def test_bad_signatures(self):
        claims = self.fake.claims(nonce=self.NONCE)
        with self.subTest("other key, published kid"):
            self.rejected(self.fake.mint(claims, key=new_key()))
        with self.subTest("unknown kid"):
            self.rejected(self.fake.mint(claims, key=new_key(), kid="unknown"))
        with self.subTest("alg none"):
            self.rejected(self.fake.mint(claims, algorithm="none"))
        with self.subTest("HS256"):
            self.rejected(jwt_hs256(claims, self.fake.kid))
        with self.subTest("tampered payload"):
            header, _, signature = self.token().split(".")
            forged = self.fake.claims(nonce=self.NONCE)
            forged["sub"] = "attacker"
            body = base64.urlsafe_b64encode(json_bytes(forged)).rstrip(b"=").decode()
            self.rejected(f"{header}.{body}.{signature}")
        with self.subTest("not a JWT"):
            self.rejected("not-a-token")

    def test_key_rotation_refetches_jwks(self):
        self.verifier.verify(self.token(), nonce=self.NONCE)
        self.fake.rotate_key()
        self.verifier.verify(self.token(), nonce=self.NONCE)
        self.assertEqual(self.fake.jwks_requests, 2)

    def test_unreachable_jwks(self):
        token = self.token()
        self.fake.close()
        self.rejected(token, "provider_unavailable")


def json_bytes(value) -> bytes:
    return json.dumps(value).encode()


def jwt_hs256(claims, kid) -> str:
    return jwt.encode(claims, "a-shared-secret-of-sufficient-length!", "HS256", {"kid": kid})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Browser:
    """A minimal browser: keeps cookies by name, honours Path and Max-Age=0, and
    never follows redirects on its own."""

    def __init__(self):
        self.cookies: dict[str, tuple[str, str, dict[str, str]]] = {}
        self.opener = urllib.request.build_opener(_NoRedirect)

    def get(self, url: str, method: str = "GET"):
        path = urlsplit(url).path
        request = urllib.request.Request(url, method=method)
        sent = [f"{k}={v}" for k, (v, p, _) in self.cookies.items() if path.startswith(p)]
        if sent:
            request.add_header("Cookie", "; ".join(sent))
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read().decode()
            for header in response.headers.get_all("Set-Cookie") or []:
                jar = SimpleCookie()
                jar.load(header)
                for name, morsel in jar.items():
                    if morsel["max-age"] == "0":
                        self.cookies.pop(name, None)
                    else:
                        flags = header.lower()
                        attrs = {
                            "httponly": str("httponly" in flags),
                            "samesite": morsel["samesite"],
                            "secure": str("secure" in flags),
                            "max-age": morsel["max-age"],
                        }
                        self.cookies[name] = (morsel.value, morsel["path"] or "/", attrs)
            return response.status, response.headers, body


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GoogleSignInFlowTests(PostgresTestCase):
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
        self.redirect_url = f"{self.base}/auth/google/callback"
        self.config = load_controller_config(
            {
                "NEST_DATABASE_URL": self.url,
                "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
                "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
                "NEST_GOOGLE_REDIRECT_URL": self.redirect_url,
                "NEST_AUTH_HTTP_BIND": f"127.0.0.1:{port}",
            }
        ).google
        assert self.config is not None
        self.sign_in = GoogleSignIn(self.config, self.db, endpoints=self.fake.endpoints, timeout=5)
        self.service = start_auth_service(self.config, self.db, sign_in=self.sign_in)
        self.addCleanup(self.service.stop)
        self.identities = IdentityStore(self.db)

    # Helpers ------------------------------------------------------------------

    def start(self, browser: Browser) -> str:
        """Starts a sign-in and returns the callback URL Google redirects to."""
        status, headers, _ = browser.get(self.base + LOGIN_PATH)
        self.assertEqual(status, 302)
        location = headers["Location"]
        self.assertTrue(location.startswith(self.fake.endpoints.authorization_url + "?"))
        self.assertIn(SIGN_IN_COOKIE, browser.cookies)
        status, headers, _ = browser.get(location)
        self.assertEqual(status, 302)
        callback = headers["Location"]
        self.assertTrue(callback.startswith(self.redirect_url + "?"))
        return callback

    def login(self, browser: Browser | None = None):
        browser = browser or Browser()
        return browser, *browser.get(self.start(browser))

    def count(self, table: str) -> int:
        with connect(self.url) as conn:
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        assert row is not None
        return row[0]

    def assert_refused(self, status: int, body: str, expected: int, message: str):
        self.assertEqual(status, expected, body)
        self.assertIn(message, body)

    # Success ------------------------------------------------------------------

    def test_login_through_redirect_url(self):
        browser, status, headers, body = self.login()
        self.assertEqual(status, 200, body)
        self.assertIn("signed in to Nest as researcher@example.org", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        # The one-time sign-in cookie is cleared; a session cookie replaces it.
        self.assertNotIn(SIGN_IN_COOKIE, browser.cookies)
        token, path, attrs = browser.cookies[INSECURE_SESSION_COOKIE]
        self.assertEqual(path, "/")
        self.assertEqual(attrs["httponly"], "True")
        self.assertEqual(attrs["samesite"], "Lax")
        self.assertEqual(attrs["max-age"], str(12 * 3600))

        user = self.identities.find_user(issuer=GOOGLE_ISSUER, subject=self.fake.account.sub)
        assert user is not None
        self.assertEqual(user.email, "researcher@example.org")
        self.assertEqual(user.display_name, "Test Researcher")
        session = self.identities.active_session(hash_token(token))
        assert session is not None
        self.assertEqual((session.user_id, session.kind), (user.id, "browser"))
        [identity] = self.identities.identities(user.id)
        self.assertTrue(identity.email_verified)

        # Only the hash of the session secret is stored, and the pending sign-in is gone.
        with connect(self.url) as conn:
            [row] = conn.execute("SELECT token_hash FROM user_sessions").fetchall()
        self.assertEqual(bytes(row[0]), hash_token(token))
        self.assertEqual(self.count("oidc_login_flows"), 0)

        # The configured redirect URL and S256 PKCE were used end to end; the fake's
        # token endpoint checked the verifier against the challenge.
        [authorization] = self.fake.authorizations
        self.assertEqual(authorization["redirect_uri"], self.redirect_url)
        self.assertEqual(authorization["code_challenge_method"], "S256")
        self.assertEqual(authorization["scope"], "openid email profile")
        [exchange] = self.fake.token_requests
        self.assertEqual(exchange["redirect_uri"], self.redirect_url)
        self.assertEqual(pkce_challenge(exchange["code_verifier"]), authorization["code_challenge"])
        # The verifier never appeared in the browser's URLs.
        self.assertNotIn(exchange["code_verifier"], urlencode(authorization))

    def test_returning_user_is_linked_by_subject(self):
        _, status, _, _ = self.login()
        self.assertEqual(status, 200)
        first = self.identities.find_user(issuer=GOOGLE_ISSUER, subject=self.fake.account.sub)
        assert first is not None
        # Same Google account, new email address and the short issuer form.
        self.fake.account = Account(email="renamed@example.org")
        self.fake.claim_overrides = {"iss": "accounts.google.com"}
        _, status, _, _ = self.login()
        self.assertEqual(status, 200)
        self.assertEqual(self.count("users"), 1)
        self.assertEqual(self.count("user_identities"), 1)
        [identity] = self.identities.identities(first.id)
        self.assertEqual(identity.issuer, GOOGLE_ISSUER)
        self.assertEqual(identity.email, "renamed@example.org")

    def test_same_email_with_another_subject_is_another_user(self):
        self.login()
        self.fake.account = Account(sub="108000000000000000999")
        _, status, _, _ = self.login()
        self.assertEqual(status, 200)
        self.assertEqual(self.count("users"), 2)
        a = self.identities.find_user(issuer=GOOGLE_ISSUER, subject="108000000000000000001")
        b = self.identities.find_user(issuer=GOOGLE_ISSUER, subject="108000000000000000999")
        assert a is not None and b is not None
        self.assertNotEqual(a.id, b.id)

    # State, binding and replay ------------------------------------------------

    def test_tampered_state_is_refused(self):
        browser = Browser()
        callback = urlsplit(self.start(browser))
        params = parse_qs(callback.query)
        params["state"] = ["forged-state"]
        status, _, body = browser.get(
            urlunsplit(callback._replace(query=urlencode(params, doseq=True)))
        )
        self.assert_refused(status, body, 400, "not started in this browser")
        self.assertEqual(self.count("users"), 0)
        self.assertEqual(self.fake.token_requests, [])

    def test_missing_state_is_refused(self):
        browser = Browser()
        callback = urlsplit(self.start(browser))
        params = parse_qs(callback.query)
        del params["state"]
        status, _, body = browser.get(
            urlunsplit(callback._replace(query=urlencode(params, doseq=True)))
        )
        self.assert_refused(status, body, 400, "not started in this browser")

    def test_callback_in_another_browser_is_refused_and_burns_the_state(self):
        victim = Browser()
        callback = self.start(victim)
        attacker = Browser()
        status, _, body = attacker.get(callback)
        self.assert_refused(status, body, 400, "not started in this browser")
        # The state is consumed: the right browser cannot use it afterwards either.
        status, _, body = victim.get(callback)
        self.assert_refused(status, body, 400, "not started in this browser")
        self.assertEqual(self.count("users"), 0)
        self.assertEqual(self.fake.token_requests, [])

    def test_replayed_callback_is_refused(self):
        browser = Browser()
        callback = self.start(browser)
        cookie = browser.cookies[SIGN_IN_COOKIE]
        status, _, _ = browser.get(callback)
        self.assertEqual(status, 200)
        browser.cookies[SIGN_IN_COOKIE] = cookie
        status, _, body = browser.get(callback)
        self.assert_refused(status, body, 400, "not started in this browser")
        self.assertEqual(self.count("user_sessions"), 1)

    def test_expired_sign_in_is_refused(self):
        browser = Browser()
        callback = self.start(browser)
        with connect(self.url) as conn:
            conn.execute(
                "UPDATE oidc_login_flows SET created_at = now() - interval '1 hour', "
                "expires_at = now() - interval '1 second'"
            )
        status, _, body = browser.get(callback)
        self.assert_refused(status, body, 400, "took too long")
        self.assertEqual(self.count("users"), 0)
        self.assertEqual(self.count("oidc_login_flows"), 0)

    def test_provider_error_is_refused(self):
        self.fake.deny = True
        _, status, _, body = self.login()
        self.assert_refused(status, body, 400, "cancelled or refused")

    def test_wrong_client_secret_is_refused(self):
        self.fake.client_secret = "a-different-secret"
        _, status, _, body = self.login()
        self.assert_refused(status, body, 502, "did not confirm")
        self.assertEqual(self.count("users"), 0)

    # ID-token checks through the whole flow -------------------------------------

    def test_invalid_identities_are_refused(self):
        now = int(time.time())
        cases = [
            ("wrong issuer", {"iss": "https://login.example.com"}, None, 401, "not be verified"),
            ("wrong audience", {"aud": "other.apps.googleusercontent.com"}, None, 401, "verified"),
            ("wrong nonce", {"nonce": "replayed-nonce"}, None, 401, "not be verified"),
            ("expired", {"exp": now - 600, "iat": now - 4200}, None, 401, "not be verified"),
            ("bad signature", {}, new_key(), 401, "not be verified"),
            ("unverified email", {"email_verified": False}, None, 403, "not verified"),
        ]
        for name, overrides, signing_key, expected, message in cases:
            with self.subTest(name):
                self.fake.claim_overrides = overrides
                self.fake.signing_key = signing_key
                _, status, _, body = self.login()
                self.assert_refused(status, body, expected, message)
        self.assertEqual(self.count("users"), 0)
        self.assertEqual(self.count("user_identities"), 0)
        self.assertEqual(self.count("user_sessions"), 0)

    def test_disabled_user_is_refused(self):
        _, status, _, _ = self.login()
        self.assertEqual(status, 200)
        user = self.identities.find_user(issuer=GOOGLE_ISSUER, subject=self.fake.account.sub)
        assert user is not None
        self.identities.set_user_status(user.id, "disabled")
        browser, status, _, body = self.login()
        self.assert_refused(status, body, 403, "disabled")
        self.assertNotIn(INSECURE_SESSION_COOKIE, browser.cookies)
        with connect(self.url) as conn:
            row = conn.execute(
                "SELECT count(*) FROM user_sessions WHERE revoked_at IS NULL"
            ).fetchone()
        assert row is not None
        self.assertEqual(row[0], 0)
        self.assertEqual(self.count("users"), 1)

    # Listener and storage -------------------------------------------------------

    def test_other_routes(self):
        browser = Browser()
        status, _, _ = browser.get(self.base + "/")
        self.assertEqual(status, 404)
        status, headers, _ = browser.get(self.base + LOGIN_PATH, method="POST")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")
        status, _, _ = browser.get(self.base + LOGIN_PATH, method="HEAD")
        self.assertEqual(status, 405)
        self.assertEqual(self.count("oidc_login_flows"), 0)

    def test_pending_sign_ins_are_bounded(self):
        flows = LoginFlowStore(self.db)

        def create(state: str):
            flows.create(
                state_hash=hash_token(state),
                binding_hash=hash_token(state + "-binding"),
                provider="google",
                ttl_seconds=600,
                max_pending=1,
            )

        create("a")
        with self.assertRaises(TooManyPendingSignInsError):
            create("b")

    def test_pending_sign_in_cap_holds_under_concurrent_starts(self):
        flows = LoginFlowStore(self.db)
        starts, cap = 8, 3
        barrier = threading.Barrier(starts)
        results: list[object] = [None] * starts

        def create(index: int):
            state = f"concurrent-{index}"
            with self.db.connection() as conn:
                barrier.wait()
                try:
                    flows.create(
                        state_hash=hash_token(state),
                        binding_hash=hash_token(state + "-binding"),
                        provider="google",
                        ttl_seconds=600,
                        max_pending=cap,
                        conn=conn,
                    )
                    # Hold the transaction open so the starts overlap.
                    time.sleep(0.2)
                    results[index] = "created"
                except TooManyPendingSignInsError as exc:
                    results[index] = exc

        threads = [threading.Thread(target=create, args=(i,)) for i in range(starts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(results.count("created"), cap, results)
        self.assertEqual(
            sum(isinstance(r, TooManyPendingSignInsError) for r in results), starts - cap
        )
        self.assertEqual(self.count("oidc_login_flows"), cap)

    def test_logs_contain_no_secrets(self):
        with self.assertLogs("nest", logging.DEBUG) as logs:
            browser, status, _, _ = self.login()
            self.assertEqual(status, 200)
            # A refused callback logs too.
            Browser().get(self.start(Browser()))
        output = "\n".join(logs.output)
        [authorization, _] = self.fake.authorizations
        exchange = self.fake.token_requests[0]
        session_token = browser.cookies[INSECURE_SESSION_COOKIE][0]
        for secret in (
            authorization["state"],
            authorization["nonce"],
            exchange["code"],
            exchange["code_verifier"],
            CLIENT_SECRET,
            session_token,
            "researcher@example.org",
        ):
            self.assertNotIn(secret, output)
        self.assertIn("Signed in user_id=", output)

    def test_sign_in_cookie_is_scoped_to_the_callback(self):
        browser = Browser()
        browser.get(self.base + LOGIN_PATH)
        _, path, attrs = browser.cookies[SIGN_IN_COOKIE]
        self.assertEqual(path, "/auth/google/callback")
        self.assertEqual(attrs["httponly"], "True")
        self.assertEqual(attrs["samesite"], "Lax")
        # Only digests are stored for the pending sign-in.
        binding = browser.cookies[SIGN_IN_COOKIE][0]
        with connect(self.url) as conn:
            [row] = conn.execute("SELECT binding_hash FROM oidc_login_flows").fetchall()
        self.assertEqual(bytes(row[0]), hash_token(binding))


if __name__ == "__main__":
    unittest.main()
