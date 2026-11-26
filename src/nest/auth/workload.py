"""
CI workload identity: exchanging a GitHub Actions OIDC token for a short-lived
Nest service session (B09).

A GitHub Actions job with `permissions: id-token: write` can ask GitHub for an
OIDC token whose audience it chooses. [`auth.WorkloadExchange.exchange`] accepts
such a token only when:

1. its RS256 signature verifies against GitHub's published keys;
2. `iss` is exactly GitHub's issuer and `aud` is exactly
   `NEST_WORKLOAD_OIDC_AUDIENCE`; `exp`, `iat` and `nbf` hold (60 seconds of clock
   skew), it lives at most an hour, and it carries a `jti`;
3. it matches one active trust policy of the named service principal exactly:
   `repository`, `repository_id`, `repository_owner_id`, `ref`, the workflow file
   (from `workflow_ref`, which must name the same repository and ref),
   `environment` and `event_name` (see [`db.TrustPolicy`]). Nothing is a pattern,
   pull request refs are never trusted, and neither is any event a fork's pull
   request can trigger (`pull_request_target`, `workflow_run`, `issue_comment`);
4. its `jti` was never exchanged before, and the principal is within its exchange
   and active-session quotas, checked in the transaction that issues the session.

The answer is an opaque bearer token (`nest_svc_...`), stored only as its SHA-256
digest, valid for `NEST_WORKLOAD_TOKEN_TTL` seconds and never refreshed. Nothing
here contacts GitHub other than for its public keys, and no workload token, Nest
token or claim value is logged.

A workload token proves only which GitHub workflow run is calling. It is never an
HPC credential and grants no Controller SSH authority (see [`authz.ServiceAccess`]).
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from .._logging import get_logger
from ..audit import AuditLog
from ..authz.workload import SERVICE_TOKEN_PREFIX
from ..config import WorkloadIdentityConfig
from ..db.connection import Database
from ..db.identity import hash_token
from ..db.service_principals import (
    GITHUB_ACTIONS_ISSUER,
    ExchangeRefusedError,
    ServicePrincipalStore,
    ServiceSession,
    TrustPolicy,
)

logger = get_logger("nest.auth.workload")

__all__ = [
    "GITHUB_ACTIONS_JWKS_URL",
    "MAX_WORKLOAD_TOKEN_LIFETIME",
    "WORKLOAD_ERRORS",
    "IssuedServiceToken",
    "WorkloadClaims",
    "WorkloadExchange",
    "WorkloadExchangeError",
    "WorkloadTokenVerifier",
    "match_policy",
]

GITHUB_ACTIONS_JWKS_URL = "https://token.actions.githubusercontent.com/.well-known/jwks"
"""Where GitHub publishes the keys that sign Actions OIDC tokens."""

WORKLOAD_ALGORITHMS = ("RS256",)
"""Signature algorithms accepted: GitHub signs with RS256."""

CLOCK_SKEW = 60.0
"""Seconds of clock difference tolerated for `exp`, `iat` and `nbf`."""

MAX_WORKLOAD_TOKEN_LIFETIME = 3600
"""Longest `exp - iat` accepted, in seconds. GitHub's tokens live minutes."""

MAX_WORKLOAD_TOKEN_BYTES = 8192
"""Longest workload token accepted, before any parsing."""

_REQUIRED = (
    "iss",
    "aud",
    "exp",
    "iat",
    "nbf",
    "jti",
    "sub",
    "repository",
    "repository_id",
    "repository_owner_id",
    "ref",
    "workflow_ref",
)

WORKLOAD_ERRORS: dict[str, int] = {
    "invalid_request": 400,
    "invalid_token": 401,
    "not_trusted": 403,
    "replayed": 401,
    "exchange_quota": 429,
    "session_quota": 429,
    "provider_unavailable": 503,
}
"""HTTP status for each [`auth.WorkloadExchangeError`] reason."""


class WorkloadExchangeError(Exception):
    """
    A workload token was not exchanged. The message names no claim value.

    Attributes:
        reason (`str`): A key of [`auth.WORKLOAD_ERRORS`].
        status (`int`): HTTP status for the answer.
    """

    def __init__(self, reason: str):
        self.reason = reason
        self.status = WORKLOAD_ERRORS[reason]
        super().__init__(f"The workload token was not accepted: {reason}")


@dataclass(frozen=True)
class WorkloadClaims:
    """
    The verified claims of a GitHub Actions OIDC token that Nest uses.

    Attributes:
        issuer, audience, jti, subject (`str`): Standard claims.
        expires_at (`datetime`): The token's `exp`.
        repository, repository_id, repository_owner_id, ref, workflow_ref (`str`):
            GitHub's claims, as strings.
        environment (`str | None`): The deployment environment, if any.
        event_name (`str | None`): The event that triggered the run.
        provenance (`dict`): Non-secret claims kept with the service session.
    """

    issuer: str
    audience: str
    jti: str
    subject: str
    expires_at: datetime
    repository: str
    repository_id: str
    repository_owner_id: str
    ref: str
    workflow_ref: str
    environment: str | None
    event_name: str | None
    provenance: Mapping[str, object]


@dataclass(frozen=True)
class IssuedServiceToken:
    """
    The result of an exchange. Show `token` to the caller once; never log it.

    Attributes:
        token (`str`): The bearer token.
        session ([`db.ServiceSession`]): Its session.
    """

    token: str
    session: ServiceSession


def _text(value: object, limit: int = 255) -> str | None:
    """A claim as a string: GitHub sends IDs as strings, some issuers as numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    return value


class WorkloadTokenVerifier:
    """
    Verifies GitHub Actions OIDC tokens with PyJWT against GitHub's JWKS.

    Args:
        audience (`str`): The required `aud`.
        jwks_url (`str`): The key set. Only tests point it elsewhere.
        issuer (`str`): The required `iss`.
        timeout (`float`): Seconds for a JWKS request.
        refetch_cooldown (`float`): Least seconds between JWKS fetches, so tokens
            with made-up `kid`s cannot make Nest flood GitHub.
    """

    def __init__(
        self,
        audience: str,
        *,
        jwks_url: str = GITHUB_ACTIONS_JWKS_URL,
        issuer: str = GITHUB_ACTIONS_ISSUER,
        timeout: float = 10.0,
        refetch_cooldown: float = 30.0,
    ):
        self.audience = audience
        self.issuer = issuer
        self.jwks = jwt.PyJWKClient(
            jwks_url,
            cache_keys=True,
            lifespan=3600,
            timeout=timeout,
            cooldown_duration=refetch_cooldown,
        )

    def verify(self, token: str) -> WorkloadClaims:
        """
        Returns a token's verified claims.

        Raises:
            [`auth.WorkloadExchangeError`]: `invalid_token` for any failed check,
                `provider_unavailable` when the keys cannot be fetched.
        """
        if not isinstance(token, str) or not 1 <= len(token) <= MAX_WORKLOAD_TOKEN_BYTES:
            raise WorkloadExchangeError("invalid_token")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in WORKLOAD_ALGORITHMS:
                raise jwt.InvalidAlgorithmError("alg")
            key = self.jwks.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=list(WORKLOAD_ALGORITHMS),
                audience=self.audience,
                issuer=self.issuer,
                leeway=CLOCK_SKEW,
                options={"require": list(_REQUIRED[:7]), "strict_aud": True},
            )
        except jwt.PyJWKClientConnectionError:
            logger.warning("Workload exchange: the issuer's keys could not be fetched")
            raise WorkloadExchangeError("provider_unavailable") from None
        except jwt.PyJWTError as exc:
            # The exception names the failed check, never a claim value.
            logger.warning("Workload exchange: token rejected check=%s", type(exc).__name__)
            raise WorkloadExchangeError("invalid_token") from None
        now = datetime.now(UTC).timestamp()
        exp, iat = claims["exp"], claims["iat"]
        if (
            not isinstance(exp, int | float)
            or not isinstance(iat, int | float)
            or exp - iat > MAX_WORKLOAD_TOKEN_LIFETIME
            or iat > now + CLOCK_SKEW
        ):
            logger.warning("Workload exchange: token rejected check=lifetime")
            raise WorkloadExchangeError("invalid_token")
        values: dict[str, str | None] = {}
        for name in _REQUIRED[5:]:
            values[name] = _text(claims.get(name), 512 if name == "workflow_ref" else 255)
            if values[name] is None:
                logger.warning("Workload exchange: token rejected check=%s", name)
                raise WorkloadExchangeError("invalid_token")
        environment = claims.get("environment")
        env = None if environment is None else _text(environment)
        if environment is not None and env is None:
            logger.warning("Workload exchange: token rejected check=environment")
            raise WorkloadExchangeError("invalid_token")
        provenance: dict[str, object] = {"issuer": self.issuer}
        for name in (
            "repository",
            "repository_id",
            "repository_owner_id",
            "ref",
            "sha",
            "workflow_ref",
            "environment",
            "run_id",
            "run_number",
            "run_attempt",
            "event_name",
            "actor_id",
            "runner_environment",
        ):
            value = _text(claims.get(name), 512)
            if value is not None:
                provenance[name] = value
        return WorkloadClaims(
            issuer=self.issuer,
            audience=self.audience,
            jti=str(values["jti"]),
            subject=str(values["sub"]),
            expires_at=datetime.fromtimestamp(exp, UTC),
            repository=str(values["repository"]),
            repository_id=str(values["repository_id"]),
            repository_owner_id=str(values["repository_owner_id"]),
            ref=str(values["ref"]),
            workflow_ref=str(values["workflow_ref"]),
            environment=env,
            event_name=_text(claims.get("event_name"), 64),
            provenance=provenance,
        )


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def match_policy(claims: WorkloadClaims, policy: TrustPolicy) -> str | None:
    """
    Why `claims` do not match `policy` exactly, or `None` when they do.

    Returns:
        `str | None`: The first mismatching check: `inactive`, `issuer`,
            `audience`, `repository`, `repository_id`, `repository_owner_id`,
            `ref`, `workflow`, `environment` or `event`.
    """
    if not policy.active:
        return "inactive"
    if not _same(claims.issuer, policy.issuer):
        return "issuer"
    if not _same(claims.audience, policy.audience):
        return "audience"
    # GitHub compares repository names case-insensitively, but the policy pins the
    # numeric IDs, which never change and are never reused.
    if claims.repository.lower() != policy.repository.lower():
        return "repository"
    if not _same(claims.repository_id, policy.repository_id):
        return "repository_id"
    if not _same(claims.repository_owner_id, policy.repository_owner_id):
        return "repository_owner_id"
    if claims.ref not in policy.refs:
        return "ref"
    workflow, at, ref = claims.workflow_ref.rpartition("@")
    prefix = policy.repository.lower() + "/"
    if not at or ref != claims.ref or not workflow.lower().startswith(prefix):
        return "workflow"
    if workflow[len(prefix) :] not in policy.workflows:
        return "workflow"
    if claims.environment is None:
        if policy.environments:
            return "environment"
    elif claims.environment not in policy.environments:
        return "environment"
    # Checked last but never optional: a fork's pull request can trigger
    # pull_request_target, workflow_run or issue_comment runs on the default branch,
    # with the same ref and workflow_ref as a trusted push.
    if claims.event_name is None or claims.event_name not in policy.event_names:
        return "event"
    return None


class WorkloadExchange:
    """
    Exchanges verified workload tokens for service sessions.

    Args:
        config ([`config.WorkloadIdentityConfig`]): The audience and session lifetime.
        database ([`db.Database`]): Open database.
        verifier ([`auth.WorkloadTokenVerifier`], *optional*): Replaces the default
            verifier of GitHub's keys, for tests with synthetic tokens.
        audit ([`audit.AuditLog`], *optional*): Defaults to one on `database`.
    """

    def __init__(
        self,
        config: WorkloadIdentityConfig,
        database: Database,
        *,
        verifier: WorkloadTokenVerifier | None = None,
        audit: AuditLog | None = None,
    ):
        self.config = config
        self.store = ServicePrincipalStore(database)
        self.verifier = verifier or WorkloadTokenVerifier(config.audience)
        self.audit = audit or AuditLog(database)
        self._log_only = AuditLog(None)

    def exchange(self, token: object, service_principal_id: object) -> IssuedServiceToken:
        """
        Verifies `token` and issues a service session for the named principal.

        Refusals before the token is matched to a trust policy of the principal
        are only logged: anyone can mint a validly signed GitHub token for their
        own repository, so they must not be able to fill the database. Refusals of
        a token from a trusted repository, and every issued session, are recorded
        in the security audit log.

        Raises:
            [`auth.WorkloadExchangeError`]: If refused; nothing is issued.
        """
        try:
            principal_id = UUID(str(service_principal_id))
        except ValueError:
            raise WorkloadExchangeError("invalid_request") from None
        if not isinstance(token, str):
            raise WorkloadExchangeError("invalid_request")
        try:
            claims = self.verifier.verify(token)
        except WorkloadExchangeError as exc:
            self._log_only.record("workload.exchange", "denied", reason=exc.reason)
            raise
        principal = self.store.get_principal(principal_id)
        policies = self.store.policies(principal_id) if principal else []
        trusted_repo = [p for p in policies if _same(p.repository_id, claims.repository_id)]
        details: dict[str, object] = {
            "service_principal_id": str(principal_id),
            "repository_id": claims.repository_id,
            "run_id": claims.provenance.get("run_id"),
        }
        if principal is None or not principal.active or not trusted_repo:
            self._log_only.record(
                "workload.exchange", "denied", reason="not_trusted", details=details
            )
            raise WorkloadExchangeError("not_trusted")
        mismatches = {p.id: match_policy(claims, p) for p in trusted_repo}
        matched = [p for p in trusted_repo if mismatches[p.id] is None]
        if not matched:
            checks = sorted({str(m) for m in mismatches.values()})
            self.audit.record(
                "workload.exchange",
                "denied",
                resource_type="service_principal",
                resource_id=str(principal_id),
                reason="not_trusted",
                details={**details, "checks": ",".join(checks)},
            )
            raise WorkloadExchangeError("not_trusted")
        policy = matched[0]
        secret = SERVICE_TOKEN_PREFIX + secrets.token_urlsafe(32)
        try:
            session = self.store.exchange(
                principal_id,
                policy.id,
                issuer=claims.issuer,
                jti=claims.jti,
                # The verifier accepts the token until exp plus the clock skew, so
                # its use is kept at least that long (see workload_token_uses).
                token_expires_at=claims.expires_at + timedelta(seconds=CLOCK_SKEW),
                token_hash=hash_token(secret),
                ttl_seconds=self.config.token_ttl,
                provenance={**claims.provenance, "subject": claims.subject[:255]},
            )
        except ExchangeRefusedError as exc:
            if exc.reason == "expired":
                reason = "invalid_token"
            else:
                reason = exc.reason if exc.reason in WORKLOAD_ERRORS else "not_trusted"
            self.audit.record(
                "workload.exchange",
                "denied",
                resource_type="service_principal",
                resource_id=str(principal_id),
                reason=exc.reason,
                details={**details, "trust_policy_id": str(policy.id)},
            )
            raise WorkloadExchangeError(reason) from None
        logger.info(
            "Service session issued service_principal_id=%s session_id=%s expires_at=%s",
            principal_id,
            session.id,
            session.expires_at.isoformat(),
        )
        return IssuedServiceToken(secret, session)
