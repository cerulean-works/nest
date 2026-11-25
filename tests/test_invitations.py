"""Project email invitations, Invite Links and membership onboarding (B08).

The service tests call `nest.authz.InvitationService` and `nest.db.InvitationStore`
against a real PostgreSQL (see `postgres_fixture`). The HTTP tests drive the
invitation API, the invitation page and Google sign-in (with the fake provider in
`fake_oidc`) on the Controller's auth listener over real HTTP.
"""

import base64
import hashlib
import json
import logging
import secrets
import threading
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import urlencode, urlsplit

from admin_fixture import owned_project
from fake_oidc import CLIENT_ID, CLIENT_SECRET, Account, FakeGoogle
from test_authz import ROLES, _Base, browser, cli
from test_email import email_settings, enabled
from test_google_auth import Browser, free_port

from nest.audit import AuditLog
from nest.auth import INSECURE_SESSION_COOKIE, LOGIN_PATH, GoogleSignIn, start_auth_service
from nest.authz import Action, InvitationService
from nest.config import ConfigError, load_controller_config
from nest.db import (
    Acceptance,
    AlreadyInvitedError,
    DuplicateMemberError,
    EmailMismatchError,
    EmailOutbox,
    InvitationNotFoundError,
    InvitationStore,
    InvitationUnavailableError,
    SessionStore,
    hash_token,
)
from nest.mail import EmailService

ISSUER = "https://accounts.google.com"
INVITEE = "Invitee.Private@Example.ORG"
NORMALIZED = "invitee.private@example.org"


def token_of(link: str) -> str:
    """The raw token in an invitation link's fragment."""
    fragment = urlsplit(link).fragment
    assert fragment.startswith("token="), link
    return fragment.removeprefix("token=")


class _InvitationBase(_Base):
    def setUp(self):
        super().setUp()
        self.invitations = InvitationService(
            self.db, self.service, link_base_url="https://nest.test"
        )
        self.inv = InvitationStore(self.db)

    def signed_up(self, email: str = INVITEE, *, verified: bool = True, name: str = "u"):
        """An existing User whose sign-in identity reports `email`."""
        user = self.user(name)
        self.identities.link_identity(
            user,
            issuer=ISSUER,
            subject=secrets.token_hex(8),
            email=email,
            email_verified=verified,
        )
        return user

    def first_sign_in(self, email: str = INVITEE, *, verified: bool = True):
        """A first sign-in, as GoogleSignIn.finish runs it: one transaction."""
        with self.db.connection() as conn:
            user, _, created = self.identities.sign_in(
                issuer=ISSUER,
                subject=secrets.token_hex(8),
                email=email,
                email_verified=verified,
                conn=conn,
            )
            self.assertTrue(created)
            joined = self.inv.redeem_for_new_user(user.id, conn=conn)
        return user.id, joined

    def count(self, sql: str, *args) -> int:
        row = self.conn.execute(sql, args).fetchone()
        assert row is not None
        return row[0]

    def audit(self, *prefixes: str):
        rows = self.conn.execute(
            "SELECT event, outcome, actor_user_id, resource_id, reason, details::text "
            "FROM security_audit_events ORDER BY id"
        ).fetchall()
        return [r for r in rows if not prefixes or r[0].startswith(prefixes)]

    def expire(self, table: str, row_id: int):
        self.conn.execute(
            f"UPDATE {table} SET created_at = now() - interval '2 days', "
            "expires_at = now() - interval '1 day' WHERE id = %s",
            (row_id,),
        )


class EmailInvitationTests(_InvitationBase):
    def test_only_owners_and_admins_invite_and_never_as_owner(self):
        pid, users = self.team()
        for role in (*ROLES, "outsider"):
            for granted in ("admin", "member", "viewer", "owner"):
                with self.subTest(inviter=role, role=granted):
                    call = partial(
                        self.invitations.invite,
                        browser(users[role]),
                        pid,
                        f"{role}.{granted}@example.org",
                        granted,
                    )
                    if granted == "owner":
                        reason = "owner_by_transfer_only" if role in ("owner", "admin") else None
                        error = self.denied(call, reason)
                        if role == "outsider":
                            self.assertEqual(error.reason, "not_a_member")
                    elif role in ("owner", "admin"):
                        issued = call()
                        self.assertEqual(issued.invitation.status, "pending")
                    else:
                        self.denied(call, "not_a_member" if role == "outsider" else "role")
        # The management actions follow the same matrix.
        for role in ("member", "viewer", "outsider"):
            with self.subTest(manager=role):
                who = browser(users[role])
                self.denied(partial(self.invitations.invitations, who, pid))
                self.denied(partial(self.invitations.links, who, pid))
                self.denied(partial(self.invitations.create_link, who, pid, "viewer"))
        # A CLI session, whatever its scopes, cannot manage invitations.
        token = cli(users["owner"], "projects:read", "jobs:run")
        self.denied(lambda: self.invitations.invite(token, pid, "a@example.org", "viewer"))
        self.denied(lambda: self.invitations.invitations(token, pid), "browser_only")

    def test_unregistered_invitee_is_pending_without_a_user_or_membership(self):
        pid, users = self.team()
        before = (self.count("SELECT count(*) FROM users"), len(self.service.store.members(pid)))
        issued = self.invitations.invite(browser(users["admin"]), pid, INVITEE, "member")
        self.assertEqual(issued.invitation.email, NORMALIZED)
        self.assertFalse(issued.emailed)
        self.assertIsNone(issued.invitation.delivery)
        self.assertTrue(issued.link.startswith("https://nest.test/invitations/accept#token="))
        after = (self.count("SELECT count(*) FROM users"), len(self.service.store.members(pid)))
        self.assertEqual(before, after)
        # Only the token's SHA-256 is stored.
        token = token_of(issued.link)
        stored = self.conn.execute(
            "SELECT token_hash FROM project_email_invitations WHERE id = %s",
            (issued.invitation.id,),
        ).fetchone()
        assert stored is not None
        self.assertEqual(bytes(stored[0]), hash_token(token))
        self.assertNotIn(token, repr(issued))

    def test_first_verified_sign_in_redeems_matching_invitations_atomically(self):
        pid, users = self.team()
        other, other_users = self.team()
        self.invitations.invite(browser(users["owner"]), pid, INVITEE, "member")
        self.invitations.invite(browser(other_users["owner"]), other, INVITEE.upper(), "viewer")
        self.invitations.invite(browser(users["owner"]), pid, "someone@example.org", "admin")
        user, joined = self.first_sign_in(" invitee.private@EXAMPLE.org ")
        self.assertEqual(
            sorted((a.project_id, a.role) for a in joined),
            sorted([(pid, "member"), (other, "viewer")]),
        )
        self.assertEqual(self.service.store.role_of(pid, user), "member")
        self.assertEqual({p.id for p, _ in self.service.list_projects(browser(user))}, {pid, other})
        # Repeating the redemption (a repeated first-login event) changes nothing.
        with self.db.connection() as conn:
            self.assertEqual(self.inv.redeem_for_new_user(user, conn=conn), [])
        self.assertEqual(
            self.count("SELECT count(*) FROM project_memberships WHERE user_id = %s", user),
            2,
        )

    def test_unverified_or_nonmatching_accounts_cannot_redeem(self):
        pid, users = self.team()
        issued = self.invitations.invite(browser(users["owner"]), pid, INVITEE, "member")
        token = token_of(issued.link)
        unverified, joined = self.first_sign_in(verified=False)
        self.assertEqual(joined, [])
        stranger = self.signed_up("stranger@example.org", name="stranger")
        for user in (unverified, stranger):
            with self.subTest(user=user):
                # By ID the invitation is invisible; by token the holder learns why.
                with self.assertRaises(InvitationNotFoundError):
                    self.invitations.accept(browser(user), issued.invitation.id)
                with self.assertRaises(EmailMismatchError):
                    self.invitations.redeem(browser(user), token)
                self.assertEqual(self.invitations.pending(browser(user)), [])
                self.assertIsNone(self.service.store.role_of(pid, user))
        # A sign-in whose verified address changed away from the invited one loses it.
        moved = self.signed_up(name="moved")
        self.assertEqual(len(self.invitations.pending(browser(moved))), 1)
        self.conn.execute(
            "UPDATE user_identities SET email = 'new@example.org' WHERE user_id = %s", (moved,)
        )
        self.assertEqual(self.invitations.pending(browser(moved)), [])

    def test_existing_user_accepts_or_declines_pending_invitations(self):
        pid, users = self.team()
        other, other_users = self.team()
        invitee = self.signed_up(name="invitee")
        first = self.invitations.invite(browser(users["owner"]), pid, INVITEE, "viewer")
        second = self.invitations.invite(browser(other_users["admin"]), other, INVITEE, "member")
        pending = self.invitations.pending(browser(invitee))
        self.assertEqual([p.id for p in pending], [first.invitation.id, second.invitation.id])
        self.assertEqual(pending[0].invited_by_name, "owner")
        # Pending is not membership.
        self.assertIsNone(self.service.store.role_of(pid, invitee))
        accepted = self.invitations.accept(browser(invitee), first.invitation.id)
        self.assertEqual((accepted.project_id, accepted.role), (pid, "viewer"))
        self.assertEqual(self.service.store.role_of(pid, invitee), "viewer")
        declined = self.invitations.decline(browser(invitee), second.invitation.id)
        self.assertEqual(declined.status, "declined")
        self.assertIsNone(self.service.store.role_of(other, invitee))
        self.assertEqual(self.invitations.pending(browser(invitee)), [])
        for call in (self.invitations.accept, self.invitations.decline):
            with self.assertRaises(InvitationUnavailableError) as caught:
                call(browser(invitee), second.invitation.id)
            self.assertEqual(caught.exception.reason, "declined")
        # Accepting needs a browser session.
        third = self.invitations.invite(browser(users["owner"]), pid, "x@example.org", "viewer")
        self.denied(
            lambda: self.invitations.accept(cli(invitee, "projects:read"), third.invitation.id),
            "browser_only",
        )
        self.assertEqual(len(self.invitations.pending(cli(invitee, "projects:read"))), 0)

    def test_duplicates_expiry_and_revocation(self):
        pid, users = self.team()
        owner = browser(users["owner"])
        issued = self.invitations.invite(owner, pid, INVITEE, "member")
        with self.assertRaises(AlreadyInvitedError) as caught:
            self.invitations.invite(browser(users["admin"]), pid, INVITEE.lower(), "admin")
        self.assertEqual(caught.exception.invitation_id, issued.invitation.id)
        # A member who already signs in with the address cannot be invited again.
        member_email = "member@example.org"
        self.identities.link_identity(
            users["member"], issuer=ISSUER, subject="m", email=member_email, email_verified=True
        )
        with self.assertRaises(DuplicateMemberError):
            self.invitations.invite(owner, pid, member_email, "viewer")
        with self.assertRaises(ValueError):
            self.invitations.invite(owner, pid, "not an address", "viewer")
        with self.assertRaises(ValueError):
            self.invitations.invite(owner, pid, "a@example.org, b@example.org", "viewer")
        with self.assertRaises(ValueError):
            self.invitations.invite(owner, pid, "c@example.org", "viewer", ttl_seconds=86400 * 31)

        # Expired: refused, listed as expired, and replaceable.
        self.expire("project_email_invitations", issued.invitation.id)
        invitee = self.signed_up(name="invitee")
        self.assertEqual(self.invitations.pending(browser(invitee)), [])
        with self.assertRaises(InvitationUnavailableError) as expired:
            self.invitations.redeem(browser(invitee), token_of(issued.link))
        self.assertEqual(expired.exception.reason, "expired")
        listed = self.invitations.invitations(owner, pid, include_closed=True)
        self.assertEqual([i.status for i in listed if i.id == issued.invitation.id], ["expired"])
        replacement = self.invitations.invite(owner, pid, INVITEE, "viewer")

        # Revoked: refused, and revoking is checked against the Project in the path.
        other, other_users = self.team()
        self.denied(
            lambda: self.invitations.revoke(
                browser(users["member"]), pid, replacement.invitation.id
            ),
            "role",
        )
        with self.assertRaises(InvitationNotFoundError):
            self.invitations.revoke(browser(other_users["owner"]), other, replacement.invitation.id)
        self.invitations.revoke(owner, pid, replacement.invitation.id)
        with self.assertRaises(InvitationUnavailableError) as revoked:
            self.invitations.accept(browser(invitee), replacement.invitation.id)
        self.assertEqual(revoked.exception.reason, "revoked")
        with self.assertRaises(InvitationUnavailableError):
            self.invitations.revoke(owner, pid, replacement.invitation.id)
        self.assertIsNone(self.service.store.role_of(pid, invitee))
        _, joined = self.first_sign_in(INVITEE)
        self.assertEqual(joined, [])

    def test_already_member_keeps_their_role(self):
        pid, users = self.team()
        viewer_email = "viewer@example.org"
        issued = self.invitations.invite(browser(users["owner"]), pid, viewer_email, "admin")
        self.identities.link_identity(
            users["viewer"], issuer=ISSUER, subject="v", email=viewer_email, email_verified=True
        )
        accepted = self.invitations.accept(browser(users["viewer"]), issued.invitation.id)
        self.assertTrue(accepted.already_member)
        self.assertEqual(accepted.role, "viewer")
        self.assertEqual(self.service.store.role_of(pid, users["viewer"]), "viewer")

    def test_concurrent_acceptance_creates_one_membership(self):
        pid, users = self.team()
        invitee = self.signed_up(name="invitee")
        issued = self.invitations.invite(browser(users["owner"]), pid, INVITEE, "member")
        token = token_of(issued.link)
        barrier = threading.Barrier(6)
        outcomes: list[str] = []
        lock = threading.Lock()

        def accept(n: int):
            barrier.wait()
            try:
                if n % 2:
                    self.invitations.accept(browser(invitee), issued.invitation.id)
                else:
                    self.invitations.redeem(browser(invitee), token)
                result = "won"
            except InvitationUnavailableError as exc:
                result = exc.reason
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=accept, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        # One wins; the others find the invitation already accepted.
        self.assertEqual(sorted(outcomes), ["accepted"] * 5 + ["won"])
        self.assertEqual(
            self.count(
                "SELECT count(*) FROM project_memberships WHERE project_id = %s AND user_id = %s",
                pid,
                invitee,
            ),
            1,
        )

    def test_concurrent_first_sign_ins_redeem_once(self):
        pid, users = self.team()
        self.invitations.invite(browser(users["owner"]), pid, INVITEE, "member")
        barrier = threading.Barrier(4)
        joined: list[int] = []
        lock = threading.Lock()

        def sign_in():
            barrier.wait()
            _, accepted = self.first_sign_in()
            with lock:
                joined.append(len(accepted))

        threads = [threading.Thread(target=sign_in) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(joined), [0, 0, 0, 1])
        self.assertEqual(
            self.count(
                "SELECT count(*) FROM project_memberships WHERE project_id = %s AND role = 'member'",
                pid,
            ),
            2,
        )

    def test_invitation_is_honored_only_while_its_issuer_may_invite(self):
        pid, users = self.team()
        issued = self.invitations.invite(browser(users["admin"]), pid, INVITEE, "admin")
        self.service.change_role(browser(users["owner"]), pid, users["admin"], "member")
        invitee = self.signed_up(name="invitee")
        with self.assertRaises(InvitationUnavailableError) as caught:
            self.invitations.accept(browser(invitee), issued.invitation.id)
        self.assertEqual(caught.exception.reason, "issuer_not_authorized")
        _, joined = self.first_sign_in()
        self.assertEqual(joined, [])
        # A deleted Project's invitations are gone for the invitee.
        project = owned_project(self.service, users["owner"], "doomed")
        doomed = self.invitations.invite(browser(users["owner"]), project.id, INVITEE, "viewer")
        self.service.delete_project(browser(users["owner"]), project.id)
        self.assertEqual(self.invitations.pending(browser(invitee)), [])
        with self.assertRaises(InvitationNotFoundError):
            self.invitations.accept(browser(invitee), doomed.invitation.id)
        with self.assertRaises(InvitationNotFoundError):
            self.invitations.preview(browser(invitee), token_of(doomed.link))

    def test_accepted_role_is_enforced_and_grants_no_provider_authority(self):
        pid, users = self.team()
        invitee = self.signed_up(name="invitee")
        issued = self.invitations.invite(browser(users["owner"]), pid, INVITEE, "admin")
        self.invitations.accept(browser(invitee), issued.invitation.id)
        self.assertTrue(self.authorizer.check(browser(invitee), Action.MEMBER_LIST, pid))
        self.assertTrue(self.authorizer.check(browser(invitee), Action.PROJECT_UPDATE, pid))
        self.assertFalse(self.authorizer.check(browser(invitee), Action.PROJECT_DELETE, pid))
        # Run actions also need the Provider's permission, which no invitation gives.
        decision = self.authorizer.check(browser(invitee), Action.RUN_SUBMIT, pid)
        self.assertEqual(decision.reason, "provider_denied")
        # Losing the membership ends access at once.
        self.service.remove_member(browser(users["owner"]), pid, invitee)
        decision = self.authorizer.check(browser(invitee), Action.MEMBER_LIST, pid)
        self.assertEqual(decision.reason, "not_a_member")
        self.denied(lambda: self.invitations.invitations(browser(invitee), pid), "not_a_member")


class InviteLinkTests(_InvitationBase):
    def test_link_token_is_hashed_and_opening_it_joins_nothing(self):
        pid, users = self.team()
        created = self.invitations.create_link(browser(users["admin"]), pid, "viewer", max_uses=2)
        token = token_of(created.link)
        self.assertGreaterEqual(len(token), 43)
        stored = self.conn.execute(
            "SELECT token_hash FROM project_invite_links WHERE id = %s", (created.invite_link.id,)
        ).fetchone()
        assert stored is not None
        self.assertEqual(bytes(stored[0]), hash_token(token))
        self.assertNotIn(token, repr(created))
        visitor = self.user("visitor")
        preview = self.invitations.preview(browser(visitor), token)
        self.assertEqual((preview.kind, preview.role, preview.status), ("link", "viewer", "active"))
        self.assertIsNone(self.service.store.role_of(pid, visitor))
        self.assertEqual(self.inv.list_invite_links(pid)[0].use_count, 0)
        accepted = self.invitations.redeem(browser(visitor), token)
        assert isinstance(accepted, Acceptance)
        self.assertEqual((accepted.kind, accepted.role), ("link", "viewer"))
        self.assertEqual(self.service.store.role_of(pid, visitor), "viewer")
        # A member redeeming again uses nothing up.
        with self.assertRaises(DuplicateMemberError):
            self.invitations.redeem(browser(visitor), token)
        self.assertEqual(self.inv.list_invite_links(pid)[0].use_count, 1)
        # Leaving and coming back through the same link is refused: one redemption each.
        self.service.remove_member(browser(visitor), pid, visitor)
        with self.assertRaises(InvitationUnavailableError) as caught:
            self.invitations.redeem(browser(visitor), token)
        self.assertEqual(caught.exception.reason, "already_redeemed")
        with self.assertRaises(ValueError):
            self.invitations.redeem(browser(visitor), token, accept=False)
        with self.assertRaises(InvitationNotFoundError):
            self.invitations.redeem(browser(visitor), secrets.token_urlsafe(32))
        self.denied(
            lambda: self.invitations.redeem(cli(visitor, "jobs:run"), token), "browser_only"
        )

    def test_concurrent_redemptions_never_exceed_max_uses(self):
        pid, users = self.team()
        created = self.invitations.create_link(browser(users["owner"]), pid, "member", max_uses=3)
        token = token_of(created.link)
        visitors = [self.user(f"v{n}") for n in range(8)]
        barrier = threading.Barrier(len(visitors))
        outcomes: list[str] = []
        lock = threading.Lock()

        def redeem(user):
            barrier.wait()
            try:
                self.invitations.redeem(browser(user), token)
                result = "joined"
            except InvitationUnavailableError as exc:
                result = exc.reason
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=redeem, args=(u,)) for u in visitors]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(outcomes), ["exhausted"] * 5 + ["joined"] * 3)
        link = self.inv.list_invite_links(pid, include_closed=True)[0]
        self.assertEqual((link.use_count, link.status), (3, "exhausted"))
        self.assertEqual(
            self.count(
                "SELECT count(*) FROM project_invitation_redemptions WHERE invite_link_id = %s",
                link.id,
            ),
            3,
        )
        self.assertEqual(
            self.count(
                "SELECT count(*) FROM project_memberships WHERE project_id = %s AND role = 'member'",
                pid,
            ),
            4,
        )
        events = [e[0] for e in self.audit("invite_link.")]
        self.assertEqual(events.count("invite_link.redeemed"), 3)
        self.assertEqual(events.count("invite_link.exhausted"), 1)

    def test_expired_revoked_and_bounded_links(self):
        pid, users = self.team()
        owner = browser(users["owner"])
        for bad in (0, 10_001, True):
            with self.subTest(max_uses=bad), self.assertRaises(ValueError):
                self.invitations.create_link(owner, pid, "viewer", max_uses=bad)
        self.denied(
            lambda: self.invitations.create_link(owner, pid, "owner"), "owner_by_transfer_only"
        )
        expired = self.invitations.create_link(owner, pid, "viewer", max_uses=5)
        self.expire("project_invite_links", expired.invite_link.id)
        revoked = self.invitations.create_link(owner, pid, "viewer", max_uses=5)
        self.invitations.revoke_link(owner, pid, revoked.invite_link.id)
        visitor = self.user("visitor")
        for created, reason in ((expired, "expired"), (revoked, "revoked")):
            with self.subTest(reason=reason):
                with self.assertRaises(InvitationUnavailableError) as caught:
                    self.invitations.redeem(browser(visitor), token_of(created.link))
                self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.invitations.links(owner, pid), [])
        statuses = {l.id: l.status for l in self.invitations.links(owner, pid, include_closed=True)}
        self.assertEqual(
            statuses, {expired.invite_link.id: "expired", revoked.invite_link.id: "revoked"}
        )
        other, other_users = self.team()
        with self.assertRaises(InvitationNotFoundError):
            self.invitations.revoke_link(
                browser(other_users["owner"]), other, expired.invite_link.id
            )
        # A link stops working when its creator can no longer invite.
        live = self.invitations.create_link(browser(users["admin"]), pid, "viewer", max_uses=5)
        self.service.remove_member(browser(users["owner"]), pid, users["admin"])
        with self.assertRaises(InvitationUnavailableError) as caught:
            self.invitations.redeem(browser(visitor), token_of(live.link))
        self.assertEqual(caught.exception.reason, "issuer_not_authorized")
        self.assertIsNone(self.service.store.role_of(pid, visitor))


class InvitationAuditTests(_InvitationBase):
    def test_lifecycle_is_audited_without_addresses_or_tokens(self):
        pid, users = self.team()
        owner = browser(users["owner"])
        with self.assertLogs("nest", logging.DEBUG) as logs:
            issued = self.invitations.invite(owner, pid, INVITEE, "member")
            resent = self.invitations.resend(owner, pid, issued.invitation.id)
            revoked = self.invitations.invite(owner, pid, "gone@example.org", "viewer")
            self.invitations.revoke(owner, pid, revoked.invitation.id)
            link = self.invitations.create_link(owner, pid, "viewer", max_uses=1)
            invitee = self.signed_up(name="invitee")
            self.invitations.redeem(browser(invitee), token_of(resent.link))
            visitor = self.user("visitor")
            self.invitations.redeem(browser(visitor), token_of(link.link))
            stranger = self.signed_up("stranger@example.org", name="stranger")
            with self.assertRaises(InvitationNotFoundError):
                self.invitations.accept(browser(stranger), issued.invitation.id)
            with self.assertRaises(InvitationNotFoundError):
                self.invitations.redeem(browser(stranger), token_of(issued.link))
        events = [(e[0], e[1]) for e in self.audit("invitation.", "invite_link.")]
        for expected in (
            ("invitation.created", "succeeded"),
            ("invitation.token_issued", "succeeded"),
            ("invitation.revoked", "succeeded"),
            ("invitation.accepted", "succeeded"),
            ("invite_link.created", "succeeded"),
            ("invite_link.redeemed", "succeeded"),
            ("invite_link.exhausted", "succeeded"),
            ("invitation.accept", "denied"),
        ):
            self.assertIn(expected, events)
        # The replaced link no longer works.
        self.assertEqual(self.inv.token_kind(hash_token(token_of(issued.link))), None)
        secrets_ = [
            NORMALIZED,
            INVITEE,
            "gone@example.org",
            token_of(issued.link),
            token_of(resent.link),
            token_of(link.link),
        ]
        dumped = "\n".join(
            str(row)
            for table in ("security_audit_events", "project_audit_events")
            for row in self.conn.execute(f"SELECT * FROM {table}").fetchall()
        )
        logged = "\n".join(logs.output)
        for value in secrets_:
            with self.subTest(value=value[:12]):
                self.assertNotIn(value, dumped)
                self.assertNotIn(value, logged)
        self.assertTrue(AuditLog(self.db).verify().intact)


class SMTPEnabledTests(_InvitationBase):
    def setUp(self):
        super().setUp()
        config = enabled(email_settings(NEST_DATABASE_URL=self.url))
        self.outbox = EmailOutbox(self.db, "example.com", max_attempts=2)
        self.mail = EmailService(config, self.outbox)
        self.invitations = InvitationService(self.db, self.service, self.mail)

    def test_send_failure_and_resend_through_the_outbox(self):
        pid, users = self.team()
        owner = browser(users["owner"])
        issued = self.invitations.invite(owner, pid, INVITEE, "member")
        self.assertTrue(issued.emailed)
        self.assertTrue(issued.link.startswith("https://nest.example.com/invitations/accept#"))
        assert issued.invitation.delivery is not None
        self.assertEqual(issued.invitation.delivery.status, "pending")
        row = self.conn.execute(
            "SELECT recipient, body_text, message_key FROM email_outbox"
        ).fetchone()
        assert row is not None
        self.assertEqual(row[0], NORMALIZED)
        self.assertIn(issued.link, row[1])
        self.assertEqual(row[2], f"invitation:{issued.invitation.id}:send:1")
        # Delivery fails permanently: the invitation stays pending, the failure is
        # visible to the Project's managers and audited.
        [claimed] = self.outbox.claim()
        self.outbox.mark_failed(claimed, "SMTPRecipientsRefused code=550", permanent=True)
        [listed] = self.invitations.invitations(owner, pid)
        self.assertEqual(listed.status, "pending")
        assert listed.delivery is not None
        self.assertEqual(
            (listed.delivery.status, listed.delivery.last_error),
            ("failed", "SMTPRecipientsRefused code=550"),
        )
        failed = [e for e in self.audit("invitation.email_failed")]
        self.assertEqual(
            [(e[1], e[3], e[4]) for e in failed], [("failed", str(listed.id), "permanent")]
        )
        self.assertNotIn(NORMALIZED, failed[0][5])
        # Resend queues a new message with a new link; the old link stops working.
        resent = self.invitations.resend(owner, pid, issued.invitation.id)
        self.assertTrue(resent.emailed)
        self.assertNotEqual(token_of(resent.link), token_of(issued.link))
        assert resent.invitation.delivery is not None
        self.assertEqual(resent.invitation.delivery.message_key, f"invitation:{listed.id}:send:2")
        invitee = self.signed_up(name="invitee")
        with self.assertRaises(InvitationNotFoundError):
            self.invitations.redeem(browser(invitee), token_of(issued.link))
        self.invitations.redeem(browser(invitee), token_of(resent.link))
        self.assertEqual(self.service.store.role_of(pid, invitee), "member")
        with self.assertRaises(InvitationUnavailableError):
            self.invitations.resend(owner, pid, issued.invitation.id)
        # Opting out of email still returns the link to share manually.
        manual = self.invitations.invite(
            owner, pid, "manual@example.org", "viewer", send_email=False
        )
        self.assertFalse(manual.emailed)
        self.assertIsNone(manual.invitation.delivery)

    def test_invalid_enabled_smtp_settings_fail_and_disabled_ignores_them(self):
        with self.assertRaises(ConfigError) as caught:
            load_controller_config(email_settings(NEST_SMTP_HOST="", NEST_DATABASE_URL=self.url))
        self.assertIn("NEST_SMTP_HOST", str(caught.exception))
        disabled = load_controller_config(
            {"NEST_DATABASE_URL": self.url, "NEST_EMAIL_ENABLED": "false", "NEST_SMTP_HOST": ""}
        )
        self.assertIsNone(disabled.email)
        with self.assertRaises(ValueError):
            InvitationService(self.db, self.service, EmailService())


class InvitationHTTPTests(_InvitationBase):
    def setUp(self):
        super().setUp()
        self.fake = FakeGoogle()
        self.addCleanup(self.fake.close)
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        config = load_controller_config(
            {
                "NEST_DATABASE_URL": self.url,
                "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
                "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
                "NEST_GOOGLE_REDIRECT_URL": f"{self.base}/auth/google/callback",
                "NEST_AUTH_HTTP_BIND": f"127.0.0.1:{port}",
            }
        ).google
        assert config is not None
        sign_in = GoogleSignIn(config, self.db, endpoints=self.fake.endpoints, timeout=5)
        self.http = start_auth_service(config, self.db, sign_in=sign_in)
        self.addCleanup(self.http.stop)
        self.sessions = SessionStore(self.db)

    def token(self, user_id, kind="browser", scope=None) -> str:
        session = self.identities.create_session(
            user_id,
            kind=kind,
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=scope,
        )
        access = secrets.token_urlsafe()
        self.sessions.issue_access_token(session.id, token_hash=hash_token(access), ttl_seconds=600)
        return access

    def request(self, method, path, *, cookie=None, bearer=None, body=None, form=None):
        headers = {}
        if cookie:
            headers["Cookie"] = f"{INSECURE_SESSION_COOKIE}={cookie}"
        if method == "POST" and not bearer:
            headers.update({"Origin": self.base, "Sec-Fetch-Site": "same-origin"})
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        data = None
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif method == "POST":
            data = json.dumps(body or {}).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read().decode()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.headers, exc.read().decode()

    def call(self, method, path, **kwargs):
        status, _, text = self.request(method, path, **kwargs)
        return status, json.loads(text or "{}")

    def test_manage_invitations_over_the_api(self):
        pid, users = self.team()
        owner, member = self.token(users["owner"]), self.token(users["member"])
        outsider = self.token(users["outsider"])
        path = f"/api/projects/{pid}/invitations"
        status, body = self.call(
            "POST", path, cookie=owner, body={"email": INVITEE, "role": "member"}
        )
        self.assertEqual(status, 201, body)
        self.assertFalse(body["emailed"])
        self.assertTrue(body["link"].startswith(self.base + "/invitations/accept#token="))
        self.assertEqual(
            (body["invitation"]["email"], body["invitation"]["status"]), (NORMALIZED, "pending")
        )
        invitation_id, first_link = body["invitation"]["id"], body["link"]
        status, again = self.call(
            "POST", path, cookie=owner, body={"email": INVITEE, "role": "viewer"}
        )
        self.assertEqual(
            (status, again), (409, {"error": "already_invited", "invitation_id": invitation_id})
        )
        status, body = self.call(
            "POST", path, cookie=owner, body={"email": "x@example.org", "role": "owner"}
        )
        self.assertEqual((status, body["reason"]), (403, "owner_by_transfer_only"))
        status, body = self.call(
            "POST", path, cookie=member, body={"email": "y@example.org", "role": "viewer"}
        )
        self.assertEqual((status, body), (403, {"error": "forbidden", "reason": "role"}))
        for token in (outsider,):
            self.assertEqual(self.call("GET", path, cookie=token), (404, {"error": "not_found"}))
        cli_token = self.token(users["owner"], kind="cli", scope="projects:read jobs:run")
        status, body = self.call(
            "POST", path, bearer=cli_token, body={"email": "z@example.org", "role": "viewer"}
        )
        self.assertEqual((status, body["reason"]), (403, "browser_only"))
        status, listed = self.call("GET", path, cookie=owner)
        self.assertEqual([i["id"] for i in listed["invitations"]], [invitation_id])
        status, resent = self.call("POST", f"{path}/{invitation_id}/resend", cookie=owner)
        self.assertEqual(status, 200)
        self.assertNotEqual(resent["link"], first_link)
        status, revoked = self.call("POST", f"{path}/{invitation_id}/revoke", cookie=owner)
        self.assertEqual((status, revoked["invitation"]["status"]), (200, "revoked"))
        status, body = self.call("POST", f"{path}/{invitation_id}/revoke", cookie=owner)
        self.assertEqual(
            (status, body), (410, {"error": "invitation_unavailable", "reason": "revoked"})
        )
        self.assertEqual(self.call("POST", f"{path}/abc/revoke", cookie=owner)[0], 404)
        self.assertEqual(self.call("GET", f"{path}/{invitation_id}/revoke", cookie=owner)[0], 405)
        # Invite Links.
        links = f"/api/projects/{pid}/invite-links"
        status, created = self.call(
            "POST", links, cookie=owner, body={"role": "viewer", "max_uses": 2}
        )
        self.assertEqual((status, created["invite_link"]["max_uses"]), (201, 2))
        status, body = self.call(
            "POST", links, cookie=owner, body={"role": "viewer", "max_uses": "2"}
        )
        self.assertEqual((status, body), (400, {"error": "invalid_request"}))
        status, listed = self.call("GET", links, cookie=owner)
        self.assertEqual([l["id"] for l in listed["invite_links"]], [created["invite_link"]["id"]])
        self.assertNotIn(token_of(created["link"]), json.dumps(listed))
        status, body = self.call(
            "POST", f"{links}/{created['invite_link']['id']}/revoke", cookie=owner
        )
        self.assertEqual((status, body["invite_link"]["status"]), (200, "revoked"))

    def test_own_invitations_do_not_reveal_others(self):
        pid, users = self.team()
        invitee = self.signed_up(name="invitee")
        stranger = self.signed_up("stranger@example.org", name="stranger")
        issued = self.invitations.invite(browser(users["owner"]), pid, INVITEE, "viewer")
        mine, theirs = self.token(invitee), self.token(stranger)
        status, body = self.call("GET", "/api/invitations", cookie=mine)
        self.assertEqual(
            [(i["id"], i["project_name"]) for i in body["invitations"]],
            [(issued.invitation.id, "shared")],
        )
        self.assertNotIn("email", body["invitations"][0])
        self.assertEqual(
            self.call("GET", "/api/invitations", cookie=theirs), (200, {"invitations": []})
        )
        # Another User's invitation and a nonexistent one look the same.
        own_path = f"/api/invitations/{issued.invitation.id}/accept"
        missing = self.call("POST", "/api/invitations/999999/accept", cookie=theirs)
        self.assertEqual(self.call("POST", own_path, cookie=theirs), missing)
        self.assertEqual(missing, (404, {"error": "invitation_not_found"}))
        status, body = self.call(
            "POST", "/api/invitations/redeem", cookie=theirs, body={"token": token_of(issued.link)}
        )
        self.assertEqual((status, body), (403, {"error": "email_mismatch"}))
        status, body = self.call(
            "POST", "/api/invitations/preview", cookie=theirs, body={"token": "nope"}
        )
        self.assertEqual((status, body), (404, {"error": "invitation_not_found"}))
        status, body = self.call("POST", own_path, cookie=mine)
        self.assertEqual((status, body["role"], body["already_member"]), (200, "viewer", False))
        status, projects = self.call("GET", "/api/projects", cookie=mine)
        self.assertEqual([p["id"] for p in projects["projects"]], [str(pid)])
        self.assertEqual(
            self.call("GET", "/api/invitations", bearer=self.token(invitee, "cli", "jobs:run"))[0],
            200,
        )
        self.assertEqual(
            self.call(
                "POST", "/api/invitations/1/accept", bearer=self.token(invitee, "cli", "jobs:run")
            )[1]["reason"],
            "browser_only",
        )

    def test_invite_link_needs_explicit_acceptance_on_the_page(self):
        pid, users = self.team()
        created = self.invitations.create_link(browser(users["owner"]), pid, "member", max_uses=1)
        visitor = self.user("visitor")
        cookie = self.token(visitor)
        # Opening the page changes nothing; it carries the token only in the fragment.
        status, headers, page = self.request("GET", "/invitations/accept", cookie=cookie)
        self.assertEqual(status, 200)
        # The CSP allows exactly the page's one inline script, and a form POST from
        # the page carries a real Origin (a no-referrer page would send "null").
        script = page.split("<script>")[1].split("</script>")[0]
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        self.assertIn(f"script-src 'sha256-{digest}'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "same-origin")
        self.assertIn('name="step" value="review"', page)
        token = token_of(created.link)
        status, _, review = self.request(
            "POST", "/invitations/accept", cookie=cookie, form={"token": token, "step": "review"}
        )
        self.assertEqual(status, 200, review)
        self.assertIn("<strong>shared</strong>", review)
        self.assertIn("grants no HPC", review)
        self.assertIsNone(self.service.store.role_of(pid, visitor))
        self.assertEqual(self.inv.list_invite_links(pid)[0].use_count, 0)
        # Accepting needs the page's CSRF token and a same-origin POST.
        status, _, _ = self.request(
            "POST",
            "/invitations/accept",
            cookie=cookie,
            form={"token": token, "step": "accept", "csrf": "x"},
        )
        self.assertEqual(status, 403)
        csrf = review.split('name="csrf" value="')[1].split('"')[0]
        status, _, done = self.request(
            "POST",
            "/invitations/accept",
            cookie=cookie,
            form={"token": token, "step": "accept", "csrf": csrf},
        )
        self.assertEqual(status, 200, done)
        self.assertIn("You joined the Project as Member", done)
        self.assertIn("data-done", done)
        self.assertEqual(self.service.store.role_of(pid, visitor), "member")
        # The link is now exhausted for everyone else.
        other = self.token(self.user("late"))
        status, _, page = self.request(
            "POST", "/invitations/accept", cookie=other, form={"token": token, "step": "review"}
        )
        self.assertEqual(status, 410)
        self.assertIn("used as many times", page)
        # Without a session the page offers sign-in and joins nothing.
        status, _, page = self.request("GET", "/invitations/accept")
        self.assertEqual(status, 200)
        self.assertIn(LOGIN_PATH + "?next=invitation", page)
        status, _, _ = self.request(
            "POST", "/invitations/accept", form={"token": token, "step": "review"}
        )
        self.assertEqual(status, 401)

    def test_first_google_sign_in_redeems_and_returns_to_the_invitation(self):
        pid, users = self.team()
        self.fake.account = Account(sub="108000000000000000777", email=INVITEE.lower())
        self.invitations.invite(browser(users["owner"]), pid, INVITEE, "viewer")
        visitor = Browser()
        status, headers, _ = visitor.get(self.base + LOGIN_PATH + "?next=invitation")
        self.assertEqual(status, 302)
        status, headers, _ = visitor.get(headers["Location"])
        status, headers, body = visitor.get(headers["Location"])
        self.assertEqual((status, headers["Location"]), (303, "/invitations/accept"), body)
        user = self.identities.find_user(issuer=ISSUER, subject="108000000000000000777")
        assert user is not None
        self.assertEqual(self.service.store.role_of(pid, user.id), "viewer")
        # An unverified Google address never redeems: the ID token is refused outright.
        self.fake.account = Account(
            sub="108000000000000000778", email="v@example.org", email_verified=False
        )
        self.invitations.invite(browser(users["owner"]), pid, "v@example.org", "viewer")
        unverified = Browser()
        _, headers, _ = unverified.get(self.base + LOGIN_PATH)
        _, headers, _ = unverified.get(headers["Location"])
        status, _, _ = unverified.get(headers["Location"])
        self.assertNotEqual(status, 200)
        self.assertIsNone(self.identities.find_user(issuer=ISSUER, subject="108000000000000000778"))

    def test_first_google_sign_in_reports_joined_projects(self):
        pid, users = self.team()
        self.fake.account = Account(sub="108000000000000000779", email="first@example.org")
        self.invitations.invite(browser(users["owner"]), pid, "first@example.org", "member")
        visitor = Browser()
        status, headers, _ = visitor.get(self.base + LOGIN_PATH)
        status, headers, _ = visitor.get(headers["Location"])
        status, _, body = visitor.get(headers["Location"])
        self.assertEqual(status, 200, body)
        self.assertIn("Your invitations added you to 1 Project.", body)


if __name__ == "__main__":
    unittest.main()
