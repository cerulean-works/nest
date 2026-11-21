"""
Server-side Organization CRUD and membership, each change authorized for its
caller (Patch B06).

An Organization is an optional grouping of Projects with its own generated,
immutable ID. Its roles (`org_admin`, `member`) govern the Organization only:
they grant no Project role, no Platform and no HPC authority, and Projects stay
the authorization boundary. [`authz.decide_organization`] is the matrix; this
service reads the caller's role and applies it in the same transaction as the
change, locking the Organization, so a concurrent demotion or removal of the
caller cannot slip in between. Nothing is cached.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from .._logging import get_logger
from ..db._transaction import as_uuid, audit_context, transaction
from ..db.connection import Database
from ..db.identity import ORGANIZATION_ROLES
from .policy import (
    Decision,
    OrganizationAction,
    Principal,
    Target,
    decide_organization,
    may_create_project,
)
from .service import AccessDeniedError, Authorizer

logger = get_logger("nest.authz.organizations")

_ORG_COLUMNS = "o.id, o.name, o.created_by_user_id, o.created_at, o.updated_at"

_ROLE_QUERY = (
    "SELECT om.role FROM organization_memberships om "
    "JOIN users u ON u.id = om.user_id AND u.status = 'active' "
    "WHERE om.organization_id = %s AND om.user_id = %s"
)


class OrganizationRuleError(Exception):
    """
    The change would break an Organization invariant. Nothing was changed.

    Attributes:
        reason (`str`): `last_org_admin` (an Organization keeps at least one
            `org_admin`), `not_empty` (Projects are still filed under it) or
            `already_member`.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(
            {
                "last_org_admin": "An Organization must keep at least one org_admin",
                "not_empty": "Projects are filed under this Organization",
                "already_member": "The User is already a member of this Organization",
            }[reason]
        )


@dataclass(frozen=True)
class Organization:
    """
    An Organization.

    Attributes:
        id (`UUID`): Immutable Organization ID.
        name (`str`): Display name; not unique and not a key.
        created_by_user_id (`UUID | None`): Audit only.
        created_at (`datetime`): When it was created.
        updated_at (`datetime`): When it last changed.
    """

    id: UUID
    name: str
    created_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class OrganizationMembership:
    """
    A User's role in an Organization.

    Attributes:
        organization_id (`UUID`): The Organization.
        user_id (`UUID`): The member.
        role (`str`): `org_admin` or `member`.
        created_at (`datetime`): When they joined.
    """

    organization_id: UUID
    user_id: UUID
    role: str
    created_at: datetime


def _name(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise ValueError("name must be 1 to 200 characters")
    return value.strip()


class OrganizationService:
    """
    Organization operations, each authorized for its caller.

    Args:
        database ([`db.Database`]): Open database.
        authorizer ([`authz.Authorizer`], *optional*): Records refusals in the
            security audit log. Defaults to one on `database`.
    """

    def __init__(self, database: Database, authorizer: Authorizer | None = None):
        self.database = database
        self.authorizer = authorizer or Authorizer(database)

    def _refuse(
        self,
        principal: Principal,
        action: str,
        decision: Decision,
        organization_id: UUID | None,
        details: dict[str, object] | None = None,
    ) -> AccessDeniedError:
        self.authorizer.denied(
            principal,
            action,
            None,
            decision.reason,
            {**({"organization_id": str(organization_id)} if organization_id else {}),
             **(details or {})},
        )  # fmt: skip
        return AccessDeniedError(action, decision)

    @contextmanager
    def _authorized(
        self,
        principal: Principal,
        organization_id: UUID | str,
        action: OrganizationAction,
        what: str,
        *,
        member_user_id: UUID | None = None,
        new_role: str | None = None,
    ) -> Generator[psycopg.Cursor[dict[str, Any]]]:
        oid = as_uuid(organization_id, "organization_id")
        refused: Decision | None = None
        with (
            self.database.connection() as conn,
            transaction(self.database, conn, what) as cursor,
        ):
            exists = cursor.execute(
                "SELECT 1 FROM organizations WHERE id = %s FOR NO KEY UPDATE", (oid,)
            ).fetchone()
            row = (
                cursor.execute(_ROLE_QUERY, (oid, principal.user_id)).fetchone() if exists else None
            )
            member_role = None
            if member_user_id is not None and exists:
                member = cursor.execute(
                    "SELECT role FROM organization_memberships "
                    "WHERE organization_id = %s AND user_id = %s FOR UPDATE",
                    (oid, member_user_id),
                ).fetchone()
                member_role = member["role"] if member else None
            target = Target(
                member_user_id=member_user_id, member_role=member_role, new_role=new_role
            )
            decision = decide_organization(principal, row["role"] if row else None, action, target)
            if decision:
                with audit_context(cursor, actor_user_id=principal.user_id):
                    yield cursor
            else:
                refused = decision
        if refused is not None:
            details: dict[str, object] = {"new_role": new_role} if new_role else {}
            if member_user_id is not None:
                details["member_user_id"] = str(member_user_id)
            raise self._refuse(principal, str(action), refused, oid, details)

    @staticmethod
    def _organization(cursor: psycopg.Cursor[dict[str, Any]], oid: UUID) -> Organization:
        row = cursor.execute(
            f"SELECT {_ORG_COLUMNS} FROM organizations o WHERE o.id = %s", (oid,)
        ).fetchone()
        assert row is not None
        return Organization(**row)

    @staticmethod
    def _admins(cursor: psycopg.Cursor[dict[str, Any]], oid: UUID) -> int:
        # Locks every org_admin row, so two concurrent demotions cannot both pass.
        rows = cursor.execute(
            "SELECT user_id FROM organization_memberships "
            "WHERE organization_id = %s AND role = 'org_admin' FOR UPDATE",
            (oid,),
        ).fetchall()
        return len(rows)

    # Organizations -------------------------------------------------------------

    def create_organization(self, principal: Principal, name: str) -> Organization:
        """
        Creates an Organization with the caller as its first `org_admin`. Any
        active User may, from a browser session.

        Raises:
            [`authz.AccessDeniedError`]: If the session may not create one.
        """
        decision = may_create_project(principal)
        if not decision:
            raise self._refuse(principal, "organization.create", decision, None)
        clean = _name(name)
        with (
            self.database.connection() as conn,
            transaction(self.database, conn, "create an Organization") as cursor,
        ):
            active = cursor.execute(
                "SELECT 1 FROM users WHERE id = %s AND status = 'active'", (principal.user_id,)
            ).fetchone()
            if active is None:
                raise AccessDeniedError("organization.create", Decision(False, "not_a_member"))
            with audit_context(cursor, actor_user_id=principal.user_id):
                row = cursor.execute(
                    "INSERT INTO organizations (name, created_by_user_id) VALUES (%s, %s) "
                    "RETURNING id",
                    (clean, principal.user_id),
                ).fetchone()
                assert row is not None
                cursor.execute(
                    "INSERT INTO organization_memberships (organization_id, user_id, role) "
                    "VALUES (%s, %s, 'org_admin')",
                    (row["id"], principal.user_id),
                )
                organization = self._organization(cursor, row["id"])
        logger.info("Organization created organization_id=%s", organization.id)
        return organization

    def list_organizations(self, principal: Principal) -> list[tuple[Organization, str]]:
        """The caller's Organizations, by name, with their role in each."""
        decision = decide_organization(principal, "member", OrganizationAction.READ)
        if not decision:
            raise self._refuse(principal, "organization.list", decision, None)
        with transaction(self.database, None, "list Organizations") as cursor:
            rows = cursor.execute(
                f"SELECT {_ORG_COLUMNS}, om.role FROM organizations o "
                "JOIN organization_memberships om ON om.organization_id = o.id "
                "JOIN users u ON u.id = om.user_id AND u.status = 'active' "
                "WHERE om.user_id = %s ORDER BY o.name, o.id",
                (principal.user_id,),
            ).fetchall()
        return [
            (Organization(**{k: v for k, v in r.items() if k != "role"}), r["role"]) for r in rows
        ]

    def get_organization(
        self, principal: Principal, organization_id: UUID | str
    ) -> tuple[Organization, str]:
        """
        The Organization and the caller's role in it.

        Raises:
            [`authz.AccessDeniedError`]: If the caller is not a member (invisible).
        """
        oid = as_uuid(organization_id, "organization_id")
        with transaction(self.database, None, "read an Organization") as cursor:
            row = cursor.execute(_ROLE_QUERY, (oid, principal.user_id)).fetchone()
            role = row["role"] if row else None
            decision = decide_organization(principal, role, OrganizationAction.READ)
            organization = self._organization(cursor, oid) if decision else None
        if organization is None or role is None:
            raise self._refuse(principal, str(OrganizationAction.READ), decision, oid)
        return organization, role

    def rename_organization(
        self, principal: Principal, organization_id: UUID | str, name: str
    ) -> Organization:
        """Renames the Organization. `org_admin` only. Its ID never changes."""
        clean = _name(name)
        with self._authorized(
            principal, organization_id, OrganizationAction.UPDATE, "rename an Organization"
        ) as cursor:
            oid = as_uuid(organization_id, "organization_id")
            cursor.execute(
                "UPDATE organizations SET name = %s, updated_at = now() WHERE id = %s",
                (clean, oid),
            )
            return self._organization(cursor, oid)

    def delete_organization(self, principal: Principal, organization_id: UUID | str) -> None:
        """
        Deletes the Organization and its memberships. `org_admin` only, and only
        once no Project, live or deleted, is filed under it, so no Project ever
        loses its tenant record.

        Raises:
            [`authz.OrganizationRuleError`]: `not_empty`.
        """
        with self._authorized(
            principal, organization_id, OrganizationAction.DELETE, "delete an Organization"
        ) as cursor:
            oid = as_uuid(organization_id, "organization_id")
            filed = cursor.execute(
                "SELECT 1 FROM projects WHERE organization_id = %s LIMIT 1 FOR KEY SHARE", (oid,)
            ).fetchone()
            if filed is not None:
                raise OrganizationRuleError("not_empty")
            cursor.execute("DELETE FROM organizations WHERE id = %s", (oid,))
        logger.info("Organization deleted organization_id=%s", organization_id)

    # Members -------------------------------------------------------------------

    def members(
        self, principal: Principal, organization_id: UUID | str
    ) -> list[OrganizationMembership]:
        """The Organization's members, `org_admin`s first."""
        with self._authorized(
            principal, organization_id, OrganizationAction.MEMBER_LIST, "list Organization members"
        ) as cursor:
            rows = cursor.execute(
                "SELECT organization_id, user_id, role, created_at FROM organization_memberships "
                "WHERE organization_id = %s ORDER BY role <> 'org_admin', created_at, user_id",
                (as_uuid(organization_id, "organization_id"),),
            ).fetchall()
        return [OrganizationMembership(**r) for r in rows]

    def add_member(
        self,
        principal: Principal,
        organization_id: UUID | str,
        user_id: UUID | str,
        role: str,
    ) -> OrganizationMembership:
        """
        Adds an existing, active User. `org_admin` only. Grants no Project role.

        Raises:
            [`authz.OrganizationRuleError`]: `already_member`.
            `LookupError`: If there is no such active User.
        """
        uid = as_uuid(user_id, "user_id")
        if role not in ORGANIZATION_ROLES:
            raise ValueError(f"role must be one of {ORGANIZATION_ROLES}")
        with self._authorized(
            principal,
            organization_id,
            OrganizationAction.MEMBER_ADD,
            "add an Organization member",
            new_role=role,
        ) as cursor:
            oid = as_uuid(organization_id, "organization_id")
            if (
                cursor.execute(
                    "SELECT 1 FROM users WHERE id = %s AND status = 'active'", (uid,)
                ).fetchone()
                is None
            ):
                raise LookupError("No such User")
            row = cursor.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING "
                "RETURNING organization_id, user_id, role, created_at",
                (oid, uid, role),
            ).fetchone()
            if row is None:
                raise OrganizationRuleError("already_member")
        return OrganizationMembership(**row)

    def change_role(
        self,
        principal: Principal,
        organization_id: UUID | str,
        user_id: UUID | str,
        role: str,
    ) -> OrganizationMembership:
        """
        Changes a member's role. `org_admin` only; the last `org_admin` cannot be
        demoted.

        Raises:
            [`authz.OrganizationRuleError`]: `last_org_admin`.
        """
        uid = as_uuid(user_id, "user_id")
        with self._authorized(
            principal,
            organization_id,
            OrganizationAction.MEMBER_ROLE_CHANGE,
            "change an Organization role",
            member_user_id=uid,
            new_role=role,
        ) as cursor:
            oid = as_uuid(organization_id, "organization_id")
            current = cursor.execute(
                "SELECT role FROM organization_memberships "
                "WHERE organization_id = %s AND user_id = %s",
                (oid, uid),
            ).fetchone()
            assert current is not None
            if (
                current["role"] == "org_admin"
                and role != "org_admin"
                and self._admins(cursor, oid) <= 1
            ):
                raise OrganizationRuleError("last_org_admin")
            row = cursor.execute(
                "UPDATE organization_memberships SET role = %s, updated_at = now() "
                "WHERE organization_id = %s AND user_id = %s "
                "RETURNING organization_id, user_id, role, created_at",
                (role, oid, uid),
            ).fetchone()
        assert row is not None
        return OrganizationMembership(**row)

    def remove_member(
        self, principal: Principal, organization_id: UUID | str, user_id: UUID | str
    ) -> None:
        """
        Removes a member: an `org_admin` removes anyone, any member may leave.
        The last `org_admin` cannot leave or be removed. The member's Project
        memberships are untouched.

        Raises:
            [`authz.OrganizationRuleError`]: `last_org_admin`.
        """
        uid = as_uuid(user_id, "user_id")
        with self._authorized(
            principal,
            organization_id,
            OrganizationAction.MEMBER_REMOVE,
            "remove an Organization member",
            member_user_id=uid,
        ) as cursor:
            oid = as_uuid(organization_id, "organization_id")
            current = cursor.execute(
                "SELECT role FROM organization_memberships "
                "WHERE organization_id = %s AND user_id = %s",
                (oid, uid),
            ).fetchone()
            assert current is not None
            if current["role"] == "org_admin" and self._admins(cursor, oid) <= 1:
                raise OrganizationRuleError("last_org_admin")
            cursor.execute(
                "DELETE FROM organization_memberships WHERE organization_id = %s AND user_id = %s",
                (oid, uid),
            )
