"""
Nest sign-in for the command line and the SDK.

A CLI session is approved in a browser: [`login.login`] starts a sign-in at the
Nest auth URL (`NEST_AUTH_URL`), shows a short code and an approval link, and waits
while the User approves it in a browser where they are signed in to Nest with
Google. The session is kept in a credentials file readable only by its owner
(`NEST_CLI_CREDENTIALS_FILE`) and is refreshed as it nears expiry.

`nest login` is optional: [`login.ensure_session`] returns a valid session and,
when there is none, runs the same browser approval inline, so a command such as
`nest platforms list` can sign the User in the first time it needs to.

A Nest session identifies a User to Nest only. It is not an NCHC SSH password,
one-time code or key, and Nest never sends it to an HPC login node; HPC
credentials are configured separately.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import IO, Any
from uuid import UUID

from ._logging import get_logger, register_secret
from .config import DEFAULT_CLI_SCOPE, CLIAuthConfig, load_cli_auth_config

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Windows
    fcntl = None

logger = get_logger("nest.login")

CREDENTIALS_KIND = "nest-cli-session"
"""Marks a credentials file as holding a Nest session, never HPC credentials."""

CREDENTIALS_VERSION = 1

START_PATH = "/auth/cli/start"
TOKEN_PATH = "/auth/cli/token"
SESSION_PATH = "/auth/cli/session"
REFRESH_PATH = "/auth/cli/refresh"
LOGOUT_PATH = "/auth/cli/logout"

ACCESS_MARGIN = timedelta(seconds=30)
"""An access token this close to expiry is refreshed before use."""

_MAX_RESPONSE_BYTES = 64 * 1024


class AuthError(Exception):
    """A Nest sign-in failed. The message never contains a token or code."""


class NotSignedInError(AuthError):
    """There is no usable Nest session and an interactive sign-in was not allowed."""


class LoginDeniedError(AuthError):
    """The CLI sign-in was denied in the browser."""


class LoginExpiredError(AuthError):
    """The CLI sign-in was not approved in time."""


class NestUnavailableError(AuthError):
    """Nest's sign-in service could not be reached."""


class CredentialsFileError(AuthError):
    """The credentials file is unsafe or unreadable."""


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("missing time")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("naive time")
    return moment


@dataclass(frozen=True)
class NestSession:
    """
    A CLI session. Its secrets are left out of `repr()`; never log them.

    Attributes:
        auth_url (`str`): The Nest origin that issued it.
        user_id (`UUID`): The signed-in Nest User.
        session_id (`UUID`): The current refresh session.
        scope (`str`): Space-separated scopes, such as `jobs:run projects:read`.
        access_token (`str`): Short-lived bearer token.
        refresh_token (`str`): Rotating refresh secret.
        access_expires_at, refresh_expires_at, session_expires_at (`datetime`):
            When the access token, the refresh secret and the whole sign-in end.
    """

    auth_url: str
    user_id: UUID
    session_id: UUID
    scope: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_expires_at: datetime
    refresh_expires_at: datetime
    session_expires_at: datetime

    @property
    def scopes(self) -> frozenset[str]:
        """The session's scopes as a set."""
        return frozenset(self.scope.split())

    def access_usable(self, now: datetime | None = None) -> bool:
        """Whether the access token is good for at least [`login.ACCESS_MARGIN`]."""
        now = now or datetime.now(UTC)
        return self.access_expires_at - ACCESS_MARGIN > now

    def refreshable(self, now: datetime | None = None) -> bool:
        """Whether the refresh secret has not expired."""
        now = now or datetime.now(UTC)
        return self.refresh_expires_at > now and self.session_expires_at > now

    @classmethod
    def from_response(cls, auth_url: str, payload: Mapping[str, Any]) -> NestSession:
        """Builds a session from a token or refresh response."""
        try:
            session = cls(
                auth_url=auth_url,
                user_id=UUID(str(payload["user_id"])),
                session_id=UUID(str(payload["session_id"])),
                scope=str(payload.get("scope") or ""),
                access_token=_secret(payload["access_token"]),
                refresh_token=_secret(payload["refresh_token"]),
                access_expires_at=_parse_time(payload["access_expires_at"]),
                refresh_expires_at=_parse_time(payload["refresh_expires_at"]),
                session_expires_at=_parse_time(payload["session_expires_at"]),
            )
        except KeyError, TypeError, ValueError:
            raise AuthError("Nest answered the sign-in with an unexpected response") from None
        register_secret(session.access_token)
        register_secret(session.refresh_token)
        return session

    def to_json(self) -> dict[str, object]:
        """The credentials file's contents."""
        return {
            "kind": CREDENTIALS_KIND,
            "version": CREDENTIALS_VERSION,
            "auth_url": self.auth_url,
            "user_id": str(self.user_id),
            "session_id": str(self.session_id),
            "scope": self.scope,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "access_expires_at": self.access_expires_at.isoformat(),
            "refresh_expires_at": self.refresh_expires_at.isoformat(),
            "session_expires_at": self.session_expires_at.isoformat(),
        }


def _secret(value: object) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= 512:
        raise ValueError("bad secret")
    return value


class CredentialStore:
    """
    Keeps one CLI session in a JSON file readable only by its owner.

    The file is written atomically with mode `0600` in a directory created with
    mode `0700`. A file that other users can read or write is refused.

    Args:
        path (`str`): Absolute path of the credentials file.
    """

    def __init__(self, path: str):
        self.path = path

    def load(self, auth_url: str) -> NestSession | None:
        """
        Returns the stored session for `auth_url`, or `None` when there is none,
        it is for another Nest, or it cannot be parsed.

        Raises:
            [`login.CredentialsFileError`]: If the file is accessible to other users.
        """
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CredentialsFileError(
                f"Cannot read the Nest credentials file {self.path}: {exc.strerror}"
            ) from None
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if os.name == "posix" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
                raise CredentialsFileError(
                    f"The Nest credentials file {self.path} is accessible to other users; "
                    "remove it with 'nest logout --local' and sign in again"
                )
            try:
                data = json.loads(handle.read(_MAX_RESPONSE_BYTES))
            except ValueError, UnicodeDecodeError:
                logger.warning("Ignoring an unreadable Nest credentials file")
                return None
        if not isinstance(data, dict) or data.get("kind") != CREDENTIALS_KIND:
            return None
        if data.get("version") != CREDENTIALS_VERSION or data.get("auth_url") != auth_url:
            return None
        try:
            return NestSession.from_response(auth_url, data)
        except AuthError:
            logger.warning("Ignoring an unreadable Nest credentials file")
            return None

    def save(self, session: NestSession) -> None:
        """Replaces the stored session atomically."""
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        # mkstemp creates the file with mode 0600.
        fd, temporary = tempfile.mkstemp(prefix=".credentials-", dir=directory)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(session.to_json(), handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    def clear(self) -> bool:
        """Deletes the stored session. Returns whether there was one."""
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            return False
        return True

    @contextlib.contextmanager
    def locked(self) -> Generator[None]:
        """
        Holds an exclusive lock beside the file, so concurrent Nest commands refresh
        one at a time. Without it, two refreshes of one secret would look like a
        replayed secret and end the session.
        """
        if fcntl is None:
            yield
            return
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


class AuthClient:
    """
    Talks to Nest's CLI sign-in routes over HTTPS (`http://` only for localhost).

    Args:
        auth_url (`str`): Nest origin, such as `https://nest.example.com`.
        timeout (`float`): Seconds for each request.
    """

    def __init__(self, auth_url: str, *, timeout: float = 30.0):
        self.auth_url = auth_url
        self.timeout = timeout

    def request(
        self,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        bearer: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Sends a JSON `POST` (or a `GET` without `payload`) and returns status and body."""
        headers = {"Accept": "application/json", "User-Agent": "nest-cli"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(
            self.auth_url + path,
            data=data,
            method="POST" if payload is not None else "GET",
            headers=headers,
        )
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, ValueError) as exc:
            raise NestUnavailableError(
                f"Cannot reach Nest at {self.auth_url}: {type(exc).__name__}"
            ) from None
        with response:
            raw = response.read(_MAX_RESPONSE_BYTES)
            status = int(response.status or 0)
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if status >= 500:
            raise NestUnavailableError(f"Nest at {self.auth_url} is unavailable (HTTP {status})")
        return status, body


def _can_open_browser() -> bool:
    """Whether a graphical browser is likely: never take over the terminal with a
    text browser on a login node."""
    if sys.platform in ("darwin", "win32"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def default_client_name() -> str:
    """The label shown on the approval page: this machine's host name."""
    return f"nest CLI on {socket.gethostname() or 'an unnamed host'}"[:64]


def login(
    config: CLIAuthConfig | None = None,
    *,
    scope: str = DEFAULT_CLI_SCOPE,
    open_browser: bool = True,
    client_name: str | None = None,
    out: IO[str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    opener: Callable[[str], object] | None = None,
) -> NestSession:
    """
    Signs in through the browser and stores the new session.

    Shows an approval link and a code on `out` (standard error by default), opens
    the link in a browser when one is available, and waits until the User
    approves or denies it. A session stored before is revoked once the new one is
    saved.

    Args:
        config ([`config.CLIAuthConfig`], *optional*): Defaults to the environment.
        scope (`str`): Scopes to ask for.
        open_browser (`bool`): Try to open the link in a browser.
        client_name (`str`, *optional*): Label shown on the approval page.
        out (`IO[str]`, *optional*): Where to write instructions.
        sleep, opener: Replaced in tests.

    Raises:
        [`login.LoginDeniedError`], [`login.LoginExpiredError`],
        [`login.NestUnavailableError`] or [`login.AuthError`].
    """
    config = config or load_cli_auth_config()
    out = out or sys.stderr
    client = AuthClient(config.url)
    status, started = client.request(
        START_PATH, {"scope": scope, "client_name": client_name or default_client_name()}
    )
    if status != 200:
        raise AuthError(
            f"Nest refused to start a sign-in: {started.get('error') or f'HTTP {status}'}"
        )
    try:
        device_code = _secret(started["device_code"])
        user_code = str(started["user_code"])
        link = str(started["verification_uri_complete"])
        expires = float(started["expires_in"])
        interval = max(1.0, float(started.get("interval", 5)))
    except KeyError, TypeError, ValueError:
        raise AuthError("Nest answered the sign-in with an unexpected response") from None
    register_secret(device_code)
    if not link.startswith(config.url + "/"):
        raise AuthError("Nest sent an approval link for another site; not opening it")

    print("To sign in to Nest, open this link in a browser:", file=out)
    print(f"\n    {link}\n", file=out)
    print(f"and check that it shows the code {user_code}.", file=out)
    print("Nest will never ask for your NCHC password or one-time code there.", file=out)
    if open_browser and _can_open_browser():
        with contextlib.suppress(Exception):
            (opener or webbrowser.open)(link)
    print("Waiting for approval...", file=out, flush=True)

    deadline = time.monotonic() + expires
    while True:
        sleep(interval)
        if time.monotonic() > deadline:
            raise LoginExpiredError("The sign-in was not approved in time; run nest login again")
        status, answer = client.request(TOKEN_PATH, {"device_code": device_code})
        if status == 200:
            break
        error = answer.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise LoginDeniedError("The sign-in was denied in the browser")
        if error == "expired_token":
            raise LoginExpiredError("The sign-in was not approved in time; run nest login again")
        raise AuthError(f"Nest refused the sign-in: {error or f'HTTP {status}'}")

    session = NestSession.from_response(config.url, answer)
    store = CredentialStore(config.credentials_file)
    with store.locked():
        try:
            previous = store.load(config.url)
        except CredentialsFileError:
            previous = None
            store.clear()
        store.save(session)
    if previous is not None and previous.session_id != session.session_id:
        with contextlib.suppress(AuthError):
            client.request(LOGOUT_PATH, {"refresh_token": previous.refresh_token})
    print("Signed in to Nest.", file=out, flush=True)
    logger.info("Signed in to Nest user_id=%s session_id=%s", session.user_id, session.session_id)
    return session


def _refresh(client: AuthClient, session: NestSession) -> NestSession | None:
    """Rotates the session; `None` when Nest refuses it (expired or revoked)."""
    status, answer = client.request(REFRESH_PATH, {"refresh_token": session.refresh_token})
    if status != 200:
        return None
    return NestSession.from_response(session.auth_url, answer)


def ensure_session(
    config: CLIAuthConfig | None = None,
    *,
    interactive: bool = True,
    **login_options: Any,
) -> NestSession:
    """
    Returns a usable Nest session: the stored one, refreshed when its access token
    is about to expire, or, when there is none, a new one from [`login.login`],
    so no prior `nest login` is needed.

    Args:
        config ([`config.CLIAuthConfig`], *optional*): Defaults to the environment.
        interactive (`bool`): Sign in through the browser when needed. When `False`,
            raise [`login.NotSignedInError`] instead.
        **login_options: Passed to [`login.login`].

    Raises:
        [`login.NotSignedInError`]: If not signed in and `interactive` is `False`.
        [`login.AuthError`]: If signing in fails.
    """
    config = config or load_cli_auth_config()
    store = CredentialStore(config.credentials_file)
    with store.locked():
        # Read under the lock: another command may have just refreshed.
        session = store.load(config.url)
        if session is not None and session.access_usable():
            return session
        if session is not None and session.refreshable():
            refreshed = _refresh(AuthClient(config.url), session)
            if refreshed is not None:
                store.save(refreshed)
                return refreshed
            logger.info("The stored Nest session has ended")
        if session is not None:
            store.clear()
    if not interactive:
        raise NotSignedInError("Not signed in to Nest; run nest login")
    return login(config, **login_options)


@dataclass(frozen=True)
class LogoutResult:
    """
    What [`login.logout`] did.

    Attributes:
        removed (`bool`): Whether a stored session was deleted.
        revoked (`bool | None`): Whether Nest revoked it; `None` when not asked or
            Nest could not be reached.
    """

    removed: bool
    revoked: bool | None


def logout(config: CLIAuthConfig | None = None, *, revoke: bool = True) -> LogoutResult:
    """
    Revokes the stored session at Nest (unless `revoke` is `False`) and deletes it.

    The local file is deleted even when Nest cannot be reached, so signing out
    always ends the session on this machine.
    """
    config = config or load_cli_auth_config()
    store = CredentialStore(config.credentials_file)
    with store.locked():
        try:
            session = store.load(config.url)
        except CredentialsFileError:
            session = None
        revoked = None
        if session is not None and revoke:
            try:
                status, answer = AuthClient(config.url).request(
                    LOGOUT_PATH, {"refresh_token": session.refresh_token}
                )
                revoked = status == 200 and answer.get("signed_out") is True
            except NestUnavailableError:
                revoked = None
        removed = store.clear()
    if session is not None:
        logger.info("Signed out of Nest session_id=%s", session.session_id)
    return LogoutResult(removed=removed, revoked=revoked)


def status(config: CLIAuthConfig | None = None, *, check: bool = False) -> dict[str, object]:
    """
    Describes the stored session without its secrets.

    Args:
        check (`bool`): Also ask Nest whether the session is still accepted,
            refreshing it first when its access token has expired.

    Returns:
        `dict`: `signed_in`, `auth_url`, `credentials_file`, and for a stored
            session `user_id`, `session_id`, `scope` and the expiries; with `check`,
            `accepted` too.
    """
    config = config or load_cli_auth_config()
    store = CredentialStore(config.credentials_file)
    session = store.load(config.url)
    info: dict[str, object] = {
        "signed_in": session is not None and session.refreshable(),
        "auth_url": config.url,
        "credentials_file": config.credentials_file,
    }
    if session is None:
        return info
    if check:
        try:
            session = ensure_session(config, interactive=False)
            code, _ = AuthClient(config.url).request(SESSION_PATH, bearer=session.access_token)
            info["accepted"] = code == 200
        except NotSignedInError:
            info["accepted"] = False
        info["signed_in"] = info["accepted"]
    info.update(
        {
            "user_id": str(session.user_id),
            "session_id": str(session.session_id),
            "scope": session.scope,
            "access_expires_at": session.access_expires_at.isoformat(),
            "refresh_expires_at": session.refresh_expires_at.isoformat(),
            "session_expires_at": session.session_expires_at.isoformat(),
        }
    )
    return info
