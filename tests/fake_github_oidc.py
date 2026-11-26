"""A synthetic GitHub Actions OIDC issuer for tests (B09).

`FakeGitHubOIDC` serves a JWKS on 127.0.0.1 and mints RS256 tokens shaped like the
ones a GitHub Actions job gets from `ACTIONS_ID_TOKEN_REQUEST_URL`, signed with a
throwaway key. Nothing contacts GitHub, and no real workflow, repository or
credential is involved: every repository, ID and run number below is made up.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from nest.db import GITHUB_ACTIONS_ISSUER

AUDIENCE = "https://nest.example.test/workload"
REPOSITORY = "example-org/pipeline"
REPOSITORY_ID = "100000001"
OWNER_ID = "200000001"
WORKFLOW = ".github/workflows/nest.yml"
REF = "refs/heads/main"

DROP = object()
"""Value in `mint(**claims)` that removes a claim."""


def new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeGitHubOIDC:
    """Runs the fake JWKS endpoint in a background thread."""

    def __init__(self):
        self.key = new_key()
        self.kid = "fake-actions-key-1"
        self.jwks_requests = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                fake.jwks_requests += 1
                body = json.dumps(fake.jwks()).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def jwks_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/.well-known/jwks"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def jwks(self) -> dict[str, Any]:
        public = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return {"keys": [{**public, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def claims(self, **overrides: Any) -> dict[str, Any]:
        """The claims of a push to `main` running `nest.yml`, with `overrides`."""
        now = int(time.time())
        ref = overrides.get("ref", REF)
        repository = overrides.get("repository", REPOSITORY)
        claims: dict[str, Any] = {
            "iss": GITHUB_ACTIONS_ISSUER,
            "aud": AUDIENCE,
            "sub": f"repo:{repository}:ref:{ref}",
            "jti": secrets.token_hex(16),
            "iat": now,
            "nbf": now - 5,
            "exp": now + 300,
            "repository": repository,
            "repository_id": REPOSITORY_ID,
            "repository_owner": repository.split("/")[0],
            "repository_owner_id": OWNER_ID,
            "ref": ref,
            "ref_type": "branch",
            "sha": "0" * 40,
            "workflow": "nest",
            "workflow_ref": f"{repository}/{WORKFLOW}@{ref}",
            "job_workflow_ref": f"{repository}/{WORKFLOW}@{ref}",
            "event_name": "push",
            "run_id": "3000001",
            "run_number": "7",
            "run_attempt": "1",
            "actor": "example-bot",
            "actor_id": "400000001",
            "runner_environment": "github-hosted",
        }
        claims.update(overrides)
        return {k: v for k, v in claims.items() if v is not DROP}

    def mint(
        self,
        *,
        key: rsa.RSAPrivateKey | None = None,
        kid: str | None = None,
        algorithm: str = "RS256",
        **overrides: Any,
    ) -> str:
        """Signs `claims(**overrides)` as an Actions OIDC token."""
        signing: Any = key or self.key
        if algorithm.startswith("HS"):
            signing = "shared-secret-for-an-hmac-forgery-test"
        return jwt.encode(
            self.claims(**overrides),
            signing,
            algorithm=algorithm,
            headers={"kid": kid or self.kid},
        )
