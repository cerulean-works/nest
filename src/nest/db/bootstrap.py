"""
One-time platform administrator bootstrap and the platform audit log.

`NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL` names the Google account that becomes the first
`platform_admin`. It takes effect once: at that account's first sign-in with a
verified address, [`db.PlatformAdminBootstrap.claim`] writes the single
`platform_bootstrap` row and grants the role in one transaction. Once that row
exists, by a claim or because an administrator was granted another way first,
the setting grants nothing, whatever it is changed to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from .._logging import get_logger
from ._transaction import audit_context, transaction
from .connection import Database
from .identity import Identity, normalize_email

logger = get_logger("nest.db.bootstrap")

GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
"""The issuer values Google uses in ID tokens."""

AUDIT_EVENTS = (
    "bootstrap_completed",
    "bootstrap_refused",
    "platform_role_granted",
    "platform_role_revoked",
)

_CLAIM_REASON = "First verified sign-in of NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL"

_AUDIT_COLUMNS = (
    "id, occurred_at, event, role, target_user_id, actor_user_id, actor_operator, "
    "database_role, reason, details"
)


class BootstrapOutcome(StrEnum):
    """What a bootstrap claim did. Only `CLAIMED` grants anything."""

    CLAIMED = "claimed"
    """This sign-in made its User the first `platform_admin`."""
    NOT_CONFIGURED = "not_configured"
    """`NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL` is unset."""
    NOT_CONFIGURED_ACCOUNT = "not_configured_account"
    """The identity's address is not the configured one."""
    NOT_GOOGLE = "not_google"
    """The identity is not a Google identity."""
    EMAIL_NOT_VERIFIED = "email_not_verified"
    """Google did not report the address as verified."""
    USER_NOT_ACTIVE = "user_not_active"
    """The User is disabled."""
    ALREADY_COMPLETED = "already_completed"
    """The bootstrap was completed earlier, by this User or otherwise."""


@dataclass(frozen=True)
class BootstrapState:
    """
    The completed bootstrap.

    Attributes:
        method (`str`): `google_first_login`, `direct_grant` (an administrator was
            granted another way first) or `preexisting_admin` (one existed when
            migration 0004 was applied).
        user_id (`UUID`): The first administrator.
        issuer, subject (`str | None`): The Google identity that claimed it, for
            `google_first_login`.
        completed_at (`datetime`): When the bootstrap closed.
    """

    method: str
    user_id: UUID
    issuer: str | None
    subject: str | None
    completed_at: datetime


@dataclass(frozen=True)
class AuditEvent:
    """
    A platform audit log entry. Entries are never changed or deleted.

    Attributes:
        id (`int`): Increasing row ID.
        occurred_at (`datetime`): When it happened.
        event (`str`): One of `bootstrap_completed`, `bootstrap_refused`,
            `platform_role_granted`, `platform_role_revoked`.
        role (`str | None`): The platform role involved.
        target_user_id (`UUID | None`): The User the event is about.
        actor_user_id (`UUID | None`): The Nest User who made the change.
        actor_operator (`str | None`): The operator name given to `nest-db admin`.
        database_role (`str`): The PostgreSQL role that made the change.
        reason (`str | None`): The reason given.
        details (`dict`): Event details, such as the claiming identity.
    """

    id: int
    occurred_at: datetime
    event: str
    role: str | None
    target_user_id: UUID | None
    actor_user_id: UUID | None
    actor_operator: str | None
    database_role: str
    reason: str | None
    details: dict[str, Any]


class PlatformAdminBootstrap:
    """
    Grants `platform_admin` once, to the configured Google account.

    Call [`db.PlatformAdminBootstrap.claim`] after every successful sign-in. It
    does nothing unless the signed-in identity is a Google identity whose current,
    verified address is the configured one and the bootstrap has not completed.

    Args:
        database ([`db.Database`]): Open database.
        google_email (`str | None`): `NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL`, or `None`.
    """

    def __init__(self, database: Database, google_email: str | None):
        self.database = database
        self.google_email = normalize_email(google_email) if google_email else None

    def claim(
        self, identity: Identity, *, conn: psycopg.Connection | None = None
    ) -> BootstrapOutcome:
        """
        Makes the signed-in User the first `platform_admin` if they are entitled to.

        The identity's claims are re-read from the database inside the transaction,
        so only what the sign-in stored counts, never the values on `identity`.
        Concurrent claims serialize on the single bootstrap row: at most one is
        ever `CLAIMED`. A refused claim by an identity with the configured address
        is recorded in the audit log as `bootstrap_refused`.

        Args:
            identity ([`db.Identity`]): The identity that just signed in.
            conn (`psycopg.Connection`, *optional*): Transaction to join, such as
                the sign-in's own.

        Returns:
            [`db.BootstrapOutcome`]: `CLAIMED` only when the role was granted.
        """
        if self.google_email is None:
            return BootstrapOutcome.NOT_CONFIGURED
        with transaction(self.database, conn, "claim the platform bootstrap") as cursor:
            row = cursor.execute(
                "SELECT i.user_id, i.issuer, i.subject, i.email, i.email_verified, u.status "
                "FROM user_identities i JOIN users u ON u.id = i.user_id "
                "WHERE i.id = %s FOR SHARE OF i, u",
                (identity.id,),
            ).fetchone()
            if (
                row is None
                or not row["email"]
                or normalize_email(row["email"]) != self.google_email
            ):
                return BootstrapOutcome.NOT_CONFIGURED_ACCOUNT
            if row["issuer"] not in GOOGLE_ISSUERS:
                outcome = BootstrapOutcome.NOT_GOOGLE
            elif not row["email_verified"]:
                outcome = BootstrapOutcome.EMAIL_NOT_VERIFIED
            elif row["status"] != "active":
                outcome = BootstrapOutcome.USER_NOT_ACTIVE
            else:
                with audit_context(cursor, reason=_CLAIM_REASON):
                    claimed = cursor.execute(
                        "INSERT INTO platform_bootstrap (method, user_id, issuer, subject) "
                        "VALUES ('google_first_login', %s, %s, %s) "
                        "ON CONFLICT (singleton) DO NOTHING RETURNING user_id",
                        (row["user_id"], row["issuer"], row["subject"]),
                    ).fetchone()
                    if claimed is not None:
                        cursor.execute(
                            "INSERT INTO platform_role_assignments (user_id, role) "
                            "VALUES (%s, 'platform_admin')",
                            (row["user_id"],),
                        )
                if claimed is not None:
                    outcome = BootstrapOutcome.CLAIMED
                else:
                    outcome = BootstrapOutcome.ALREADY_COMPLETED
                    first = cursor.execute("SELECT user_id FROM platform_bootstrap").fetchone()
                    if first is not None and first["user_id"] == row["user_id"]:
                        # The first administrator signing in again: nothing to record.
                        return outcome
            if outcome is not BootstrapOutcome.CLAIMED:
                cursor.execute(
                    "INSERT INTO platform_audit_events (event, role, target_user_id, details) "
                    "VALUES ('bootstrap_refused', 'platform_admin', %s, %s)",
                    (
                        row["user_id"],
                        Jsonb(
                            {
                                "outcome": outcome.value,
                                "issuer": row["issuer"],
                                "subject": row["subject"],
                            }
                        ),
                    ),
                )
        if outcome is BootstrapOutcome.CLAIMED:
            logger.warning(
                "Platform bootstrap completed: first platform_admin granted user_id=%s",
                row["user_id"],
            )
        else:
            logger.warning(
                "Platform bootstrap refused for the configured address user_id=%s outcome=%s",
                row["user_id"],
                outcome.value,
            )
        return outcome

    def state(self) -> BootstrapState | None:
        """Returns the completed bootstrap, or `None` while it is still open."""
        return bootstrap_state(self.database)


def bootstrap_state(database: Database) -> BootstrapState | None:
    """Returns the completed bootstrap, or `None` while it is still open."""
    with transaction(database, None, "read the platform bootstrap") as cursor:
        row = cursor.execute(
            "SELECT method, user_id, issuer, subject, completed_at FROM platform_bootstrap"
        ).fetchone()
    return BootstrapState(**row) if row else None


def audit_events(
    database: Database, *, target_user_id: UUID | None = None, limit: int = 100
) -> list[AuditEvent]:
    """
    Returns platform audit log entries, newest first.

    Args:
        database ([`db.Database`]): Open database.
        target_user_id (`UUID`, *optional*): Only entries about this User.
        limit (`int`, *optional*): Most entries to return. Defaults to 100.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    with transaction(database, None, "read the platform audit log") as cursor:
        rows = cursor.execute(
            f"SELECT {_AUDIT_COLUMNS} FROM platform_audit_events "
            "WHERE %(target)s::uuid IS NULL OR target_user_id = %(target)s::uuid "
            "ORDER BY id DESC LIMIT %(limit)s",
            {"target": target_user_id, "limit": limit},
        ).fetchall()
    return [AuditEvent(**row) for row in rows]
