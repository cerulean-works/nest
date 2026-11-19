"""
Nest Projects, their memberships, and the one-Owner rules.

A Project and its Users are many-to-many through `project_memberships`, one row
per (Project, User) with a fixed role. Every Project has exactly one Owner: the
membership whose role is `owner`. `created_by_user_id` only records who created
the Project, and a Run's submitter is stored on its job record; neither changes
when ownership does.

Each mutating method runs in one transaction that first locks the Project row,
so concurrent changes to the same Project's members apply one after another. The
database enforces the same invariants on its own (a unique index allows at most
one Owner and a deferred trigger requires at least one), so no interleaving of
concurrent writers, through this class or not, can commit a Project with no
Owner or two.

Who may call each method (for example, that only an Owner or Admin may add
members) is decided by the authorization layer, not here. The rules here hold
for every caller: no method assigns `owner` except an Owner's own transfer, and
the Owner can be neither removed nor demoted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from .._logging import get_logger
from ._transaction import as_uuid, transaction
from .connection import Database

logger = get_logger("nest.db.projects")

PROJECT_ROLES = ("owner", "admin", "member", "viewer")
"""Fixed V1 Project roles, most privileged first."""

ASSIGNABLE_ROLES = ("admin", "member", "viewer")
"""Roles that can be granted directly or by invitation. `owner` only moves by transfer."""

_PROJECT_COLUMNS = (
    "p.id, p.name, p.organization_id, p.created_by_user_id, p.created_at, p.updated_at, "
    "p.deleted_at, p.deleted_by_user_id, o.user_id AS owner_user_id"
)
_PROJECT_FROM = "projects p JOIN project_memberships o ON o.project_id = p.id AND o.role = 'owner'"
_AUDIT_COLUMNS = (
    "id, occurred_at, project_id, event, target_user_id, old_role, new_role, "
    "actor_user_id, actor_operator, database_role, reason"
)
_MEMBERSHIP_COLUMNS = "project_id, user_id, role, added_by_user_id, created_at, updated_at"


class ProjectError(Exception):
    """A Project or membership change was refused. Nothing was changed."""


class ProjectNotFoundError(ProjectError):
    """The Project does not exist or was deleted."""


class UserNotFoundError(ProjectError):
    """The User does not exist."""


class MemberNotFoundError(ProjectError):
    """The User is not a member of the Project."""


class DuplicateMemberError(ProjectError):
    """The User is already a member of the Project."""


class OwnerRuleError(ProjectError):
    """
    The change would break the one-Owner rule, or needs the Owner's authority.

    Raised when assigning `owner` outside a transfer, removing or demoting the
    Owner, or transferring or deleting a Project as anyone but its current Owner.
    """


@dataclass(frozen=True)
class Project:
    """
    A Nest Project.

    Attributes:
        id (`UUID`): Generated, immutable Project ID.
        name (`str`): Display name. Not unique and not a key.
        organization_id (`UUID | None`): Optional Organization.
        created_by_user_id (`UUID`): Who created the Project. Audit only.
        owner_user_id (`UUID`): The current Owner.
        created_at, updated_at (`datetime`): Row timestamps.
        deleted_at (`datetime | None`): When the Owner deleted the Project.
        deleted_by_user_id (`UUID | None`): Who deleted it.
    """

    id: UUID
    name: str
    organization_id: UUID | None
    created_by_user_id: UUID
    owner_user_id: UUID
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    deleted_by_user_id: UUID | None


@dataclass(frozen=True)
class ProjectAuditEvent:
    """
    One entry of a Project's audit history. Entries are never changed or deleted.

    Attributes:
        id (`int`): Increasing entry number.
        occurred_at (`datetime`): When the change committed its row.
        project_id (`UUID`): The Project.
        event (`str`): `project_created`, `project_renamed`, `project_deleted`,
            `member_added`, `member_role_changed` or `member_removed`.
        target_user_id (`UUID | None`): The member a membership event is about.
        old_role, new_role (`str | None`): The member's role before and after.
        actor_user_id (`UUID | None`): The User who made the change, when known.
        actor_operator (`str | None`): The operator who made it, when one did.
        database_role (`str`): The PostgreSQL role that wrote the change.
        reason (`str | None`): Reason given for the change.
    """

    id: int
    occurred_at: datetime
    project_id: UUID
    event: str
    target_user_id: UUID | None
    old_role: str | None
    new_role: str | None
    actor_user_id: UUID | None
    actor_operator: str | None
    database_role: str
    reason: str | None


@dataclass(frozen=True)
class Membership:
    """
    One User's active role in one Project.

    Attributes:
        project_id (`UUID`): The Project.
        user_id (`UUID`): The member.
        role (`str`): `owner`, `admin`, `member` or `viewer`.
        added_by_user_id (`UUID | None`): Who added the member. Audit only.
        created_at, updated_at (`datetime`): Row timestamps.
    """

    project_id: UUID
    user_id: UUID
    role: str
    added_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


def _assignable(role: str) -> str:
    if role == "owner":
        raise OwnerRuleError("The owner role cannot be granted directly; use transfer_ownership")
    if role not in ASSIGNABLE_ROLES:
        raise ValueError(f"Unknown Project role {role!r}; expected one of {ASSIGNABLE_ROLES}")
    return role


class ProjectStore:
    """
    Creates Projects and changes their memberships and ownership.

    Every method accepts `conn` to join a caller's transaction, for example to add
    the member and mark an invitation accepted atomically. Deferred checks then
    run when the caller commits.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    # Locking -----------------------------------------------------------------

    @staticmethod
    def _lock(cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID) -> None:
        # NO KEY UPDATE serializes membership changes per Project without blocking
        # foreign-key checks from rows that merely reference the Project.
        row = cursor.execute(
            "SELECT id FROM projects WHERE id = %s AND deleted_at IS NULL FOR NO KEY UPDATE",
            (project_id,),
        ).fetchone()
        if row is None:
            raise ProjectNotFoundError("The Project does not exist or was deleted")

    @staticmethod
    def _membership(
        cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID, user_id: UUID
    ) -> dict[str, Any]:
        row = cursor.execute(
            f"SELECT {_MEMBERSHIP_COLUMNS} FROM project_memberships "
            "WHERE project_id = %s AND user_id = %s FOR UPDATE",
            (project_id, user_id),
        ).fetchone()
        if row is None:
            raise MemberNotFoundError("The User is not a member of the Project")
        return row

    @staticmethod
    def _require_owner(
        cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID, actor: UUID, action: str
    ) -> None:
        row = cursor.execute(
            "SELECT user_id FROM project_memberships "
            "WHERE project_id = %s AND role = 'owner' FOR UPDATE",
            (project_id,),
        ).fetchone()
        if row is None or row["user_id"] != actor:
            raise OwnerRuleError(f"Only the Project's current Owner can {action}")

    @staticmethod
    def _project(cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID) -> Project:
        row = cursor.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM {_PROJECT_FROM} WHERE p.id = %s", (project_id,)
        ).fetchone()
        assert row is not None
        return Project(**row)

    # Projects ------------------------------------------------------------------

    def create_project(
        self,
        name: str,
        *,
        created_by_user_id: UUID | str,
        organization_id: UUID | str | None = None,
        conn: psycopg.Connection | None = None,
    ) -> Project:
        """
        Creates a Project with its creator as Owner, in one transaction.

        Args:
            name (`str`): Display name, 1 to 200 characters.
            created_by_user_id (`UUID`): The creator, who becomes the first Owner.
            organization_id (`UUID`, *optional*): Organization to file it under.
            conn (`psycopg.Connection`, *optional*): Transaction to join.

        Raises:
            [`db.UserNotFoundError`]: If the creator does not exist.
            `ValueError`: If the name is empty or too long.
        """
        creator = as_uuid(created_by_user_id, "created_by_user_id")
        org = as_uuid(organization_id, "organization_id") if organization_id else None
        if not 1 <= len(name.strip()) <= 200:
            raise ValueError("The Project name must be 1 to 200 characters")
        with transaction(self.database, conn, "create a Project") as cursor:
            if cursor.execute("SELECT 1 FROM users WHERE id = %s", (creator,)).fetchone() is None:
                raise UserNotFoundError("The creating User does not exist")
            row = cursor.execute(
                "INSERT INTO projects (name, organization_id, created_by_user_id) "
                "VALUES (%s, %s, %s) RETURNING id",
                (name.strip(), org, creator),
            ).fetchone()
            assert row is not None
            cursor.execute(
                "INSERT INTO project_memberships (project_id, user_id, role) "
                "VALUES (%s, %s, 'owner')",
                (row["id"], creator),
            )
            project = self._project(cursor, row["id"])
        logger.info("Project created project_id=%s", project.id)
        return project

    def get_project(
        self, project_id: UUID | str, *, include_deleted: bool = False
    ) -> Project | None:
        """Returns a Project with its current Owner, or `None`."""
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "read a Project") as cursor:
            row = cursor.execute(
                f"SELECT {_PROJECT_COLUMNS} FROM {_PROJECT_FROM} WHERE p.id = %s", (pid,)
            ).fetchone()
        if row is None or (row["deleted_at"] is not None and not include_deleted):
            return None
        return Project(**row)

    def delete_project(
        self,
        project_id: UUID | str,
        *,
        actor_user_id: UUID | str,
        conn: psycopg.Connection | None = None,
    ) -> Project:
        """
        Marks a Project deleted. Only its current Owner can.

        The row, its memberships and its job records are kept for history, but the
        Project is no longer returned, listed, or changeable, and nobody has a role
        in it.

        Raises:
            [`db.ProjectNotFoundError`]: If it does not exist or was deleted.
            [`db.OwnerRuleError`]: If the actor is not the current Owner.
        """
        pid = as_uuid(project_id, "project_id")
        actor = as_uuid(actor_user_id, "actor_user_id")
        with transaction(self.database, conn, "delete a Project") as cursor:
            self._lock(cursor, pid)
            self._require_owner(cursor, pid, actor, "delete it")
            cursor.execute(
                "UPDATE projects SET deleted_at = now(), deleted_by_user_id = %s, "
                "updated_at = now() WHERE id = %s",
                (actor, pid),
            )
            project = self._project(cursor, pid)
        logger.info("Project deleted project_id=%s", pid)
        return project

    def rename_project(
        self, project_id: UUID | str, name: str, *, conn: psycopg.Connection | None = None
    ) -> Project:
        """
        Changes a Project's display name. Its ID never changes.

        Raises:
            [`db.ProjectNotFoundError`]: If it does not exist or was deleted.
            `ValueError`: If the name is empty or too long.
        """
        pid = as_uuid(project_id, "project_id")
        if not 1 <= len(name.strip()) <= 200:
            raise ValueError("The Project name must be 1 to 200 characters")
        with transaction(self.database, conn, "rename a Project") as cursor:
            self._lock(cursor, pid)
            cursor.execute(
                "UPDATE projects SET name = %s, updated_at = now() WHERE id = %s",
                (name.strip(), pid),
            )
            project = self._project(cursor, pid)
        logger.info("Project renamed project_id=%s", pid)
        return project

    # Memberships ---------------------------------------------------------------

    def add_member(
        self,
        project_id: UUID | str,
        user_id: UUID | str,
        role: str,
        *,
        added_by_user_id: UUID | str | None = None,
        conn: psycopg.Connection | None = None,
    ) -> Membership:
        """
        Adds a User to a Project with a role other than `owner`.

        Raises:
            [`db.DuplicateMemberError`]: If the User is already a member, whatever
                the role; change roles with [`db.ProjectStore.set_role`].
            [`db.OwnerRuleError`]: If `role` is `owner`.
            [`db.ProjectNotFoundError`], [`db.UserNotFoundError`]: If either is missing.
        """
        pid = as_uuid(project_id, "project_id")
        uid = as_uuid(user_id, "user_id")
        by = as_uuid(added_by_user_id, "added_by_user_id") if added_by_user_id else None
        _assignable(role)
        with transaction(self.database, conn, "add a Project member") as cursor:
            self._lock(cursor, pid)
            if cursor.execute("SELECT 1 FROM users WHERE id = %s", (uid,)).fetchone() is None:
                raise UserNotFoundError("The User does not exist")
            row = cursor.execute(
                "INSERT INTO project_memberships (project_id, user_id, role, added_by_user_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (project_id, user_id) DO NOTHING "
                f"RETURNING {_MEMBERSHIP_COLUMNS}",
                (pid, uid, role, by),
            ).fetchone()
            if row is None:
                raise DuplicateMemberError("The User is already a member of the Project")
        logger.info("Project member added project_id=%s user_id=%s role=%s", pid, uid, role)
        return Membership(**row)

    def set_role(
        self,
        project_id: UUID | str,
        user_id: UUID | str,
        role: str,
        *,
        conn: psycopg.Connection | None = None,
    ) -> Membership:
        """
        Changes a non-Owner member's role to `admin`, `member` or `viewer`.

        Raises:
            [`db.OwnerRuleError`]: If `role` is `owner`, or the member is the Owner
                (ownership moves only by [`db.ProjectStore.transfer_ownership`]).
            [`db.MemberNotFoundError`]: If the User is not a member.
        """
        pid = as_uuid(project_id, "project_id")
        uid = as_uuid(user_id, "user_id")
        _assignable(role)
        with transaction(self.database, conn, "change a Project role") as cursor:
            self._lock(cursor, pid)
            if self._membership(cursor, pid, uid)["role"] == "owner":
                raise OwnerRuleError("The Owner's role changes only by transferring ownership")
            row = cursor.execute(
                "UPDATE project_memberships SET role = %s, updated_at = now() "
                f"WHERE project_id = %s AND user_id = %s RETURNING {_MEMBERSHIP_COLUMNS}",
                (role, pid, uid),
            ).fetchone()
        assert row is not None
        logger.info("Project role changed project_id=%s user_id=%s role=%s", pid, uid, role)
        return Membership(**row)

    def remove_member(
        self, project_id: UUID | str, user_id: UUID | str, *, conn: psycopg.Connection | None = None
    ) -> None:
        """
        Removes a non-Owner member. Their access ends when this commits.

        Job records they submitted keep them as submitter.

        Raises:
            [`db.OwnerRuleError`]: If the member is the Owner; transfer ownership first.
            [`db.MemberNotFoundError`]: If the User is not a member.
        """
        pid = as_uuid(project_id, "project_id")
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, conn, "remove a Project member") as cursor:
            self._lock(cursor, pid)
            if self._membership(cursor, pid, uid)["role"] == "owner":
                raise OwnerRuleError("The Owner cannot be removed; transfer ownership first")
            cursor.execute(
                "DELETE FROM project_memberships WHERE project_id = %s AND user_id = %s",
                (pid, uid),
            )
        logger.info("Project member removed project_id=%s user_id=%s", pid, uid)

    def transfer_ownership(
        self,
        project_id: UUID | str,
        *,
        actor_user_id: UUID | str,
        new_owner_user_id: UUID | str,
        previous_owner_role: str = "admin",
        conn: psycopg.Connection | None = None,
    ) -> Project:
        """
        Makes an existing member the Owner and demotes the current Owner, atomically.

        Only the current Owner can transfer. When two transfers race, the second
        runs after the first commits, finds its actor is no longer the Owner, and
        fails, so a Project never has two Owners or none.

        Args:
            project_id (`UUID`): The Project.
            actor_user_id (`UUID`): Who requests the transfer; must be the Owner.
            new_owner_user_id (`UUID`): An existing member other than the Owner.
            previous_owner_role (`str`, *optional*, defaults to `"admin"`): The
                former Owner's new role.
            conn (`psycopg.Connection`, *optional*): Transaction to join.

        Raises:
            [`db.OwnerRuleError`]: If the actor is not the Owner, or the target is
                the Owner already.
            [`db.MemberNotFoundError`]: If the target is not a member.
        """
        pid = as_uuid(project_id, "project_id")
        actor = as_uuid(actor_user_id, "actor_user_id")
        target = as_uuid(new_owner_user_id, "new_owner_user_id")
        _assignable(previous_owner_role)
        with transaction(self.database, conn, "transfer Project ownership") as cursor:
            self._lock(cursor, pid)
            self._require_owner(cursor, pid, actor, "transfer ownership")
            if target == actor:
                raise OwnerRuleError("The User is already the Project's Owner")
            self._membership(cursor, pid, target)
            # Demote first: the unique index allows at most one Owner at any moment.
            cursor.execute(
                "UPDATE project_memberships SET role = %s, updated_at = now() "
                "WHERE project_id = %s AND user_id = %s",
                (previous_owner_role, pid, actor),
            )
            cursor.execute(
                "UPDATE project_memberships SET role = 'owner', updated_at = now() "
                "WHERE project_id = %s AND user_id = %s",
                (pid, target),
            )
            cursor.execute("UPDATE projects SET updated_at = now() WHERE id = %s", (pid,))
            project = self._project(cursor, pid)
        logger.info(
            "Project ownership transferred project_id=%s from_user_id=%s to_user_id=%s",
            pid,
            actor,
            target,
        )
        return project

    # Queries -------------------------------------------------------------------

    def role_of(self, project_id: UUID | str, user_id: UUID | str) -> str | None:
        """
        Returns the User's current role, or `None` when they are not a member or
        the Project was deleted. Always read from the database, never cached.
        """
        pid = as_uuid(project_id, "project_id")
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, None, "read a Project role") as cursor:
            row = cursor.execute(
                "SELECT m.role FROM project_memberships m JOIN projects p ON p.id = m.project_id "
                "WHERE m.project_id = %s AND m.user_id = %s AND p.deleted_at IS NULL",
                (pid, uid),
            ).fetchone()
        return row["role"] if row else None

    def members(self, project_id: UUID | str) -> list[Membership]:
        """Returns the Project's active members, Owner first. Empty for a deleted Project."""
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "list Project members") as cursor:
            rows = cursor.execute(
                f"SELECT {_MEMBERSHIP_COLUMNS} FROM project_memberships WHERE project_id = %s "
                "AND EXISTS (SELECT 1 FROM projects WHERE id = %s AND deleted_at IS NULL) "
                "ORDER BY array_position(%s::text[], role), created_at, user_id",
                (pid, pid, list(PROJECT_ROLES)),
            ).fetchall()
        return [Membership(**row) for row in rows]

    def projects_for_user(self, user_id: UUID | str) -> list[tuple[Project, str]]:
        """Returns every Project the User is currently a member of, with their role."""
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, None, "list a User's Projects") as cursor:
            rows = cursor.execute(
                f"SELECT {_PROJECT_COLUMNS}, m.role AS member_role FROM {_PROJECT_FROM} "
                "JOIN project_memberships m ON m.project_id = p.id "
                "WHERE m.user_id = %s AND p.deleted_at IS NULL ORDER BY p.created_at, p.id",
                (uid,),
            ).fetchall()
        return [
            (Project(**{k: v for k, v in r.items() if k != "member_role"}), r["member_role"])
            for r in rows
        ]

    def audit_history(
        self, project_id: UUID | str, *, limit: int = 100, before_id: int | None = None
    ) -> list[ProjectAuditEvent]:
        """
        Returns a Project's audit history, newest first, including after deletion.

        Args:
            limit (`int`, *optional*, defaults to 100): At most this many, 1 to 1000.
            before_id (`int`, *optional*): Only entries older than this one, to page.
        """
        pid = as_uuid(project_id, "project_id")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1 to 1000")
        with transaction(self.database, None, "read a Project's audit history") as cursor:
            rows = cursor.execute(
                f"SELECT {_AUDIT_COLUMNS} FROM project_audit_events WHERE project_id = %s "
                "AND (%s::bigint IS NULL OR id < %s::bigint) ORDER BY id DESC LIMIT %s",
                (pid, before_id, before_id, limit),
            ).fetchall()
        return [ProjectAuditEvent(**row) for row in rows]
