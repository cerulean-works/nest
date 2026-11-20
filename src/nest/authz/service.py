"""
The one server-side enforcement point for Project permissions.

[`authz.Authorizer`] answers a single check against the database, and
[`authz.ProjectService`] performs each Project and membership change only after
checking it, in the same transaction. Every interface (HTTP, CLI, SDK, gRPC and
the Web UI) must go through these; clients never decide permissions themselves.

Nothing is cached. Each check reads the caller's membership, the caller's User
status and the Project's deletion in one query, so a removed member, a lowered
role, a disabled User or a deleted Project is refused from the next request on,
and a long-lived stream must check again before each event it sends.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Protocol
from uuid import UUID

import psycopg

from .._logging import get_logger
from ..audit import AuditLog
from ..db._transaction import as_uuid, audit_context, transaction
from ..db.connection import Database
from ..db.projects import Membership, Project, ProjectAuditEvent, ProjectStore
from .policy import (
    NO_TARGET,
    PROVIDER_ACTIONS,
    Action,
    Decision,
    Principal,
    Target,
    decide,
    may_create_project,
    session_allows,
)

logger = get_logger("nest.authz")

_ROLE_QUERY = (
    "SELECT m.role FROM project_memberships m "
    "JOIN projects p ON p.id = m.project_id AND p.deleted_at IS NULL "
    "JOIN users u ON u.id = m.user_id AND u.status = 'active' "
    "WHERE m.project_id = %s AND m.user_id = %s"
)


class AccessDeniedError(Exception):
    """
    The policy refused an action. Nothing was changed.

    Attributes:
        action (`str`): The refused action.
        reason (`str`): The policy's reason (see [`authz.Decision`]).
        visible (`bool`): Whether the caller may know the Project exists. `False`
            when they are not a member (or it does not exist or was deleted);
            interfaces should then answer as if it were not found.
    """

    def __init__(self, action: str, decision: Decision):
        self.action = str(action)
        self.reason = decision.reason
        self.visible = decision.reason != "not_a_member"
        super().__init__(f"Not allowed to {self.action}: {self.reason}")


class ProviderAuthority(Protocol):
    """
    Decides whether a Provider lets a User act through it, for Run submission and
    cancellation (Provider and account policy, D08).

    Project roles never grant Provider or HPC authority: a Project Owner, Admin or
    Platform Admin is still refused here unless the Provider's own policy, such as
    the User's linked Provider Account, permits the action.
    """

    def permits(
        self, principal: Principal, project_id: UUID, action: Action, provider: str | None
    ) -> bool:
        """Whether the Provider permits `action` for this caller in this Project."""
        ...


class DenyAllProviders:
    """The default [`authz.ProviderAuthority`]: no Provider permits anything."""

    def permits(
        self, principal: Principal, project_id: UUID, action: Action, provider: str | None
    ) -> bool:
        return False


def _log_denied(
    audit: AuditLog,
    principal: Principal,
    action: str,
    project_id: UUID | None,
    reason: str,
    details: dict[str, object] | None = None,
) -> None:
    logger.warning(
        "Access denied user_id=%s session_kind=%s action=%s project_id=%s reason=%s",
        principal.user_id,
        principal.session_kind,
        action,
        project_id,
        reason,
    )
    audit.record(
        action,
        "denied",
        actor_user_id=principal.user_id,
        session_id=principal.session_id,
        project_id=project_id,
        reason=reason,
        details={"session_kind": principal.session_kind, **(details or {})},
    )


class Authorizer:
    """
    Checks one action against the database, deny by default.

    Args:
        database ([`db.Database`]): Open database.
        providers ([`authz.ProviderAuthority`], *optional*): Provider policy for
            Run actions. Defaults to [`authz.DenyAllProviders`].
        audit ([`audit.AuditLog`], *optional*): Where refusals are recorded.
            Defaults to one on `database`.
    """

    def __init__(
        self,
        database: Database,
        providers: ProviderAuthority | None = None,
        audit: AuditLog | None = None,
    ):
        self.database = database
        self.providers: ProviderAuthority = providers or DenyAllProviders()
        self.audit = audit or AuditLog(database)

    def denied(
        self,
        principal: Principal,
        action: str,
        project_id: UUID | None,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> None:
        """Logs and records a refusal in the security audit log."""
        _log_denied(self.audit, principal, action, project_id, reason, details)

    @staticmethod
    def role_in(cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID, user_id: UUID):
        """The User's current role, or `None` if not an active member of a live Project."""
        row = cursor.execute(_ROLE_QUERY, (project_id, user_id)).fetchone()
        return row["role"] if row else None

    def check(
        self,
        principal: Principal,
        action: Action | str,
        project_id: UUID | str,
        target: Target = NO_TARGET,
        *,
        conn: psycopg.Connection | None = None,
        details: dict[str, object] | None = None,
    ) -> Decision:
        """
        Decides an action from the caller's current role, read now.

        For the [`authz.Action`] members `RUN_SUBMIT` and `RUN_CANCEL`, the
        Provider must permit it too. A refusal is recorded in the security audit
        log (see [`audit.AuditLog`]) with `details`.
        """
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, conn, "check a Project permission") as cursor:
            role = self.role_in(cursor, pid, principal.user_id)
        decision = decide(principal, role, action, target)
        if (
            decision
            and Action(action) in PROVIDER_ACTIONS
            and not self.providers.permits(principal, pid, Action(action), target.provider)
        ):
            decision = Decision(False, "provider_denied")
        if not decision:
            self.denied(principal, str(action), pid, decision.reason, details)
        return decision

    def require(
        self,
        principal: Principal,
        action: Action | str,
        project_id: UUID | str,
        target: Target = NO_TARGET,
        *,
        conn: psycopg.Connection | None = None,
    ) -> None:
        """
        Like [`authz.Authorizer.check`], but raises when refused.

        Raises:
            [`authz.AccessDeniedError`]: If the action is not allowed.
        """
        decision = self.check(principal, action, project_id, target, conn=conn)
        if not decision:
            raise AccessDeniedError(str(action), decision)


class ProjectService:
    """
    Project and membership operations, each authorized for its caller.

    Each change locks the Project, reads the caller's and the target member's
    current roles, applies the policy and makes the change in one transaction, so
    a concurrent demotion or removal of the caller cannot slip in between check
    and change. The caller is recorded as actor in the Project audit history.

    Args:
        database ([`db.Database`]): Open database.
        authorizer ([`authz.Authorizer`], *optional*): Defaults to one on `database`.
    """

    def __init__(self, database: Database, authorizer: Authorizer | None = None):
        self.database = database
        self.authorizer = authorizer or Authorizer(database)
        self.store = ProjectStore(database)

    @contextmanager
    def _authorized(
        self,
        principal: Principal,
        project_id: UUID | str,
        action: Action,
        what: str,
        *,
        member_user_id: UUID | None = None,
        new_role: str | None = None,
        conn: psycopg.Connection | None = None,
    ) -> Generator[psycopg.Connection]:
        pid = as_uuid(project_id, "project_id")
        refused: Decision | None = None
        with self._connection(conn) as active, transaction(self.database, active, what) as cursor:
            cursor.execute(
                "SELECT 1 FROM projects WHERE id = %s AND deleted_at IS NULL FOR NO KEY UPDATE",
                (pid,),
            )
            role = Authorizer.role_in(cursor, pid, principal.user_id)
            member_role = None
            if member_user_id is not None:
                row = cursor.execute(
                    "SELECT role FROM project_memberships WHERE project_id = %s AND user_id = %s "
                    "FOR UPDATE",
                    (pid, member_user_id),
                ).fetchone()
                member_role = row["role"] if row else None
            target = Target(
                member_user_id=member_user_id, member_role=member_role, new_role=new_role
            )
            decision = decide(principal, role, action, target)
            if decision:
                with audit_context(cursor, actor_user_id=principal.user_id):
                    yield active
            else:
                refused = decision
        if refused is not None:
            # Recorded once the transaction has ended, so the refusal is kept
            # although nothing else is.
            details: dict[str, object] = {"new_role": new_role} if new_role else {}
            if member_user_id is not None:
                details["member_user_id"] = str(member_user_id)
            self.authorizer.denied(principal, str(action), pid, refused.reason, details)
            raise AccessDeniedError(str(action), refused)

    @contextmanager
    def _connection(self, conn: psycopg.Connection | None) -> Generator[psycopg.Connection]:
        if conn is not None:
            yield conn
            return
        with self.database.connection() as owned:
            yield owned

    def _read(self, principal: Principal, project_id: UUID | str, action: Action) -> UUID:
        pid = as_uuid(project_id, "project_id")
        self.authorizer.require(principal, action, pid)
        return pid

    # Projects ------------------------------------------------------------------

    def create_project(
        self, principal: Principal, name: str, *, organization_id: UUID | str | None = None
    ) -> Project:
        """
        Creates a Project with the caller as Owner.

        Filing it under an Organization needs the caller to be a member of it.

        Raises:
            [`authz.AccessDeniedError`]: If the session cannot create Projects or
                the caller is not in the Organization.
        """
        decision = may_create_project(principal)
        if not decision:
            self.authorizer.denied(principal, "project.create", None, decision.reason)
            raise AccessDeniedError("project.create", decision)
        org = as_uuid(organization_id, "organization_id") if organization_id else None
        with (
            self.database.connection() as conn,
            transaction(self.database, conn, "create a Project") as cursor,
        ):
            if org is not None:
                member = cursor.execute(
                    "SELECT 1 FROM organization_memberships "
                    "WHERE organization_id = %s AND user_id = %s",
                    (org, principal.user_id),
                ).fetchone()
                if member is None:
                    denied = Decision(False, "not_a_member")
                    self.authorizer.denied(principal, "project.create", None, "organization")
                    raise AccessDeniedError("project.create", denied)
            active = cursor.execute(
                "SELECT 1 FROM users WHERE id = %s AND status = 'active'",
                (principal.user_id,),
            ).fetchone()
            if active is None:
                raise AccessDeniedError("project.create", Decision(False, "not_a_member"))
            with audit_context(cursor, actor_user_id=principal.user_id):
                return self.store.create_project(
                    name, created_by_user_id=principal.user_id, organization_id=org, conn=conn
                )

    def list_projects(self, principal: Principal) -> list[tuple[Project, str]]:
        """The caller's Projects with their role in each."""
        decision = session_allows(principal, Action.PROJECT_READ)
        if not decision:
            self.authorizer.denied(principal, "project.list", None, decision.reason)
            raise AccessDeniedError("project.list", decision)
        with transaction(self.database, None, "check a User") as cursor:
            active = cursor.execute(
                "SELECT 1 FROM users WHERE id = %s AND status = 'active'", (principal.user_id,)
            ).fetchone()
        return self.store.projects_for_user(principal.user_id) if active else []

    def get_project(self, principal: Principal, project_id: UUID | str) -> tuple[Project, str]:
        """
        The Project and the caller's role in it.

        Raises:
            [`authz.AccessDeniedError`]: If the caller is not a member.
        """
        pid = self._read(principal, project_id, Action.PROJECT_READ)
        project = self.store.get_project(pid)
        role = self.store.role_of(pid, principal.user_id)
        if project is None or role is None:
            raise AccessDeniedError(Action.PROJECT_READ, Decision(False, "not_a_member"))
        return project, role

    def members(self, principal: Principal, project_id: UUID | str) -> list[Membership]:
        """The Project's members, Owner first."""
        return self.store.members(self._read(principal, project_id, Action.MEMBER_LIST))

    def audit_history(
        self,
        principal: Principal,
        project_id: UUID | str,
        *,
        limit: int = 100,
        before_id: int | None = None,
    ) -> list[ProjectAuditEvent]:
        """The Project's audit history, newest first. Owners and Admins only."""
        pid = self._read(principal, project_id, Action.PROJECT_AUDIT_READ)
        return self.store.audit_history(pid, limit=limit, before_id=before_id)

    def rename_project(self, principal: Principal, project_id: UUID | str, name: str) -> Project:
        """Renames the Project. Owners and Admins only."""
        with self._authorized(
            principal, project_id, Action.PROJECT_UPDATE, "rename a Project"
        ) as conn:
            return self.store.rename_project(project_id, name, conn=conn)

    def delete_project(self, principal: Principal, project_id: UUID | str) -> Project:
        """Deletes the Project. Its Owner only."""
        with self._authorized(
            principal, project_id, Action.PROJECT_DELETE, "delete a Project"
        ) as conn:
            return self.store.delete_project(project_id, actor_user_id=principal.user_id, conn=conn)

    def transfer_ownership(
        self,
        principal: Principal,
        project_id: UUID | str,
        new_owner_user_id: UUID | str,
        *,
        previous_owner_role: str = "admin",
    ) -> Project:
        """Makes another member Owner and demotes the caller. Its Owner only."""
        with self._authorized(
            principal, project_id, Action.PROJECT_TRANSFER, "transfer Project ownership"
        ) as conn:
            return self.store.transfer_ownership(
                project_id,
                actor_user_id=principal.user_id,
                new_owner_user_id=new_owner_user_id,
                previous_owner_role=previous_owner_role,
                conn=conn,
            )

    # Members -------------------------------------------------------------------

    def authorize_invitation(
        self,
        principal: Principal,
        project_id: UUID | str,
        role: str,
        *,
        conn: psycopg.Connection | None = None,
    ) -> None:
        """
        Checks that the caller may invite someone with `role`, for the invitation
        flows. Pass `conn` to hold the check's Project lock while creating the
        invitation in the same transaction.

        Raises:
            [`authz.AccessDeniedError`]: If not allowed, including for `owner`.
        """
        with self._authorized(
            principal,
            project_id,
            Action.MEMBER_INVITE,
            "authorize an invitation",
            new_role=role,
            conn=conn,
        ):
            pass

    def add_member(
        self, principal: Principal, project_id: UUID | str, user_id: UUID | str, role: str
    ) -> Membership:
        """Adds an existing User with `role`. Owners and Admins only; never `owner`."""
        with self._authorized(
            principal, project_id, Action.MEMBER_INVITE, "add a Project member", new_role=role
        ) as conn:
            return self.store.add_member(
                project_id, user_id, role, added_by_user_id=principal.user_id, conn=conn
            )

    def change_role(
        self, principal: Principal, project_id: UUID | str, user_id: UUID | str, role: str
    ) -> Membership:
        """Changes a non-Owner member's role. Owners and Admins only; never to `owner`."""
        uid = as_uuid(user_id, "user_id")
        with self._authorized(
            principal,
            project_id,
            Action.MEMBER_ROLE_CHANGE,
            "change a Project role",
            member_user_id=uid,
            new_role=role,
        ) as conn:
            return self.store.set_role(project_id, uid, role, conn=conn)

    def remove_member(
        self, principal: Principal, project_id: UUID | str, user_id: UUID | str
    ) -> None:
        """
        Removes a non-Owner member: Owners and Admins remove anyone else, and any
        non-Owner member may remove themselves (leave). The Owner transfers first.
        """
        uid = as_uuid(user_id, "user_id")
        with self._authorized(
            principal,
            project_id,
            Action.MEMBER_REMOVE,
            "remove a Project member",
            member_user_id=uid,
        ) as conn:
            self.store.remove_member(project_id, uid, conn=conn)
