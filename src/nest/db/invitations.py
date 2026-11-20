"""
Project email invitations and Invite Links, and their redemption.

Invitation records, memberships and email delivery state are kept apart:

- An email invitation (`project_email_invitations`) is bound to one normalized
  address. Issuing one creates no User and no membership; the address is plain data
  until a User whose *verified* sign-in identity reports the same address accepts
  it, or signs in for the first time.
- An Invite Link (`project_invite_links`) is a bearer grant with an expiry and a
  bounded number of uses. Each acceptance is a row in
  `project_invitation_redemptions`; opening the link changes nothing.
- Delivery state lives only in `email_outbox` (subject `invitation:<id>`), which
  this module reads but never writes.

Only a SHA-256 of each token is stored. Every redemption locks the Project row,
then the invitation or link row, and makes its change in the same transaction, so
concurrent acceptances, repeated first sign-ins and simultaneous Invite Link
redemptions can neither duplicate a membership nor exceed a link's use limit.

Who may issue, list or revoke invitations is decided by the authorization layer
(see [`authz.InvitationService`]); the rules here hold for every caller: no
invitation grants `owner`, an invitation is honored only while its issuer is still
an Owner or Admin of a live Project, and nothing here grants Provider or HPC access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from .._logging import get_logger
from ._transaction import as_uuid, audit_context, transaction
from .connection import Database
from .identity import normalize_email
from .outbox import DeliveryStatus, normalize_recipient
from .projects import ASSIGNABLE_ROLES, DuplicateMemberError, Membership, OwnerRuleError

logger = get_logger("nest.db.invitations")

MAX_INVITATION_TTL = 30 * 86_400
"""Longest lifetime of an email invitation or Invite Link: 30 days."""

MAX_LINK_USES = 10_000
"""Largest use limit of an Invite Link (the schema's bound)."""

_MANAGERS = ("owner", "admin")

_EMAIL_STATUS = (
    "CASE WHEN i.status = 'pending' AND i.expires_at <= now() THEN 'expired' ELSE i.status END"
)
_EMAIL_COLUMNS = (
    "i.id, i.project_id, i.invited_email_normalized AS email, i.role, i.invited_by_user_id, "
    f"{_EMAIL_STATUS} AS status, i.expires_at, i.created_at, i.updated_at, i.accepted_at, "
    "i.accepted_by_user_id, i.declined_at, i.revoked_at, i.revoked_by_user_id"
)
_LINK_COLUMNS = (
    "l.id, l.project_id, l.role, l.created_by_user_id, l.expires_at, l.max_uses, l.use_count, "
    "l.created_at, l.revoked_at, l.revoked_by_user_id"
)
_DELIVERY_COLUMNS = (
    "id, message_key, kind, subject_ref, status, attempts, max_attempts, next_attempt_at, "
    "last_error, last_error_kind, created_at, updated_at, sent_at"
)
_MEMBERSHIP_COLUMNS = "project_id, user_id, role, added_by_user_id, created_at, updated_at"

# Whether invitation `i`'s issuer is still an active Owner or Admin.
_ISSUER_MAY_INVITE = (
    "SELECT 1 FROM project_memberships im JOIN users iu ON iu.id = im.user_id "
    "WHERE im.project_id = i.project_id AND im.user_id = i.invited_by_user_id "
    "AND im.role = ANY(%s) AND iu.status = 'active'"
)

# The User's current verified addresses, in normalized form.
_VERIFIED_EMAILS = (
    "SELECT lower(btrim(email)) FROM user_identities "
    "WHERE user_id = %s AND email_verified AND email IS NOT NULL"
)


class InvitationError(Exception):
    """An invitation could not be issued, changed or redeemed. Nothing was changed."""


class InvitationNotFoundError(InvitationError):
    """
    No such invitation or Invite Link, or none the caller may see.

    Unknown tokens, IDs of another Project and invitations addressed to someone
    else all raise this, so a caller cannot tell them apart.
    """


class InvitationUnavailableError(InvitationError):
    """
    The invitation or Invite Link exists but cannot be redeemed now.

    Attributes:
        reason (`str`): `expired`, `revoked`, `accepted`, `declined`, `exhausted`,
            `already_redeemed`, `project_deleted`, `issuer_not_authorized` or
            `user_disabled`.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"The invitation cannot be used: {reason}")


class EmailMismatchError(InvitationError):
    """
    The caller has no verified sign-in identity with the invited address.

    Attributes:
        reason (`str`): `email_mismatch`.
    """

    reason = "email_mismatch"

    def __init__(self):
        super().__init__("This invitation is for another email address")


class AlreadyInvitedError(InvitationError):
    """
    The address already has a pending invitation to the Project.

    Attributes:
        invitation_id (`int`): The pending invitation, to resend or revoke instead.
    """

    def __init__(self, invitation_id: int):
        self.invitation_id = invitation_id
        super().__init__("The address already has a pending invitation to this Project")


@dataclass(frozen=True)
class EmailInvitation:
    """
    An email invitation, as its Project's Owner and Admins see it.

    Attributes:
        id (`int`): Invitation ID.
        project_id (`UUID`): The Project.
        email (`str`): Normalized invited address. Never logged.
        role (`str`): `admin`, `member` or `viewer`, granted on acceptance.
        invited_by_user_id (`UUID`): Who issued it.
        status (`str`): `pending`, `accepted`, `declined`, `revoked` or `expired`
            (a pending invitation past its expiry reads as `expired`).
        expires_at, created_at, updated_at (`datetime`): Timestamps.
        accepted_at, accepted_by_user_id: When and by whom it was accepted.
        declined_at (`datetime | None`): When it was declined.
        revoked_at, revoked_by_user_id: When and by whom it was revoked.
        delivery ([`db.DeliveryStatus`] or `None`): The latest email's delivery
            state, without recipient or content; `None` when no email was queued.
    """

    id: int
    project_id: UUID
    email: str
    role: str
    invited_by_user_id: UUID
    status: str
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    accepted_at: datetime | None
    accepted_by_user_id: UUID | None
    declined_at: datetime | None
    revoked_at: datetime | None
    revoked_by_user_id: UUID | None
    delivery: DeliveryStatus | None = field(default=None, compare=False)

    def __repr__(self) -> str:
        return (
            f"EmailInvitation(id={self.id}, project_id={self.project_id}, role={self.role!r}, "
            f"status={self.status!r})"
        )


@dataclass(frozen=True)
class InviteLink:
    """
    An Invite Link. Its token is shown once, when it is created, and never stored.

    Attributes:
        id (`int`): Invite Link ID.
        project_id (`UUID`): The Project.
        role (`str`): `admin`, `member` or `viewer`, granted on acceptance.
        created_by_user_id (`UUID`): Who created it.
        expires_at, created_at (`datetime`): Timestamps.
        max_uses (`int`): How many Users may accept it.
        use_count (`int`): How many have.
        revoked_at, revoked_by_user_id: When and by whom it was revoked.
        now (`datetime | None`): The database time it was read at, which decides
            whether it has expired.
    """

    id: int
    project_id: UUID
    role: str
    created_by_user_id: UUID
    expires_at: datetime
    max_uses: int
    use_count: int
    created_at: datetime
    revoked_at: datetime | None
    revoked_by_user_id: UUID | None
    now: datetime | None = field(default=None, repr=False, compare=False)

    @property
    def status(self) -> str:
        """`active`, `revoked`, `exhausted` or `expired`."""
        if self.revoked_at is not None:
            return "revoked"
        if self.use_count >= self.max_uses:
            return "exhausted"
        if self.now is not None and self.expires_at <= self.now:
            return "expired"
        return "active"


@dataclass(frozen=True)
class PendingInvitation:
    """
    A pending email invitation, as its invitee sees it.

    Attributes:
        id (`int`): Invitation ID.
        project_id (`UUID`): The Project.
        project_name (`str`): Its display name.
        role (`str`): Role granted on acceptance.
        invited_by_name (`str | None`): The issuer's display name.
        expires_at, created_at (`datetime`): Timestamps.
    """

    id: int
    project_id: UUID
    project_name: str
    role: str
    invited_by_name: str | None
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True)
class InvitationPreview:
    """
    What a token grants, shown before the holder accepts it.

    Attributes:
        kind (`str`): `email` or `link`.
        id (`int`): Invitation or Invite Link ID.
        project_id (`UUID`): The Project.
        project_name (`str`): Its display name.
        role (`str`): Role granted on acceptance.
        status (`str`): For `email`, as [`db.EmailInvitation`]; for `link`, as
            [`db.InviteLink`].
        expires_at (`datetime`): When it expires.
    """

    kind: str
    id: int
    project_id: UUID
    project_name: str
    role: str
    status: str
    expires_at: datetime


@dataclass(frozen=True)
class Acceptance:
    """
    The outcome of accepting an invitation or Invite Link.

    Attributes:
        kind (`str`): `email` or `link`.
        invitation_id (`int`): The invitation or Invite Link.
        project_id (`UUID`): The Project joined.
        role (`str`): The caller's role in it now.
        membership ([`db.Membership`]): The membership.
        already_member (`bool`): `True` when the User was a member already, so
            their existing role was kept (email invitations only).
    """

    kind: str
    invitation_id: int
    project_id: UUID
    role: str
    membership: Membership
    already_member: bool = False


def normalize_invited_email(email: str) -> str:
    """
    Validates one invited address and returns its comparison form.

    Raises:
        `ValueError`: If it is not exactly one valid address. The message never
            repeats the value.
    """
    return normalize_email(normalize_recipient(email))


def _assignable(role: str) -> str:
    if role == "owner":
        raise OwnerRuleError("An invitation can never grant the owner role")
    if role not in ASSIGNABLE_ROLES:
        raise ValueError(f"Unknown Project role; expected one of {ASSIGNABLE_ROLES}")
    return role


def _ttl(ttl_seconds: float) -> float:
    if not 60 <= ttl_seconds <= MAX_INVITATION_TTL:
        raise ValueError(f"The lifetime must be 60 to {MAX_INVITATION_TTL} seconds")
    return float(ttl_seconds)


def _delivery(row: dict[str, Any]) -> DeliveryStatus | None:
    if row.get("d_id") is None:
        return None
    return DeliveryStatus(
        **{column.strip(): row["d_" + column.strip()] for column in _DELIVERY_COLUMNS.split(",")}
    )


def _email_invitation(row: dict[str, Any]) -> EmailInvitation:
    fields = {k: v for k, v in row.items() if not k.startswith("d_")}
    return EmailInvitation(**fields, delivery=_delivery(row))


class InvitationStore:
    """
    Issues, lists, revokes and redeems email invitations and Invite Links.

    Every method accepts `conn` to join a caller's transaction, such as the
    authorization layer's, which holds the Project lock while it checks the caller.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    # Helpers -----------------------------------------------------------------

    @staticmethod
    def _lock_project(cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID) -> None:
        row = cursor.execute(
            "SELECT id FROM projects WHERE id = %s AND deleted_at IS NULL FOR NO KEY UPDATE",
            (project_id,),
        ).fetchone()
        if row is None:
            raise InvitationUnavailableError("project_deleted")

    @staticmethod
    def _issuer_may_invite(
        cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID, issuer: UUID
    ) -> bool:
        row = cursor.execute(
            "SELECT 1 FROM project_memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.project_id = %s AND m.user_id = %s AND m.role = ANY(%s) "
            "AND u.status = 'active'",
            (project_id, issuer, list(_MANAGERS)),
        ).fetchone()
        return row is not None

    @staticmethod
    def _require_active(cursor: psycopg.Cursor[dict[str, Any]], user_id: UUID) -> None:
        row = cursor.execute("SELECT status FROM users WHERE id = %s", (user_id,)).fetchone()
        if row is None or row["status"] != "active":
            raise InvitationUnavailableError("user_disabled")

    @staticmethod
    def _join(
        cursor: psycopg.Cursor[dict[str, Any]],
        project_id: UUID,
        user_id: UUID,
        role: str,
        added_by: UUID,
    ) -> tuple[Membership, bool]:
        """Adds the membership, or returns the existing one unchanged."""
        row = cursor.execute(
            "INSERT INTO project_memberships (project_id, user_id, role, added_by_user_id) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (project_id, user_id) DO NOTHING "
            f"RETURNING {_MEMBERSHIP_COLUMNS}",
            (project_id, user_id, role, added_by),
        ).fetchone()
        if row is not None:
            return Membership(**row), False
        row = cursor.execute(
            f"SELECT {_MEMBERSHIP_COLUMNS} FROM project_memberships "
            "WHERE project_id = %s AND user_id = %s",
            (project_id, user_id),
        ).fetchone()
        assert row is not None
        return Membership(**row), True

    # Email invitations ---------------------------------------------------------

    def create_email_invitation(
        self,
        project_id: UUID | str,
        email: str,
        role: str,
        *,
        invited_by_user_id: UUID | str,
        token_hash: bytes,
        ttl_seconds: float,
        conn: psycopg.Connection | None = None,
    ) -> EmailInvitation:
        """
        Records a pending invitation for an address. Creates no User or membership.

        A pending invitation past its expiry for the same address is marked
        `expired` first, so a new one can replace it.

        Args:
            project_id (`UUID`): The Project.
            email (`str`): One address.
            role (`str`): `admin`, `member` or `viewer`.
            invited_by_user_id (`UUID`): The issuer.
            token_hash (`bytes`): SHA-256 of the recipient-bound link token.
            ttl_seconds (`float`): Lifetime, 60 seconds to 30 days.
            conn (`psycopg.Connection`, *optional*): Transaction to join.

        Raises:
            [`db.AlreadyInvitedError`]: If the address has a pending invitation.
            [`db.DuplicateMemberError`]: If a member's verified identity already
                reports the address.
            [`db.OwnerRuleError`]: If `role` is `owner`.
            [`db.InvitationUnavailableError`]: If the Project was deleted.
            `ValueError`: If the address, role or lifetime is invalid.
        """
        pid = as_uuid(project_id, "project_id")
        issuer = as_uuid(invited_by_user_id, "invited_by_user_id")
        address = normalize_invited_email(email)
        _assignable(role)
        ttl = _ttl(ttl_seconds)
        with transaction(self.database, conn, "create an email invitation") as cursor:
            self._lock_project(cursor, pid)
            cursor.execute(
                "UPDATE project_email_invitations SET status = 'expired', updated_at = now() "
                "WHERE project_id = %s AND invited_email_normalized = %s "
                "AND status = 'pending' AND expires_at <= now()",
                (pid, address),
            )
            member = cursor.execute(
                "SELECT 1 FROM project_memberships m JOIN user_identities u "
                "ON u.user_id = m.user_id WHERE m.project_id = %s AND u.email_verified "
                "AND lower(btrim(u.email)) = %s LIMIT 1",
                (pid, address),
            ).fetchone()
            if member is not None:
                raise DuplicateMemberError("A member already signs in with this address")
            row = cursor.execute(
                "INSERT INTO project_email_invitations AS i (project_id, "
                "invited_email_normalized, role, invited_by_user_id, token_hash, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s)) "
                "ON CONFLICT (project_id, invited_email_normalized) WHERE status = 'pending' "
                f"DO NOTHING RETURNING {_EMAIL_COLUMNS}",
                (pid, address, role, issuer, token_hash, ttl),
            ).fetchone()
            if row is None:
                existing = cursor.execute(
                    "SELECT id FROM project_email_invitations WHERE project_id = %s "
                    "AND invited_email_normalized = %s AND status = 'pending'",
                    (pid, address),
                ).fetchone()
                raise AlreadyInvitedError(existing["id"] if existing else 0)
        logger.info(
            "Email invitation created invitation_id=%s project_id=%s role=%s", row["id"], pid, role
        )
        return EmailInvitation(**row)

    def reissue_token(
        self,
        project_id: UUID | str,
        invitation_id: int,
        *,
        token_hash: bytes,
        conn: psycopg.Connection | None = None,
    ) -> EmailInvitation:
        """
        Replaces a pending invitation's link token; the previous link stops working.
        The expiry is unchanged.

        Raises:
            [`db.InvitationNotFoundError`]: If the Project has no such invitation.
            [`db.InvitationUnavailableError`]: If it is no longer pending.
        """
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, conn, "reissue an invitation link") as cursor:
            row = cursor.execute(
                "UPDATE project_email_invitations AS i SET token_hash = %s, updated_at = now() "
                "WHERE i.id = %s AND i.project_id = %s AND i.status = 'pending' "
                f"AND i.expires_at > now() RETURNING {_EMAIL_COLUMNS}",
                (token_hash, invitation_id, pid),
            ).fetchone()
            if row is None:
                self._raise_unusable(cursor, pid, invitation_id)
        assert row is not None
        logger.info("Invitation link reissued invitation_id=%s project_id=%s", invitation_id, pid)
        return EmailInvitation(**row)

    def revoke_email_invitation(
        self,
        project_id: UUID | str,
        invitation_id: int,
        *,
        actor_user_id: UUID | str,
        conn: psycopg.Connection | None = None,
    ) -> EmailInvitation:
        """
        Revokes a pending (or pending but expired) invitation.

        Raises:
            [`db.InvitationNotFoundError`]: If the Project has no such invitation.
            [`db.InvitationUnavailableError`]: If it was already completed.
        """
        pid = as_uuid(project_id, "project_id")
        actor = as_uuid(actor_user_id, "actor_user_id")
        with transaction(self.database, conn, "revoke an email invitation") as cursor:
            row = cursor.execute(
                "UPDATE project_email_invitations AS i SET status = 'revoked', "
                "revoked_at = now(), revoked_by_user_id = %s, updated_at = now() "
                "WHERE i.id = %s AND i.project_id = %s AND i.status = 'pending' "
                f"RETURNING {_EMAIL_COLUMNS}",
                (actor, invitation_id, pid),
            ).fetchone()
            if row is None:
                self._raise_unusable(cursor, pid, invitation_id)
        assert row is not None
        logger.info("Email invitation revoked invitation_id=%s project_id=%s", invitation_id, pid)
        return EmailInvitation(**row)

    @staticmethod
    def _raise_unusable(
        cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID, invitation_id: int
    ) -> None:
        found = cursor.execute(
            f"SELECT {_EMAIL_STATUS} AS status FROM project_email_invitations i "
            "WHERE i.id = %s AND i.project_id = %s",
            (invitation_id, project_id),
        ).fetchone()
        if found is None:
            raise InvitationNotFoundError("No such invitation in this Project")
        raise InvitationUnavailableError(found["status"])

    def get_email_invitation(
        self, project_id: UUID | str, invitation_id: int, *, conn: psycopg.Connection | None = None
    ) -> EmailInvitation:
        """
        Returns one of the Project's invitations with its delivery state.

        Raises:
            [`db.InvitationNotFoundError`]: If the Project has no such invitation.
        """
        pid = as_uuid(project_id, "project_id")
        rows = self._email_invitations(pid, invitation_id=invitation_id, conn=conn)
        if not rows:
            raise InvitationNotFoundError("No such invitation in this Project")
        return rows[0]

    def list_email_invitations(
        self, project_id: UUID | str, *, include_closed: bool = False, limit: int = 200
    ) -> list[EmailInvitation]:
        """
        Returns the Project's invitations, newest first, each with the delivery
        state of its latest email.

        Args:
            include_closed (`bool`, *optional*): Also return accepted, declined,
                revoked and expired ones.
            limit (`int`, *optional*, defaults to 200): At most this many, 1 to 1000.
        """
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1 to 1000")
        pid = as_uuid(project_id, "project_id")
        return self._email_invitations(pid, include_closed=include_closed, limit=limit)

    def _email_invitations(
        self,
        project_id: UUID,
        *,
        invitation_id: int | None = None,
        include_closed: bool = True,
        limit: int = 1000,
        conn: psycopg.Connection | None = None,
    ) -> list[EmailInvitation]:
        delivery = ", ".join(
            f"d.{c.strip()} AS d_{c.strip()}" for c in _DELIVERY_COLUMNS.split(",")
        )
        with transaction(self.database, conn, "list email invitations") as cursor:
            rows = cursor.execute(
                f"SELECT {_EMAIL_COLUMNS}, {delivery} FROM project_email_invitations i "
                f"LEFT JOIN LATERAL (SELECT {_DELIVERY_COLUMNS} FROM email_outbox o "
                "WHERE o.subject_ref = 'invitation:' || i.id ORDER BY o.id DESC LIMIT 1) d "
                "ON true WHERE i.project_id = %s AND (%s::bigint IS NULL OR i.id = %s::bigint) "
                "AND (%s OR (i.status = 'pending' AND i.expires_at > now())) "
                "ORDER BY i.id DESC LIMIT %s",
                (project_id, invitation_id, invitation_id, include_closed, limit),
            ).fetchall()
        return [_email_invitation(row) for row in rows]

    def expire_stale(self, *, conn: psycopg.Connection | None = None) -> int:
        """
        Marks pending invitations past their expiry `expired`, recording each in
        the security audit log. Expired invitations are refused either way; this
        only makes their state explicit. Returns how many were marked.
        """
        with transaction(self.database, conn, "expire invitations") as cursor:
            cursor.execute(
                "UPDATE project_email_invitations SET status = 'expired', updated_at = now() "
                "WHERE status = 'pending' AND expires_at <= now()"
            )
            return cursor.rowcount

    def pending_for_user(self, user_id: UUID | str) -> list[PendingInvitation]:
        """
        Returns the pending, unexpired invitations addressed to any address the
        User's verified sign-in identities currently report, for live Projects
        whose issuer may still invite.
        """
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, None, "list pending invitations") as cursor:
            rows = cursor.execute(
                "SELECT i.id, i.project_id, p.name AS project_name, i.role, "
                "u.display_name AS invited_by_name, i.expires_at, i.created_at "
                "FROM project_email_invitations i "
                "JOIN projects p ON p.id = i.project_id AND p.deleted_at IS NULL "
                "LEFT JOIN users u ON u.id = i.invited_by_user_id "
                "WHERE i.status = 'pending' AND i.expires_at > now() "
                f"AND i.invited_email_normalized IN ({_VERIFIED_EMAILS}) "
                f"AND EXISTS ({_ISSUER_MAY_INVITE}) "
                "ORDER BY i.created_at, i.id",
                (uid, list(_MANAGERS)),
            ).fetchall()
        return [PendingInvitation(**row) for row in rows]

    def _find_email(
        self,
        cursor: psycopg.Cursor[dict[str, Any]],
        *,
        invitation_id: int | None,
        token_hash: bytes | None,
    ) -> dict[str, Any] | None:
        if token_hash is not None:
            return cursor.execute(
                "SELECT id, project_id FROM project_email_invitations WHERE token_hash = %s",
                (token_hash,),
            ).fetchone()
        return cursor.execute(
            "SELECT id, project_id FROM project_email_invitations WHERE id = %s",
            (invitation_id,),
        ).fetchone()

    def _addressed_to(
        self, cursor: psycopg.Cursor[dict[str, Any]], invitation: dict[str, Any], user_id: UUID
    ) -> bool:
        row = cursor.execute(
            f"SELECT 1 WHERE %s IN ({_VERIFIED_EMAILS})", (invitation["email"], user_id)
        ).fetchone()
        return row is not None

    def _locked_email(
        self,
        cursor: psycopg.Cursor[dict[str, Any]],
        user_id: UUID,
        *,
        invitation_id: int | None,
        token_hash: bytes | None,
    ) -> dict[str, Any]:
        """
        Locks the Project and then the invitation, and checks it is pending and
        addressed to the User. A lookup by ID that is not addressed to the User
        raises [`db.InvitationNotFoundError`], so IDs cannot be probed; a lookup by
        token (whose holder knows it exists) raises [`db.EmailMismatchError`].
        """
        found = self._find_email(cursor, invitation_id=invitation_id, token_hash=token_hash)
        if found is None:
            raise InvitationNotFoundError("No such invitation")
        by_id = token_hash is None
        try:
            self._lock_project(cursor, found["project_id"])
        except InvitationUnavailableError:
            if by_id:
                raise InvitationNotFoundError("No such invitation") from None
            raise
        row = cursor.execute(
            f"SELECT {_EMAIL_COLUMNS} FROM project_email_invitations i WHERE i.id = %s FOR UPDATE",
            (found["id"],),
        ).fetchone()
        assert row is not None
        if token_hash is not None and row["status"] == "pending":
            # The token may have been reissued between the lookup and the lock.
            current = cursor.execute(
                "SELECT token_hash = %s AS same FROM project_email_invitations WHERE id = %s",
                (token_hash, row["id"]),
            ).fetchone()
            if not (current and current["same"]):
                raise InvitationNotFoundError("No such invitation")
        if not self._addressed_to(cursor, row, user_id):
            if by_id:
                raise InvitationNotFoundError("No such invitation")
            raise EmailMismatchError()
        if row["status"] != "pending":
            raise InvitationUnavailableError(row["status"])
        return row

    def _accept_locked(
        self, cursor: psycopg.Cursor[dict[str, Any]], row: dict[str, Any], user_id: UUID
    ) -> Acceptance:
        self._require_active(cursor, user_id)
        if not self._issuer_may_invite(cursor, row["project_id"], row["invited_by_user_id"]):
            raise InvitationUnavailableError("issuer_not_authorized")
        with audit_context(cursor, actor_user_id=user_id):
            membership, existed = self._join(
                cursor, row["project_id"], user_id, row["role"], row["invited_by_user_id"]
            )
            cursor.execute(
                "UPDATE project_email_invitations SET status = 'accepted', accepted_at = now(), "
                "accepted_by_user_id = %s, updated_at = now() WHERE id = %s",
                (user_id, row["id"]),
            )
        logger.info(
            "Email invitation accepted invitation_id=%s project_id=%s user_id=%s role=%s "
            "already_member=%s",
            row["id"],
            row["project_id"],
            user_id,
            membership.role,
            existed,
        )
        return Acceptance(
            "email", row["id"], row["project_id"], membership.role, membership, existed
        )

    def accept_email_invitation(
        self,
        user_id: UUID | str,
        *,
        invitation_id: int | None = None,
        token_hash: bytes | None = None,
        conn: psycopg.Connection | None = None,
    ) -> Acceptance:
        """
        Accepts an email invitation for a User whose verified identity reports the
        invited address, and adds them with its role in the same transaction.

        Name the invitation by `invitation_id` (from the User's pending list) or by
        `token_hash` (from its link). A User who is already a member keeps their
        role and the invitation is marked accepted. Concurrent acceptances and
        retries are serialized on the invitation row: one wins, the rest see it
        accepted.

        Raises:
            [`db.InvitationNotFoundError`]: If there is no such invitation, or (by
                ID) it is not addressed to this User.
            [`db.EmailMismatchError`]: If (by token) it is addressed to someone else
                or the User's address is not verified.
            [`db.InvitationUnavailableError`]: If it is no longer pending, expired,
                its Project was deleted or its issuer is no longer an Owner or Admin.
        """
        uid = as_uuid(user_id, "user_id")
        if (invitation_id is None) == (token_hash is None):
            raise ValueError("Pass exactly one of invitation_id and token_hash")
        with transaction(self.database, conn, "accept an invitation") as cursor:
            row = self._locked_email(
                cursor, uid, invitation_id=invitation_id, token_hash=token_hash
            )
            return self._accept_locked(cursor, row, uid)

    def decline_email_invitation(
        self,
        user_id: UUID | str,
        *,
        invitation_id: int | None = None,
        token_hash: bytes | None = None,
        conn: psycopg.Connection | None = None,
    ) -> EmailInvitation:
        """
        Declines an email invitation addressed to the User. Changes no membership.

        Raises:
            The same errors as [`db.InvitationStore.accept_email_invitation`].
        """
        uid = as_uuid(user_id, "user_id")
        if (invitation_id is None) == (token_hash is None):
            raise ValueError("Pass exactly one of invitation_id and token_hash")
        with transaction(self.database, conn, "decline an invitation") as cursor:
            row = self._locked_email(
                cursor, uid, invitation_id=invitation_id, token_hash=token_hash
            )
            with audit_context(cursor, actor_user_id=uid):
                declined = cursor.execute(
                    "UPDATE project_email_invitations AS i SET status = 'declined', "
                    "declined_at = now(), updated_at = now() WHERE i.id = %s "
                    f"RETURNING {_EMAIL_COLUMNS}",
                    (row["id"],),
                ).fetchone()
        assert declined is not None
        logger.info(
            "Email invitation declined invitation_id=%s project_id=%s user_id=%s",
            row["id"],
            row["project_id"],
            uid,
        )
        return EmailInvitation(**declined)

    def redeem_for_new_user(
        self, user_id: UUID | str, *, conn: psycopg.Connection | None = None
    ) -> list[Acceptance]:
        """
        Accepts every eligible pending invitation addressed to the User's verified
        addresses, at their first sign-in.

        Run it in the sign-in transaction, so the User, their identity and their
        new memberships commit together or not at all. Each invitation is tried in
        its own savepoint: one that is no longer usable (expired, revoked, already
        accepted by a concurrent first sign-in, issuer no longer authorized, Project
        deleted) is skipped and left as it is. Projects are locked in ID order, the
        same order for every caller.

        Returns:
            `list[Acceptance]`: The invitations accepted.
        """
        uid = as_uuid(user_id, "user_id")
        accepted: list[Acceptance] = []
        with transaction(self.database, conn, "redeem invitations at sign-in") as cursor:
            candidates = cursor.execute(
                "SELECT id FROM project_email_invitations WHERE status = 'pending' "
                f"AND expires_at > now() AND invited_email_normalized IN ({_VERIFIED_EMAILS}) "
                "ORDER BY project_id, id",
                (uid,),
            ).fetchall()
            for candidate in candidates:
                try:
                    with cursor.connection.transaction():
                        row = self._locked_email(
                            cursor, uid, invitation_id=candidate["id"], token_hash=None
                        )
                        accepted.append(self._accept_locked(cursor, row, uid))
                except InvitationError as exc:
                    logger.info(
                        "Invitation skipped at first sign-in invitation_id=%s reason=%s",
                        candidate["id"],
                        getattr(exc, "reason", "not_found"),
                    )
        return accepted

    # Invite Links --------------------------------------------------------------

    def create_invite_link(
        self,
        project_id: UUID | str,
        role: str,
        *,
        created_by_user_id: UUID | str,
        token_hash: bytes,
        ttl_seconds: float,
        max_uses: int,
        conn: psycopg.Connection | None = None,
    ) -> InviteLink:
        """
        Creates an Invite Link.

        Raises:
            [`db.OwnerRuleError`]: If `role` is `owner`.
            [`db.InvitationUnavailableError`]: If the Project was deleted.
            `ValueError`: If the role, lifetime or use limit is invalid.
        """
        pid = as_uuid(project_id, "project_id")
        creator = as_uuid(created_by_user_id, "created_by_user_id")
        _assignable(role)
        ttl = _ttl(ttl_seconds)
        if isinstance(max_uses, bool) or not 1 <= max_uses <= MAX_LINK_USES:
            raise ValueError(f"max_uses must be 1 to {MAX_LINK_USES}")
        with transaction(self.database, conn, "create an Invite Link") as cursor:
            self._lock_project(cursor, pid)
            row = cursor.execute(
                "INSERT INTO project_invite_links AS l (project_id, role, created_by_user_id, "
                "token_hash, expires_at, max_uses) "
                "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s), %s) "
                f"RETURNING {_LINK_COLUMNS}, now() AS now",
                (pid, role, creator, token_hash, ttl, max_uses),
            ).fetchone()
        assert row is not None
        logger.info(
            "Invite Link created invite_link_id=%s project_id=%s role=%s max_uses=%s",
            row["id"],
            pid,
            role,
            max_uses,
        )
        return InviteLink(**row)

    def revoke_invite_link(
        self,
        project_id: UUID | str,
        link_id: int,
        *,
        actor_user_id: UUID | str,
        conn: psycopg.Connection | None = None,
    ) -> InviteLink:
        """
        Revokes an Invite Link. Memberships already granted through it stay.

        Raises:
            [`db.InvitationNotFoundError`]: If the Project has no such link.
            [`db.InvitationUnavailableError`]: `revoked` if it already was.
        """
        pid = as_uuid(project_id, "project_id")
        actor = as_uuid(actor_user_id, "actor_user_id")
        with transaction(self.database, conn, "revoke an Invite Link") as cursor:
            row = cursor.execute(
                "UPDATE project_invite_links AS l SET revoked_at = now(), "
                "revoked_by_user_id = %s WHERE l.id = %s AND l.project_id = %s "
                f"AND l.revoked_at IS NULL RETURNING {_LINK_COLUMNS}, now() AS now",
                (actor, link_id, pid),
            ).fetchone()
            if row is None:
                exists = cursor.execute(
                    "SELECT 1 FROM project_invite_links WHERE id = %s AND project_id = %s",
                    (link_id, pid),
                ).fetchone()
                if exists is None:
                    raise InvitationNotFoundError("No such Invite Link in this Project")
                raise InvitationUnavailableError("revoked")
        logger.info("Invite Link revoked invite_link_id=%s project_id=%s", link_id, pid)
        return InviteLink(**row)

    def list_invite_links(
        self, project_id: UUID | str, *, include_closed: bool = False, limit: int = 200
    ) -> list[InviteLink]:
        """Returns the Project's Invite Links, newest first; by default only usable ones."""
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1 to 1000")
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "list Invite Links") as cursor:
            rows = cursor.execute(
                f"SELECT {_LINK_COLUMNS}, now() AS now FROM project_invite_links l "
                "WHERE l.project_id = %s AND (%s OR (l.revoked_at IS NULL "
                "AND l.use_count < l.max_uses AND l.expires_at > now())) "
                "ORDER BY l.id DESC LIMIT %s",
                (pid, include_closed, limit),
            ).fetchall()
        return [InviteLink(**row) for row in rows]

    def redeem_invite_link(
        self, user_id: UUID | str, *, token_hash: bytes, conn: psycopg.Connection | None = None
    ) -> Acceptance:
        """
        Accepts an Invite Link for a signed-in User: records the redemption,
        counts the use and adds the membership in one transaction.

        The link row is locked, so concurrent redemptions are counted one at a time
        and a link never admits more than `max_uses` Users. A User who is already a
        member, or already redeemed the link, uses up nothing.

        Raises:
            [`db.InvitationNotFoundError`]: If no link has this token.
            [`db.DuplicateMemberError`]: If the User is already a member.
            [`db.InvitationUnavailableError`]: `revoked`, `expired`, `exhausted`,
                `already_redeemed`, `project_deleted`, `issuer_not_authorized` or
                `user_disabled`.
        """
        uid = as_uuid(user_id, "user_id")
        with transaction(self.database, conn, "redeem an Invite Link") as cursor:
            found = cursor.execute(
                "SELECT id, project_id FROM project_invite_links WHERE token_hash = %s",
                (token_hash,),
            ).fetchone()
            if found is None:
                raise InvitationNotFoundError("No such Invite Link")
            self._lock_project(cursor, found["project_id"])
            link = cursor.execute(
                f"SELECT {_LINK_COLUMNS}, now() AS now FROM project_invite_links l "
                "WHERE l.id = %s FOR UPDATE",
                (found["id"],),
            ).fetchone()
            assert link is not None
            status = InviteLink(**link).status
            if status != "active":
                raise InvitationUnavailableError(status)
            self._require_active(cursor, uid)
            if not self._issuer_may_invite(cursor, link["project_id"], link["created_by_user_id"]):
                raise InvitationUnavailableError("issuer_not_authorized")
            member = cursor.execute(
                "SELECT 1 FROM project_memberships WHERE project_id = %s AND user_id = %s",
                (link["project_id"], uid),
            ).fetchone()
            if member is not None:
                raise DuplicateMemberError("The User is already a member of the Project")
            with audit_context(cursor, actor_user_id=uid):
                redeemed = cursor.execute(
                    "INSERT INTO project_invitation_redemptions (invite_link_id, user_id) "
                    "VALUES (%s, %s) ON CONFLICT (invite_link_id, user_id) DO NOTHING "
                    "RETURNING id",
                    (link["id"], uid),
                ).fetchone()
                if redeemed is None:
                    raise InvitationUnavailableError("already_redeemed")
                counted = cursor.execute(
                    "UPDATE project_invite_links SET use_count = use_count + 1 "
                    "WHERE id = %s AND use_count < max_uses RETURNING use_count",
                    (link["id"],),
                ).fetchone()
                if counted is None:
                    raise InvitationUnavailableError("exhausted")
                membership, _ = self._join(
                    cursor, link["project_id"], uid, link["role"], link["created_by_user_id"]
                )
        logger.info(
            "Invite Link redeemed invite_link_id=%s project_id=%s user_id=%s role=%s uses=%s/%s",
            link["id"],
            link["project_id"],
            uid,
            link["role"],
            counted["use_count"],
            link["max_uses"],
        )
        return Acceptance("link", link["id"], link["project_id"], membership.role, membership)

    # Tokens --------------------------------------------------------------------

    def preview(self, token_hash: bytes) -> InvitationPreview:
        """
        Describes what a token grants, without changing anything.

        Raises:
            [`db.InvitationNotFoundError`]: If no invitation or link has this token,
                or its Project was deleted.
        """
        with transaction(self.database, None, "describe an invitation") as cursor:
            row = cursor.execute(
                f"SELECT 'email' AS kind, i.id, i.project_id, p.name AS project_name, i.role, "
                f"{_EMAIL_STATUS} AS status, i.expires_at "
                "FROM project_email_invitations i JOIN projects p ON p.id = i.project_id "
                "AND p.deleted_at IS NULL WHERE i.token_hash = %s",
                (token_hash,),
            ).fetchone()
            if row is None:
                link = cursor.execute(
                    f"SELECT {_LINK_COLUMNS}, now() AS now, p.name AS project_name "
                    "FROM project_invite_links l JOIN projects p ON p.id = l.project_id "
                    "AND p.deleted_at IS NULL WHERE l.token_hash = %s",
                    (token_hash,),
                ).fetchone()
                if link is None:
                    raise InvitationNotFoundError("No such invitation")
                name = link.pop("project_name")
                parsed = InviteLink(**link)
                return InvitationPreview(
                    kind="link",
                    id=parsed.id,
                    project_id=parsed.project_id,
                    project_name=name,
                    role=parsed.role,
                    status=parsed.status,
                    expires_at=parsed.expires_at,
                )
        return InvitationPreview(**row)

    def token_kind(self, token_hash: bytes) -> str | None:
        """`email`, `link`, or `None` when no invitation or link has this token."""
        with transaction(self.database, None, "look up an invitation") as cursor:
            row = cursor.execute(
                "SELECT 'email' AS kind FROM project_email_invitations WHERE token_hash = %s "
                "UNION ALL SELECT 'link' FROM project_invite_links WHERE token_hash = %s LIMIT 1",
                (token_hash, token_hash),
            ).fetchone()
        return row["kind"] if row else None
