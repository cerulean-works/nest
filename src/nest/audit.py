"""
Structured security audit records.

[`audit.AuditLog`] writes one row of `security_audit_events` (migration 0009) for
each security-relevant event the Controller handles itself: authorization
decisions on the gRPC execution service and the Projects API, and the
Controller's start. Identity, session, CLI sign-in, invitation, Invite Link and
Provider Profile changes are recorded by database triggers into the same table,
whoever makes them. Every event is also logged as one JSON line on the
`nest.audit` logger.

Records never hold secrets. Details are reduced to short scalar values by
[`audit.redact_details`], which masks anything that looks like a token, key,
password, email address or registered secret, so a caller cannot leak one into
the audit log by mistake.

Rows are sealed into a SHA-256 hash chain ([`audit.AuditLog.seal`]), so a change
to or removal of a sealed row is detected by [`audit.AuditLog.verify`] and
`nest-db audit verify`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import _logging
from ._logging import current_request_id, get_logger
from .db.connection import Database, DatabaseError
from .errors import DatabaseUnavailableError

logger = get_logger("nest.audit")

OUTCOMES = ("succeeded", "denied", "failed")
"""What happened: the action was done, refused by policy, or failed."""

INTERFACES = ("grpc", "http", "cli", "controller", "database")
"""Where an event came from. `database` is used by the triggers only."""

REDACTED = "***"

_EVENT = re.compile(r"[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){1,3}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}")
_RESOURCE_TYPE = re.compile(r"[a-z][a-z0-9_]{0,31}")
_RESOURCE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_SENSITIVE_KEY = re.compile(
    r"token|secret|password|passwd|credential|cookie|authorization|private|key|code|email|"
    r"recipient|body|payload"
)
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_OPAQUE = re.compile(r"[A-Za-z0-9_\-+/=.]{24,}")
_HEX_OR_UUID = re.compile(r"[0-9a-f]{32}|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_MAX_DETAILS = 32
_MAX_VALUE = 200

_interface: ContextVar[str | None] = ContextVar("nest_audit_interface", default=None)


@contextmanager
def audit_interface(name: str) -> Generator[None]:
    """Names the interface (`grpc`, `http`, `cli` or `controller`) of the events inside."""
    if name not in INTERFACES:
        raise ValueError(f"Unknown audit interface {name!r}")
    token = _interface.set(name)
    try:
        yield
    finally:
        _interface.reset(token)


def _looks_secret(text: str) -> bool:
    if "PRIVATE KEY" in text or _EMAIL.search(text):
        return True
    if any(secret in text for secret in _logging._secrets):
        return True
    for match in _OPAQUE.finditer(text):
        if not _HEX_OR_UUID.fullmatch(match.group(0)):
            return True
    return False


def redact_details(details: Mapping[str, object] | None) -> dict[str, object]:
    """
    Reduces audit details to values that are safe to keep.

    Keeps at most 32 entries whose keys are lowercase identifiers. Each value must
    be `None`, a `bool`, an `int`, a `float` or a short string; anything else, a
    key that names a secret (such as `token`, `password`, `key` or `email`), and a
    string that looks like a token, a private key, an email address or a
    registered secret (see [`_logging.register_secret`]) is replaced by `***`.
    Hex IDs and UUIDs are kept.

    Args:
        details (`Mapping[str, object]`, *optional*): Proposed details.

    Returns:
        `dict[str, object]`: The details to record.
    """
    safe: dict[str, object] = {}
    for key, value in list((details or {}).items())[:_MAX_DETAILS]:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            continue
        if _SENSITIVE_KEY.search(key):
            safe[key] = REDACTED
        elif value is None or isinstance(value, bool | int | float):
            safe[key] = value
        elif isinstance(value, str):
            text = value[:_MAX_VALUE]
            safe[key] = REDACTED if _looks_secret(text) else text
        else:
            safe[key] = REDACTED
    return safe


@dataclass(frozen=True)
class SecurityAuditEvent:
    """
    One row of the security audit log.

    Attributes:
        id (`int`): Row ID, in insertion order.
        occurred_at (`datetime`): When it happened.
        event (`str`): Dotted name, such as `run.submit` or `session.revoked`.
        outcome (`str`): `succeeded`, `denied` or `failed`.
        interface (`str`): `grpc`, `http`, `cli`, `controller` or `database`.
        actor_user_id, session_id (`UUID | None`): Who acted, through which session.
        actor_operator (`str | None`): Operator name given to `nest-db`.
        database_role (`str`): PostgreSQL role that wrote the row.
        project_id, target_user_id (`UUID | None`): The Project and User concerned.
        resource_type, resource_id (`str | None`): What was acted on.
        request_id (`str | None`): Correlation ID from the Controller log.
        reason (`str | None`): Machine-readable reason, such as `not_a_member`.
        note (`str | None`): Free-text reason an operator gave.
        details (`dict`): Redacted scalar details.
        chain_seq (`int | None`): Position in the hash chain; `None` until sealed.
    """

    id: int
    occurred_at: datetime
    event: str
    outcome: str
    interface: str
    actor_user_id: UUID | None
    session_id: UUID | None
    actor_operator: str | None
    database_role: str
    project_id: UUID | None
    target_user_id: UUID | None
    resource_type: str | None
    resource_id: str | None
    request_id: str | None
    reason: str | None
    note: str | None
    details: dict[str, Any]
    chain_seq: int | None


@dataclass(frozen=True)
class ChainStatus:
    """
    The result of [`audit.AuditLog.verify`].

    Attributes:
        intact (`bool`): Whether every sealed row matches its hash and link.
        sealed (`int`): Sealed rows checked.
        unsealed (`int`): Rows not sealed yet.
        first_broken (`int | None`): `chain_seq` of the first row that does not
            match, or where a row is missing.
    """

    intact: bool
    sealed: int
    unsealed: int
    first_broken: int | None = None


_COLUMNS = (
    "id, occurred_at, event, outcome, interface, actor_user_id, session_id, actor_operator, "
    "database_role, project_id, target_user_id, resource_type, resource_id, request_id, "
    "reason, note, details, chain_seq"
)


def _clean(value: str | None, pattern: re.Pattern[str]) -> str | None:
    return value if value is not None and pattern.fullmatch(value) else None


class AuditLog:
    """
    Writes and reads the security audit log.

    Args:
        database ([`db.Database`], *optional*): Open database. Without one, events
            are only logged.
    """

    def __init__(self, database: Database | None):
        self.database = database

    def record(
        self,
        event: str,
        outcome: str,
        *,
        actor_user_id: UUID | None = None,
        session_id: UUID | None = None,
        project_id: UUID | None = None,
        target_user_id: UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        reason: str | None = None,
        details: Mapping[str, object] | None = None,
        interface: str | None = None,
        required: bool = False,
    ) -> int | None:
        """
        Records one event and logs it as a JSON line on `nest.audit`.

        Args:
            event (`str`): Dotted lowercase name, such as `run.submit`.
            outcome (`str`): `succeeded`, `denied` or `failed`.
            reason (`str`, *optional*): Machine-readable reason, such as the
                policy's; anything else is dropped.
            details (`Mapping`, *optional*): Passed through [`audit.redact_details`].
            interface (`str`, *optional*): Defaults to the one set by
                [`audit.audit_interface`], or `controller`.
            required (`bool`): Raise when the row cannot be written, so a
                sensitive action is refused rather than done without its record.
                Otherwise a failed write is logged as an error.

        Returns:
            `int | None`: The row ID, or `None` without a database or on failure.

        Raises:
            [`errors.DatabaseUnavailableError`]: If `required` and the row cannot
                be written.
        """
        if not _EVENT.fullmatch(event):
            raise ValueError(f"Invalid audit event name {event!r}")
        if outcome not in OUTCOMES:
            raise ValueError(f"Invalid audit outcome {outcome!r}")
        interface = interface or _interface.get() or "controller"
        if interface not in INTERFACES:
            raise ValueError(f"Unknown audit interface {interface!r}")
        row = {
            "event": event,
            "outcome": outcome,
            "interface": interface,
            "actor_user_id": actor_user_id,
            "session_id": session_id,
            "project_id": project_id,
            "target_user_id": target_user_id,
            "resource_type": _clean(resource_type, _RESOURCE_TYPE),
            "resource_id": _clean(resource_id, _RESOURCE_ID),
            "request_id": _clean(current_request_id(), _REQUEST_ID),
            "reason": _clean(reason, _CODE),
            "details": redact_details(details),
        }
        logger.info(
            "Audit %s",
            json.dumps(
                {k: str(v) if isinstance(v, UUID) else v for k, v in row.items() if v is not None},
                sort_keys=True,
            ),
        )
        if self.database is None:
            return None
        try:
            with self.database.connection() as conn:
                found = conn.execute(
                    "INSERT INTO security_audit_events (event, outcome, interface, "
                    "actor_user_id, session_id, project_id, target_user_id, resource_type, "
                    "resource_id, request_id, reason, details) VALUES (%(event)s, %(outcome)s, "
                    "%(interface)s, %(actor_user_id)s, %(session_id)s, %(project_id)s, "
                    "%(target_user_id)s, %(resource_type)s, %(resource_id)s, %(request_id)s, "
                    "%(reason)s, %(details)s) RETURNING id",
                    {**row, "details": Jsonb(row["details"])},
                ).fetchone()
        except (psycopg.Error, DatabaseError) as exc:
            logger.error(
                "Audit record could not be stored event=%s: %s",
                event,
                self.database.scrub(str(exc)),
            )
            if required:
                raise DatabaseUnavailableError(
                    "The Controller database is unavailable, so the request was not run. "
                    "Nothing was submitted; retry later."
                ) from None
            return None
        self.seal()
        return found[0] if found else None

    def seal(self) -> int:
        """
        Seals every committed, unsealed row onto the hash chain, in its own
        transaction. Returns how many rows it sealed; `0` when it cannot.
        """
        if self.database is None:
            return 0
        try:
            with self.database.connection() as conn:
                found = conn.execute("SELECT nest_seal_security_audit()").fetchone()
        except (psycopg.Error, DatabaseError) as exc:
            logger.error("Audit log could not be sealed: %s", self.database.scrub(str(exc)))
            return 0
        return int(found[0]) if found else 0

    def _require_database(self) -> Database:
        if self.database is None:
            raise RuntimeError("The audit log needs a database")
        return self.database

    def verify(self, *, conn: psycopg.Connection | None = None) -> ChainStatus:
        """
        Checks every sealed row against its hash and its link to the row before.

        A changed, deleted or reordered sealed row breaks the chain from that row
        on. Rows written after the last seal are counted, not checked; call
        [`audit.AuditLog.seal`] first to include them.
        """
        query = (
            "SELECT chain_seq, row_hash = nest_security_audit_hash(e) AS hash_ok, "
            "prev_hash IS NOT DISTINCT FROM lag(row_hash) OVER w AS link_ok, "
            "chain_seq = COALESCE(lag(chain_seq) OVER w, 0) + 1 AS seq_ok "
            "FROM security_audit_events e WHERE chain_seq IS NOT NULL "
            "WINDOW w AS (ORDER BY chain_seq) ORDER BY chain_seq"
        )
        with self._connection(conn) as active, active.cursor(row_factory=dict_row) as cursor:
            sealed = 0
            broken = None
            for row in cursor.execute(query):
                sealed += 1
                if broken is None and not (row["hash_ok"] and row["link_ok"] and row["seq_ok"]):
                    broken = row["chain_seq"]
            unsealed_row = cursor.execute(
                "SELECT count(*) AS n FROM security_audit_events WHERE chain_seq IS NULL"
            ).fetchone()
        unsealed = unsealed_row["n"] if unsealed_row else 0
        return ChainStatus(broken is None, sealed, unsealed, broken)

    def events(
        self,
        *,
        limit: int = 100,
        before_id: int | None = None,
        event: str | None = None,
        actor_user_id: UUID | None = None,
        project_id: UUID | None = None,
        conn: psycopg.Connection | None = None,
    ) -> list[SecurityAuditEvent]:
        """Returns events newest first, optionally filtered."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connection(conn) as active, active.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute(
                "SELECT " + _COLUMNS + " FROM security_audit_events "
                "WHERE (%(before)s::bigint IS NULL OR id < %(before)s) "
                "AND (%(event)s::text IS NULL OR event = %(event)s) "
                "AND (%(actor)s::uuid IS NULL OR actor_user_id = %(actor)s) "
                "AND (%(project)s::uuid IS NULL OR project_id = %(project)s) "
                "ORDER BY id DESC LIMIT %(limit)s",
                {
                    "before": before_id,
                    "event": event,
                    "actor": actor_user_id,
                    "project": project_id,
                    "limit": limit,
                },
            ).fetchall()
        return [SecurityAuditEvent(**row) for row in rows]

    @contextmanager
    def _connection(self, conn: psycopg.Connection | None) -> Generator[psycopg.Connection]:
        if conn is not None:
            yield conn
            return
        with self._require_database().connection() as owned:
            yield owned
