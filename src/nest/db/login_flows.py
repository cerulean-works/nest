"""
Pending OAuth/OIDC sign-ins, stored between the redirect to the identity provider
and its callback.

A row binds the sign-in's OAuth `state` to the browser that started it. Both are
stored as SHA-256 digests only, and a row is deleted when it is consumed, so each
`state` can finish at most one sign-in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg

from .._logging import get_logger
from ._transaction import transaction
from .connection import Database

logger = get_logger("nest.db.login_flows")

PENDING_CAP_LOCK_CLASS = 0x4E534C46
"""First key of the per-provider `pg_advisory_xact_lock` that serializes the
pending-sign-in cap ("NSLF"). The two-key form never collides with the one-key
migration lock."""


class TooManyPendingSignInsError(Exception):
    """Too many sign-ins are waiting for their callback; try again later."""


@dataclass(frozen=True)
class LoginFlow:
    """
    A pending sign-in, as consumed by its callback.

    Attributes:
        binding_hash (`bytes`): SHA-256 of the browser's sign-in cookie secret.
        provider (`str`): Identity provider, such as `google`.
        created_at, expires_at (`datetime`): When it started and stops being usable.
        expired (`bool`): Whether it had expired when it was consumed.
    """

    binding_hash: bytes
    provider: str
    created_at: datetime
    expires_at: datetime
    expired: bool


class LoginFlowStore:
    """
    Creates and consumes pending sign-ins.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    def create(
        self,
        *,
        state_hash: bytes,
        binding_hash: bytes,
        provider: str,
        ttl_seconds: float,
        max_pending: int,
        conn: psycopg.Connection | None = None,
    ) -> None:
        """
        Stores a pending sign-in, first deleting expired ones.

        Raises:
            [`db.TooManyPendingSignInsError`]: If `max_pending` unexpired sign-ins
                already wait, which bounds what unauthenticated requests can store.
                The bound holds under concurrent starts: they are serialized per
                provider by a transaction-scoped advisory lock.
        """
        if len(state_hash) != 32 or len(binding_hash) != 32:
            raise ValueError("state_hash and binding_hash must be SHA-256 digests")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        with transaction(self.database, conn, "start a sign-in") as cursor:
            # Under READ COMMITTED, concurrent starts could all count below the cap
            # and each insert. This lock, held to the end of the transaction,
            # makes the count and insert atomic per provider.
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (PENDING_CAP_LOCK_CLASS, provider),
            )
            cursor.execute("DELETE FROM oidc_login_flows WHERE expires_at <= now()")
            row = cursor.execute(
                "SELECT count(*) AS pending FROM oidc_login_flows WHERE provider = %s",
                (provider,),
            ).fetchone()
            assert row is not None
            if row["pending"] >= max_pending:
                raise TooManyPendingSignInsError("Too many sign-ins are pending")
            cursor.execute(
                "INSERT INTO oidc_login_flows (state_hash, binding_hash, provider, expires_at) "
                "VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
                (state_hash, binding_hash, provider, ttl_seconds),
            )

    def consume(
        self, state_hash: bytes, *, conn: psycopg.Connection | None = None
    ) -> LoginFlow | None:
        """
        Deletes and returns the pending sign-in for a `state`, or `None`.

        The row is deleted even when it has expired, so a `state` never works twice;
        check [`db.LoginFlow.expired`] before using the result.
        """
        if len(state_hash) != 32:
            return None
        with transaction(self.database, conn, "finish a sign-in") as cursor:
            row = cursor.execute(
                "DELETE FROM oidc_login_flows WHERE state_hash = %s RETURNING binding_hash, "
                "provider, created_at, expires_at, expires_at <= now() AS expired",
                (state_hash,),
            ).fetchone()
        if row is None:
            return None
        return LoginFlow(
            binding_hash=bytes(row["binding_hash"]),
            provider=row["provider"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            expired=row["expired"],
        )
