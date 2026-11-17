"""Shared transaction handling for the identity and Project stores."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from .connection import Database, DatabaseError
from .outbox import safe_diagnostic


@contextmanager
def transaction(
    database: Database, conn: psycopg.Connection | None, what: str
) -> Generator[psycopg.Cursor[dict[str, Any]]]:
    """
    Yields a dictionary cursor inside one transaction.

    With `conn`, the work joins the caller's transaction (as a savepoint when one
    is already open), and deferred constraints, such as the one-Owner rule, are
    checked when the caller commits. An autocommit `conn` gets a transaction of
    its own. Without it, a pooled connection is borrowed and committed on exit.
    Driver errors become [`db.DatabaseError`] naming only the SQLSTATE and
    schema identifiers, never row values such as email addresses.
    """
    try:
        if conn is not None:
            if conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
                # Autocommit: a real transaction for this work. Already in a
                # transaction: a savepoint inside it.
                with conn.transaction(), conn.cursor(row_factory=dict_row) as cursor:
                    yield cursor
            else:
                # An idle connection outside autocommit: the first statement starts
                # the caller's transaction, which the caller commits or rolls back.
                # transaction() here would commit on exit instead.
                with conn.cursor(row_factory=dict_row) as cursor:
                    yield cursor
            return
        with database.connection() as owned, owned.cursor(row_factory=dict_row) as cursor:
            yield cursor
    except psycopg.Error as exc:
        raise DatabaseError(
            f"Cannot {what} in {database.describe()}: {safe_diagnostic(exc)}"
        ) from None


_AUDIT_SETTINGS = ("nest.audit_actor_user_id", "nest.audit_operator", "nest.audit_reason")


@contextmanager
def audit_context(
    cursor: psycopg.Cursor[Any],
    *,
    actor_user_id: UUID | None = None,
    operator: str | None = None,
    reason: str | None = None,
) -> Generator[None]:
    """
    Names who is making the platform changes run inside it, for the audit triggers
    of migration 0004. The settings are local to the transaction and cleared on
    exit, so they never describe a later change in the same transaction.
    """
    if operator is not None and not 1 <= len(operator.strip()) <= 200:
        raise ValueError("operator must be 1 to 200 characters")
    if reason is not None and not 1 <= len(reason.strip()) <= 1000:
        raise ValueError("reason must be 1 to 1000 characters")
    values = (
        str(actor_user_id) if actor_user_id else "",
        operator.strip() if operator else "",
        reason.strip() if reason else "",
    )
    cursor.execute(
        "SELECT " + ", ".join("set_config(%s, %s, true)" for _ in _AUDIT_SETTINGS),
        [item for pair in zip(_AUDIT_SETTINGS, values, strict=True) for item in pair],
    )
    try:
        yield
    finally:
        if (
            not cursor.closed
            and not cursor.connection.closed
            and (cursor.connection.info.transaction_status == TransactionStatus.INTRANS)
        ):
            cursor.execute(
                "SELECT " + ", ".join("set_config(%s, '', true)" for _ in _AUDIT_SETTINGS),
                list(_AUDIT_SETTINGS),
            )


def as_uuid(value: UUID | str, name: str) -> UUID:
    """Parses an ID, raising `ValueError` that names the argument, not the value."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        raise ValueError(f"{name} is not a valid UUID") from None
