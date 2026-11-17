"""A local fake of Google's OAuth 2.0 / OpenID Connect endpoints for tests.

`FakeGoogle` serves an authorization endpoint (which "consents" at once and
redirects back with a code), a token endpoint that enforces the client secret, the
registered redirect URI and the PKCE `S256` verifier, and a JWKS endpoint. ID
tokens are signed with the fake's own throwaway RSA keys, so tests never contact
Google and no real credential is involved.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from nest.auth import GOOGLE_ISSUER, GOOGLE_ISSUERS, OIDCEndpoints

CLIENT_ID = "1234-fake.apps.googleusercontent.com"
CLIENT_SECRET = "fake-client-secret-for-tests"

DROP = object()
"""Value in `FakeGoogle.claim_overrides` that removes a claim from the ID token."""


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@dataclass
class Account:
    """The Google account that "signs in" at the fake authorization endpoint."""

    sub: str = "108000000000000000001"
    email: str = "researcher@example.org"
    email_verified: Any = True
    name: str | None = "Test Researcher"


@dataclass
class _Grant:
    params: dict[str, str]
    account: Account
    used: bool = False


@dataclass
class FakeGoogle:
    """Runs the fake provider on 127.0.0.1 in a background thread."""

    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    account: Account = field(default_factory=Account)
    # Claims replaced in (or, with DROP, removed from) the next ID tokens.
    claim_overrides: dict[str, Any] = field(default_factory=dict)
    # Sign ID tokens with this key instead of the published one (a bad signature).
    signing_key: rsa.RSAPrivateKey | None = None
    deny: bool = False

    def __post_init__(self):
        self.key = new_key()
        self.kid = "fake-key-1"
        self.authorizations: list[dict[str, str]] = []
        self.token_requests: list[dict[str, str]] = []
        self.jwks_requests = 0
        self._grants: dict[str, _Grant] = {}
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def endpoints(self) -> OIDCEndpoints:
        return OIDCEndpoints(
            authorization_url=f"{self.base_url}/authorize",
            token_url=f"{self.base_url}/token",
            jwks_url=f"{self.base_url}/jwks",
            issuers=GOOGLE_ISSUERS,
            issuer=GOOGLE_ISSUER,
        )

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def rotate_key(self) -> None:
        """Publishes a new key under a new `kid` and signs with it from now on."""
        self.key = new_key()
        self.kid = f"fake-key-{secrets.token_hex(4)}"

    def jwks(self) -> dict[str, Any]:
        public = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**public, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def mint(
        self,
        claims: dict[str, Any],
        *,
        key: rsa.RSAPrivateKey | None = None,
        kid: str | None = None,
        algorithm: str = "RS256",
    ) -> str:
        """Signs `claims` as an ID token."""
        payload = {k: v for k, v in claims.items() if v is not DROP}
        signer: Any = None if algorithm == "none" else key or self.signing_key or self.key
        return jwt.encode(payload, signer, algorithm=algorithm, headers={"kid": kid or self.kid})

    def claims(self, *, nonce: str, account: Account | None = None) -> dict[str, Any]:
        """The claims Google would issue, with `claim_overrides` applied."""
        account = account or self.account
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": GOOGLE_ISSUER,
            "azp": self.client_id,
            "aud": self.client_id,
            "sub": account.sub,
            "email": account.email,
            "email_verified": account.email_verified,
            "nonce": nonce,
            "iat": now,
            "exp": now + 3600,
        }
        if account.name is not None:
            claims["name"] = account.name
        claims.update(self.claim_overrides)
        return claims

    # HTTP ---------------------------------------------------------------------

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _json(self, status: int, body: dict[str, Any]):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlsplit(self.path)
                if url.path == "/jwks":
                    fake.jwks_requests += 1
                    self._json(200, fake.jwks())
                elif url.path == "/authorize":
                    self._authorize({k: v[0] for k, v in parse_qs(url.query).items()})
                else:
                    self._json(404, {"error": "not_found"})

            def _authorize(self, params: dict[str, str]):
                fake.authorizations.append(params)
                if (
                    params.get("client_id") != fake.client_id
                    or params.get("response_type") != "code"
                    or "openid" not in params.get("scope", "").split()
                    or params.get("code_challenge_method") != "S256"
                    or not params.get("code_challenge")
                    or not params.get("redirect_uri")
                ):
                    self._json(400, {"error": "invalid_request"})
                    return
                if fake.deny:
                    query = {"error": "access_denied", "state": params.get("state", "")}
                else:
                    code = secrets.token_urlsafe(24)
                    with fake._lock:
                        fake._grants[code] = _Grant(params, fake.account)
                    query = {"code": code, "state": params.get("state", "")}
                self.send_response(302)
                self.send_header("Location", f"{params['redirect_uri']}?{urlencode(query)}")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                if urlsplit(self.path).path != "/token":
                    self._json(404, {"error": "not_found"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                fake.token_requests.append(form)
                if (
                    form.get("client_id") != fake.client_id
                    or form.get("client_secret") != fake.client_secret
                ):
                    self._json(401, {"error": "invalid_client"})
                    return
                with fake._lock:
                    grant = fake._grants.get(form.get("code", ""))
                    if grant is None or grant.used:
                        grant = None
                    else:
                        grant.used = True
                verifier = form.get("code_verifier", "")
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                    .rstrip(b"=")
                    .decode()
                )
                if (
                    form.get("grant_type") != "authorization_code"
                    or grant is None
                    or form.get("redirect_uri") != grant.params["redirect_uri"]
                    or challenge != grant.params["code_challenge"]
                ):
                    self._json(400, {"error": "invalid_grant"})
                    return
                id_token = fake.mint(
                    fake.claims(nonce=grant.params["nonce"], account=grant.account)
                )
                self._json(
                    200,
                    {
                        "access_token": secrets.token_urlsafe(24),
                        "token_type": "Bearer",
                        "expires_in": 3599,
                        "id_token": id_token,
                    },
                )

        return Handler
