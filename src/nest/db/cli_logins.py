"""
Pending browser-approved CLI sign-ins, following the OAuth 2.0 device
authorization grant (RFC 8628).

The CLI starts a request and receives a secret *device code* and a short *user
code*. The User approves the user code in a browser where they are signed in;
the CLI, polling with the device code, then receives a CLI session. Only SHA-256
digests of both codes are stored. A request is decided once and turned into a
session at most once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from .._logging import get_logger
from ._transaction import as_uuid, transaction
from .connection import Database
from .identity import IdentityStore, Session, UserNotActiveError
from .sessions import SessionStore

logger = get_logger("nest.db.cli_logins")

CLI_LOGIN_CAP_LOCK_CLASS = 0x4E53434C
"""First key of the `pg_advisory_xact_lock` that serializes the pending CLI sign-in
cap ("NSCL")."""

_COLUMNS = (
    "id, scope, client_name, status, user_id, decided_by_session_id, session_id, "
    "created_at, expires_at, decided_at"
)


class TooManyPendingCLILoginsError(Exception):
    """Too many CLI sign-ins are waiting for approval; try again later."""


class UserCodeCollisionError(Exception):
    """The user code is already in use by another request; draw another."""


@dataclass(frozen=True)
class CLILoginRequest:
    """
    A CLI sign-in request. Its codes are never stored or returned.

    Attributes:
        id (`UUID`): Request ID.
        scope (`str`): Space-separated scopes the CLI asked for.
        client_name (`str`): Label the CLI reported, such as its host name.
            Unauthenticated: show it, never trust it.
        status (`str`): `pending`, `approved`, `denied` or `consumed`.
        user_id (`UUID | None`): The User who decided it.
        decided_by_session_id (`UUID | None`): The browser session it was decided in.
        session_id (`UUID | None`): The CLI session issued for it.
        created_at, expires_at (`datetime`): Lifetime.
        decided_at (`datetime | None`): When it was approved or denied.
    """

    id: UUID
    scope: str
    client_name: str
    status: str
    user_id: UUID | None
    decided_by_session_id: UUID | None
    session_id: UUID | None
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True)
class CLIPoll:
    """
    The answer to one poll with a device code.

    Attributes:
        outcome (`str`): `issued`, or why not: `authorization_pending`,
            `slow_down`, `access_denied`, `expired_token` or `invalid_grant`
            (unknown or already used), the RFC 8628 error codes.
        session ([`db.Session`] or `None`): The new CLI session when `issued`.
        access_expires_at (`datetime` or `None`): Expiry of its access token.
    """

    outcome: str
    session: Session | None = None
    access_expires_at: datetime | None = None


def _request(row: dict[str, Any]) -> CLILoginRequest:
    return CLILoginRequest(**{key: row[key] for key in CLILoginRequest.__dataclass_fields__})


def _digest(value: bytes, name: str) -> bytes:
    if len(value) != 32:
        raise ValueError(f"{name} must be a SHA-256 digest")
    return value


class CLILoginStore:
    """
    Creates, decides and redeems CLI sign-in requests.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database
        self.identities = IdentityStore(database)
        self.sessions = SessionStore(database)

    def create(
        self,
        *,
        device_code_hash: bytes,
        user_code_hash: bytes,
        scope: str,
        client_name: str,
        ttl_seconds: float,
        max_pending: int,
    ) -> CLILoginRequest:
        """
        Stores a pending request, first deleting expired ones.

        Raises:
            [`db.TooManyPendingCLILoginsError`]: If `max_pending` unexpired requests
                already wait, which bounds what unauthenticated requests can store.
            [`db.UserCodeCollisionError`]: If the user code is in use.
        """
        _digest(device_code_hash, "device_code_hash")
        _digest(user_code_hash, "user_code_hash")
        if not ttl_seconds > 0:
            raise ValueError("ttl_seconds must be positive")
        with transaction(self.database, None, "start a CLI sign-in") as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s, 0)", (CLI_LOGIN_CAP_LOCK_CLASS,))
            cursor.execute("DELETE FROM cli_login_requests WHERE expires_at <= now()")
            row = cursor.execute(
                "SELECT count(*) AS pending FROM cli_login_requests WHERE status = 'pending'"
            ).fetchone()
            assert row is not None
            if row["pending"] >= max_pending:
                raise TooManyPendingCLILoginsError("Too many CLI sign-ins are pending")
            taken = cursor.execute(
                "SELECT 1 FROM cli_login_requests WHERE user_code_hash = %s", (user_code_hash,)
            ).fetchone()
            if taken is not None:
                raise UserCodeCollisionError("The user code is in use")
            created = cursor.execute(
                "INSERT INTO cli_login_requests (device_code_hash, user_code_hash, scope, "
                "client_name, expires_at) VALUES (%s, %s, %s, %s, "
                f"now() + make_interval(secs => %s)) RETURNING {_COLUMNS}",
                (device_code_hash, user_code_hash, scope, client_name, ttl_seconds),
            ).fetchone()
        assert created is not None
        request = _request(created)
        logger.info("CLI sign-in started request_id=%s scope=%s", request.id, request.scope)
        return request

    def find_pending(self, user_code_hash: bytes) -> CLILoginRequest | None:
        """Returns the unexpired pending request for a user code, or `None`."""
        if len(user_code_hash) != 32:
            return None
        with transaction(self.database, None, "look up a CLI sign-in") as cursor:
            row = cursor.execute(
                f"SELECT {_COLUMNS} FROM cli_login_requests WHERE user_code_hash = %s "
                "AND status = 'pending' AND expires_at > now()",
                (user_code_hash,),
            ).fetchone()
        return _request(row) if row else None

    def decide(
        self,
        request_id: UUID | str,
        *,
        approve: bool,
        user_id: UUID | str,
        session_id: UUID | str,
    ) -> CLILoginRequest | None:
        """
        Approves or denies a pending, unexpired request on behalf of a signed-in
        User. Returns the decided request, or `None` when it is no longer pending
        (already decided, expired or unknown), so a request is decided once.

        Raises:
            [`db.UserNotActiveError`]: If approving for a disabled User.
        """
        rid = as_uuid(request_id, "request_id")
        uid = as_uuid(user_id, "user_id")
        sid = as_uuid(session_id, "session_id")
        with transaction(self.database, None, "decide a CLI sign-in") as cursor:
            if approve:
                user = cursor.execute(
                    "SELECT status FROM users WHERE id = %s FOR SHARE", (uid,)
                ).fetchone()
                if user is None or user["status"] != "active":
                    raise UserNotActiveError("The User is disabled")
            row = cursor.execute(
                "UPDATE cli_login_requests SET status = %s, user_id = %s, "
                "decided_by_session_id = %s, decided_at = now() "
                "WHERE id = %s AND status = 'pending' AND expires_at > now() "
                f"RETURNING {_COLUMNS}",
                ("approved" if approve else "denied", uid, sid, rid),
            ).fetchone()
        if row is None:
            return None
        request = _request(row)
        logger.info(
            "CLI sign-in %s request_id=%s user_id=%s",
            request.status,
            request.id,
            request.user_id,
        )
        return request

    def redeem(
        self,
        device_code_hash: bytes,
        *,
        refresh_hash: bytes,
        access_hash: bytes,
        poll_interval: float,
        refresh_ttl: float,
        access_ttl: float,
        max_lifetime: float,
    ) -> CLIPoll:
        """
        Answers one poll with a device code. An approved request becomes a CLI
        session with the request's scope, and its access token, in one
        transaction; the request is then consumed, so a device code yields at most
        one session.

        Polls closer together than `poll_interval` seconds answer `slow_down`.
        """
        _digest(refresh_hash, "refresh_hash")
        _digest(access_hash, "access_hash")
        if len(device_code_hash) != 32:
            return CLIPoll("invalid_grant")
        with transaction(self.database, None, "redeem a CLI sign-in") as cursor:
            row = cursor.execute(
                f"SELECT {_COLUMNS}, expires_at <= now() AS expired, "
                "last_polled_at > now() - make_interval(secs => %s) AS too_soon "
                "FROM cli_login_requests WHERE device_code_hash = %s FOR UPDATE",
                (poll_interval, device_code_hash),
            ).fetchone()
            if row is None or row["status"] == "consumed":
                return CLIPoll("invalid_grant")
            if row["expired"]:
                return CLIPoll("expired_token")
            if row["status"] == "denied":
                return CLIPoll("access_denied")
            if row["status"] == "pending":
                cursor.execute(
                    "UPDATE cli_login_requests SET last_polled_at = now() WHERE id = %s",
                    (row["id"],),
                )
                return CLIPoll("slow_down" if row["too_soon"] else "authorization_pending")
            conn = cursor.connection
            now = cursor.execute("SELECT now() AS now").fetchone()
            assert now is not None
            try:
                session = self.identities.create_session(
                    row["user_id"],
                    kind="cli",
                    token_hash=refresh_hash,
                    expires_at=now["now"] + timedelta(seconds=refresh_ttl),
                    family_expires_at=now["now"] + timedelta(seconds=max_lifetime),
                    scope=row["scope"],
                    conn=conn,
                )
            except UserNotActiveError:
                logger.warning("CLI sign-in refused for a disabled User request_id=%s", row["id"])
                return CLIPoll("access_denied")
            access_expires_at = self.sessions.issue_access_token(
                session.id, token_hash=access_hash, ttl_seconds=access_ttl, conn=conn
            )
            cursor.execute(
                "UPDATE cli_login_requests SET status = 'consumed', session_id = %s, "
                "last_polled_at = now() WHERE id = %s",
                (session.id, row["id"]),
            )
        logger.info(
            "CLI sign-in completed request_id=%s session_id=%s user_id=%s",
            row["id"],
            session.id,
            session.user_id,
        )
        return CLIPoll("issued", session=session, access_expires_at=access_expires_at)
