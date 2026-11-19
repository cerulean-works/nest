"""
Browser-approved CLI sign-in, following the OAuth 2.0 device authorization grant
(RFC 8628).

1. The CLI posts to `/auth/cli/start` and receives a secret device code, a short
   user code and the approval URL.
2. The User opens the approval URL in a browser where they are signed in to Nest
   with Google, checks that the page shows the code their terminal shows, and
   approves.
3. The CLI polls `/auth/cli/token` with the device code and receives a CLI session:
   a scoped, short-lived access token and a rotating refresh secret.

Nest sessions identify a User to Nest only. They are never NCHC SSH passwords,
one-time codes or keys, and are never sent to an HPC login node.
"""

from __future__ import annotations

import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from .._logging import get_logger
from ..config import CLI_SCOPES, DEFAULT_CLI_SCOPE, SessionPolicy
from ..db import (
    CLILoginRequest,
    CLILoginStore,
    CLIPoll,
    Database,
    DecidingSessionNotActiveError,
    TooManyPendingCLILoginsError,
    UserCodeCollisionError,
    UserNotActiveError,
    hash_token,
)

logger = get_logger("nest.auth.cli")

CLI_LOGIN_TTL = 600.0
"""Seconds a CLI sign-in waits for approval and redemption."""

CLI_POLL_INTERVAL = 5.0
"""Least seconds between two polls with one device code (RFC 8628 `interval`)."""

MAX_PENDING_CLI_LOGINS = 10_000
"""Most pending CLI sign-ins kept at once, bounding what unauthenticated requests store."""

USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
"""Letters of a user code: no vowels, so codes spell no words, and none that are
easily confused. Eight of them carry about 34.6 bits."""

USER_CODE_LENGTH = 8


class CLISignInError(Exception):
    """
    A CLI sign-in request was refused.

    Attributes:
        reason (`str`): `invalid_scope`, `invalid_request` or `busy`.
        status (`int`): HTTP status for the response.
    """

    def __init__(self, reason: str, status: int, message: str):
        super().__init__(message)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class CLISignInStart:
    """
    A started CLI sign-in.

    Attributes:
        device_code (`str`): Secret the CLI polls with. Never log or show it.
        user_code (`str`): Code shown to the User, such as `BCDF-GHJK`.
        request ([`db.CLILoginRequest`]): The stored request.
    """

    device_code: str
    user_code: str
    request: CLILoginRequest


@dataclass(frozen=True)
class CLITokens:
    """
    A CLI session issued after approval. Never log the secrets.

    Attributes:
        poll ([`db.CLIPoll`]): The redemption, with the new session.
        access_token (`str`): Short-lived bearer token.
        refresh_token (`str`): Rotating refresh secret.
    """

    poll: CLIPoll
    access_token: str
    refresh_token: str


def new_user_code() -> str:
    """Returns a random user code, formatted `XXXX-XXXX`."""
    code = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
    return f"{code[:4]}-{code[4:]}"


def normalize_user_code(raw: str | None) -> str | None:
    """
    Returns a user code in its stored form (eight letters, uppercase, no dash), or
    `None` when `raw` cannot be one. Spaces, dashes and case are ignored, since
    people retype codes.
    """
    if not raw or len(raw) > 32:
        return None
    code = "".join(ch for ch in raw.upper() if ch not in "- \t")
    if len(code) != USER_CODE_LENGTH or any(ch not in USER_CODE_ALPHABET for ch in code):
        return None
    return code


def format_user_code(code: str) -> str:
    """Returns a normalized user code as `XXXX-XXXX`."""
    return f"{code[:4]}-{code[4:]}"


def normalize_scope(raw: object) -> str:
    """
    Returns the canonical form of a requested scope: known scopes, deduplicated,
    sorted and space-separated. A missing scope asks for [`config.DEFAULT_CLI_SCOPE`].

    Raises:
        [`auth.CLISignInError`]: `invalid_scope` for an unknown or malformed scope.
    """
    if raw is None:
        raw = DEFAULT_CLI_SCOPE
    if not isinstance(raw, str) or len(raw) > 512:
        raise CLISignInError("invalid_scope", 400, "The scope must be a string")
    scopes = set(raw.split())
    unknown = scopes - set(CLI_SCOPES)
    if not scopes or unknown:
        raise CLISignInError(
            "invalid_scope", 400, f"Scopes must be some of {', '.join(sorted(CLI_SCOPES))}"
        )
    return " ".join(sorted(scopes))


def clean_client_name(raw: object) -> str:
    """
    Returns a displayable client label: printable characters only, whitespace
    collapsed, at most 64 characters. Anything unusable becomes `unnamed client`.
    """
    if not isinstance(raw, str):
        return "unnamed client"
    kept = "".join(
        ch if not unicodedata.category(ch).startswith(("C", "Z")) else " " for ch in raw[:256]
    )
    name = " ".join(kept.split())[:64].strip()
    return name or "unnamed client"


class CLISignIn:
    """
    Runs browser-approved CLI sign-ins for the auth listener.

    Args:
        database ([`db.Database`]): Open database with the current schema.
        sessions ([`config.SessionPolicy`]): Session lifetimes, shared with browser
            sessions.
        poll_interval (`float`): Seconds the CLI is told to wait between polls.
    """

    def __init__(
        self,
        database: Database,
        sessions: SessionPolicy,
        *,
        poll_interval: float = CLI_POLL_INTERVAL,
    ):
        self.store = CLILoginStore(database)
        self.sessions = sessions
        self.poll_interval = poll_interval

    def start(self, *, scope: object = None, client_name: object = None) -> CLISignInStart:
        """
        Stores a pending CLI sign-in.

        Raises:
            [`auth.CLISignInError`]: `invalid_scope`, or `busy` when too many are
                pending.
        """
        canonical = normalize_scope(scope)
        name = clean_client_name(client_name)
        device_code = secrets.token_urlsafe(32)
        for _ in range(5):
            user_code = new_user_code()
            try:
                request = self.store.create(
                    device_code_hash=hash_token(device_code),
                    user_code_hash=hash_token(normalize_user_code(user_code) or ""),
                    scope=canonical,
                    client_name=name,
                    ttl_seconds=CLI_LOGIN_TTL,
                    max_pending=MAX_PENDING_CLI_LOGINS,
                )
            except UserCodeCollisionError:
                continue
            except TooManyPendingCLILoginsError:
                logger.warning("CLI sign-in refused: %s pending", MAX_PENDING_CLI_LOGINS)
                raise CLISignInError(
                    "busy", 503, "Too many sign-ins are in progress. Try again later."
                ) from None
            return CLISignInStart(device_code=device_code, user_code=user_code, request=request)
        raise CLISignInError("busy", 503, "Too many sign-ins are in progress. Try again later.")

    def find(self, user_code: str | None) -> CLILoginRequest | None:
        """Returns the pending request for a user code as typed, or `None`."""
        code = normalize_user_code(user_code)
        return self.store.find_pending(hash_token(code)) if code else None

    def decide(
        self, request: CLILoginRequest, *, approve: bool, user_id, session_id
    ) -> CLILoginRequest | None:
        """
        Approves or denies a request; `None` when it is no longer pending.

        Raises:
            [`db.DecidingSessionNotActiveError`]: If the browser session making the
                decision has ended since it was authenticated, or the User is
                disabled. Nothing is changed.
        """
        try:
            return self.store.decide(
                request.id, approve=approve, user_id=user_id, session_id=session_id
            )
        except UserNotActiveError:
            raise DecidingSessionNotActiveError("The User is disabled") from None

    def redeem(self, device_code: object) -> CLITokens:
        """Answers a poll; [`auth.CLITokens.poll`] says whether a session was issued."""
        refresh, access = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        code_hash = hash_token(device_code) if isinstance(device_code, str) else b""
        poll = self.store.redeem(
            code_hash,
            refresh_hash=hash_token(refresh),
            access_hash=hash_token(access),
            # Some slack: network delays can bring two polls a little closer together.
            poll_interval=self.poll_interval * 0.8,
            refresh_ttl=self.sessions.refresh_ttl,
            access_ttl=self.sessions.access_token_ttl,
            max_lifetime=self.sessions.max_lifetime,
        )
        return CLITokens(poll=poll, access_token=access, refresh_token=refresh)


def expires_in(moment: datetime, now: datetime) -> int:
    """Whole seconds from `now` until `moment`, at least 0."""
    return max(0, int((moment - now).total_seconds()))
