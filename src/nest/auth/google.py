"""
Google sign-in: the OAuth 2.0 authorization code flow with PKCE, run by the server,
and OpenID Connect ID-token verification.

[`auth.GoogleSignIn.start`] stores a pending sign-in and returns Google's
authorization URL plus a secret for the browser's sign-in cookie.
[`auth.GoogleSignIn.finish`] handles the callback: it consumes the pending sign-in
bound to both the `state` parameter and that cookie, exchanges the code with the
PKCE verifier, verifies the ID token against Google's published keys, and signs the
User in by Google's stable `sub`, never by email address.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import jwt

from .._logging import get_logger, register_secret
from ..config import GoogleAuthConfig
from ..db import (
    Database,
    Identity,
    IdentityStore,
    LoginFlowStore,
    Session,
    TooManyPendingSignInsError,
    User,
    UserNotActiveError,
    hash_token,
)

logger = get_logger("nest.auth.google")

GOOGLE_ISSUER = "https://accounts.google.com"
"""Issuer stored for every Google identity, whichever accepted form the token used."""

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
"""`iss` values Google documents for its ID tokens."""

SIGN_IN_TTL = 600.0
"""Seconds between starting a sign-in and its callback."""

SESSION_TTL = timedelta(hours=12)
"""Lifetime of the browser session created by a sign-in."""

MAX_PENDING_SIGN_INS = 10_000
"""Most unexpired sign-ins kept at once, bounding what unauthenticated requests store."""

CLOCK_SKEW = 60.0
"""Seconds of clock difference tolerated for `exp` and `iat`."""

ID_TOKEN_ALGORITHMS = ("RS256",)
"""Signature algorithms accepted for ID tokens: Google signs them with RS256."""

_MAX_RESPONSE_BYTES = 1 << 20


@dataclass(frozen=True)
class OIDCEndpoints:
    """
    Where an OpenID provider authorizes, exchanges codes, and publishes its keys.

    Attributes:
        authorization_url (`str`): Authorization endpoint the browser is sent to.
        token_url (`str`): Token endpoint for the code exchange.
        jwks_url (`str`): JSON Web Key Set that signs ID tokens.
        issuers (`tuple[str, ...]`): Accepted `iss` claim values.
        issuer (`str`): Issuer stored with each identity, so the accepted forms of
            one issuer link to the same User.
    """

    authorization_url: str
    token_url: str
    jwks_url: str
    issuers: tuple[str, ...]
    issuer: str


GOOGLE_ENDPOINTS = OIDCEndpoints(
    authorization_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    jwks_url="https://www.googleapis.com/oauth2/v3/certs",
    issuers=GOOGLE_ISSUERS,
    issuer=GOOGLE_ISSUER,
)
"""Google's endpoints, from its OpenID discovery document."""


class SignInError(Exception):
    """
    A sign-in was refused. The message is safe to show the user and to log: it
    never contains a code, token, `state` or claim value.

    Attributes:
        reason (`str`): Stable machine-readable cause, one of [`auth.SIGN_IN_ERRORS`].
        status (`int`): HTTP status for the callback response.
    """

    def __init__(self, reason: str):
        self.reason = reason
        self.status, message = SIGN_IN_ERRORS[reason]
        super().__init__(message)


SIGN_IN_ERRORS: dict[str, tuple[int, str]] = {
    "provider_denied": (400, "Google sign-in was cancelled or refused."),
    "invalid_state": (400, "This sign-in was not started in this browser. Start again."),
    "expired": (400, "The sign-in took too long. Start again."),
    "busy": (503, "Too many sign-ins are in progress. Try again in a few minutes."),
    "exchange_failed": (502, "Google did not confirm the sign-in. Start again."),
    "provider_unavailable": (503, "Google could not be reached. Try again later."),
    "invalid_token": (401, "Google's answer could not be verified."),
    "unverified_email": (403, "Your Google account's email address is not verified."),
    "user_disabled": (403, "Your Nest account is disabled."),
}
"""HTTP status and user-facing message for each [`auth.SignInError`] reason."""


@dataclass(frozen=True)
class VerifiedIdentity:
    """
    Claims of a verified ID token.

    Attributes:
        issuer (`str`): Canonical issuer, such as `https://accounts.google.com`.
        subject (`str`): The provider's stable `sub`: the identity's key.
        email (`str`): Email address, verified by the provider.
        name (`str | None`): Display name, if the token carries one.
    """

    issuer: str
    subject: str
    email: str
    name: str | None


@dataclass(frozen=True)
class SignInStart:
    """
    A started sign-in.

    Attributes:
        authorization_url (`str`): Google URL to redirect the browser to.
        binding (`str`): Secret for the browser's sign-in cookie. The callback must
            present it with the matching `state`. Never log it.
    """

    authorization_url: str
    binding: str


@dataclass(frozen=True)
class SignInResult:
    """
    A finished sign-in.

    Attributes:
        user ([`db.User`]): The signed-in User.
        identity ([`db.Identity`]): The Google identity used.
        created (`bool`): Whether this sign-in created the User.
        session ([`db.Session`]): The new browser session.
        session_token (`str`): The session secret for the browser's cookie. Only its
            hash is stored. Never log it.
    """

    user: User
    identity: Identity
    created: bool
    session: Session
    session_token: str


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_challenge(verifier: str) -> str:
    """Returns the RFC 7636 `S256` code challenge for a verifier."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def derive_flow_secrets(binding: str) -> tuple[str, str]:
    """
    Returns the PKCE verifier and the OIDC nonce for a sign-in cookie secret.

    Both come from the browser's cookie, so the database stores neither and a
    callback presenting another cookie cannot complete the exchange. The labels
    keep them independent: the nonce is visible in the authorization URL, the
    verifier is not.
    """
    key = binding.encode()
    verifier = _b64url(hmac.digest(key, b"nest oauth pkce verifier", "sha256"))
    nonce = _b64url(hmac.digest(key, b"nest oidc nonce", "sha256"))
    return verifier, nonce


class IDTokenVerifier:
    """
    Verifies OpenID Connect ID tokens with PyJWT against the provider's JWKS.

    Checks the RS256 signature with a key from `jwks_url` (fetched once and cached;
    an unknown `kid` refetches, so key rotation needs no restart), then `iss`,
    `aud`, `azp`, `exp`, `iat`, `sub`, the nonce, and `email_verified`.

    Args:
        client_id (`str`): OAuth client ID: the required audience.
        endpoints ([`auth.OIDCEndpoints`]): Issuer and JWKS location.
        timeout (`float`): Seconds for a JWKS request.
        refetch_cooldown (`float`): Least seconds between JWKS fetches, so tokens with
            made-up `kid`s cannot make Nest flood the provider.
    """

    def __init__(
        self,
        client_id: str,
        endpoints: OIDCEndpoints = GOOGLE_ENDPOINTS,
        *,
        timeout: float = 10.0,
        refetch_cooldown: float = 30.0,
    ):
        self.client_id = client_id
        self.endpoints = endpoints
        self.jwks = jwt.PyJWKClient(
            endpoints.jwks_url,
            cache_keys=True,
            lifespan=3600,
            timeout=timeout,
            cooldown_duration=refetch_cooldown,
        )

    def verify(self, id_token: str, *, nonce: str) -> VerifiedIdentity:
        """
        Returns the verified identity in an ID token.

        Raises:
            [`auth.SignInError`]: `invalid_token` for a bad signature, issuer,
                audience, time, subject or nonce; `unverified_email` when the email
                is missing or not verified; `provider_unavailable` when the keys
                cannot be fetched.
        """
        try:
            key = self.jwks.get_signing_key_from_jwt(id_token)
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key,
                algorithms=list(ID_TOKEN_ALGORITHMS),
                audience=self.client_id,
                issuer=self.endpoints.issuers,
                leeway=CLOCK_SKEW,
                options={"require": ["iss", "aud", "exp", "iat", "sub", "nonce"]},
            )
        except jwt.PyJWKClientConnectionError:
            logger.warning("Sign-in: the identity provider's keys could not be fetched")
            raise SignInError("provider_unavailable") from None
        except jwt.PyJWTError as exc:
            # The exception names the failed check, never a claim value.
            logger.warning("Sign-in: ID token rejected check=%s", type(exc).__name__)
            raise SignInError("invalid_token") from None

        audience = claims["aud"]
        audiences = audience if isinstance(audience, list) else [audience]
        azp = claims.get("azp")
        if (azp is not None or len(audiences) > 1) and azp != self.client_id:
            logger.warning("Sign-in: ID token rejected check=azp")
            raise SignInError("invalid_token")
        token_nonce = claims["nonce"]
        if not isinstance(token_nonce, str) or not hmac.compare_digest(
            token_nonce.encode(), nonce.encode()
        ):
            logger.warning("Sign-in: ID token rejected check=nonce")
            raise SignInError("invalid_token")
        subject = claims["sub"]
        if not isinstance(subject, str) or not 1 <= len(subject) <= 255:
            logger.warning("Sign-in: ID token rejected check=sub")
            raise SignInError("invalid_token")

        email = claims.get("email")
        # Google sends a JSON boolean; anything else, such as the string "true", is
        # not a verification.
        if claims.get("email_verified") is not True or not isinstance(email, str):
            logger.warning("Sign-in: refused an unverified email address")
            raise SignInError("unverified_email")
        if not 3 <= len(email) <= 320 or "@" not in email:
            logger.warning("Sign-in: ID token rejected check=email")
            raise SignInError("invalid_token")
        name = claims.get("name")
        if not isinstance(name, str) or not name.strip():
            name = None
        else:
            name = name.strip()[:200]
        return VerifiedIdentity(
            issuer=self.endpoints.issuer, subject=subject, email=email, name=name
        )


class GoogleSignIn:
    """
    Runs Google sign-ins for the Controller's auth listener.

    Args:
        config ([`config.GoogleAuthConfig`]): Client ID, secret and redirect URL.
        database ([`db.Database`]): Open database with the current schema.
        endpoints ([`auth.OIDCEndpoints`], *optional*): Google's, unless a test
            replaces them with a local fake provider.
        timeout (`float`): Seconds for each request to Google.
        session_ttl (`timedelta`): Lifetime of the browser session.
    """

    provider = "google"

    def __init__(
        self,
        config: GoogleAuthConfig,
        database: Database,
        *,
        endpoints: OIDCEndpoints = GOOGLE_ENDPOINTS,
        timeout: float = 10.0,
        session_ttl: timedelta = SESSION_TTL,
    ):
        self.config = config
        self.endpoints = endpoints
        self.timeout = timeout
        self.session_ttl = session_ttl
        self.identities = IdentityStore(database)
        self.flows = LoginFlowStore(database)
        self.database = database
        self.verifier = IDTokenVerifier(config.client_id, endpoints, timeout=timeout)
        if config.client_secret is not None:
            register_secret(config.client_secret.reveal())

    def start(self) -> SignInStart:
        """
        Stores a pending sign-in and returns where to send the browser.

        Raises:
            [`auth.SignInError`]: `busy` when too many sign-ins are pending.
        """
        state = secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        verifier, nonce = derive_flow_secrets(binding)
        try:
            self.flows.create(
                state_hash=hash_token(state),
                binding_hash=hash_token(binding),
                provider=self.provider,
                ttl_seconds=SIGN_IN_TTL,
                max_pending=MAX_PENDING_SIGN_INS,
            )
        except TooManyPendingSignInsError:
            logger.warning("Sign-in refused: %s pending sign-ins", MAX_PENDING_SIGN_INS)
            raise SignInError("busy") from None
        params = {
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_url,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        url = f"{self.endpoints.authorization_url}?{urlencode(params)}"
        return SignInStart(authorization_url=url, binding=binding)

    def finish(
        self,
        *,
        state: str | None,
        code: str | None,
        binding: str | None,
        error: str | None = None,
    ) -> SignInResult:
        """
        Completes a sign-in from Google's callback and creates a browser session.

        Args:
            state (`str`): The callback's `state` parameter.
            code (`str`): The callback's authorization code.
            binding (`str`): The secret from the browser's sign-in cookie.
            error (`str`, *optional*): The callback's `error` parameter.

        Raises:
            [`auth.SignInError`]: If any check fails. No User, identity or session
                is created or changed then.
            [`db.DatabaseError`]: If the database is unavailable.
        """
        if error:
            logger.info("Sign-in: Google returned an error")
            raise SignInError("provider_denied")
        if not state:
            raise SignInError("invalid_state")
        # Consume first: a state is used at most once, even when a later check fails.
        flow = self.flows.consume(hash_token(state))
        if (
            flow is None
            or flow.provider != self.provider
            or not binding
            or not code
            or not hmac.compare_digest(flow.binding_hash, hash_token(binding))
        ):
            logger.warning("Sign-in: callback with an unknown state or another browser's")
            raise SignInError("invalid_state")
        if flow.expired:
            raise SignInError("expired")
        verifier, nonce = derive_flow_secrets(binding)
        id_token = self._exchange(code, verifier)
        identity = self.verifier.verify(id_token, nonce=nonce)

        token = secrets.token_urlsafe(32)
        try:
            with self.database.connection() as conn:
                user, linked, created = self.identities.sign_in(
                    issuer=identity.issuer,
                    subject=identity.subject,
                    email=identity.email,
                    email_verified=True,
                    display_name=identity.name,
                    conn=conn,
                )
                session = self.identities.create_session(
                    user.id,
                    kind="browser",
                    token_hash=hash_token(token),
                    expires_at=datetime.now(UTC) + self.session_ttl,
                    conn=conn,
                )
        except UserNotActiveError:
            logger.warning("Sign-in refused for a disabled User")
            raise SignInError("user_disabled") from None
        logger.info(
            "Signed in user_id=%s provider=%s created=%s session_id=%s",
            user.id,
            self.provider,
            created,
            session.id,
        )
        return SignInResult(
            user=user, identity=linked, created=created, session=session, session_token=token
        )

    def _exchange(self, code: str, verifier: str) -> str:
        """Returns the ID token Google issues for `code`."""
        secret = self.config.read_client_secret()
        register_secret(secret)
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_url,
                "client_id": self.config.client_id,
                "client_secret": secret,
                "code_verifier": verifier,
            }
        ).encode()
        request = urllib.request.Request(
            self.endpoints.token_url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read(_MAX_RESPONSE_BYTES))
        except urllib.error.HTTPError as exc:
            logger.warning("Sign-in: code exchange refused status=%s", exc.code)
            raise SignInError("exchange_failed") from None
        except (OSError, ValueError) as exc:
            logger.warning("Sign-in: code exchange failed error=%s", type(exc).__name__)
            raise SignInError("provider_unavailable") from None
        id_token = payload.get("id_token") if isinstance(payload, dict) else None
        if not isinstance(id_token, str) or not id_token:
            logger.warning("Sign-in: the token response has no ID token")
            raise SignInError("exchange_failed")
        return id_token
