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


def as_uuid(value: UUID | str, name: str) -> UUID:
    """Parses an ID, raising `ValueError` that names the argument, not the value."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        raise ValueError(f"{name} is not a valid UUID") from None
