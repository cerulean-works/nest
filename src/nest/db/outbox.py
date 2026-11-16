"""Persistent email outbox: queued messages and their delivery status."""

from __future__ import annotations

import hashlib
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from email.headerregistry import Address
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .._logging import get_logger
from .connection import Database, DatabaseError

logger = get_logger("nest.db.outbox")

DELIVERY_STATUSES = ("pending", "sending", "sent", "failed")
DEFAULT_LEASE_SECONDS = 300.0

_STATUS_COLUMNS = (
    "id, message_key, kind, subject_ref, status, attempts, max_attempts, next_attempt_at, "
    "last_error, last_error_kind, created_at, updated_at, sent_at"
)


@dataclass(frozen=True)
class DeliveryStatus:
    """
    Delivery state of one queued message, safe to show to the record's owner.

    It never carries the recipient, subject or body, so it can be returned to a
    Project administrator without disclosing anything beyond the delivery outcome.

    Attributes:
        id (`int`): Outbox row ID.
        message_key (`str`): Idempotency key given to [`db.EmailOutbox.enqueue`].
        kind (`str`): Template name.
        subject_ref (`str | None`): Reference to the record the message is about.
        status (`str`): `pending`, `sending`, `sent` or `failed`.
        attempts (`int`): Delivery attempts made so far.
        max_attempts (`int`): Attempts allowed before the message is `failed`.
        next_attempt_at (`datetime`): Earliest time of the next attempt.
        last_error (`str | None`): Last failure as an error class and SMTP code.
        last_error_kind (`str | None`): `transient` or `permanent`.
        created_at, updated_at (`datetime`): Row timestamps.
        sent_at (`datetime | None`): When the SMTP server accepted the message.
    """

    id: int
    message_key: str
    kind: str
    subject_ref: str | None
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime
    last_error: str | None
    last_error_kind: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None

    @property
    def retriable(self) -> bool:
        """Whether [`db.EmailOutbox.retry`] can queue this message again."""
        return self.status in ("pending", "failed")


@dataclass(frozen=True)
class QueuedEmail:
    """
    A message claimed for one delivery attempt. Only the delivery worker sees it.

    Attributes:
        id (`int`): Outbox row ID.
        kind (`str`): Template name, for logs.
        recipient (`str`): Envelope and `To` address. Never log it.
        subject (`str`): Subject line.
        body_text (`str`): Plain-text body. Can hold an invitation link; never log it.
        message_id (`str`): Stable `Message-ID` header shared by every attempt.
        attempts (`int`): This attempt's number, starting at 1.
        max_attempts (`int`): Attempts allowed.
    """

    id: int
    kind: str
    recipient: str
    subject: str
    body_text: str
    message_id: str
    attempts: int
    max_attempts: int

    def __repr__(self) -> str:
        return f"QueuedEmail(id={self.id}, kind={self.kind!r}, attempts={self.attempts})"


def normalize_recipient(address: str) -> str:
    """
    Validates one recipient address and returns it with a lowercase domain.

    Raises:
        `ValueError`: If the value is not exactly one address. The message never
            repeats the value.
    """
    value = address.strip()
    try:
        if "\r" in value or "\n" in value or "," in value:
            raise ValueError
        parsed = Address(addr_spec=value)
        if not parsed.username or "." not in parsed.domain:
            raise ValueError
    except ValueError:
        raise ValueError("The recipient is not a single valid email address") from None
    return f"{parsed.username}@{parsed.domain.lower()}"


def message_id_for(message_key: str, domain: str) -> str:
    """Returns the stable `Message-ID` for an idempotency key."""
    digest = hashlib.sha256(message_key.encode()).hexdigest()[:32]
    return f"<nest.{digest}@{domain}>"


class EmailOutbox:
    """
    Queues messages in `email_outbox` and records each delivery attempt.

    Delivery is at least once with bounded retries: a message is attempted at most
    `max_attempts` times, waiting `retry_delay` seconds, doubled per attempt up to
    `retry_max_delay`, between transient failures. A permanent failure (an SMTP
    `5xx` rejection of the sender, recipient or content) stops retrying at once.
    Only this table is ever updated, so no delivery outcome can change an
    invitation or a membership.

    Args:
        database ([`db.Database`]): Open database.
        message_id_domain (`str`): Domain for generated `Message-ID` headers;
            normally the sender's domain.
        max_attempts (`int`, *optional*): Default attempt limit for new messages.
        retry_delay (`float`, *optional*): Seconds before the first retry.
        retry_max_delay (`float`, *optional*): Longest wait between attempts.
        lease (`float`, *optional*): Seconds a claimed message stays reserved for
            one worker. A claim older than this is assumed interrupted.
    """

    def __init__(
        self,
        database: Database,
        message_id_domain: str,
        *,
        max_attempts: int = 5,
        retry_delay: float = 60.0,
        retry_max_delay: float = 3600.0,
        lease: float = DEFAULT_LEASE_SECONDS,
    ):
        self.database = database
        self.message_id_domain = message_id_domain
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.retry_max_delay = retry_max_delay
        self.lease = lease

    @contextmanager
    def _cursor(
        self, conn: psycopg.Connection | None = None
    ) -> Generator[psycopg.Cursor[dict[str, Any]]]:
        try:
            if conn is not None:
                with conn.cursor(row_factory=dict_row) as cursor:
                    yield cursor
                return
            with (
                self.database.connection() as owned,
                owned.cursor(row_factory=dict_row) as cursor,
            ):
                yield cursor
        except psycopg.Error as exc:
            raise DatabaseError(
                f"Email outbox operation failed in {self.database.describe()}: "
                + self.database.scrub(str(exc))
            ) from None

    def retry_wait(self, attempts: int) -> float:
        """Seconds to wait after the given number of failed attempts."""
        return min(self.retry_max_delay, self.retry_delay * 2 ** max(0, attempts - 1))

    def enqueue(
        self,
        *,
        message_key: str,
        kind: str,
        recipient: str,
        subject: str,
        body_text: str,
        subject_ref: str | None = None,
        max_attempts: int | None = None,
        conn: psycopg.Connection | None = None,
    ) -> DeliveryStatus:
        """
        Queues a message once per `message_key`.

        Pass the caller's `conn` to queue the message in the same transaction as the
        record it is about (a transactional outbox): it is then queued if and only
        if that transaction commits. Queuing an existing key changes nothing and
        returns the existing message's status, so a retried request never sends a
        second copy; use [`db.EmailOutbox.retry`] to attempt a `failed` message again.

        Args:
            message_key (`str`): Idempotency key, such as `invitation:42:send:1`.
            kind (`str`): Template name, such as `project_invitation`.
            recipient (`str`): One email address.
            subject (`str`): Subject line, without line breaks.
            body_text (`str`): Plain-text body.
            subject_ref (`str`, *optional*): Reference used to look up status.
            max_attempts (`int`, *optional*): Overrides the outbox default.
            conn (`psycopg.Connection`, *optional*): Transaction to join.

        Returns:
            [`db.DeliveryStatus`]: The queued, or previously queued, message.

        Raises:
            `ValueError`: If the recipient or subject is invalid.
            [`db.DatabaseError`]: If the row cannot be written.
        """
        address = normalize_recipient(recipient)
        if "\r" in subject or "\n" in subject:
            raise ValueError("The subject must not contain line breaks")
        attempts = self.max_attempts if max_attempts is None else max_attempts
        with self._cursor(conn) as cursor:
            row = cursor.execute(
                f"""
                INSERT INTO email_outbox (message_key, kind, subject_ref, recipient, subject,
                    body_text, message_id, max_attempts)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (message_key) DO NOTHING
                RETURNING {_STATUS_COLUMNS}
                """,
                (
                    message_key,
                    kind,
                    subject_ref,
                    address,
                    subject,
                    body_text,
                    message_id_for(message_key, self.message_id_domain),
                    attempts,
                ),
            ).fetchone()
            if row is None:
                row = cursor.execute(
                    f"SELECT {_STATUS_COLUMNS} FROM email_outbox WHERE message_key = %s",
                    (message_key,),
                ).fetchone()
                assert row is not None
                logger.info("Email already queued outbox_id=%s kind=%s", row["id"], kind)
            else:
                logger.info("Email queued outbox_id=%s kind=%s", row["id"], kind)
        assert row is not None
        return DeliveryStatus(**row)

    def claim(self, limit: int = 10) -> list[QueuedEmail]:
        """
        Reserves due messages for one delivery attempt each.

        Concurrent workers never claim the same message. A claim whose lease
        expired (its worker stopped before recording a result) is claimed again;
        its earlier attempt still counts, and it fails once out of attempts.
        """
        with self._cursor() as cursor:
            cursor.execute(
                """
                UPDATE email_outbox SET status = 'failed', locked_until = NULL,
                    last_error = 'DeliveryInterrupted', last_error_kind = 'transient',
                    updated_at = now()
                WHERE status = 'sending' AND locked_until < now() AND attempts >= max_attempts
                """
            )
            rows = cursor.execute(
                """
                WITH due AS (
                    SELECT id FROM email_outbox
                    WHERE (status = 'pending' AND next_attempt_at <= now())
                       OR (status = 'sending' AND locked_until < now())
                    ORDER BY next_attempt_at, id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE email_outbox AS o SET status = 'sending', attempts = o.attempts + 1,
                    locked_until = now() + make_interval(secs => %s), updated_at = now()
                FROM due WHERE o.id = due.id
                RETURNING o.id, o.kind, o.recipient, o.subject, o.body_text, o.message_id,
                    o.attempts, o.max_attempts
                """,
                (limit, self.lease),
            ).fetchall()
        return [QueuedEmail(**row) for row in rows]

    def mark_sent(self, message: QueuedEmail) -> None:
        """Records acceptance by the SMTP server and clears the stored body."""
        with self._cursor() as cursor:
            cursor.execute(
                "UPDATE email_outbox SET status = 'sent', sent_at = now(), body_text = NULL, "
                "locked_until = NULL, last_error = NULL, last_error_kind = NULL, "
                "updated_at = now() WHERE id = %s AND status = 'sending'",
                (message.id,),
            )

    def mark_failed(self, message: QueuedEmail, error: str, *, permanent: bool) -> DeliveryStatus:
        """
        Records a failed attempt: schedules a retry, or marks the message `failed`.

        Args:
            message ([`db.QueuedEmail`]): The claimed message.
            error (`str`): Error class and SMTP code. Must not contain addresses.
            permanent (`bool`): Whether the server rejected the message for good.

        Returns:
            [`db.DeliveryStatus`]: The message's new state.
        """
        final = permanent or message.attempts >= message.max_attempts
        with self._cursor() as cursor:
            row = cursor.execute(
                f"""
                UPDATE email_outbox SET status = %s, locked_until = NULL,
                    last_error = %s, last_error_kind = %s, updated_at = now(),
                    next_attempt_at = now() + make_interval(secs => %s)
                WHERE id = %s AND status = 'sending'
                RETURNING {_STATUS_COLUMNS}
                """,
                (
                    "failed" if final else "pending",
                    error[:500],
                    "permanent" if permanent else "transient",
                    0 if final else self.retry_wait(message.attempts),
                    message.id,
                ),
            ).fetchone()
            if row is None:
                row = cursor.execute(
                    f"SELECT {_STATUS_COLUMNS} FROM email_outbox WHERE id = %s", (message.id,)
                ).fetchone()
        assert row is not None
        return DeliveryStatus(**row)

    def retry(self, message_key: str, *, conn: psycopg.Connection | None = None) -> bool:
        """
        Queues a `failed` or waiting message for an immediate attempt.

        The attempt count restarts, so the message again gets its full number of
        attempts. A message already `sent`, or being sent, is left alone, so
        re-triggering never sends a second copy of a delivered message.

        Returns:
            `bool`: Whether the message was queued again.
        """
        with self._cursor(conn) as cursor:
            row = cursor.execute(
                "UPDATE email_outbox SET status = 'pending', attempts = 0, "
                "next_attempt_at = now(), updated_at = now() "
                "WHERE message_key = %s AND status IN ('pending', 'failed') RETURNING id, kind",
                (message_key,),
            ).fetchone()
        if row is not None:
            logger.info("Email queued again outbox_id=%s kind=%s", row["id"], row["kind"])
        return row is not None

    def status(self, message_key: str) -> DeliveryStatus | None:
        """Returns a message's delivery status, or `None` when the key is unknown."""
        with self._cursor() as cursor:
            row = cursor.execute(
                f"SELECT {_STATUS_COLUMNS} FROM email_outbox WHERE message_key = %s",
                (message_key,),
            ).fetchone()
        return DeliveryStatus(**row) if row else None

    def statuses(self, subject_ref: str) -> list[DeliveryStatus]:
        """Returns every message about one record, oldest first."""
        with self._cursor() as cursor:
            rows = cursor.execute(
                f"SELECT {_STATUS_COLUMNS} FROM email_outbox WHERE subject_ref = %s ORDER BY id",
                (subject_ref,),
            ).fetchall()
        return [DeliveryStatus(**row) for row in rows]

    def counts(self) -> dict[str, Any]:
        """Returns the number of messages in each status, for monitoring."""
        with self._cursor() as cursor:
            rows = cursor.execute(
                "SELECT status, count(*) AS n FROM email_outbox GROUP BY status"
            ).fetchall()
        return {status: 0 for status in DELIVERY_STATUSES} | {r["status"]: r["n"] for r in rows}
