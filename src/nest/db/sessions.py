"""
Browser session management: short-lived access tokens and rotating, revocable
refresh sessions.

A sign-in creates a refresh session (a `user_sessions` row) and an access token
tied to it. The access token is presented on every request and lives minutes; the
refresh secret is presented only to refresh, which replaces the session with a new
one and a new secret. Presenting a secret that was already rotated away revokes
every session of that sign-in, because only a copy of a stolen secret can be
replayed after its owner refreshed. Only SHA-256 digests of secrets are stored.
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
from .identity import _SESSION_COLUMNS, Session, lock_session_family

logger = get_logger("nest.db.sessions")

_S_COLUMNS = ", ".join("s." + column.strip() for column in _SESSION_COLUMNS.split(","))


class SessionRefreshError(Exception):
    """
    A refresh was refused.

    Attributes:
        reason (`str`): `unknown`, `expired`, `revoked`, `reused` or `user_disabled`.
    """

    def __init__(self, reason: str):
        super().__init__(f"The session cannot be refreshed: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class AccessGrant:
    """
    A session authenticated by its access token.

    Attributes:
        session ([`db.Session`]): The refresh session the token belongs to.
        access_expires_at (`datetime`): When the access token stops being accepted.
    """

    session: Session
    access_expires_at: datetime


@dataclass(frozen=True)
class Rotation:
    """
    The result of a refresh.

    Attributes:
        session ([`db.Session`]): The new refresh session.
        replaced ([`db.Session`]): The session it replaced, now revoked as `rotated`.
        access_expires_at (`datetime`): Expiry of the new access token.
    """

    session: Session
    replaced: Session
    access_expires_at: datetime


def _digest(value: bytes, name: str) -> bytes:
    if len(value) != 32:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


def _positive(value: float, name: str) -> float:
    if not value > 0:
        raise ValueError(f"{name} must be positive")
    return value


def _session(row: dict[str, Any]) -> Session:
    return Session(**{key: row[key] for key in Session.__dataclass_fields__})


class SessionStore:
    """
    Issues and checks access tokens, and rotates and revokes refresh sessions.

    Sessions are created by [`db.IdentityStore.create_session`] and revoked by
    [`db.IdentityStore.revoke_session`] and [`db.IdentityStore.revoke_user_sessions`].

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    def issue_access_token(
        self,
        session_id: UUID | str,
        *,
        token_hash: bytes,
        ttl_seconds: float,
        conn: psycopg.Connection | None = None,
    ) -> datetime:
        """
        Stores an access token for an active session and returns its expiry.

        The token never outlives its session. Expired access tokens are deleted.

        Raises:
            `LookupError`: If the session is missing, revoked or expired.
        """
        sid = as_uuid(session_id, "session_id")
        _digest(token_hash, "token_hash")
        _positive(ttl_seconds, "ttl_seconds")
        with transaction(self.database, conn, "issue an access token") as cursor:
            cursor.execute("DELETE FROM session_access_tokens WHERE expires_at <= now()")
            row = cursor.execute(
                "INSERT INTO session_access_tokens (token_hash, session_id, expires_at) "
                "SELECT %s, id, LEAST(now() + make_interval(secs => %s), expires_at) "
                "FROM user_sessions WHERE id = %s AND revoked_at IS NULL AND expires_at > now() "
                "RETURNING expires_at",
                (token_hash, ttl_seconds, sid),
            ).fetchone()
        if row is None:
            raise LookupError("The session is not active")
        return row["expires_at"]

    def authenticate(self, token_hash: bytes, *, kind: str | None = None) -> AccessGrant | None:
        """
        Returns the session for an access token's hash if the token is accepted
        now: unexpired, its session unrevoked and unexpired, and its User active.
        Otherwise `None`.

        Args:
            kind (`str`, *optional*): Accept only a session of this kind, `browser`
                or `cli`, so a CLI token never works as a browser session or the
                reverse.
        """
        if len(token_hash) != 32:
            return None
        with transaction(self.database, None, "check an access token") as cursor:
            row = cursor.execute(
                f"SELECT {_S_COLUMNS}, a.expires_at AS access_expires_at "
                "FROM session_access_tokens a "
                "JOIN user_sessions s ON s.id = a.session_id "
                "JOIN users u ON u.id = s.user_id "
                "WHERE a.token_hash = %s AND a.expires_at > now() AND s.revoked_at IS NULL "
                "AND s.expires_at > now() AND u.status = 'active' "
                "AND (%s::text IS NULL OR s.kind = %s::text)",
                (token_hash, kind, kind),
            ).fetchone()
        if row is None:
            return None
        return AccessGrant(session=_session(row), access_expires_at=row["access_expires_at"])

    def rotate(
        self,
        refresh_hash: bytes,
        *,
        new_refresh_hash: bytes,
        new_access_hash: bytes,
        refresh_ttl: float,
        access_ttl: float,
        kind: str | None = None,
        conn: psycopg.Connection | None = None,
    ) -> Rotation:
        """
        Replaces the refresh session for `refresh_hash` with a new one and issues
        its access token, in one transaction.

        The new session expires `refresh_ttl` seconds from now, but never after its
        family's end. The old session is revoked as `rotated` and its access tokens
        are deleted. Concurrent refreshes of one secret are serialized: one rotates,
        the others see a rotated secret.

        Args:
            kind (`str`, *optional*): Accept only a refresh secret of this kind of
                session; another kind's is refused as `unknown`, and nothing changes.
            conn (`psycopg.Connection`, *optional*): Transaction to join. The caller
                then commits, including the family revocation of a `reused` secret.

        Raises:
            [`db.SessionRefreshError`]: If the secret is unknown, expired or
                revoked, or its User is disabled. A secret that was already rotated
                (`reused`) also revokes every session of its family, and that
                revocation is committed.
        """
        _digest(refresh_hash, "refresh_hash")
        _digest(new_refresh_hash, "new_refresh_hash")
        _digest(new_access_hash, "new_access_hash")
        _positive(refresh_ttl, "refresh_ttl")
        _positive(access_ttl, "access_ttl")
        refused: tuple[str, Session | None] | None = None
        with transaction(self.database, conn, "refresh a session") as cursor:
            # The family and User never change, so they can be read before locking.
            owner = cursor.execute(
                "SELECT family_id, user_id FROM user_sessions WHERE token_hash = %s "
                "AND (%s::text IS NULL OR kind = %s::text)",
                (refresh_hash, kind, kind),
            ).fetchone()
            row = None
            if owner is not None:
                # Locks in the order every revocation uses: family, User, session.
                # The family lock serializes this rotation with revoke_session(); the
                # User lock (FOR SHARE) with revoke_user_sessions() and a disable.
                # Each waits, then reads the replacement this rotation committed.
                lock_session_family(cursor, owner["family_id"])
                user = cursor.execute(
                    "SELECT status FROM users WHERE id = %s FOR SHARE", (owner["user_id"],)
                ).fetchone()
                row = cursor.execute(
                    f"SELECT {_SESSION_COLUMNS}, expires_at <= now() AS expired "
                    "FROM user_sessions WHERE token_hash = %s FOR UPDATE",
                    (refresh_hash,),
                ).fetchone()
                if row is not None and user is not None:
                    row["status"] = user["status"]
            if row is None:
                refused = ("unknown", None)
            elif row["revoked_reason"] == "rotated":
                cursor.execute(
                    "WITH revoked AS (UPDATE user_sessions SET revoked_at = now(), "
                    "revoked_reason = 'refresh_reuse' WHERE family_id = %s "
                    "AND revoked_at IS NULL RETURNING id) "
                    "DELETE FROM session_access_tokens "
                    "WHERE session_id IN (SELECT id FROM revoked)",
                    (row["family_id"],),
                )
                refused = ("reused", _session(row))
            elif row["revoked_at"] is not None:
                refused = ("revoked", _session(row))
            elif row["expired"]:
                refused = ("expired", _session(row))
            elif row["status"] != "active":
                refused = ("user_disabled", _session(row))
            else:
                new = cursor.execute(
                    "INSERT INTO user_sessions (user_id, kind, token_hash, expires_at, "
                    "family_id, family_expires_at, scope) VALUES (%s, %s, %s, "
                    "LEAST(now() + make_interval(secs => %s), %s), %s, %s, %s) "
                    f"RETURNING {_SESSION_COLUMNS}",
                    (
                        row["user_id"],
                        row["kind"],
                        new_refresh_hash,
                        refresh_ttl,
                        row["family_expires_at"],
                        row["family_id"],
                        row["family_expires_at"],
                        row["scope"],
                    ),
                ).fetchone()
                assert new is not None
                old = cursor.execute(
                    "UPDATE user_sessions SET revoked_at = now(), revoked_reason = 'rotated', "
                    "replaced_by_session_id = %s, last_used_at = now() WHERE id = %s "
                    f"RETURNING {_SESSION_COLUMNS}",
                    (new["id"], row["id"]),
                ).fetchone()
                assert old is not None
                cursor.execute(
                    "DELETE FROM session_access_tokens WHERE session_id = %s", (row["id"],)
                )
                access = cursor.execute(
                    "INSERT INTO session_access_tokens (token_hash, session_id, expires_at) "
                    "VALUES (%s, %s, LEAST(now() + make_interval(secs => %s), %s)) "
                    "RETURNING expires_at",
                    (new_access_hash, new["id"], access_ttl, new["expires_at"]),
                ).fetchone()
                assert access is not None
        if refused is not None:
            reason, session = refused
            if reason == "reused":
                assert session is not None
                logger.warning(
                    "Refresh secret reused; session family revoked family_id=%s user_id=%s",
                    session.family_id,
                    session.user_id,
                )
            else:
                logger.info(
                    "Session refresh refused reason=%s session_id=%s",
                    reason,
                    session.id if session else "-",
                )
            raise SessionRefreshError(reason)
        rotation = Rotation(
            session=Session(**new), replaced=Session(**old), access_expires_at=access["expires_at"]
        )
        logger.info(
            "Session refreshed session_id=%s replaced_session_id=%s user_id=%s",
            rotation.session.id,
            rotation.replaced.id,
            rotation.session.user_id,
        )
        return rotation

    def find_by_refresh(self, refresh_hash: bytes, *, kind: str | None = None) -> Session | None:
        """
        Returns the session for a refresh secret's hash, whatever its state, or
        only of `kind` when given.
        """
        if len(refresh_hash) != 32:
            return None
        with transaction(self.database, None, "look up a session") as cursor:
            row = cursor.execute(
                f"SELECT {_SESSION_COLUMNS} FROM user_sessions WHERE token_hash = %s "
                "AND (%s::text IS NULL OR kind = %s::text)",
                (refresh_hash, kind, kind),
            ).fetchone()
        return Session(**row) if row else None

    def active_sessions(self, user_id: UUID | str) -> list[Session]:
        """Returns a User's unrevoked, unexpired sessions, newest first."""
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, None, "list sessions") as cursor:
            rows = cursor.execute(
                f"SELECT {_SESSION_COLUMNS} FROM user_sessions WHERE user_id = %s "
                "AND revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC",
                (uid,),
            ).fetchall()
        return [Session(**row) for row in rows]
