"""
Nest Users, their external sign-in identities, sessions, and service-wide and
Organization roles.

A User is identified by the identity provider's stable subject ID under its
issuer (`user_identities`), never by an email address. Sessions are stored by a
hash of their secret only. Project roles live in [`db.ProjectStore`].
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from .._logging import get_logger
from ._transaction import as_uuid, audit_context, transaction
from .connection import Database

logger = get_logger("nest.db.identity")

SESSION_KINDS = ("browser", "cli")
PLATFORM_ROLES = ("platform_admin",)
ORGANIZATION_ROLES = ("org_admin", "member")

_USER_COLUMNS = "id, display_name, email, status, created_at, updated_at, disabled_at"
_IDENTITY_COLUMNS = "id, user_id, issuer, subject, email, email_verified, created_at, last_login_at"
_SESSION_COLUMNS = (
    "id, user_id, kind, created_at, expires_at, last_used_at, revoked_at, revoked_reason, "
    "family_id, family_expires_at, replaced_by_session_id"
)


class IdentityConflictError(Exception):
    """The external identity is already linked to a different User."""


class UserNotActiveError(Exception):
    """The User is disabled, so no new session can be created for them."""


SESSION_FAMILY_LOCK_CLASS = 0x4E53534E
"""First key of the per-family `pg_advisory_xact_lock` that serializes a refresh
with the revocation of its family ("NSSN")."""


def lock_session_family(cursor: psycopg.Cursor[Any], family_id: UUID) -> None:
    """
    Takes the transaction-scoped lock of a session family.

    Rotation and family revocation both hold it, so a revocation that waits for a
    rotation sees the session that rotation inserted. Take it before any User or
    session row lock, the order every caller uses.
    """
    cursor.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
        (SESSION_FAMILY_LOCK_CLASS, str(family_id)),
    )


def normalize_email(email: str) -> str:
    """Returns an address trimmed and lowercased, the form addresses are compared in."""
    return email.strip().lower()


def hash_token(token: str) -> bytes:
    """Returns the SHA-256 digest stored for a session or invitation secret."""
    return hashlib.sha256(token.encode()).digest()


@dataclass(frozen=True)
class User:
    """
    A Nest User.

    Attributes:
        id (`UUID`): Generated, immutable User ID.
        display_name (`str | None`): Name for display.
        email (`str | None`): Last verified address seen. Informational, not a key.
        status (`str`): `active` or `disabled`.
        created_at, updated_at (`datetime`): Row timestamps.
        disabled_at (`datetime | None`): When the User was disabled.
    """

    id: UUID
    display_name: str | None
    email: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None


@dataclass(frozen=True)
class Identity:
    """
    An external sign-in identity linked to a User.

    Attributes:
        id (`int`): Row ID.
        user_id (`UUID`): The linked User.
        issuer (`str`): OIDC issuer, such as `https://accounts.google.com`.
        subject (`str`): The issuer's stable subject ID (`sub`).
        email (`str | None`): Email claim at the last sign-in.
        email_verified (`bool`): Whether that claim was verified.
        created_at (`datetime`): When the identity was linked.
        last_login_at (`datetime | None`): Last sign-in recorded.
    """

    id: int
    user_id: UUID
    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    created_at: datetime
    last_login_at: datetime | None


@dataclass(frozen=True)
class Session:
    """
    A signed-in (refresh) session. Its secret is never stored or returned.

    Each refresh replaces a session with a new one in the same family (see
    [`db.SessionStore.rotate`]).

    Attributes:
        id (`UUID`): Session ID.
        user_id (`UUID`): The signed-in User.
        kind (`str`): `browser` or `cli`.
        created_at, expires_at (`datetime`): Lifetime.
        last_used_at (`datetime | None`): Last recorded use.
        revoked_at (`datetime | None`): When it was revoked.
        revoked_reason (`str | None`): Such as `logout`, `rotated`, `refresh_reuse`
            or `admin_revoked`.
        family_id (`UUID`): The sign-in this session descends from by rotation: the
            ID of its first session.
        family_expires_at (`datetime`): When the family ends, however often it is
            refreshed.
        replaced_by_session_id (`UUID | None`): The session that replaced this one
            when it was rotated.
    """

    id: UUID
    user_id: UUID
    kind: str
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    revoked_reason: str | None
    family_id: UUID
    family_expires_at: datetime
    replaced_by_session_id: UUID | None


class IdentityStore:
    """
    Reads and writes Users, identities, sessions, and platform and Organization roles.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    # Users and identities ------------------------------------------------------

    def create_user(
        self,
        *,
        display_name: str | None = None,
        email: str | None = None,
        conn: psycopg.Connection | None = None,
    ) -> User:
        """Creates a User with a generated ID."""
        with transaction(self.database, conn, "create a User") as cursor:
            row = cursor.execute(
                f"INSERT INTO users (display_name, email) VALUES (%s, %s) RETURNING {_USER_COLUMNS}",
                (display_name, email),
            ).fetchone()
        assert row is not None
        logger.info("User created user_id=%s", row["id"])
        return User(**row)

    def get_user(self, user_id: UUID | str) -> User | None:
        """Returns a User, or `None`."""
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, None, "read a User") as cursor:
            row = cursor.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s", (uid,)
            ).fetchone()
        return User(**row) if row else None

    def link_identity(
        self,
        user_id: UUID | str,
        *,
        issuer: str,
        subject: str,
        email: str | None = None,
        email_verified: bool = False,
        conn: psycopg.Connection | None = None,
    ) -> Identity:
        """
        Links an external identity to a User, or records a sign-in for it.

        Linking the same (issuer, subject) to the same User again only refreshes its
        email claims and `last_login_at`, so it is safe to call on every sign-in.
        Concurrent calls for a new identity link it once.

        Raises:
            [`db.IdentityConflictError`]: If the identity belongs to another User.
        """
        uid = as_uuid(user_id, "user_id")
        if not issuer or not subject:
            raise ValueError("issuer and subject are required")
        with transaction(self.database, conn, "link an identity") as cursor:
            row = cursor.execute(
                "INSERT INTO user_identities (user_id, issuer, subject, email, email_verified, "
                "last_login_at) VALUES (%s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (issuer, subject) DO UPDATE SET email = EXCLUDED.email, "
                "email_verified = EXCLUDED.email_verified, last_login_at = now() "
                "WHERE user_identities.user_id = EXCLUDED.user_id "
                f"RETURNING {_IDENTITY_COLUMNS}",
                (uid, issuer, subject, email, email_verified),
            ).fetchone()
            if row is None:
                raise IdentityConflictError("The identity is already linked to another User")
        return Identity(**row)

    def sign_in(
        self,
        *,
        issuer: str,
        subject: str,
        email: str | None = None,
        email_verified: bool = False,
        display_name: str | None = None,
        conn: psycopg.Connection | None = None,
    ) -> tuple[User, Identity, bool]:
        """
        Returns the User for an external identity, creating both on first sign-in.

        This is the sign-in path: it is atomic, so concurrent first sign-ins with
        the same (issuer, subject) create exactly one User and leave no orphan.
        For an existing identity it refreshes the email claims and
        `last_login_at`, as [`db.IdentityStore.link_identity`] does. It does not
        check the User's status; [`db.IdentityStore.create_session`] refuses a
        disabled User.

        Args:
            issuer (`str`): OIDC issuer.
            subject (`str`): The issuer's stable subject ID.
            email (`str`, *optional*): Email claim.
            email_verified (`bool`, *optional*): Whether the claim is verified.
            display_name (`str`, *optional*): Name for a newly created User.
            conn (`psycopg.Connection`, *optional*): Transaction to join.

        Returns:
            `tuple[User, Identity, bool]`: The User, the identity, and whether the
            User was created by this call.
        """
        if not issuer or not subject:
            raise ValueError("issuer and subject are required")
        with transaction(self.database, conn, "sign in") as cursor:
            identity = cursor.execute(
                "UPDATE user_identities SET email = %s, email_verified = %s, "
                "last_login_at = now() WHERE issuer = %s AND subject = %s "
                f"RETURNING {_IDENTITY_COLUMNS}",
                (email, email_verified, issuer, subject),
            ).fetchone()
            created = False
            if identity is None:
                # Create the User and identity inside a savepoint. A concurrent
                # first sign-in that wins the unique (issuer, subject) index makes
                # this insert do nothing; the savepoint is then rolled back so no
                # orphan User remains, and the winner's identity is used.
                with cursor.connection.transaction() as savepoint:
                    user = cursor.execute(
                        "INSERT INTO users (display_name, email) VALUES (%s, %s) RETURNING id",
                        (display_name, email),
                    ).fetchone()
                    assert user is not None
                    identity = cursor.execute(
                        "INSERT INTO user_identities (user_id, issuer, subject, email, "
                        "email_verified, last_login_at) VALUES (%s, %s, %s, %s, %s, now()) "
                        f"ON CONFLICT (issuer, subject) DO NOTHING RETURNING {_IDENTITY_COLUMNS}",
                        (user["id"], issuer, subject, email, email_verified),
                    ).fetchone()
                    if identity is None:
                        raise psycopg.Rollback(savepoint)
                    created = True
                if identity is None:
                    identity = cursor.execute(
                        "UPDATE user_identities SET email = %s, email_verified = %s, "
                        "last_login_at = now() WHERE issuer = %s AND subject = %s "
                        f"RETURNING {_IDENTITY_COLUMNS}",
                        (email, email_verified, issuer, subject),
                    ).fetchone()
                    assert identity is not None
            row = cursor.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s", (identity["user_id"],)
            ).fetchone()
        assert row is not None
        if created:
            logger.info("User created at first sign-in user_id=%s", row["id"])
        return User(**row), Identity(**identity), created

    def find_user(self, *, issuer: str, subject: str) -> User | None:
        """Returns the User linked to an external identity, or `None`."""
        with transaction(self.database, None, "find a User by identity") as cursor:
            row = cursor.execute(
                f"SELECT {', '.join('u.' + c.strip() for c in _USER_COLUMNS.split(','))} "
                "FROM users u JOIN user_identities i ON i.user_id = u.id "
                "WHERE i.issuer = %s AND i.subject = %s",
                (issuer, subject),
            ).fetchone()
        return User(**row) if row else None

    def identities(self, user_id: UUID | str) -> list[Identity]:
        """Returns a User's linked identities, oldest first."""
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, None, "list identities") as cursor:
            rows = cursor.execute(
                f"SELECT {_IDENTITY_COLUMNS} FROM user_identities WHERE user_id = %s ORDER BY id",
                (uid,),
            ).fetchall()
        return [Identity(**row) for row in rows]

    def set_user_status(
        self, user_id: UUID | str, status: str, *, conn: psycopg.Connection | None = None
    ) -> User:
        """
        Enables or disables a User. Disabling also revokes all their sessions.

        Raises:
            `LookupError`: If the User does not exist.
        """
        uid = as_uuid(user_id, "user_id")
        if status not in ("active", "disabled"):
            raise ValueError("status must be 'active' or 'disabled'")
        with transaction(self.database, conn, "change a User's status") as cursor:
            row = cursor.execute(
                "UPDATE users SET status = %s, updated_at = now(), disabled_at = CASE "
                "WHEN %s::text = 'disabled' THEN COALESCE(disabled_at, now()) END "
                f"WHERE id = %s RETURNING {_USER_COLUMNS}",
                (status, status, uid),
            ).fetchone()
            if row is None:
                raise LookupError("The User does not exist")
            if status == "disabled":
                cursor.execute(
                    "UPDATE user_sessions SET revoked_at = now(), revoked_reason = 'user_disabled' "
                    "WHERE user_id = %s AND revoked_at IS NULL",
                    (uid,),
                )
                cursor.execute(
                    "DELETE FROM session_access_tokens WHERE session_id IN "
                    "(SELECT id FROM user_sessions WHERE user_id = %s)",
                    (uid,),
                )
        logger.info("User status changed user_id=%s status=%s", uid, status)
        return User(**row)

    # Sessions ------------------------------------------------------------------

    def create_session(
        self,
        user_id: UUID | str,
        *,
        kind: str,
        token_hash: bytes,
        expires_at: datetime,
        family_expires_at: datetime | None = None,
        conn: psycopg.Connection | None = None,
    ) -> Session:
        """
        Stores a new session for a User. It starts a new session family.

        Args:
            user_id (`UUID`): The signed-in User.
            kind (`str`): `browser` or `cli`.
            token_hash (`bytes`): [`db.hash_token`] of the session secret.
            expires_at (`datetime`): Timezone-aware expiry.
            family_expires_at (`datetime`, *optional*): When the family ends however
                often it is refreshed. Defaults to `expires_at`, so the session cannot
                be extended.
            conn (`psycopg.Connection`, *optional*): Transaction to join.

        Raises:
            [`db.UserNotActiveError`]: If the User is disabled.
            `LookupError`: If the User does not exist.
        """
        uid = as_uuid(user_id, "user_id")
        if kind not in SESSION_KINDS:
            raise ValueError(f"kind must be one of {SESSION_KINDS}")
        if len(token_hash) != 32:
            raise ValueError("token_hash must be a SHA-256 digest")
        if expires_at.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        if family_expires_at is None:
            family_expires_at = expires_at
        if family_expires_at.tzinfo is None:
            raise ValueError("family_expires_at must be timezone-aware")
        if family_expires_at < expires_at:
            raise ValueError("family_expires_at must not be before expires_at")
        with transaction(self.database, conn, "create a session") as cursor:
            # FOR SHARE conflicts with set_user_status()'s UPDATE, so a disable
            # either commits first and is seen here, or waits for this session to
            # commit and then revokes it.
            user = cursor.execute(
                "SELECT status FROM users WHERE id = %s FOR SHARE", (uid,)
            ).fetchone()
            if user is None:
                raise LookupError("The User does not exist")
            if user["status"] != "active":
                raise UserNotActiveError("The User is disabled")
            row = cursor.execute(
                "INSERT INTO user_sessions "
                "(user_id, kind, token_hash, expires_at, family_expires_at) "
                f"VALUES (%s, %s, %s, %s, %s) RETURNING {_SESSION_COLUMNS}",
                (uid, kind, token_hash, expires_at, family_expires_at),
            ).fetchone()
        assert row is not None
        logger.info("Session created session_id=%s user_id=%s kind=%s", row["id"], uid, kind)
        return Session(**row)

    def active_session(self, token_hash: bytes) -> Session | None:
        """
        Returns the session for a secret's hash if it is usable now: not revoked,
        not expired, and its User is active. Otherwise `None`.
        """
        with transaction(self.database, None, "look up a session") as cursor:
            row = cursor.execute(
                f"SELECT {', '.join('s.' + c.strip() for c in _SESSION_COLUMNS.split(','))} "
                "FROM user_sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now() "
                "AND u.status = 'active'",
                (token_hash,),
            ).fetchone()
        return Session(**row) if row else None

    def revoke_session(
        self,
        session_id: UUID | str,
        *,
        reason: str = "logout",
        conn: psycopg.Connection | None = None,
    ) -> bool:
        """
        Revokes a session and every session rotated from the same sign-in, and
        deletes their access tokens. Returns whether one was active until now.

        Any session ID of the family works, including a rotated one.
        """
        sid = as_uuid(session_id, "session_id")
        with transaction(self.database, conn, "revoke a session") as cursor:
            family = cursor.execute(
                "SELECT family_id FROM user_sessions WHERE id = %s", (sid,)
            ).fetchone()
            rows = []
            if family is not None:
                lock_session_family(cursor, family["family_id"])
                # A new statement after the lock: under READ COMMITTED it sees the
                # replacement of any rotation that held the lock first.
                rows = cursor.execute(
                    "UPDATE user_sessions SET revoked_at = now(), revoked_reason = %s "
                    "WHERE family_id = %s AND revoked_at IS NULL RETURNING id",
                    (reason, family["family_id"]),
                ).fetchall()
            revoked = bool(rows)
            if revoked:
                cursor.execute(
                    "DELETE FROM session_access_tokens WHERE session_id = ANY(%s)",
                    ([row["id"] for row in rows],),
                )
        if revoked:
            logger.info("Session revoked session_id=%s reason=%s", sid, reason)
        return revoked

    def revoke_user_sessions(
        self,
        user_id: UUID | str,
        *,
        reason: str = "admin_revoked",
        conn: psycopg.Connection | None = None,
    ) -> int:
        """Revokes every active session of a User. Returns how many were revoked."""
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, conn, "revoke sessions") as cursor:
            # Conflicts with a rotation's FOR SHARE, so this waits for one in progress;
            # the UPDATE below is a new statement and sees its replacement session.
            cursor.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (uid,))
            cursor.execute(
                "UPDATE user_sessions SET revoked_at = now(), revoked_reason = %s "
                "WHERE user_id = %s AND revoked_at IS NULL",
                (reason, uid),
            )
            count = cursor.rowcount
            cursor.execute(
                "DELETE FROM session_access_tokens WHERE session_id IN "
                "(SELECT id FROM user_sessions WHERE user_id = %s)",
                (uid,),
            )
        logger.info("Sessions revoked user_id=%s count=%s reason=%s", uid, count, reason)
        return count

    # Platform and Organization roles -------------------------------------------

    def grant_platform_role(
        self,
        user_id: UUID | str,
        role: str = "platform_admin",
        *,
        granted_by_user_id: UUID | str | None = None,
        operator: str | None = None,
        reason: str | None = None,
        conn: psycopg.Connection | None = None,
    ) -> bool:
        """
        Grants a platform role. Returns `False` if the User already had it.

        Every grant is recorded in the platform audit log, and the first
        `platform_admin` granted this way closes the one-time bootstrap (see
        [`db.PlatformAdminBootstrap`]).

        Args:
            user_id (`UUID`): The User to grant the role to.
            role (`str`, *optional*): Defaults to `platform_admin`.
            granted_by_user_id (`UUID`, *optional*): The administrator granting it.
            operator (`str`, *optional*): Operator name, for an operator recovery.
            reason (`str`, *optional*): Why, for the audit log.
            conn (`psycopg.Connection`, *optional*): Transaction to join.

        Raises:
            `LookupError`: If the User does not exist.
        """
        uid = as_uuid(user_id, "user_id")
        by = as_uuid(granted_by_user_id, "granted_by_user_id") if granted_by_user_id else None
        if role not in PLATFORM_ROLES:
            raise ValueError(f"role must be one of {PLATFORM_ROLES}")
        with transaction(self.database, conn, "grant a platform role") as cursor:
            if cursor.execute("SELECT 1 FROM users WHERE id = %s", (uid,)).fetchone() is None:
                raise LookupError("The User does not exist")
            with audit_context(cursor, actor_user_id=by, operator=operator, reason=reason):
                cursor.execute(
                    "INSERT INTO platform_role_assignments (user_id, role, granted_by_user_id) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (uid, role, by),
                )
                granted = cursor.rowcount == 1
        if granted:
            logger.info("Platform role granted user_id=%s role=%s", uid, role)
        return granted

    def revoke_platform_role(
        self,
        user_id: UUID | str,
        role: str = "platform_admin",
        *,
        revoked_by_user_id: UUID | str | None = None,
        operator: str | None = None,
        reason: str | None = None,
        conn: psycopg.Connection | None = None,
    ) -> bool:
        """
        Revokes a platform role. Returns whether the User had it.

        Every revocation is recorded in the platform audit log. Revoking never
        reopens the one-time bootstrap.

        Args:
            user_id (`UUID`): The User to revoke the role from.
            role (`str`, *optional*): Defaults to `platform_admin`.
            revoked_by_user_id (`UUID`, *optional*): The administrator revoking it.
            operator (`str`, *optional*): Operator name, for an operator recovery.
            reason (`str`, *optional*): Why, for the audit log.
            conn (`psycopg.Connection`, *optional*): Transaction to join.
        """
        uid = as_uuid(user_id, "user_id")
        by = as_uuid(revoked_by_user_id, "revoked_by_user_id") if revoked_by_user_id else None
        with (
            transaction(self.database, conn, "revoke a platform role") as cursor,
            audit_context(cursor, actor_user_id=by, operator=operator, reason=reason),
        ):
            cursor.execute(
                "DELETE FROM platform_role_assignments WHERE user_id = %s AND role = %s",
                (uid, role),
            )
            revoked = cursor.rowcount == 1
        if revoked:
            logger.info("Platform role revoked user_id=%s role=%s", uid, role)
        return revoked

    def platform_admins(self) -> list[tuple[UUID, datetime]]:
        """Returns the current `platform_admin` Users and when each was granted, oldest first."""
        with transaction(self.database, None, "list platform administrators") as cursor:
            rows = cursor.execute(
                "SELECT user_id, granted_at FROM platform_role_assignments "
                "WHERE role = 'platform_admin' ORDER BY granted_at, user_id"
            ).fetchall()
        return [(row["user_id"], row["granted_at"]) for row in rows]

    def find_users_by_email(self, email: str) -> list[tuple[User, Identity]]:
        """
        Returns the Users whose sign-in identities last reported this address, with
        those identities. Matching ignores case. For operators locating a User;
        an address never identifies a User by itself.
        """
        normalized = normalize_email(email)
        with transaction(self.database, None, "find Users by email") as cursor:
            rows = cursor.execute(
                f"SELECT {_IDENTITY_COLUMNS} FROM user_identities "
                "WHERE lower(email) = %s ORDER BY user_id, id",
                (normalized,),
            ).fetchall()
            users = {
                row["id"]: User(**row)
                for row in cursor.execute(
                    f"SELECT {_USER_COLUMNS} FROM users WHERE id = ANY(%s)",
                    ([row["user_id"] for row in rows],),
                ).fetchall()
            }
        return [(users[row["user_id"]], Identity(**row)) for row in rows]

    def platform_roles(self, user_id: UUID | str) -> set[str]:
        """Returns a User's platform roles."""
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, None, "read platform roles") as cursor:
            rows = cursor.execute(
                "SELECT role FROM platform_role_assignments WHERE user_id = %s", (uid,)
            ).fetchall()
        return {row["role"] for row in rows}

    def create_organization(
        self,
        name: str,
        *,
        created_by_user_id: UUID | str,
        conn: psycopg.Connection | None = None,
    ) -> UUID:
        """Creates an Organization with its creator as `org_admin`. Returns its ID."""
        creator = as_uuid(created_by_user_id, "created_by_user_id")
        with transaction(self.database, conn, "create an Organization") as cursor:
            row = cursor.execute(
                "INSERT INTO organizations (name, created_by_user_id) VALUES (%s, %s) RETURNING id",
                (name, creator),
            ).fetchone()
            assert row is not None
            cursor.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role) "
                "VALUES (%s, %s, 'org_admin')",
                (row["id"], creator),
            )
        logger.info("Organization created organization_id=%s", row["id"])
        return row["id"]

    def set_organization_role(
        self,
        organization_id: UUID | str,
        user_id: UUID | str,
        role: str,
        *,
        conn: psycopg.Connection | None = None,
    ) -> None:
        """Adds a User to an Organization, or changes their role in it."""
        oid = as_uuid(organization_id, "organization_id")
        uid = as_uuid(user_id, "user_id")
        if role not in ORGANIZATION_ROLES:
            raise ValueError(f"role must be one of {ORGANIZATION_ROLES}")
        with transaction(self.database, conn, "set an Organization role") as cursor:
            cursor.execute(
                "INSERT INTO organization_memberships (organization_id, user_id, role) "
                "VALUES (%s, %s, %s) ON CONFLICT (organization_id, user_id) "
                "DO UPDATE SET role = EXCLUDED.role, updated_at = now()",
                (oid, uid, role),
            )
        logger.info("Organization role set organization_id=%s user_id=%s role=%s", oid, uid, role)

    def remove_organization_member(
        self,
        organization_id: UUID | str,
        user_id: UUID | str,
        *,
        conn: psycopg.Connection | None = None,
    ) -> bool:
        """Removes a User from an Organization. Returns whether they were a member."""
        oid = as_uuid(organization_id, "organization_id")
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, conn, "remove an Organization member") as cursor:
            cursor.execute(
                "DELETE FROM organization_memberships WHERE organization_id = %s AND user_id = %s",
                (oid, uid),
            )
            return cursor.rowcount == 1

    def organization_role(self, organization_id: UUID | str, user_id: UUID | str) -> str | None:
        """Returns a User's role in an Organization, or `None`."""
        oid = as_uuid(organization_id, "organization_id")
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, None, "read an Organization role") as cursor:
            row = cursor.execute(
                "SELECT role FROM organization_memberships "
                "WHERE organization_id = %s AND user_id = %s",
                (oid, uid),
            ).fetchone()
        return row["role"] if row else None
