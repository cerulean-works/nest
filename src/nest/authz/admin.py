"""
The privileged management API's service: platform administration through Nest (G03).

[`authz.AdminService`] is the one enforcement point for operations only an active
`platform_admin` may perform: the User directory, creating Projects with an
explicit Owner, Platform grants, Slurm account links, Provider approvals and audit
readback. Every call:

- needs a **browser** session (`browser_only` otherwise): no CLI scope grants
  platform administration;
- reads the caller's `platform_admin` role from the database in the same
  transaction as the change, share-locked, so a revoked or disabled administrator
  is refused from the next request on and a concurrent revocation cannot slip in
  between check and change (`platform_admin_required` otherwise);
- names the administrator as actor and requires a reason for every change, which
  the database's audit triggers record together with the PostgreSQL role.

Every refusal is recorded in the security audit log. Being a `platform_admin`
grants no Project role, Platform, Slurm account, Provider approval or HPC
identity: creating a Project for someone else does not make the administrator a
member, and an approval made here is one of three independent checks a
submission needs (see *Onboard a Project*).

Registering Platforms, login nodes and Slurm accounts, and every other operation
not listed here, stays with the audited operator CLI (`nest-db`).
"""

from __future__ import annotations

from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from .._logging import get_logger
from ..audit import AuditLog, SecurityAuditEvent
from ..db._transaction import as_uuid, transaction
from ..db.accounts import ProjectAccountLink, SlurmAccount, SlurmAccountStore
from ..db.approvals import ProviderApproval, ProviderApprovalStore, normalize_provider
from ..db.connection import Database
from ..db.platforms import Platform, PlatformStore
from ..db.projects import Project, ProjectNotFoundError, ProjectStore
from .policy import Decision, Principal
from .service import (
    AccessDeniedError,
    Authorizer,
    ProjectService,
    is_platform_admin,
    require_reason,
)

logger = get_logger("nest.authz.admin")

__all__ = ["AdminService", "DirectoryUser", "Page", "ProjectAccess"]

MAX_PAGE = 200
"""Largest page the directory and listings return."""

_USER_COLUMNS = (
    "u.id, u.display_name, u.email, u.status, u.created_at, u.disabled_at, "
    "COALESCE(array_agg(r.role ORDER BY r.role) FILTER (WHERE r.role IS NOT NULL), "
    "'{}'::text[]) AS platform_roles"
)


@dataclass(frozen=True)
class DirectoryUser:
    """
    One User in the administrator's directory. Minimal on purpose: no identity
    subject, session, token or credential is ever included.

    Attributes:
        id (`UUID`): The User ID.
        display_name (`str | None`): Name for display.
        email (`str | None`): Last verified address seen. Informational; it never
            identifies a User by itself.
        status (`str`): `active` or `disabled`.
        created_at (`datetime`): When the User first signed in.
        disabled_at (`datetime | None`): When the User was disabled.
        platform_roles (`tuple[str, ...]`): Assigned platform roles, effective
            only while the User is active.
    """

    id: UUID
    display_name: str | None
    email: str | None
    status: str
    created_at: datetime
    disabled_at: datetime | None
    platform_roles: tuple[str, ...]


@dataclass(frozen=True)
class Page[T]:
    """
    One page of a listing.

    Attributes:
        items (`list`): The page.
        next_after (`str | None`): The cursor for the next page, `None` on the last.
    """

    items: list[T]
    next_after: str | None


@dataclass(frozen=True)
class ProjectAccess:
    """
    Readback of everything that lets a Project's members compute, each granted
    independently.

    Attributes:
        project ([`db.Project`]): The Project.
        platforms (`list[db.Platform]`): Platforms granted to it.
        accounts (`list[db.ProjectAccountLink]`): Slurm accounts linked to it, on
            those Platforms.
        approvals (`list[db.ProviderApproval]`): Its Provider approval records,
            approved or revoked.
    """

    project: Project
    platforms: list[Platform]
    accounts: list[ProjectAccountLink]
    approvals: list[ProviderApproval]


def _limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"limit must be between 1 and {MAX_PAGE}")
    return limit


def _reason(reason: str | None) -> str:
    return require_reason(reason)


class AdminService:
    """
    Platform administration through Nest. See the module documentation.

    Args:
        database ([`db.Database`]): Open database.
        projects ([`authz.ProjectService`], *optional*): Used to create Projects.
            Defaults to one on `database`.
        audit ([`audit.AuditLog`], *optional*): Where refusals are recorded.
            Defaults to one on `database`.
    """

    def __init__(
        self,
        database: Database,
        projects: ProjectService | None = None,
        audit: AuditLog | None = None,
    ):
        self.database = database
        self.audit = audit or AuditLog(database)
        self.authorizer = (
            projects.authorizer if projects else Authorizer(database, None, self.audit)
        )
        self.projects = projects or ProjectService(database, self.authorizer)
        self.platforms = PlatformStore(database)
        self.accounts = SlurmAccountStore(database)
        self.approvals = ProviderApprovalStore(database)
        self.project_store = ProjectStore(database)

    # Authorization -------------------------------------------------------------

    def _deny(
        self,
        principal: Principal,
        action: str,
        reason: str,
        project_id: UUID | None = None,
        details: dict[str, object] | None = None,
    ) -> AccessDeniedError:
        self.authorizer.denied(principal, action, project_id, reason, details)
        return AccessDeniedError(action, Decision(False, reason))

    @contextmanager
    def _admin(
        self,
        principal: Principal,
        action: str,
        what: str,
        *,
        project_id: UUID | None = None,
        details: dict[str, object] | None = None,
    ) -> Generator[psycopg.Connection]:
        """
        Runs the block in one transaction, only if the caller is an active
        `platform_admin` through a browser session, checked in that transaction.
        """
        if principal.session_kind != "browser":
            raise self._deny(principal, action, "browser_only", project_id, details)
        allowed = False
        with (
            self.database.connection() as conn,
            transaction(self.database, conn, what) as cursor,
        ):
            if is_platform_admin(cursor, principal.user_id):
                allowed = True
                yield conn
        if not allowed:
            raise self._deny(principal, action, "platform_admin_required", project_id, details)

    def _read(self, principal: Principal, action: str, what: str) -> None:
        with self._admin(principal, action, what):
            pass

    # Users -------------------------------------------------------------------

    def list_users(
        self,
        principal: Principal,
        *,
        limit: int = 50,
        after: UUID | str | None = None,
        email: str | None = None,
    ) -> Page[DirectoryUser]:
        """
        The User directory, oldest first. `email` keeps only Users whose last
        verified address matches, ignoring case.
        """
        limit = _limit(limit)
        cursor_id = as_uuid(after, "after") if after else None
        wanted = email.strip().lower() if email else None
        with (
            self._admin(principal, "admin.user.list", "list Users") as conn,
            transaction(self.database, conn, "list Users") as cursor,
        ):
            rows = cursor.execute(
                f"SELECT {_USER_COLUMNS} FROM users u "
                "LEFT JOIN platform_role_assignments r ON r.user_id = u.id "
                "WHERE (%(after)s::uuid IS NULL OR (u.created_at, u.id) > "
                "       (SELECT created_at, id FROM users WHERE id = %(after)s)) "
                "AND (%(email)s::text IS NULL OR lower(u.email) = %(email)s) "
                "GROUP BY u.id ORDER BY u.created_at, u.id LIMIT %(limit)s",
                {"after": cursor_id, "email": wanted, "limit": limit + 1},
            ).fetchall()
        users = [self._user(row) for row in rows[:limit]]
        return Page(users, str(users[-1].id) if len(rows) > limit else None)

    def get_user(self, principal: Principal, user_id: UUID | str) -> DirectoryUser:
        """
        One User of the directory.

        Raises:
            `LookupError`: If there is no such User.
        """
        uid = as_uuid(user_id, "user_id")
        with (
            self._admin(principal, "admin.user.read", "read a User") as conn,
            transaction(self.database, conn, "read a User") as cursor,
        ):
            row = cursor.execute(
                f"SELECT {_USER_COLUMNS} FROM users u "
                "LEFT JOIN platform_role_assignments r ON r.user_id = u.id "
                "WHERE u.id = %s GROUP BY u.id",
                (uid,),
            ).fetchone()
        if row is None:
            raise LookupError("No such User")
        return self._user(row)

    @staticmethod
    def _user(row: dict[str, Any]) -> DirectoryUser:
        return DirectoryUser(
            id=row["id"],
            display_name=row["display_name"],
            email=row["email"],
            status=row["status"],
            created_at=row["created_at"],
            disabled_at=row["disabled_at"],
            platform_roles=tuple(row["platform_roles"]),
        )

    # Projects ----------------------------------------------------------------

    def list_projects(
        self,
        principal: Principal,
        *,
        limit: int = 50,
        after: UUID | str | None = None,
        include_deleted: bool = False,
    ) -> Page[Project]:
        """Every Project, oldest first, whether or not the caller is a member."""
        limit = _limit(limit)
        self._read(principal, "admin.project.list", "list Projects")
        rows = self.project_store.list_projects(
            limit=limit + 1, after=after, include_deleted=include_deleted
        )
        items = rows[:limit]
        return Page(items, str(items[-1].id) if len(rows) > limit else None)

    def create_project(
        self,
        principal: Principal,
        name: str,
        *,
        owner_user_id: UUID | str | None = None,
        organization_id: UUID | str | None = None,
        reason: str | None,
    ) -> Project:
        """
        Creates a Project with an explicit first Owner, atomically (see
        [`authz.ProjectService.create_project`]). The Owner defaults to the caller.
        """
        # The reason is checked by ProjectService, after the caller is authorized.
        return self.projects.create_project(
            principal,
            name,
            owner_user_id=owner_user_id,
            organization_id=organization_id,
            reason=reason,
        )

    def project_access(self, principal: Principal, project_id: UUID | str) -> ProjectAccess:
        """
        The Project's Platform grants, Slurm account links and Provider approvals.

        Raises:
            [`db.ProjectNotFoundError`]: If there is no such Project.
        """
        pid = as_uuid(project_id, "project_id")
        self._read(principal, "admin.project.read", "read a Project's access")
        project = self.project_store.get_project(pid, include_deleted=True)
        if project is None:
            raise ProjectNotFoundError("No such Project")
        platforms = [
            platform
            for platform in self.platforms.list_platforms()
            if pid in self.platforms.granted_projects(platform.id)
        ]
        links: list[ProjectAccountLink] = []
        for platform in platforms:
            links.extend(self.accounts.mapping(pid, platform.id).links)
        approvals = self.approvals.list_approvals(project_id=pid, limit=1000)
        return ProjectAccess(project, platforms, links, approvals)

    # Platforms and Slurm accounts --------------------------------------------

    def list_platforms(self, principal: Principal) -> list[Platform]:
        """Every Platform in the catalog, including disabled ones."""
        self._read(principal, "admin.platform.list", "list Platforms")
        return self.platforms.list_platforms()

    def list_accounts(self, principal: Principal, platform: UUID | str) -> list[SlurmAccount]:
        """
        A Platform's registered Slurm accounts.

        Raises:
            [`db.PlatformNotFoundError`]: If there is no such Platform.
        """
        self._read(principal, "admin.account.list", "list Slurm accounts")
        return self.accounts.list_accounts(platform)

    def grant_platform(
        self,
        principal: Principal,
        project_id: UUID | str,
        platform: UUID | str,
        *,
        reason: str | None,
    ) -> bool:
        """Grants the Project a Platform. Returns `False` when it already had it."""
        pid = as_uuid(project_id, "project_id")
        why = _reason(reason)
        with self._admin(
            principal, "admin.platform.grant", "grant a Platform", project_id=pid
        ) as conn:
            return self.platforms.grant_project(
                platform, pid, actor_user_id=principal.user_id, reason=why, conn=conn
            )

    def revoke_platform(
        self,
        principal: Principal,
        project_id: UUID | str,
        platform: UUID | str,
        *,
        reason: str | None,
    ) -> bool:
        """
        Withdraws the Project's Platform grant from the next submission on.
        Returns `False` when it had none.
        """
        pid = as_uuid(project_id, "project_id")
        why = _reason(reason)
        with self._admin(
            principal, "admin.platform.revoke", "revoke a Platform grant", project_id=pid
        ) as conn:
            return self.platforms.revoke_project(
                platform, pid, actor_user_id=principal.user_id, reason=why, conn=conn
            )

    def link_account(
        self,
        principal: Principal,
        project_id: UUID | str,
        platform: UUID | str,
        account: str,
        *,
        modes: Iterable[str],
        default: bool = False,
        reason: str | None,
    ) -> ProjectAccountLink:
        """Links one of the Platform's registered Slurm accounts to the Project."""
        pid = as_uuid(project_id, "project_id")
        why = _reason(reason)
        with self._admin(
            principal, "admin.account.link", "link a Slurm account", project_id=pid
        ) as conn:
            return self.accounts.link_project(
                pid,
                platform,
                account,
                modes=modes,
                default=default,
                actor_user_id=principal.user_id,
                reason=why,
                conn=conn,
            )

    def unlink_account(
        self,
        principal: Principal,
        project_id: UUID | str,
        platform: UUID | str,
        account: str,
        *,
        reason: str | None,
    ) -> None:
        """Stops the Project charging a Slurm account."""
        pid = as_uuid(project_id, "project_id")
        why = _reason(reason)
        with self._admin(
            principal, "admin.account.unlink", "unlink a Slurm account", project_id=pid
        ) as conn:
            self.accounts.unlink_project(
                pid, platform, account, actor_user_id=principal.user_id, reason=why, conn=conn
            )

    # Provider approvals --------------------------------------------------------

    def list_approvals(
        self,
        principal: Principal,
        *,
        provider: str | None = None,
        project_id: UUID | str | None = None,
        status: str | None = None,
        limit: int = 50,
        after: tuple[str, UUID] | None = None,
    ) -> Page[ProviderApproval]:
        """Provider approval records, ordered by Provider then Project."""
        limit = _limit(limit)
        self._read(principal, "admin.provider.list", "list Provider approvals")
        rows = self.approvals.list_approvals(
            provider=provider, project_id=project_id, status=status, limit=limit + 1, after=after
        )
        items = rows[:limit]
        cursor = f"{items[-1].provider}:{items[-1].project_id}" if len(rows) > limit else None
        return Page(items, cursor)

    def approve(
        self,
        principal: Principal,
        provider: str,
        project_id: UUID | str,
        *,
        reason: str | None,
    ) -> bool:
        """
        Approves the Project for the Provider, effective from the next submission
        on every Controller using `NEST_PROVIDER_APPROVALS=database`. Returns
        `False` when it was already approved.
        """
        key = normalize_provider(provider)
        pid = as_uuid(project_id, "project_id")
        why = _reason(reason)
        with self._admin(
            principal,
            "admin.provider.approve",
            "approve a Project for a Provider",
            project_id=pid,
            details={"provider": key},
        ) as conn:
            return self.approvals.approve(
                key, pid, actor_user_id=principal.user_id, reason=why, conn=conn
            )

    def revoke_approval(
        self,
        principal: Principal,
        provider: str,
        project_id: UUID | str,
        *,
        reason: str | None,
    ) -> bool:
        """
        Revokes the approval from the next submission on. Running jobs are not
        cancelled. Returns `False` when the Project was not approved.
        """
        key = normalize_provider(provider)
        pid = as_uuid(project_id, "project_id")
        why = _reason(reason)
        with self._admin(
            principal,
            "admin.provider.revoke",
            "revoke a Provider approval",
            project_id=pid,
            details={"provider": key},
        ) as conn:
            return self.approvals.revoke(
                key, pid, actor_user_id=principal.user_id, reason=why, conn=conn
            )

    # Audit -------------------------------------------------------------------

    def audit_events(
        self,
        principal: Principal,
        *,
        limit: int = 50,
        before_id: int | None = None,
        event: str | None = None,
        project_id: UUID | str | None = None,
        actor_user_id: UUID | str | None = None,
    ) -> Page[SecurityAuditEvent]:
        """The security audit log, newest first."""
        limit = _limit(limit)
        pid = as_uuid(project_id, "project_id") if project_id else None
        actor = as_uuid(actor_user_id, "actor_user_id") if actor_user_id else None
        self._read(principal, "admin.audit.read", "read the audit log")
        rows = self.audit.events(
            limit=limit + 1, before_id=before_id, event=event, project_id=pid, actor_user_id=actor
        )
        items = rows[:limit]
        return Page(items, str(items[-1].id) if len(rows) > limit else None)
