"""
Execution authority policies and HPC account bindings (D01, D02; migration 0019).

Execution authority says whose HPC identity runs a job's Slurm commands:
`controller_owned` (Mode A, Controller SSH: the Controller's operator-configured
identity, trusted deployments only) or `user_owned` (Mode B, User SSH Proxy: the
submitting User's own HPC account, through a session the User owns). It is a
separate dimension from the login-node transport modes `A` and `B` of the HPC
Platform Catalog, whose meaning is unchanged.

[`db.HPCAccessStore`] keeps two operator-managed, versioned policy layers, the
Platform's and the Project's on one Platform, and the per-User, per-Platform
HPC account bindings. A layer without a policy has version 0, which permits only
`controller_owned`. A binding is keyed by `(user_id, platform_id)`, never by a
login node, and is only a claim until an operator records an independent
attestation of ownership. No credential is ever stored.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from .._logging import get_logger
from ._transaction import as_uuid, audit_context, transaction
from .connection import Database, DatabaseError

logger = get_logger("nest.db.hpc_access")

EXECUTION_AUTHORITIES = ("controller_owned", "user_owned")
"""Execution authorities, in a stable order."""

DEFAULT_AUTHORITIES = ("controller_owned",)
"""What a policy layer without a policy row (version 0) permits."""

BINDING_STATUSES = ("claimed", "verified", "replaced", "revoked")
VERIFICATION_METHODS = ("operator_attestation", "nchc_attestation")

_UNIX_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
_EVIDENCE_REF = re.compile(r"[A-Za-z0-9_.:/#@+=-]{1,200}")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")

_BINDING_COLUMNS = (
    "id, user_id, platform_id, version, unix_user, unix_uid, claim_source, status, "
    "verification_method, verification_evidence_ref, verified_by, verified_at, "
    "replaced_by_binding_id, revoked_reason, created_at, ended_at"
)


class HPCAccessError(Exception):
    """An authority policy or HPC account binding change was refused."""


class BindingNotFoundError(HPCAccessError, LookupError):
    """No such HPC account binding, or none current."""


class BindingConflictError(HPCAccessError):
    """
    The change conflicts with another binding: the User already has a current
    binding on the Platform, or the UNIX account is verified for another User.
    """


class BindingStateError(HPCAccessError):
    """The binding is not in a state that allows the change."""


def normalize_authorities(values: Iterable[str]) -> tuple[str, ...]:
    """
    Validates execution authorities and returns them in canonical order.

    Raises:
        `ValueError`: For an unknown or repeated authority.
    """
    items = [str(v).strip() for v in values]
    unknown = [v for v in items if v not in EXECUTION_AUTHORITIES]
    if unknown:
        raise ValueError(
            f"Unknown execution authority {unknown[0]!r}; use one of "
            + ", ".join(EXECUTION_AUTHORITIES)
        )
    if len(set(items)) != len(items):
        raise ValueError("Execution authorities must not repeat")
    return tuple(a for a in EXECUTION_AUTHORITIES if a in items)


def normalize_unix_user(value: str) -> str:
    """Validates a UNIX user name (`ValueError` never echoes it)."""
    name = str(value).strip()
    if not _UNIX_USER.fullmatch(name):
        raise ValueError(
            "A UNIX user name is 1 to 64 letters, digits, '_', '.' or '-', not starting "
            "with '.' or '-'"
        )
    return name


@dataclass(frozen=True)
class AuthorityPolicy:
    """
    The execution authorities in force for a Project on a Platform.

    Attributes:
        project_id, platform_id (`UUID`): The scope.
        authorities (`tuple[str, ...]`): What both layers permit.
        platform_authorities, project_authorities (`tuple[str, ...]`): Each layer.
        platform_version, project_version (`int`): The versions in force; `0` for
            a layer without a policy row.
    """

    project_id: UUID
    platform_id: UUID
    authorities: tuple[str, ...]
    platform_authorities: tuple[str, ...]
    project_authorities: tuple[str, ...]
    platform_version: int
    project_version: int


@dataclass(frozen=True)
class AuthorityPin:
    """
    What an execution attempt records about its authority, written with it and
    never changed.

    Attributes:
        authority (`str`): `controller_owned` or `user_owned`.
        platform_policy_version, project_policy_version (`int`): The policy
            versions it was admitted under.
    """

    authority: str
    platform_policy_version: int
    project_policy_version: int

    def __post_init__(self):
        if self.authority not in EXECUTION_AUTHORITIES:
            raise ValueError("Unknown execution authority")
        if self.platform_policy_version < 0 or self.project_policy_version < 0:
            raise ValueError("Policy versions are not negative")


@dataclass(frozen=True)
class HPCBinding:
    """
    One version of a Nest User's HPC account binding on one Platform. Holds no
    credential.

    Attributes:
        id (`UUID`): The binding version's ID.
        user_id (`UUID`): The Nest User.
        platform_id (`UUID`): The immutable Platform.
        version (`int`): 1, 2, ... per User and Platform.
        unix_user (`str`): The UNIX user name on the Platform.
        unix_uid (`int | None`): The UNIX user ID, when attested.
        claim_source (`str`): `user_claim` or `operator`.
        status (`str`): `claimed`, `verified`, `replaced` or `revoked`.
        verification_method (`str | None`): `operator_attestation` or
            `nchc_attestation`.
        verification_evidence_ref (`str | None`): Reference to the attestation.
        verified_by (`str | None`): The operator who recorded it.
        verified_at, created_at, ended_at (`datetime | None`): When.
        replaced_by_binding_id (`UUID | None`): The version that replaced it.
        revoked_reason (`str | None`): Why it was revoked.
    """

    id: UUID
    user_id: UUID
    platform_id: UUID
    version: int
    unix_user: str
    unix_uid: int | None
    claim_source: str
    status: str
    verification_method: str | None
    verification_evidence_ref: str | None
    verified_by: str | None
    verified_at: datetime | None
    replaced_by_binding_id: UUID | None
    revoked_reason: str | None
    created_at: datetime
    ended_at: datetime | None

    @property
    def verified(self) -> bool:
        """Whether this version is current and independently attested."""
        return self.status == "verified"

    @property
    def current(self) -> bool:
        return self.status in ("claimed", "verified")


def _binding(row: dict[str, Any]) -> HPCBinding:
    return HPCBinding(**row)


def _platform_id(cursor: psycopg.Cursor[dict[str, Any]], ref: UUID | str) -> UUID:
    from .platforms import PlatformNotFoundError, _platform_key

    column, value = _platform_key(ref)
    row = cursor.execute(f"SELECT id FROM hpc_platforms WHERE {column} = %s", (value,)).fetchone()
    if row is None:
        raise PlatformNotFoundError("No such Platform")
    return row["id"]


def _check_reason(value: str) -> str:
    reason = str(value).strip()
    if not _REASON.fullmatch(reason):
        raise ValueError("A revocation reason is a short snake_case word, such as left_project")
    return reason


def _check_evidence(method: str, evidence_ref: str) -> tuple[str, str]:
    if method not in VERIFICATION_METHODS:
        raise ValueError("verification method must be one of " + ", ".join(VERIFICATION_METHODS))
    ref = str(evidence_ref).strip()
    if not _EVIDENCE_REF.fullmatch(ref):
        raise ValueError(
            "The evidence reference is 1 to 200 characters without spaces, such as a "
            "ticket or attestation ID; it is a reference, never the evidence or a credential"
        )
    return method, ref


def _conflict(exc: DatabaseError) -> bool:
    return "hpc_account_bindings_one_owner" in str(exc) or "one_current" in str(exc)


class HPCAccessStore:
    """
    Execution authority policies and HPC account bindings.

    Policy changes and every change to a verified binding name an `operator`; the
    database refuses them otherwise and records each in the security audit log. A
    User may record and withdraw only their own unverified claim.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    # Policies -----------------------------------------------------------------

    def set_platform_policy(
        self,
        platform: UUID | str,
        authorities: Iterable[str],
        *,
        operator: str,
        reason: str,
    ) -> int:
        """
        Appends the Platform's next authority policy version and returns it.

        Raises:
            [`db.PlatformNotFoundError`]: If there is no such Platform.
            `ValueError`: For an unknown authority.
        """
        values = list(normalize_authorities(authorities))
        with transaction(self.database, None, "set a Platform authority policy") as cursor:
            pid = _platform_id(cursor, platform)
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "INSERT INTO hpc_platform_authority_policies (platform_id, version, "
                    "authorities) VALUES (%s, 1, %s) RETURNING version",
                    (pid, values),
                ).fetchone()
        assert row is not None
        return row["version"]

    def set_project_policy(
        self,
        project_id: UUID | str,
        platform: UUID | str,
        authorities: Iterable[str],
        *,
        operator: str,
        reason: str,
    ) -> int:
        """
        Appends the next authority policy version of a Project on a Platform and
        returns it. The Project must be live; it need not be granted the Platform
        yet, and a grant is still required for every job.

        Raises:
            [`db.PlatformNotFoundError`]: If there is no such Platform.
            [`db.ProjectNotFoundError`]: If the Project does not exist or was deleted.
            `ValueError`: For an unknown authority.
        """
        from .projects import ProjectNotFoundError

        values = list(normalize_authorities(authorities))
        project = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "set a Project authority policy") as cursor:
            pid = _platform_id(cursor, platform)
            if (
                cursor.execute(
                    "SELECT 1 FROM projects WHERE id = %s AND deleted_at IS NULL", (project,)
                ).fetchone()
                is None
            ):
                raise ProjectNotFoundError("The Project does not exist or was deleted")
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "INSERT INTO project_platform_authority_policies (project_id, platform_id, "
                    "version, authorities) VALUES (%s, %s, 1, %s) RETURNING version",
                    (project, pid, values),
                ).fetchone()
        assert row is not None
        return row["version"]

    def effective_policy(self, project_id: UUID | str, platform_id: UUID | str) -> AuthorityPolicy:
        """The authorities in force for a Project on a Platform, with both versions."""
        project = as_uuid(project_id, "project_id")
        platform = as_uuid(platform_id, "platform_id")
        with transaction(self.database, None, "read the execution authority policy") as cursor:
            return effective_policy(cursor, project, platform)

    def policy_history(self, platform: UUID | str) -> list[dict[str, Any]]:
        """Every Platform and Project policy version on a Platform, oldest first."""
        with transaction(self.database, None, "list authority policies") as cursor:
            pid = _platform_id(cursor, platform)
            return cursor.execute(
                "SELECT NULL::uuid AS project_id, version, authorities, created_at "
                "FROM hpc_platform_authority_policies WHERE platform_id = %s "
                "UNION ALL SELECT project_id, version, authorities, created_at "
                "FROM project_platform_authority_policies WHERE platform_id = %s "
                "ORDER BY created_at, project_id NULLS FIRST, version",
                (pid, pid),
            ).fetchall()

    # Bindings -----------------------------------------------------------------

    def claim(self, user_id: UUID | str, platform: UUID | str, unix_user: str) -> HPCBinding:
        """
        Records a User's own, unverified claim of a UNIX user name on a Platform. A
        claim is never evidence of ownership and authorizes no job.

        Raises:
            [`db.BindingConflictError`]: If the User already has a current binding.
            [`db.PlatformNotFoundError`]: If there is no such Platform.
            `ValueError`: For an invalid UNIX user name.
        """
        user = as_uuid(user_id, "user_id")
        name = normalize_unix_user(unix_user)
        try:
            with transaction(self.database, None, "record an HPC account claim") as cursor:
                pid = _platform_id(cursor, platform)
                row = cursor.execute(
                    f"INSERT INTO hpc_account_bindings (user_id, platform_id, version, "
                    f"unix_user, claim_source) VALUES (%s, %s, 1, %s, 'user_claim') "
                    f"RETURNING {_BINDING_COLUMNS}",
                    (user, pid, name),
                ).fetchone()
        except DatabaseError as exc:
            if _conflict(exc):
                raise BindingConflictError(
                    "You already have an HPC account binding on this Platform"
                ) from None
            raise
        assert row is not None
        return _binding(row)

    def attest(
        self,
        user_id: UUID | str,
        platform: UUID | str,
        unix_user: str,
        *,
        method: str,
        evidence_ref: str,
        operator: str,
        reason: str,
        unix_uid: int | None = None,
    ) -> HPCBinding:
        """
        Records an operator's independent attestation that `unix_user` on the
        Platform belongs to the User, as the User's verified binding.

        A current claim for the same name is verified in place. Any other current
        binding (a claim for another name, or an earlier verified binding) is
        marked `replaced` by a new version, in the same transaction, so the
        attestation never edits history and attempts keep the version they ran as.

        Raises:
            [`db.BindingConflictError`]: If the UNIX account, by name or by
                `unix_uid`, is verified for another User on the Platform.
            [`db.PlatformNotFoundError`]: If there is no such Platform.
            `ValueError`: For invalid input.
        """
        user = as_uuid(user_id, "user_id")
        name = normalize_unix_user(unix_user)
        method, ref = _check_evidence(method, evidence_ref)
        if unix_uid is not None and unix_uid < 0:
            raise ValueError("unix_uid must not be negative")
        try:
            with transaction(self.database, None, "verify an HPC account binding") as cursor:
                pid = _platform_id(cursor, platform)
                with audit_context(cursor, operator=operator, reason=reason):
                    current = self._lock_current(cursor, user, pid)
                    verification = (method, ref, operator.strip())
                    if (
                        current is not None
                        and current.status == "claimed"
                        and current.unix_user == name
                    ):
                        row = cursor.execute(
                            "UPDATE hpc_account_bindings SET status = 'verified', "
                            "verification_method = %s, verification_evidence_ref = %s, "
                            "verified_by = %s, verified_at = now(), unix_uid = %s, "
                            f"updated_at = now() WHERE id = %s RETURNING {_BINDING_COLUMNS}",
                            (*verification, unix_uid, current.id),
                        ).fetchone()
                    else:
                        row = self._insert_verified(
                            cursor, user, pid, name, unix_uid, verification, current
                        )
        except DatabaseError as exc:
            if _conflict(exc):
                raise BindingConflictError(
                    "That UNIX account (its name or UID) is already verified for another User "
                    "on this Platform"
                ) from None
            raise
        assert row is not None
        binding = _binding(row)
        logger.info(
            "HPC account binding verified user_id=%s platform_id=%s version=%s method=%s",
            user,
            pid,
            binding.version,
            method,
        )
        return binding

    @staticmethod
    def _lock_current(
        cursor: psycopg.Cursor[dict[str, Any]], user: UUID, platform_id: UUID
    ) -> HPCBinding | None:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'nest.hpc_binding:' || %s || ':' || %s, 0))",
            (str(user), str(platform_id)),
        )
        row = cursor.execute(
            f"SELECT {_BINDING_COLUMNS} FROM hpc_account_bindings WHERE user_id = %s "
            "AND platform_id = %s AND status IN ('claimed', 'verified') FOR UPDATE",
            (user, platform_id),
        ).fetchone()
        return _binding(row) if row else None

    @staticmethod
    def _insert_verified(
        cursor: psycopg.Cursor[dict[str, Any]],
        user: UUID,
        platform_id: UUID,
        name: str,
        unix_uid: int | None,
        verification: tuple[str, str, str],
        current: HPCBinding | None,
    ) -> dict[str, Any] | None:
        """
        Inserts a verified version. A `current` binding is first marked replaced by
        it (the reference is checked at commit), which frees the one-current slot.
        """
        new = cursor.execute("SELECT gen_random_uuid() AS id").fetchone()
        assert new is not None
        if current is not None:
            cursor.execute(
                "UPDATE hpc_account_bindings SET status = 'replaced', "
                "replaced_by_binding_id = %s, ended_at = now(), updated_at = now() "
                "WHERE id = %s",
                (new["id"], current.id),
            )
        return cursor.execute(
            "INSERT INTO hpc_account_bindings (id, user_id, platform_id, version, unix_user, "
            "unix_uid, claim_source, status, verification_method, "
            "verification_evidence_ref, verified_by, verified_at) "
            "VALUES (%s, %s, %s, 1, %s, %s, 'operator', 'verified', %s, %s, %s, now()) "
            f"RETURNING {_BINDING_COLUMNS}",
            (new["id"], user, platform_id, name, unix_uid, *verification),
        ).fetchone()

    def revoke(
        self,
        binding_id: UUID | str,
        *,
        reason: str,
        operator: str | None = None,
        user_id: UUID | str | None = None,
        note: str | None = None,
    ) -> HPCBinding:
        """
        Revokes a current binding. Future submissions under it are refused; attempts
        recorded under it keep it.

        An operator may revoke any current binding. Without `operator`, `user_id`
        must be the binding's own User and the binding an unverified claim: a User
        can withdraw their claim but not a verified binding.

        Args:
            reason (`str`): Machine-readable revocation reason, such as
                `left_project`, recorded on the binding.
            note (`str`, *optional*): Free-text reason for the audit log.

        Raises:
            [`db.BindingNotFoundError`]: If there is no such current binding.
            [`db.BindingStateError`]: If a User tries to revoke a verified binding.
        """
        bid = as_uuid(binding_id, "binding_id")
        revoked_reason = _check_reason(reason)
        if operator is None and user_id is None:
            raise ValueError("Name the operator, or the User withdrawing their own claim")
        with transaction(self.database, None, "revoke an HPC account binding") as cursor:
            row = cursor.execute(
                f"SELECT {_BINDING_COLUMNS} FROM hpc_account_bindings WHERE id = %s FOR UPDATE",
                (bid,),
            ).fetchone()
            if row is None or row["status"] not in ("claimed", "verified"):
                raise BindingNotFoundError("No such current HPC account binding")
            if operator is None:
                assert user_id is not None
                if row["user_id"] != as_uuid(user_id, "user_id"):
                    raise BindingNotFoundError("No such current HPC account binding")
                if row["status"] == "verified":
                    raise BindingStateError(
                        "A verified HPC account binding can only be revoked by an operator"
                    )
            with audit_context(
                cursor,
                operator=operator,
                actor_user_id=None if operator else row["user_id"],
                reason=note,
            ):
                updated = cursor.execute(
                    "UPDATE hpc_account_bindings SET status = 'revoked', revoked_reason = %s, "
                    f"ended_at = now(), updated_at = now() WHERE id = %s "
                    f"RETURNING {_BINDING_COLUMNS}",
                    (revoked_reason, bid),
                ).fetchone()
        assert updated is not None
        return _binding(updated)

    def current_binding(self, user_id: UUID | str, platform_id: UUID | str) -> HPCBinding | None:
        """The User's current (claimed or verified) binding on the Platform, if any."""
        user = as_uuid(user_id, "user_id")
        platform = as_uuid(platform_id, "platform_id")
        with transaction(self.database, None, "read an HPC account binding") as cursor:
            row = cursor.execute(
                f"SELECT {_BINDING_COLUMNS} FROM hpc_account_bindings WHERE user_id = %s "
                "AND platform_id = %s AND status IN ('claimed', 'verified')",
                (user, platform),
            ).fetchone()
        return _binding(row) if row else None

    def get_binding(self, binding_id: UUID | str) -> HPCBinding | None:
        bid = as_uuid(binding_id, "binding_id")
        with transaction(self.database, None, "read an HPC account binding") as cursor:
            row = cursor.execute(
                f"SELECT {_BINDING_COLUMNS} FROM hpc_account_bindings WHERE id = %s", (bid,)
            ).fetchone()
        return _binding(row) if row else None

    def bindings(
        self, *, user_id: UUID | str | None = None, platform: UUID | str | None = None
    ) -> list[HPCBinding]:
        """Every binding version, optionally of one User and/or Platform."""
        with transaction(self.database, None, "list HPC account bindings") as cursor:
            user = as_uuid(user_id, "user_id") if user_id is not None else None
            pid = _platform_id(cursor, platform) if platform is not None else None
            rows = cursor.execute(
                f"SELECT {_BINDING_COLUMNS} FROM hpc_account_bindings "
                "WHERE (%(user)s::uuid IS NULL OR user_id = %(user)s) "
                "AND (%(platform)s::uuid IS NULL OR platform_id = %(platform)s) "
                "ORDER BY platform_id, user_id, version",
                {"user": user, "platform": pid},
            ).fetchall()
        return [_binding(r) for r in rows]


def effective_policy(
    cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID, platform_id: UUID
) -> AuthorityPolicy:
    """[`db.HPCAccessStore.effective_policy`] inside the caller's transaction."""
    row = cursor.execute(
        """
        SELECT e.authorities, e.platform_version, e.project_version,
            COALESCE((SELECT authorities FROM hpc_platform_authority_policies
                      WHERE platform_id = %(platform)s ORDER BY version DESC LIMIT 1),
                     %(default)s) AS platform_authorities,
            COALESCE((SELECT authorities FROM project_platform_authority_policies
                      WHERE project_id = %(project)s AND platform_id = %(platform)s
                      ORDER BY version DESC LIMIT 1),
                     %(default)s) AS project_authorities
        FROM nest_effective_authority_policy(%(project)s, %(platform)s) e
        """,
        {"project": project_id, "platform": platform_id, "default": list(DEFAULT_AUTHORITIES)},
    ).fetchone()
    assert row is not None
    return AuthorityPolicy(
        project_id,
        platform_id,
        normalize_authorities(row["authorities"]),
        normalize_authorities(row["platform_authorities"]),
        normalize_authorities(row["project_authorities"]),
        row["platform_version"],
        row["project_version"],
    )
