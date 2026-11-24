"""
Durable Provider approvals (G03, migration 0017).

A Provider approval is the operator's decision that a Controller's Provider
accepts Runs from one Project. [`db.ProviderApprovalStore`] reads and changes them;
[`authz.DatabaseProviderApprovals`] checks them on every submission, and
[`db.ExecutionRegistry.begin_attempt`] checks them again, share-locked, in the
transaction that records the attempt.

An approval is never deleted. A revocation keeps the row with status `revoked`,
so [`db.ProviderApprovalStore.import_projects`] and restarts can never turn it
back into an approval; only an explicit [`db.ProviderApprovalStore.approve`] can.
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
from .connection import Database

logger = get_logger("nest.db.approvals")

__all__ = [
    "APPROVAL_STATUSES",
    "ApprovalImport",
    "ProviderApproval",
    "ProviderApprovalStore",
    "normalize_provider",
    "parse_project_ids",
]

APPROVAL_STATUSES = ("approved", "revoked")
"""Statuses of a [`db.ProviderApproval`]."""

_PROVIDER = re.compile(r"[a-z][a-z0-9_]{0,31}")
_COLUMNS = "provider, project_id, status, created_at, updated_at"


def normalize_provider(value: str) -> str:
    """
    A Provider key: a Platform Profile ID such as `default` or `nano4`, or `local`.

    Raises:
        `ValueError`: If it is not one.
    """
    if not isinstance(value, str) or not _PROVIDER.fullmatch(value.strip()):
        raise ValueError(
            "provider must be a Platform Profile ID (lowercase letters, digits and _, "
            "starting with a letter, at most 32 characters) or 'local'"
        )
    return value.strip()


@dataclass(frozen=True)
class ProviderApproval:
    """
    One Project's approval record for one Provider.

    Attributes:
        provider (`str`): The Provider key.
        project_id (`UUID`): The Project.
        status (`str`): `approved` or `revoked`.
        created_at (`datetime`): When the Project was first approved.
        updated_at (`datetime`): When the status last changed.
    """

    provider: str
    project_id: UUID
    status: str
    created_at: datetime
    updated_at: datetime

    @property
    def approved(self) -> bool:
        """Whether the Provider currently accepts the Project's Runs."""
        return self.status == "approved"


@dataclass(frozen=True)
class ApprovalImport:
    """
    What [`db.ProviderApprovalStore.import_projects`] did, or would do.

    Attributes:
        approved (`tuple[UUID, ...]`): Newly approved Projects.
        unchanged (`tuple[UUID, ...]`): Projects already approved.
        revoked (`tuple[UUID, ...]`): Projects with a recorded revocation, left
            revoked. Approve them explicitly if that is really intended.
        dry_run (`bool`): Whether nothing was written.
    """

    approved: tuple[UUID, ...]
    unchanged: tuple[UUID, ...]
    revoked: tuple[UUID, ...]
    dry_run: bool


def parse_project_ids(values: Iterable[str]) -> list[UUID]:
    """
    Explicit Project IDs from comma-separated values, such as the value of
    `NEST_PROVIDER_ALLOWED_PROJECTS`. Duplicates are dropped, order is kept.

    Raises:
        `ValueError`: For `*`, which is never imported, or anything not a UUID.
    """
    found: list[UUID] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            if item == "*":
                raise ValueError(
                    "'*' approves every Project and is never imported; list the Project IDs "
                    "to approve explicitly"
                )
            try:
                pid = UUID(item)
            except ValueError:
                raise ValueError(f"{item[:40]!r} is not a Project ID") from None
            if pid not in found:
                found.append(pid)
    return found


class ProviderApprovalStore:
    """
    Reads and changes Provider approvals.

    Every change names who made it: an `operator` (with `nest-db`), or an active
    `platform_admin` as `actor_user_id` (through the privileged management API).
    The database refuses a change that names neither, and records every
    approval and revocation in the security audit log.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    # Reads -------------------------------------------------------------------

    def is_approved(
        self,
        provider: str | None,
        project_id: UUID | str,
        *,
        lock: bool = False,
        conn: psycopg.Connection | None = None,
    ) -> bool:
        """
        Whether `provider` currently accepts Runs from the live Project.

        With `lock`, the approval row is share-locked until the caller's
        transaction ends, so a concurrent revocation waits for it or wins
        outright. No answer is cached.

        Raises:
            [`db.DatabaseError`]: If the database cannot answer; callers must then
                refuse (fail closed).
        """
        if provider is None:
            return False
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, conn, "check a Provider approval") as cursor:
            row = cursor.execute(
                "SELECT 1 FROM provider_project_approvals a "
                "JOIN projects p ON p.id = a.project_id AND p.deleted_at IS NULL "
                "WHERE a.provider = %s AND a.project_id = %s AND a.status = 'approved'"
                + (" FOR SHARE OF a" if lock else ""),
                (provider, pid),
            ).fetchone()
        return row is not None

    def get(self, provider: str, project_id: UUID | str) -> ProviderApproval | None:
        """The approval record, approved or revoked, or `None` if there was never one."""
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "read a Provider approval") as cursor:
            row = cursor.execute(
                f"SELECT {_COLUMNS} FROM provider_project_approvals "
                "WHERE provider = %s AND project_id = %s",
                (normalize_provider(provider), pid),
            ).fetchone()
        return ProviderApproval(**row) if row else None

    def list_approvals(
        self,
        *,
        provider: str | None = None,
        project_id: UUID | str | None = None,
        status: str | None = None,
        limit: int = 100,
        after: tuple[str, UUID] | None = None,
    ) -> list[ProviderApproval]:
        """
        Approval records ordered by Provider then Project ID, after the
        `(provider, project_id)` cursor `after`.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if status is not None and status not in APPROVAL_STATUSES:
            raise ValueError(f"status must be one of {APPROVAL_STATUSES}")
        params: dict[str, Any] = {
            "provider": normalize_provider(provider) if provider is not None else None,
            "project": as_uuid(project_id, "project_id") if project_id is not None else None,
            "status": status,
            "after_provider": after[0] if after else None,
            "after_project": after[1] if after else None,
            "limit": limit,
        }
        with transaction(self.database, None, "list Provider approvals") as cursor:
            rows = cursor.execute(
                f"SELECT {_COLUMNS} FROM provider_project_approvals "
                "WHERE (%(provider)s::text IS NULL OR provider = %(provider)s) "
                "AND (%(project)s::uuid IS NULL OR project_id = %(project)s) "
                "AND (%(status)s::text IS NULL OR status = %(status)s) "
                "AND (%(after_provider)s::text IS NULL "
                "     OR (provider, project_id) > (%(after_provider)s, %(after_project)s::uuid)) "
                "ORDER BY provider, project_id LIMIT %(limit)s",
                params,
            ).fetchall()
        return [ProviderApproval(**row) for row in rows]

    # Changes -----------------------------------------------------------------

    def _set(
        self,
        provider: str,
        project_id: UUID | str,
        status: str,
        *,
        operator: str | None,
        actor_user_id: UUID | None,
        reason: str | None,
        conn: psycopg.Connection | None,
    ) -> bool:
        from .projects import ProjectNotFoundError

        key = normalize_provider(provider)
        pid = as_uuid(project_id, "project_id")
        if operator is None and actor_user_id is None:
            raise ValueError("name the operator or the administrator making the change")
        what = "approve a Project for a Provider" if status == "approved" else "revoke an approval"
        with transaction(self.database, conn, what) as cursor:
            if status == "approved" and (
                cursor.execute(
                    "SELECT 1 FROM projects WHERE id = %s AND deleted_at IS NULL FOR KEY SHARE",
                    (pid,),
                ).fetchone()
                is None
            ):
                raise ProjectNotFoundError("The Project does not exist or was deleted")
            with audit_context(
                cursor, operator=operator, actor_user_id=actor_user_id, reason=reason
            ):
                if status == "approved":
                    row = cursor.execute(
                        "INSERT INTO provider_project_approvals (provider, project_id, status) "
                        "VALUES (%s, %s, 'approved') ON CONFLICT (provider, project_id) DO UPDATE "
                        "SET status = 'approved', updated_at = now() "
                        "WHERE provider_project_approvals.status = 'revoked' RETURNING 1",
                        (key, pid),
                    ).fetchone()
                else:
                    row = cursor.execute(
                        "UPDATE provider_project_approvals SET status = 'revoked', "
                        "updated_at = now() WHERE provider = %s AND project_id = %s "
                        "AND status = 'approved' RETURNING 1",
                        (key, pid),
                    ).fetchone()
        changed = row is not None
        if changed:
            logger.info(
                "Provider approval %s provider=%s project_id=%s",
                "granted" if status == "approved" else "revoked",
                key,
                pid,
            )
        return changed

    def approve(
        self,
        provider: str,
        project_id: UUID | str,
        *,
        operator: str | None = None,
        actor_user_id: UUID | None = None,
        reason: str | None = None,
        conn: psycopg.Connection | None = None,
    ) -> bool:
        """
        Approves the Project for the Provider, including after a revocation.
        Returns `False` when it was already approved.

        Raises:
            [`db.ProjectNotFoundError`]: If the Project does not exist or was deleted.
            [`db.DatabaseError`]: If the database refuses it, such as for an
                actor who is not an active `platform_admin`.
        """
        return self._set(
            provider,
            project_id,
            "approved",
            operator=operator,
            actor_user_id=actor_user_id,
            reason=reason,
            conn=conn,
        )

    def revoke(
        self,
        provider: str,
        project_id: UUID | str,
        *,
        operator: str | None = None,
        actor_user_id: UUID | None = None,
        reason: str | None = None,
        conn: psycopg.Connection | None = None,
    ) -> bool:
        """
        Revokes the approval from the next submission on. Running jobs are not
        cancelled. Returns `False` when the Project was not approved.
        """
        return self._set(
            provider,
            project_id,
            "revoked",
            operator=operator,
            actor_user_id=actor_user_id,
            reason=reason,
            conn=conn,
        )

    def import_projects(
        self,
        provider: str,
        project_ids: Iterable[UUID | str],
        *,
        operator: str,
        reason: str,
        dry_run: bool = False,
    ) -> ApprovalImport:
        """
        Approves explicit Project IDs, such as the ones listed in
        `NEST_PROVIDER_ALLOWED_PROJECTS`, in one transaction. Idempotent: an
        approved Project is left as it is, and a revoked one stays revoked. Every
        Project must exist and be live, or nothing is written.

        Raises:
            `ValueError`: For no IDs, `*` or an invalid ID (see `parse_project_ids`).
            [`db.ProjectNotFoundError`]: If a Project does not exist or was deleted.
        """
        from .projects import ProjectNotFoundError

        key = normalize_provider(provider)
        wanted = parse_project_ids(
            str(pid) if isinstance(pid, UUID) else pid for pid in project_ids
        )
        if not wanted:
            raise ValueError("name at least one Project ID to import")
        approved: list[UUID] = []
        unchanged: list[UUID] = []
        revoked: list[UUID] = []
        with (
            self.database.connection() as conn,
            transaction(self.database, conn, "import Provider approvals") as cursor,
        ):
            live = {
                row["id"]
                for row in cursor.execute(
                    "SELECT id FROM projects WHERE id = ANY(%s) AND deleted_at IS NULL "
                    "FOR KEY SHARE",
                    (wanted,),
                ).fetchall()
            }
            missing = [pid for pid in wanted if pid not in live]
            if missing:
                raise ProjectNotFoundError(
                    f"{len(missing)} Project(s) do not exist or were deleted, so nothing was "
                    "imported: " + ", ".join(str(pid) for pid in missing[:10])
                )
            existing = {
                row["project_id"]: row["status"]
                for row in cursor.execute(
                    "SELECT project_id, status FROM provider_project_approvals "
                    "WHERE provider = %s AND project_id = ANY(%s) FOR UPDATE",
                    (key, wanted),
                ).fetchall()
            }
            for pid in wanted:
                status = existing.get(pid)
                if status == "approved":
                    unchanged.append(pid)
                elif status == "revoked":
                    revoked.append(pid)
                else:
                    approved.append(pid)
            if approved and not dry_run:
                with audit_context(cursor, operator=operator, reason=reason):
                    cursor.executemany(
                        "INSERT INTO provider_project_approvals (provider, project_id, status) "
                        "VALUES (%s, %s, 'approved') ON CONFLICT DO NOTHING",
                        [(key, pid) for pid in approved],
                    )
        logger.info(
            "Provider approvals imported provider=%s approved=%s unchanged=%s revoked=%s "
            "dry_run=%s",
            key,
            len(approved),
            len(unchanged),
            len(revoked),
            dry_run,
        )
        return ApprovalImport(tuple(approved), tuple(unchanged), tuple(revoked), dry_run)
