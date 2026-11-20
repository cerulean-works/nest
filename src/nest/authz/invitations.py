"""
Email invitations, Invite Links and membership onboarding, each authorized on
the server.

[`authz.InvitationService`] is the one entry point for every interface. Issuing an
invitation is checked as [`authz.Action`] `MEMBER_INVITE` with the role it grants,
so only an Owner or Admin may issue one and nobody can grant `owner`; listing,
resending and revoking are `INVITATION_MANAGE`, for Owners and Admins. Each check
holds the Project lock while the change is made, in one transaction.

Redeeming needs no Project role: an email invitation is redeemed by the User whose
verified sign-in identity reports the invited address, and an Invite Link by any
signed-in User who explicitly accepts it. Both need a browser session; opening a
link changes nothing. A Project role, however obtained, never grants Provider or
HPC authority (see [`authz.ProviderAuthority`]).

Email is optional. When it is enabled, issuing and resending queue the invitation
email in the same transaction ([`mail.EmailService`]); either way the caller gets
the recipient-bound link once, to copy and share manually.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from uuid import UUID

import psycopg

from .._logging import get_logger
from ..db._transaction import as_uuid, transaction
from ..db.connection import Database
from ..db.identity import hash_token
from ..db.invitations import (
    MAX_INVITATION_TTL,
    Acceptance,
    EmailInvitation,
    EmailMismatchError,
    InvitationNotFoundError,
    InvitationPreview,
    InvitationStore,
    InvitationUnavailableError,
    InviteLink,
    PendingInvitation,
)
from ..mail import INVITATION_KIND, EmailService, invitation_link, render_invitation
from .policy import Action, Decision, Principal, session_allows
from .service import AccessDeniedError, Authorizer, ProjectService

logger = get_logger("nest.authz.invitations")

DEFAULT_INVITATION_TTL = 7 * 86_400.0
"""Default lifetime of an email invitation or Invite Link: 7 days."""

DEFAULT_LINK_MAX_USES = 1
"""Default use limit of an Invite Link."""

TOKEN_BYTES = 32
"""Random bytes in each invitation token (256 bits)."""

_ROLE_NAMES = {"admin": "Admin", "member": "Member", "viewer": "Viewer"}


def new_token() -> str:
    """A fresh high-entropy invitation token. Only its SHA-256 is ever stored."""
    return secrets.token_urlsafe(TOKEN_BYTES)


@dataclass(frozen=True)
class IssuedInvitation:
    """
    An email invitation just issued or resent, with its link. The link holds the
    raw token: it is returned only here, once, and never stored or logged.

    Attributes:
        invitation ([`db.EmailInvitation`]): The invitation and delivery state.
        link (`str`): The recipient-bound link, to copy and share manually.
        emailed (`bool`): Whether an email was queued for delivery.
    """

    invitation: EmailInvitation
    link: str
    emailed: bool

    def __repr__(self) -> str:
        return f"IssuedInvitation(invitation={self.invitation!r}, emailed={self.emailed})"


@dataclass(frozen=True)
class IssuedInviteLink:
    """
    An Invite Link just created, with its URL. The URL holds the raw token: it is
    returned only here, once, and never stored or logged.

    Attributes:
        invite_link ([`db.InviteLink`]): The link's record.
        link (`str`): The URL to share.
    """

    invite_link: InviteLink
    link: str

    def __repr__(self) -> str:
        return f"IssuedInviteLink(invite_link={self.invite_link!r})"


def _ttl(ttl_seconds: float | None) -> float:
    ttl = DEFAULT_INVITATION_TTL if ttl_seconds is None else float(ttl_seconds)
    if not 60 <= ttl <= MAX_INVITATION_TTL:
        raise ValueError(f"The lifetime must be 60 to {MAX_INVITATION_TTL} seconds")
    return ttl


class InvitationService:
    """
    Invitation and Invite Link operations, each authorized for its caller.

    Args:
        database ([`db.Database`]): Open database.
        projects ([`authz.ProjectService`], *optional*): Defaults to one on `database`.
        email ([`mail.EmailService`], *optional*): Email delivery. Defaults to a
            disabled service: invitations are then shared by their link only.
        link_base_url (`str`, *optional*): Base of links when email is disabled,
            such as the auth listener's public origin. With email enabled,
            `NEST_PUBLIC_URL` is used.
    """

    def __init__(
        self,
        database: Database,
        projects: ProjectService | None = None,
        email: EmailService | None = None,
        *,
        link_base_url: str | None = None,
    ):
        self.database = database
        self.projects = projects or ProjectService(database)
        self.authorizer: Authorizer = self.projects.authorizer
        self.email = email or EmailService()
        self.store = InvitationStore(database)
        if self.email.enabled and self.email.config is not None:
            self.link_base_url = self.email.config.public_url
        elif link_base_url:
            self.link_base_url = link_base_url.rstrip("/")
        else:
            raise ValueError("link_base_url is required when email delivery is disabled")

    def _link(self, token: str) -> str:
        return invitation_link(self.link_base_url, token)

    def _refuse(
        self,
        principal: Principal,
        event: str,
        reason: str,
        *,
        project_id: UUID | None = None,
        resource: tuple[str, str] | None = None,
    ) -> None:
        self.authorizer.denied(
            principal,
            event,
            project_id,
            reason,
            {"resource_type": resource[0], "resource_id": resource[1]} if resource else None,
        )

    def _browser_only(self, principal: Principal, event: str) -> None:
        if principal.session_kind != "browser":
            decision = Decision(False, "browser_only")
            self._refuse(principal, event, decision.reason)
            raise AccessDeniedError(event, decision)

    # Issuing -------------------------------------------------------------------

    def _queue_email(
        self,
        conn: psycopg.Connection,
        invitation: EmailInvitation,
        token: str,
        inviter: UUID,
    ) -> bool:
        if not self.email.enabled or self.email.config is None:
            return False
        with transaction(self.database, conn, "prepare an invitation email") as cursor:
            row = cursor.execute(
                "SELECT p.name, u.display_name, "
                "(SELECT count(*) FROM email_outbox WHERE subject_ref = %s) AS sent "
                "FROM projects p, users u WHERE p.id = %s AND u.id = %s",
                (f"invitation:{invitation.id}", invitation.project_id, inviter),
            ).fetchone()
        assert row is not None
        rendered = render_invitation(
            self.email.config,
            token=token,
            project_name=row["name"],
            role=_ROLE_NAMES.get(invitation.role, invitation.role),
            inviter_name=row["display_name"],
            expires=invitation.expires_at.strftime("on %Y-%m-%d at %H:%M UTC"),
        )
        self.email.queue(
            message_key=f"invitation:{invitation.id}:send:{row['sent'] + 1}",
            kind=INVITATION_KIND,
            recipient=invitation.email,
            subject=rendered.subject,
            body_text=rendered.body_text,
            subject_ref=f"invitation:{invitation.id}",
            conn=conn,
        )
        return True

    def invite(
        self,
        principal: Principal,
        project_id: UUID | str,
        email: str,
        role: str,
        *,
        ttl_seconds: float | None = None,
        send_email: bool = True,
    ) -> IssuedInvitation:
        """
        Invites an address, registered or not, with `role`. Owners and Admins only.

        No User or membership is created: the invitation stays `pending` until the
        address's owner signs in (first sign-in redeems it at once) or accepts it.
        With email enabled and `send_email`, the invitation email is queued in the
        same transaction; a later delivery failure never changes the invitation.

        Raises:
            [`authz.AccessDeniedError`]: If not allowed, including for `owner`.
            [`db.AlreadyInvitedError`]: If the address has a pending invitation.
            [`db.DuplicateMemberError`]: If a member already signs in with it.
            `ValueError`: If the address or lifetime is invalid.
        """
        ttl = _ttl(ttl_seconds)
        token = new_token()
        with self.projects._authorized(
            principal,
            project_id,
            Action.MEMBER_INVITE,
            "issue an email invitation",
            new_role=role,
        ) as conn:
            invitation = self.store.create_email_invitation(
                project_id,
                email,
                role,
                invited_by_user_id=principal.user_id,
                token_hash=hash_token(token),
                ttl_seconds=ttl,
                conn=conn,
            )
            emailed = send_email and self._queue_email(conn, invitation, token, principal.user_id)
        return IssuedInvitation(
            self.store.get_email_invitation(invitation.project_id, invitation.id),
            self._link(token),
            emailed,
        )

    def resend(
        self,
        principal: Principal,
        project_id: UUID | str,
        invitation_id: int,
        *,
        send_email: bool = True,
    ) -> IssuedInvitation:
        """
        Issues a new link for a pending invitation and, with email enabled, queues
        it again; the previous link stops working. Use it to retry a failed
        delivery or to redistribute the link manually. Owners and Admins only.

        Raises:
            [`authz.AccessDeniedError`]: If not allowed.
            [`db.InvitationNotFoundError`]: If the Project has no such invitation.
            [`db.InvitationUnavailableError`]: If it is no longer pending.
        """
        token = new_token()
        with self.projects._authorized(
            principal, project_id, Action.INVITATION_MANAGE, "resend an invitation"
        ) as conn:
            invitation = self.store.reissue_token(
                project_id, invitation_id, token_hash=hash_token(token), conn=conn
            )
            emailed = send_email and self._queue_email(conn, invitation, token, principal.user_id)
        return IssuedInvitation(
            self.store.get_email_invitation(invitation.project_id, invitation.id),
            self._link(token),
            emailed,
        )

    def revoke(
        self, principal: Principal, project_id: UUID | str, invitation_id: int
    ) -> EmailInvitation:
        """Revokes a pending invitation. Owners and Admins only."""
        with self.projects._authorized(
            principal, project_id, Action.INVITATION_MANAGE, "revoke an invitation"
        ) as conn:
            return self.store.revoke_email_invitation(
                project_id, invitation_id, actor_user_id=principal.user_id, conn=conn
            )

    def invitations(
        self, principal: Principal, project_id: UUID | str, *, include_closed: bool = False
    ) -> list[EmailInvitation]:
        """
        The Project's email invitations with their delivery state, newest first.
        Owners and Admins only.
        """
        pid = as_uuid(project_id, "project_id")
        self.authorizer.require(principal, Action.INVITATION_MANAGE, pid)
        self.store.expire_stale()
        return self.store.list_email_invitations(pid, include_closed=include_closed)

    def create_link(
        self,
        principal: Principal,
        project_id: UUID | str,
        role: str,
        *,
        max_uses: int = DEFAULT_LINK_MAX_USES,
        ttl_seconds: float | None = None,
    ) -> IssuedInviteLink:
        """
        Creates an Invite Link granting `role` to up to `max_uses` Users who accept
        it while signed in. Owners and Admins only; never `owner`.

        Raises:
            [`authz.AccessDeniedError`]: If not allowed.
            `ValueError`: If the use limit or lifetime is invalid.
        """
        ttl = _ttl(ttl_seconds)
        token = new_token()
        with self.projects._authorized(
            principal, project_id, Action.MEMBER_INVITE, "create an Invite Link", new_role=role
        ) as conn:
            link = self.store.create_invite_link(
                project_id,
                role,
                created_by_user_id=principal.user_id,
                token_hash=hash_token(token),
                ttl_seconds=ttl,
                max_uses=max_uses,
                conn=conn,
            )
        return IssuedInviteLink(link, self._link(token))

    def revoke_link(self, principal: Principal, project_id: UUID | str, link_id: int) -> InviteLink:
        """Revokes an Invite Link. Owners and Admins only."""
        with self.projects._authorized(
            principal, project_id, Action.INVITATION_MANAGE, "revoke an Invite Link"
        ) as conn:
            return self.store.revoke_invite_link(
                project_id, link_id, actor_user_id=principal.user_id, conn=conn
            )

    def links(
        self, principal: Principal, project_id: UUID | str, *, include_closed: bool = False
    ) -> list[InviteLink]:
        """The Project's Invite Links, newest first. Owners and Admins only."""
        pid = as_uuid(project_id, "project_id")
        self.authorizer.require(principal, Action.INVITATION_MANAGE, pid)
        return self.store.list_invite_links(pid, include_closed=include_closed)

    # Redeeming -----------------------------------------------------------------

    def pending(self, principal: Principal) -> list[PendingInvitation]:
        """
        The caller's own pending invitations: those addressed to an address their
        verified sign-in identities report. A CLI session needs `projects:read`.
        """
        decision = session_allows(principal, Action.PROJECT_READ)
        if not decision:
            self._refuse(principal, "invitation.list", decision.reason)
            raise AccessDeniedError("invitation.list", decision)
        return self.store.pending_for_user(principal.user_id)

    def _redeemed(
        self,
        principal: Principal,
        event: str,
        call,
        *,
        resource: tuple[str, str] | None = None,
    ):
        try:
            return call()
        except (EmailMismatchError, InvitationUnavailableError) as exc:
            self._refuse(principal, event, exc.reason, resource=resource)
            raise
        except InvitationNotFoundError:
            self._refuse(principal, event, "not_found", resource=resource)
            raise

    def accept(self, principal: Principal, invitation_id: int) -> Acceptance:
        """
        Accepts one of the caller's pending invitations, by ID. Browser sessions
        only. An invitation not addressed to the caller is reported as not found.

        Raises:
            [`authz.AccessDeniedError`]: If the session is not a browser session.
            [`db.InvitationNotFoundError`], [`db.InvitationUnavailableError`].
        """
        self._browser_only(principal, "invitation.accept")
        return self._redeemed(
            principal,
            "invitation.accept",
            lambda: self.store.accept_email_invitation(
                principal.user_id, invitation_id=invitation_id
            ),
            resource=("invitation", str(invitation_id)),
        )

    def decline(self, principal: Principal, invitation_id: int) -> EmailInvitation:
        """Declines one of the caller's pending invitations, by ID. Browser sessions only."""
        self._browser_only(principal, "invitation.decline")
        return self._redeemed(
            principal,
            "invitation.decline",
            lambda: self.store.decline_email_invitation(
                principal.user_id, invitation_id=invitation_id
            ),
            resource=("invitation", str(invitation_id)),
        )

    def preview(self, principal: Principal, token: str) -> InvitationPreview:
        """
        Describes what a link's token grants, so the caller can decide. Changes
        nothing. Browser sessions only.

        Raises:
            [`db.InvitationNotFoundError`]: If the token is unknown.
        """
        self._browser_only(principal, "invitation.preview")
        return self._redeemed(
            principal, "invitation.preview", lambda: self.store.preview(hash_token(token))
        )

    def redeem(
        self, principal: Principal, token: str, *, accept: bool = True
    ) -> Acceptance | EmailInvitation:
        """
        Accepts (or, for an email invitation, declines) the invitation or Invite
        Link a token names, on the caller's explicit request. Browser sessions only.

        Returns:
            [`db.Acceptance`] when accepted, or the declined [`db.EmailInvitation`].

        Raises:
            [`authz.AccessDeniedError`]: If the session is not a browser session.
            [`db.InvitationNotFoundError`]: If the token is unknown.
            [`db.EmailMismatchError`]: If an email invitation is for another address.
            [`db.InvitationUnavailableError`]: If it cannot be used now.
            [`db.DuplicateMemberError`]: If, for an Invite Link, the caller is a
                member already.
            `ValueError`: If declining an Invite Link.
        """
        event = "invitation.accept" if accept else "invitation.decline"
        self._browser_only(principal, event)
        token_hash = hash_token(token)
        kind = self.store.token_kind(token_hash)
        if kind is None:
            self._refuse(principal, event, "not_found")
            raise InvitationNotFoundError("No such invitation")
        if kind == "link":
            if not accept:
                raise ValueError("An Invite Link cannot be declined; just do not accept it")
            return self._redeemed(
                principal,
                "invite_link.redeem",
                lambda: self.store.redeem_invite_link(principal.user_id, token_hash=token_hash),
            )
        if accept:
            return self._redeemed(
                principal,
                event,
                lambda: self.store.accept_email_invitation(
                    principal.user_id, token_hash=token_hash
                ),
            )
        return self._redeemed(
            principal,
            event,
            lambda: self.store.decline_email_invitation(principal.user_id, token_hash=token_hash),
        )
