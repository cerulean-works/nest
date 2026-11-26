"""
Service principals, workload trust policies, grants and service sessions (B09,
migration 0020).

A service principal is a non-human Nest caller, such as one CI pipeline. It is not
a User and is never given a User's session, membership, role or execution. An
operator creates it with `nest-db service-principal`, names the workload tokens it
trusts (a [`db.TrustPolicy`]: exact GitHub repository, refs, workflows and
environments) and grants it capabilities on Projects ([`db.ServiceGrant`]).

[`db.ServicePrincipalStore.exchange`] turns one verified workload token into a
short-lived [`db.ServiceSession`], in one transaction that serializes on the
principal: the token's `jti` is recorded (a replay is refused), and the
principal's exchange and active-session quotas are enforced.
[`db.ServicePrincipalStore.authenticate`] accepts a service token only while its
session, its trust policy and its principal are all active and unexpired, read
now. Nothing is cached, so a revocation applies from the next request.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .._logging import get_logger
from ._transaction import as_uuid, audit_context, transaction
from .connection import Database, DatabaseError

logger = get_logger("nest.db.service_principals")

__all__ = [
    "GITHUB_ACTIONS_ISSUER",
    "SERVICE_CAPABILITIES",
    "TRUSTED_EVENTS",
    "ExchangeRefusedError",
    "ServiceGrant",
    "ServicePrincipal",
    "ServicePrincipalError",
    "ServicePrincipalNotFoundError",
    "ServicePrincipalStore",
    "ServiceSession",
    "TrustPolicy",
]

GITHUB_ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"
"""The only workload identity issuer a trust policy may name."""

SERVICE_CAPABILITIES = ("project.read", "platform.read", "run.read", "run.submit", "run.cancel")
"""Capabilities a [`db.ServiceGrant`] may name. Every one but `project.read` needs a
Platform. The `run.*` capabilities have no effect yet: no service principal is
given Controller HPC authority (see *CI workload identity*)."""

PLATFORM_CAPABILITIES = frozenset(SERVICE_CAPABILITIES) - {"project.read"}
"""Capabilities that only a Platform-scoped grant may carry."""

_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_AUDIENCE = re.compile(r"[\x21-\x7e]{1,255}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_NUMERIC_ID = re.compile(r"[0-9]{1,20}")
_REF = re.compile(r"refs/(heads|tags)/[^*?\[\]\s\\]{1,200}")
_WORKFLOW = re.compile(r"\.github/workflows/[A-Za-z0-9_.-]{1,100}\.ya?ml")
_ENVIRONMENT = re.compile(r"[^*?\s]{1,255}")
_JTI = re.compile(r"[\x21-\x7e]{1,255}")

TRUSTED_EVENTS = ("push", "workflow_dispatch", "schedule", "release")
"""GitHub events a trust policy may accept. A fork's pull request can trigger
`pull_request_target`, `workflow_run`, `issue_comment` and similar events in the
base repository's context, on its default branch, so none of those is ever
trusted, whatever ref and workflow match."""

PRUNE_MARGIN_SECONDS = 60
"""Seconds a token use is kept past `token_expires_at` (which already includes the
verifier's clock skew), so a pruned `jti` can never be accepted again."""

_PRINCIPAL_COLUMNS = (
    "id, name, description, status, max_active_sessions, max_exchanges_per_hour, "
    "created_at, revoked_at"
)
_POLICY_COLUMNS = (
    "id, service_principal_id, issuer, audience, repository, repository_id, "
    "repository_owner_id, refs, workflows, environments, event_names, status, created_at, "
    "revoked_at"
)
_GRANT_COLUMNS = (
    "id, service_principal_id, project_id, platform_id, capabilities, status, created_at, "
    "revoked_at"
)
_SESSION_COLUMNS = (
    "id, service_principal_id, trust_policy_id, issuer, jti, provenance, created_at, "
    "expires_at, revoked_at, revoked_reason"
)


class ServicePrincipalError(Exception):
    """A service principal change was refused."""


class ServicePrincipalNotFoundError(ServicePrincipalError, LookupError):
    """No such service principal, trust policy, grant or session."""


class _DeadlinePassed(Exception):
    """Rolls back an exchange whose token deadline passed during an insert wait."""


class ExchangeRefusedError(Exception):
    """
    A workload token exchange was refused. Nothing was issued.

    Attributes:
        reason (`str`): `expired`, `principal_inactive`, `policy_inactive`,
            `replayed`, `exchange_quota` or `session_quota`.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"The workload token exchange was refused: {reason}")


@dataclass(frozen=True)
class ServicePrincipal:
    """
    A non-human Nest caller.

    Attributes:
        id (`UUID`): Its ID.
        name (`str`): Unique operator-chosen name, such as `nest-ci`.
        description (`str | None`): Free text.
        status (`str`): `active` or `revoked` (final).
        max_active_sessions (`int`): Most unexpired, unrevoked service sessions.
        max_exchanges_per_hour (`int`): Most token exchanges in a rolling hour.
        created_at (`datetime`): When it was created.
        revoked_at (`datetime | None`): When it was revoked.
    """

    id: UUID
    name: str
    description: str | None
    status: str
    max_active_sessions: int
    max_exchanges_per_hour: int
    created_at: datetime
    revoked_at: datetime | None

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class TrustPolicy:
    """
    Which workload tokens a service principal accepts. Every field is an exact
    match; nothing is a pattern.

    Attributes:
        id (`UUID`): Its ID.
        service_principal_id (`UUID`): The principal it authenticates.
        issuer (`str`): [`db.GITHUB_ACTIONS_ISSUER`].
        audience (`str`): The token's required `aud`.
        repository (`str`): `owner/name`.
        repository_id (`str`): GitHub's numeric repository ID.
        repository_owner_id (`str`): GitHub's numeric owner ID.
        refs (`tuple[str, ...]`): Accepted `ref` values, `refs/heads/...` or
            `refs/tags/...`.
        workflows (`tuple[str, ...]`): Accepted workflow files, such as
            `.github/workflows/ci.yml`.
        environments (`tuple[str, ...]`): Accepted deployment environments; empty
            accepts only jobs without one.
        event_names (`tuple[str, ...]`): Accepted triggering events, from
            [`db.TRUSTED_EVENTS`]. Events a fork can trigger are never accepted.
        status (`str`): `active` or `revoked` (final).
    """

    id: UUID
    service_principal_id: UUID
    issuer: str
    audience: str
    repository: str
    repository_id: str
    repository_owner_id: str
    refs: tuple[str, ...]
    workflows: tuple[str, ...]
    environments: tuple[str, ...]
    event_names: tuple[str, ...]
    status: str
    created_at: datetime
    revoked_at: datetime | None

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class ServiceGrant:
    """
    Capabilities of a service principal on one Project, and optionally one Platform.

    Attributes:
        id (`int`): Its ID.
        service_principal_id (`UUID`): The principal.
        project_id (`UUID`): The Project.
        platform_id (`UUID | None`): The Platform, or `None` for Project level.
        capabilities (`tuple[str, ...]`): From [`db.SERVICE_CAPABILITIES`].
        status (`str`): `active` or `revoked` (final).
    """

    id: int
    service_principal_id: UUID
    project_id: UUID
    platform_id: UUID | None
    capabilities: tuple[str, ...]
    status: str
    created_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True)
class ServiceSession:
    """
    A short-lived machine credential issued for one verified workload token.

    Attributes:
        id (`UUID`): Its ID, recorded as `session_id` in the audit log.
        service_principal_id (`UUID`): Who it authenticates.
        trust_policy_id (`UUID`): The policy the workload token matched.
        issuer, jti (`str`): The workload token it was exchanged for.
        provenance (`dict`): The token's non-secret claims (repository, ref, sha,
            workflow_ref, environment, run_id, run_attempt, event_name, actor_id).
        expires_at (`datetime`): When it stops being accepted; at most an hour.
        revoked_reason (`str | None`): Why it was revoked, if it was.
    """

    id: UUID
    service_principal_id: UUID
    trust_policy_id: UUID
    issuer: str
    jti: str
    provenance: Mapping[str, Any]
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None


def _principal(row: Mapping[str, Any]) -> ServicePrincipal:
    return ServicePrincipal(**{k: row[k] for k in ServicePrincipal.__dataclass_fields__})


def _policy(row: Mapping[str, Any]) -> TrustPolicy:
    values = {k: row[k] for k in TrustPolicy.__dataclass_fields__}
    for name in ("refs", "workflows", "environments", "event_names"):
        values[name] = tuple(values[name])
    return TrustPolicy(**values)


def _grant(row: Mapping[str, Any]) -> ServiceGrant:
    values = {k: row[k] for k in ServiceGrant.__dataclass_fields__}
    values["capabilities"] = tuple(values["capabilities"])
    return ServiceGrant(**values)


def _session(row: Mapping[str, Any]) -> ServiceSession:
    return ServiceSession(**{k: row[k] for k in ServiceSession.__dataclass_fields__})


def _exact_list(values: Iterable[str], pattern: re.Pattern[str], name: str, *, empty: bool):
    items: list[str] = []
    for value in values:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ValueError(f"{name}: {str(value)[:60]!r} is not an exact value Nest accepts")
        if value not in items:
            items.append(value)
    if not items and not empty:
        raise ValueError(f"{name} must name at least one value")
    if len(items) > 20:
        raise ValueError(f"{name} may name at most 20 values")
    return items


def validate_capabilities(capabilities: Iterable[str], *, platform: bool) -> list[str]:
    """
    The capabilities of a grant, deduplicated.

    Raises:
        `ValueError`: For an unknown capability, none, or a Platform capability on a
            Project-level grant.
    """
    found: list[str] = []
    for item in capabilities:
        if item not in SERVICE_CAPABILITIES:
            raise ValueError(
                f"Unknown capability {str(item)[:40]!r}; use {', '.join(SERVICE_CAPABILITIES)}"
            )
        if item not in found:
            found.append(item)
    if not found:
        raise ValueError("A grant needs at least one capability")
    if not platform and PLATFORM_CAPABILITIES.intersection(found):
        raise ValueError(
            f"{', '.join(sorted(PLATFORM_CAPABILITIES.intersection(found)))} needs --platform"
        )
    return found


class ServicePrincipalStore:
    """
    Reads and changes service principals and their policies, grants and sessions.

    Changes other than exchanging, and logging out of, a service session need an
    `operator` and a `reason`, which the audit triggers record.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    # Operator changes --------------------------------------------------------

    def create_principal(
        self,
        name: str,
        *,
        description: str | None = None,
        max_active_sessions: int = 4,
        max_exchanges_per_hour: int = 60,
        operator: str,
        reason: str,
    ) -> ServicePrincipal:
        """
        Creates an active service principal with no policy and no grant.

        Raises:
            `ValueError`: For an invalid argument.
            [`db.ServicePrincipalError`]: If the name is taken.
        """
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(
                "name must be lowercase letters, digits, '-' and '_', starting with a letter, "
                "at most 63 characters"
            )
        if description is not None and not 1 <= len(description.strip()) <= 500:
            raise ValueError("description must be 1 to 500 characters")
        if not 1 <= max_active_sessions <= 100:
            raise ValueError("max_active_sessions must be 1 to 100")
        if not 1 <= max_exchanges_per_hour <= 10_000:
            raise ValueError("max_exchanges_per_hour must be 1 to 10000")
        try:
            with (
                transaction(self.database, None, "create a service principal") as cursor,
                audit_context(cursor, operator=operator, reason=reason),
            ):
                row = cursor.execute(
                    "INSERT INTO service_principals (name, description, "
                    "max_active_sessions, max_exchanges_per_hour) VALUES (%s, %s, %s, %s) "
                    f"RETURNING {_PRINCIPAL_COLUMNS}",
                    (
                        name,
                        description.strip() if description else None,
                        max_active_sessions,
                        max_exchanges_per_hour,
                    ),
                ).fetchone()
        except DatabaseError as exc:
            if "service_principals_name_unique" in str(exc):
                raise ServicePrincipalError(f"A service principal named {name} exists") from None
            raise
        assert row is not None
        return _principal(row)

    def add_trust_policy(
        self,
        principal: UUID | str,
        *,
        audience: str,
        repository: str,
        repository_id: str,
        repository_owner_id: str,
        refs: Iterable[str],
        workflows: Iterable[str],
        environments: Iterable[str] = (),
        event_names: Iterable[str] = ("push",),
        operator: str,
        reason: str,
    ) -> TrustPolicy:
        """
        Adds a GitHub Actions trust policy to an active principal.

        Raises:
            `ValueError`: For a value that is not exact, such as a wildcard, or a
                pull request ref.
            [`db.ServicePrincipalNotFoundError`]: If the principal is unknown.
            [`db.ServicePrincipalError`]: If it is revoked.
        """
        if not isinstance(audience, str) or not _AUDIENCE.fullmatch(audience):
            raise ValueError("audience must be 1 to 255 printable ASCII characters")
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
            raise ValueError("repository must be owner/name")
        for name, value in (
            ("repository_id", repository_id),
            ("repository_owner_id", repository_owner_id),
        ):
            if not isinstance(value, str) or not _NUMERIC_ID.fullmatch(value):
                raise ValueError(f"{name} must be GitHub's numeric ID")
        ref_list = _exact_list(refs, _REF, "refs", empty=False)
        workflow_list = _exact_list(workflows, _WORKFLOW, "workflows", empty=False)
        environment_list = _exact_list(environments, _ENVIRONMENT, "environments", empty=True)
        events: list[str] = []
        for event in event_names:
            if event not in TRUSTED_EVENTS:
                raise ValueError(
                    f"event {str(event)[:40]!r} cannot be trusted; use "
                    f"{', '.join(TRUSTED_EVENTS)} (events a fork can trigger never are)"
                )
            if event not in events:
                events.append(event)
        if not events:
            raise ValueError("event_names must name at least one event")
        with transaction(self.database, None, "add a trust policy") as cursor:
            found = self._lock_principal(cursor, principal)
            if not found.active:
                raise ServicePrincipalError("The service principal is revoked")
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "INSERT INTO workload_trust_policies (service_principal_id, issuer, audience, "
                    "repository, repository_id, repository_owner_id, refs, workflows, "
                    f"environments, event_names) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"RETURNING {_POLICY_COLUMNS}",
                    (
                        found.id,
                        GITHUB_ACTIONS_ISSUER,
                        audience,
                        repository,
                        repository_id,
                        repository_owner_id,
                        ref_list,
                        workflow_list,
                        environment_list,
                        events,
                    ),
                ).fetchone()
        assert row is not None
        return _policy(row)

    def grant(
        self,
        principal: UUID | str,
        project_id: UUID | str,
        capabilities: Iterable[str],
        *,
        platform_id: UUID | str | None = None,
        operator: str,
        reason: str,
    ) -> ServiceGrant:
        """
        Grants capabilities on a live Project, optionally on a Platform granted to it.

        Raises:
            `ValueError`: For an unknown capability or a Platform capability without
                a Platform.
            [`db.ServicePrincipalNotFoundError`]: If the principal is unknown.
            [`db.ServicePrincipalError`]: If it is revoked, the Project is not live,
                the Platform is not granted to it, or an active grant exists.
        """
        pid = as_uuid(project_id, "project_id")
        platform = as_uuid(platform_id, "platform_id") if platform_id is not None else None
        caps = validate_capabilities(capabilities, platform=platform is not None)
        try:
            with transaction(self.database, None, "grant a service principal") as cursor:
                found = self._lock_principal(cursor, principal)
                if not found.active:
                    raise ServicePrincipalError("The service principal is revoked")
                with audit_context(cursor, operator=operator, reason=reason):
                    row = cursor.execute(
                        "INSERT INTO service_principal_grants (service_principal_id, project_id, "
                        f"platform_id, capabilities) VALUES (%s, %s, %s, %s) "
                        f"RETURNING {_GRANT_COLUMNS}",
                        (found.id, pid, platform, caps),
                    ).fetchone()
        except DatabaseError as exc:
            text = str(exc)
            for constraint, message in (
                ("service_principal_grants_active", "An active grant exists; revoke it first"),
                ("service_principal_grants_project_live", "The Project is not live"),
                (
                    "service_principal_grants_platform_granted",
                    "The Platform is not granted to the Project",
                ),
                ("service_principal_grants_platform_id_fkey", "The Platform does not exist"),
                ("service_principal_grants_project_id_fkey", "The Project does not exist"),
            ):
                if constraint in text:
                    raise ServicePrincipalError(message) from None
            raise
        assert row is not None
        return _grant(row)

    def revoke_principal(
        self, principal: UUID | str, *, operator: str, reason: str
    ) -> ServicePrincipal:
        """
        Revokes a principal for good, with its trust policies, grants and sessions,
        in one transaction.

        Raises:
            [`db.ServicePrincipalNotFoundError`]: If it is unknown.
        """
        with transaction(self.database, None, "revoke a service principal") as cursor:
            found = self._lock_principal(cursor, principal)
            if not found.active:
                return found
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "UPDATE service_principals SET status = 'revoked', revoked_at = now() "
                    f"WHERE id = %s RETURNING {_PRINCIPAL_COLUMNS}",
                    (found.id,),
                ).fetchone()
        assert row is not None
        logger.info("Service principal revoked id=%s", found.id)
        return _principal(row)

    def revoke_policy(self, policy_id: UUID | str, *, operator: str, reason: str) -> TrustPolicy:
        """Revokes a trust policy for good, and every session it issued."""
        pid = as_uuid(policy_id, "policy_id")
        with (
            transaction(self.database, None, "revoke a trust policy") as cursor,
            audit_context(cursor, operator=operator, reason=reason),
        ):
            row = cursor.execute(
                "UPDATE workload_trust_policies SET status = 'revoked', "
                "revoked_at = COALESCE(revoked_at, now()) WHERE id = %s "
                f"RETURNING {_POLICY_COLUMNS}",
                (pid,),
            ).fetchone()
        if row is None:
            raise ServicePrincipalNotFoundError("No such trust policy")
        return _policy(row)

    def revoke_grant(self, grant_id: int, *, operator: str, reason: str) -> ServiceGrant:
        """Revokes a grant for good. Applies from the next request."""
        with (
            transaction(self.database, None, "revoke a service principal grant") as cursor,
            audit_context(cursor, operator=operator, reason=reason),
        ):
            row = cursor.execute(
                "UPDATE service_principal_grants SET status = 'revoked', "
                "revoked_at = COALESCE(revoked_at, now()) WHERE id = %s "
                f"RETURNING {_GRANT_COLUMNS}",
                (grant_id,),
            ).fetchone()
        if row is None:
            raise ServicePrincipalNotFoundError("No such grant")
        return _grant(row)

    def revoke_session(
        self,
        session_id: UUID | str,
        *,
        reason: str = "revoked",
        operator: str | None = None,
        note: str | None = None,
    ) -> bool:
        """
        Revokes one service session. `reason` is `revoked` (an operator, named by
        `operator` with `note` as the audit reason) or `logout` (its holder).
        Returns whether it was active.
        """
        if reason not in ("revoked", "logout"):
            raise ValueError("reason must be revoked or logout")
        sid = as_uuid(session_id, "session_id")
        with (
            transaction(self.database, None, "revoke a service session") as cursor,
            audit_context(cursor, operator=operator, reason=note),
        ):
            row = cursor.execute(
                "UPDATE service_sessions SET revoked_at = now(), revoked_reason = %s "
                "WHERE id = %s AND revoked_at IS NULL RETURNING id",
                (reason, sid),
            ).fetchone()
        return row is not None

    # Reads ---------------------------------------------------------------------

    def _lock_principal(
        self, cursor: psycopg.Cursor[dict[str, Any]], ref: UUID | str
    ) -> ServicePrincipal:
        try:
            pid: UUID | None = as_uuid(ref, "principal")
        except ValueError:
            pid = None
        row = cursor.execute(
            f"SELECT {_PRINCIPAL_COLUMNS} FROM service_principals "
            "WHERE id = %s OR (%s::uuid IS NULL AND name = %s) FOR UPDATE",
            (pid, pid, str(ref)),
        ).fetchone()
        if row is None:
            raise ServicePrincipalNotFoundError("No such service principal")
        return _principal(row)

    def get_principal(self, ref: UUID | str) -> ServicePrincipal | None:
        """A principal by ID or name, whatever its status."""
        try:
            pid: UUID | None = as_uuid(ref, "principal")
        except ValueError:
            pid = None
        with transaction(self.database, None, "read a service principal") as cursor:
            row = cursor.execute(
                f"SELECT {_PRINCIPAL_COLUMNS} FROM service_principals "
                "WHERE id = %s OR (%s::uuid IS NULL AND name = %s)",
                (pid, pid, str(ref)),
            ).fetchone()
        return _principal(row) if row else None

    def list_principals(self) -> list[ServicePrincipal]:
        """Every principal, oldest first."""
        with transaction(self.database, None, "list service principals") as cursor:
            rows = cursor.execute(
                f"SELECT {_PRINCIPAL_COLUMNS} FROM service_principals ORDER BY created_at, id"
            ).fetchall()
        return [_principal(row) for row in rows]

    def policies(self, principal_id: UUID, *, active_only: bool = False) -> list[TrustPolicy]:
        """A principal's trust policies, oldest first."""
        with transaction(self.database, None, "list trust policies") as cursor:
            rows = cursor.execute(
                f"SELECT {_POLICY_COLUMNS} FROM workload_trust_policies "
                "WHERE service_principal_id = %s AND (NOT %s OR status = 'active') "
                "ORDER BY created_at, id",
                (principal_id, active_only),
            ).fetchall()
        return [_policy(row) for row in rows]

    def grants(self, principal_id: UUID, *, active_only: bool = False) -> list[ServiceGrant]:
        """A principal's grants, oldest first."""
        with transaction(self.database, None, "list service principal grants") as cursor:
            rows = cursor.execute(
                f"SELECT {_GRANT_COLUMNS} FROM service_principal_grants "
                "WHERE service_principal_id = %s AND (NOT %s OR status = 'active') ORDER BY id",
                (principal_id, active_only),
            ).fetchall()
        return [_grant(row) for row in rows]

    def active_sessions(self, principal_id: UUID) -> list[ServiceSession]:
        """A principal's unexpired, unrevoked sessions, newest first."""
        with transaction(self.database, None, "list service sessions") as cursor:
            rows = cursor.execute(
                f"SELECT {_SESSION_COLUMNS} FROM service_sessions WHERE service_principal_id = %s "
                "AND revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC",
                (principal_id,),
            ).fetchall()
        return [_session(row) for row in rows]

    def effective_grants(self, principal_id: UUID) -> list[ServiceGrant]:
        """
        The grants in force now: active, of an active principal, on a live Project,
        and for a Platform grant, an active Platform still granted to the Project.
        """
        with transaction(self.database, None, "read service principal grants") as cursor:
            rows = cursor.execute(
                f"SELECT {', '.join('g.' + c for c in _GRANT_COLUMNS.split(', '))} "
                "FROM service_principal_grants g "
                "JOIN service_principals sp ON sp.id = g.service_principal_id "
                "AND sp.status = 'active' "
                "JOIN projects p ON p.id = g.project_id AND p.deleted_at IS NULL "
                "LEFT JOIN hpc_platforms hp ON hp.id = g.platform_id "
                "LEFT JOIN hpc_platform_project_grants pg ON pg.platform_id = g.platform_id "
                "AND pg.project_id = g.project_id "
                "WHERE g.service_principal_id = %s AND g.status = 'active' "
                "AND (g.platform_id IS NULL OR (hp.status = 'active' AND pg.project_id IS NOT NULL)) "
                "ORDER BY g.id",
                (principal_id,),
            ).fetchall()
        return [_grant(row) for row in rows]

    # The exchange and authentication -----------------------------------------

    def exchange(
        self,
        principal_id: UUID,
        policy_id: UUID,
        *,
        issuer: str,
        jti: str,
        token_expires_at: datetime,
        token_hash: bytes,
        ttl_seconds: float,
        provenance: Mapping[str, object],
    ) -> ServiceSession:
        """
        Records a verified workload token and issues its service session, in one
        transaction serialized on the principal.

        The caller has verified the token's signature and claims and matched them
        to `policy_id`; this checks, at the moment of issue, that the principal and
        the policy are active, that the token's `jti` was never exchanged, and the
        principal's quotas. The session expires `ttl_seconds` from now, at most an
        hour.

        `token_expires_at` is when the caller stops accepting the token. It is
        checked again with the wall clock once the locks are held, and by the
        schema when the token's use is recorded, so a token whose deadline passed
        while the exchange waited is refused (`expired`) and leaves no record.

        Raises:
            [`db.ExchangeRefusedError`]: If refused; nothing is written.
        """
        if len(token_hash) != 32:
            raise ValueError("token_hash must be a SHA-256 digest")
        if not 0 < ttl_seconds <= 3600:
            raise ValueError("ttl_seconds must be within an hour")
        if not isinstance(jti, str) or not _JTI.fullmatch(jti):
            raise ExchangeRefusedError("replayed")
        refused: str | None = None
        row = None
        try:
            with transaction(self.database, None, "exchange a workload token") as cursor:
                principal = cursor.execute(
                    f"SELECT {_PRINCIPAL_COLUMNS} FROM service_principals WHERE id = %s FOR UPDATE",
                    (principal_id,),
                ).fetchone()
                policy = cursor.execute(
                    "SELECT status FROM workload_trust_policies "
                    "WHERE id = %s AND service_principal_id = %s FOR SHARE",
                    (policy_id, principal_id),
                ).fetchone()
                # Serialize every exchange of this token before reading the clock,
                # so the token-use insert below never waits on a concurrent one.
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"nest.workload_jti:{issuer}:{jti}",),
                )
                # Every time below is read now, after the locks were granted: a token
                # verified just before its deadline may have waited behind another
                # transaction, and now() is when this transaction started.
                clock = cursor.execute("SELECT clock_timestamp() AS at").fetchone()
                assert clock is not None
                if token_expires_at <= clock["at"]:
                    refused = "expired"
                elif principal is None or principal["status"] != "active":
                    refused = "principal_inactive"
                elif policy is None or policy["status"] != "active":
                    refused = "policy_inactive"
                else:
                    used = cursor.execute(
                        "SELECT count(*) AS n FROM workload_token_uses "
                        "WHERE service_principal_id = %s AND used_at > %s - interval '1 hour'",
                        (principal_id, clock["at"]),
                    ).fetchone()
                    active = cursor.execute(
                        "SELECT count(*) AS n FROM service_sessions WHERE service_principal_id = %s "
                        "AND revoked_at IS NULL AND expires_at > %s",
                        (principal_id, clock["at"]),
                    ).fetchone()
                    assert used is not None and active is not None
                    if cursor.execute(
                        "SELECT 1 FROM workload_token_uses WHERE issuer = %s AND jti = %s",
                        (issuer, jti),
                    ).fetchone():
                        refused = "replayed"
                    elif used["n"] >= principal["max_exchanges_per_hour"]:
                        refused = "exchange_quota"
                    elif active["n"] >= principal["max_active_sessions"]:
                        refused = "session_quota"
                    else:
                        # The insert can wait for a concurrent exchange of the same
                        # token; the schema refuses it if the token's deadline passed.
                        inserted = cursor.execute(
                            "INSERT INTO workload_token_uses (issuer, jti, service_principal_id, "
                            "trust_policy_id, token_expires_at, used_at) "
                            "VALUES (%s, %s, %s, %s, %s, clock_timestamp()) "
                            "ON CONFLICT DO NOTHING RETURNING jti, used_at",
                            (issuer, jti, principal_id, policy_id, token_expires_at),
                        ).fetchone()
                        if inserted is None:
                            # Another principal's exchange of the same token won the race.
                            refused = "replayed"
                        else:
                            # The insert may still have waited on a writer that took no
                            # advisory lock (a BEFORE INSERT trigger runs before that
                            # wait). Check the deadline again now, and roll the whole
                            # transaction back, token use included, if it has passed.
                            issued = cursor.execute("SELECT clock_timestamp() AS at").fetchone()
                            assert issued is not None
                            if token_expires_at <= issued["at"]:
                                raise _DeadlinePassed
                            # One issuance time for the session's creation and expiry.
                            row = cursor.execute(
                                "INSERT INTO service_sessions (token_hash, service_principal_id, "
                                "trust_policy_id, issuer, jti, provenance, created_at, expires_at) "
                                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s + make_interval(secs => %s)) "
                                f"RETURNING {_SESSION_COLUMNS}",
                                (
                                    token_hash,
                                    principal_id,
                                    policy_id,
                                    issuer,
                                    jti,
                                    Jsonb(dict(provenance)),
                                    issued["at"],
                                    issued["at"],
                                    ttl_seconds,
                                ),
                            ).fetchone()
        except _DeadlinePassed:
            # Raised inside the transaction, so nothing it wrote was committed.
            refused = "expired"
        except DatabaseError as exc:
            if "workload_token_uses_unexpired" not in str(exc):
                raise
            # The deadline passed between the check above and the insert.
            refused = "expired"
        if refused is not None:
            raise ExchangeRefusedError(refused)
        assert row is not None
        return _session(row)

    def authenticate(self, token_hash: bytes) -> ServiceSession | None:
        """
        The service session of a token's hash if it is accepted now: unrevoked and
        unexpired, its trust policy and its principal active. Otherwise `None`.
        """
        if len(token_hash) != 32:
            return None
        columns = ", ".join("s." + c for c in _SESSION_COLUMNS.split(", "))
        with transaction(self.database, None, "check a service token") as cursor:
            row = cursor.execute(
                f"SELECT {columns} FROM service_sessions s "
                "JOIN service_principals sp ON sp.id = s.service_principal_id "
                "JOIN workload_trust_policies tp ON tp.id = s.trust_policy_id "
                "WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now() "
                "AND sp.status = 'active' AND tp.status = 'active'",
                (token_hash,),
            ).fetchone()
        return _session(row) if row else None

    def has_capability(
        self,
        principal_id: UUID,
        project_id: UUID,
        capability: str,
        *,
        platform_id: UUID | None = None,
    ) -> bool:
        """
        Whether a grant in force now gives `capability` on the Project, and for a
        Platform capability, on that Platform. Read now; nothing is cached.
        """
        if capability not in SERVICE_CAPABILITIES:
            return False
        if capability in PLATFORM_CAPABILITIES and platform_id is None:
            return False
        for grant in self.effective_grants(principal_id):
            if grant.project_id != project_id or capability not in grant.capabilities:
                continue
            if capability in PLATFORM_CAPABILITIES and grant.platform_id != platform_id:
                continue
            return True
        return False

    def prune_token_uses(self) -> int:
        """Deletes token uses past their replay window. Returns how many."""
        with transaction(self.database, None, "prune workload token uses") as cursor:
            cursor.execute(
                "DELETE FROM workload_token_uses "
                "WHERE token_expires_at + make_interval(secs => %s) <= now() "
                "AND used_at <= now() - interval '1 hour'",
                (PRUNE_MARGIN_SECONDS,),
            )
            return cursor.rowcount
