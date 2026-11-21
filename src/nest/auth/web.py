"""
The Controller's auth listener: a small HTTP server for the browser side of
Google sign-in.

It serves these routes and nothing else:

- `GET /auth/google/login` stores a pending sign-in, sets the sign-in cookie, and
  redirects to Google.
- `GET <path of NEST_GOOGLE_REDIRECT_URL>` is Google's callback. It finishes the
  sign-in and sets the session (access token) and refresh cookies.
- `GET /auth/session` describes the session of the presented access token.
- `POST /auth/session/refresh` rotates the refresh session and issues a new access
  token.
- `POST /auth/session/logout` revokes the session and clears both cookies.
- `/auth/cli/*` serve browser-approved CLI sign-in (see [`auth.CLISignIn`]): the
  CLI's JSON routes `POST /auth/cli/start`, `/auth/cli/token`, `/auth/cli/refresh`
  and `/auth/cli/logout` and `GET /auth/cli/session`, and the browser's approval
  page `GET`/`POST /auth/cli/approve`.
- `/api/projects` and below serve the Projects API (see *Share Projects and manage
  roles*). Every request is authorized by [`authz.ProjectService`], and its
  invitation routes by [`authz.InvitationService`].
- `GET /api/projects/<id>/platforms` lists a Project's eligible HPC Platforms, its
  default Platform and what a request naming none resolves to, and `POST
  /api/projects/<id>/platforms/default` lets its Owners and Admins choose or clear
  that default among the eligible Platforms (see [`authz.PlatformCatalog`]).
- `/api/organizations` and below serve Organization CRUD and membership (see
  [`authz.OrganizationService`]).
- `GET /api/platforms` lists the HPC Platforms the signed-in User may use, with the
  server's default for them, and `GET /api/platforms/<id or slug>` describes one
  (see [`authz.PlatformCatalog`]). Platforms the User is not authorized for are
  answered as not found. No host, port or host key is ever returned.
- `/api/invitations` and below serve the signed-in User's own invitations, and
  `GET`/`POST /invitations/accept` is the browser page that reviews and accepts an
  invitation link (see *Invite people to a Project*).

The listener speaks plain HTTP. Put it behind a TLS-terminating reverse proxy that
serves `NEST_GOOGLE_REDIRECT_URL` and forwards paths unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, quote, urlsplit
from uuid import UUID

from .._logging import get_logger
from ..audit import audit_interface
from ..authz import (
    AccessDeniedError,
    InvitationService,
    Organization,
    OrganizationMembership,
    OrganizationRuleError,
    OrganizationService,
    PlatformCatalog,
    PlatformResolutionError,
    PlatformSummary,
    Principal,
    ProjectPlatforms,
    ProjectService,
)
from ..config import (
    AUTH_CLI_PREFIX,
    AUTH_SESSION_PATHS,
    CLI_SCOPES,
    INVITATION_PAGE_PATH,
    INVITATIONS_API_PATH,
    ORGANIZATIONS_API_PATH,
    PLATFORMS_API_PATH,
    PROJECTS_API_PATH,
    GoogleAuthConfig,
)
from ..db import (
    Acceptance,
    AccessGrant,
    AlreadyInvitedError,
    Database,
    DatabaseError,
    DecidingSessionNotActiveError,
    DuplicateMemberError,
    EmailInvitation,
    EmailMismatchError,
    InvitationNotFoundError,
    InvitationPreview,
    InvitationUnavailableError,
    InviteLink,
    MemberNotFoundError,
    Membership,
    OwnerRuleError,
    PendingInvitation,
    PlatformNotFoundError,
    Project,
    ProjectAuditEvent,
    ProjectNotFoundError,
    Session,
    SessionRefreshError,
    UserNotActiveError,
    UserNotFoundError,
    hash_token,
)
from .cli import (
    CLI_LOGIN_TTL,
    CLISignIn,
    CLISignInError,
    expires_in,
    format_user_code,
    normalize_user_code,
)
from .google import SIGN_IN_TTL, GoogleSignIn, SignInError

if TYPE_CHECKING:
    from ..mail import EmailService

logger = get_logger("nest.auth.web")

LOGIN_PATH = "/auth/google/login"
"""Route that starts a Google sign-in."""

SIGN_IN_COOKIE = "nest_google_signin"
"""Cookie binding a pending sign-in to the browser that started it."""

SESSION_PATH, REFRESH_PATH, LOGOUT_PATH = AUTH_SESSION_PATHS
"""Routes that describe, refresh and end the browser session."""

SESSION_COOKIE = "__Host-nest_session"
"""Browser session cookie, holding the short-lived access token, when the redirect
URL is `https://`."""

INSECURE_SESSION_COOKIE = "nest_session"
"""Browser session cookie for an `http://localhost` redirect URL, where browsers
refuse the `__Host-` prefix."""

REFRESH_COOKIE = "__Secure-nest_refresh"
"""Refresh cookie when the redirect URL is `https://`. Scoped to `/auth/session`,
so it is sent only to refresh and log out."""

INSECURE_REFRESH_COOKIE = "nest_refresh"
"""Refresh cookie for an `http://localhost` redirect URL."""

CLI_START_PATH = AUTH_CLI_PREFIX + "start"
"""CLI route that starts a browser-approved CLI sign-in."""

CLI_TOKEN_PATH = AUTH_CLI_PREFIX + "token"
"""CLI route polled with the device code until the sign-in is approved."""

CLI_APPROVE_PATH = AUTH_CLI_PREFIX + "approve"
"""Browser page where a signed-in User approves or denies a CLI sign-in."""

CLI_SESSION_PATH = AUTH_CLI_PREFIX + "session"
"""CLI route that describes the CLI session of a bearer access token."""

CLI_REFRESH_PATH = AUTH_CLI_PREFIX + "refresh"
"""CLI route that rotates a CLI session's refresh secret."""

CLI_LOGOUT_PATH = AUTH_CLI_PREFIX + "logout"
"""CLI route that revokes a CLI session."""

CLI_API_PATHS = (CLI_START_PATH, CLI_TOKEN_PATH, CLI_REFRESH_PATH, CLI_LOGOUT_PATH)
"""The CLI's JSON `POST` routes."""

CLI_RETURN_COOKIE = "nest_cli_approval"
"""Cookie holding the user code of a CLI approval that sent the browser to sign in
first, so the callback returns to that approval."""

INVITATION_RETURN_COOKIE = "nest_invitation_return"
"""Cookie set when the invitation page sent the browser to sign in first, so the
callback returns to that page."""

MAX_BODY_BYTES = 4096
"""Largest request body accepted."""

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    # The callback URL holds the authorization code: never pass it on as a referrer.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}

_PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>{title}</title></head><body><h1>{title}</h1><p>{message}</p>{link}</body></html>"
)

_APPROVE_PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>Approve CLI sign-in</title></head><body>"
    "<h1>Approve CLI sign-in</h1>"
    "<p>A Nest command line asks to sign in as you.</p>"
    "<p>Code: <strong>{code}</strong></p>"
    "<p>Client: {client}</p>"
    "<p>It will be able to:</p><ul>{scopes}</ul>"
    "<p>Approve only if you started <code>nest login</code> yourself just now and your "
    "terminal shows this same code. Nest will never ask you for your NCHC password "
    "or one-time code here.</p>"
    '<form method="post" action="{action}">'
    '<input type="hidden" name="code" value="{code}">'
    '<input type="hidden" name="csrf" value="{csrf}">'
    '<button type="submit" name="decision" value="approve">Approve</button> '
    '<button type="submit" name="decision" value="deny">Deny</button>'
    "</form></body></html>"
)

_CODE_PAGE = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>CLI sign-in</title></head><body><h1>CLI sign-in</h1>"
    "<p>{message}</p>"
    '<form method="get" action="{action}">'
    '<label>Code shown in your terminal: <input name="code" autocomplete="off" '
    'autocapitalize="characters" maxlength="16" required></label> '
    '<button type="submit">Continue</button></form></body></html>'
)


# The invitation page's only script. The token travels in the URL fragment, which
# browsers never send to a server; this moves it into the form (and, across a
# sign-in, into sessionStorage), and drops it from the address bar.
_INVITATION_SCRIPT = (
    "(function(){var k='nest_invitation',v=null,"
    "m=/(?:^|&)token=([^&]*)/.exec(location.hash.slice(1));"
    "if(m){try{v=decodeURIComponent(m[1]);}catch(e){v=null;}"
    "history.replaceState(null,'',location.pathname);}"
    "try{if(v){sessionStorage.setItem(k,v);}else{v=sessionStorage.getItem(k);}"
    "if(document.documentElement.hasAttribute('data-done')){sessionStorage.removeItem(k);}"
    "}catch(e){}"
    "var t=document.getElementById('token');if(t&&v&&!t.value){t.value=v;}})();"
)

_INVITATION_CSP = (
    "default-src 'none'; frame-ancestors 'none'; form-action 'self'; script-src 'sha256-"
    + base64.b64encode(hashlib.sha256(_INVITATION_SCRIPT.encode()).digest()).decode()
    + "'"
)

_INVITATION_PAGE = (
    '<!doctype html><html lang="en"{done}><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>{title}</title></head><body><h1>{title}</h1>{content}"
    "<script>"
    + _INVITATION_SCRIPT.replace("{", "{{").replace("}", "}}")
    + "</script></body></html>"
)


def _invitation_csrf(access_token: str, token: str) -> str:
    """The invitation form's CSRF token: bound to the browser's access token and the
    invitation token, so it is useless to another browser or for another invitation."""
    return hmac.new(
        access_token.encode(), b"nest invitation " + hash_token(token), "sha256"
    ).hexdigest()


def _approval_csrf(access_token: str, request_id: object) -> str:
    """The approval form's CSRF token: bound to the browser's access token and the
    request, so it is useless to another browser or for another request."""
    return hmac.new(
        access_token.encode(), f"nest cli approve {request_id}".encode(), "sha256"
    ).hexdigest()


def _cookie(
    name: str, value: str, *, path: str, max_age: int, secure: bool, same_site: str = "Lax"
) -> str:
    # Lax by default: sent on Google's top-level redirect back and on navigations.
    attributes = [f"{name}={value}", f"Path={path}", f"Max-Age={max_age}", "HttpOnly"]
    attributes.append(f"SameSite={same_site}")
    if secure:
        attributes.append("Secure")
    return "; ".join(attributes)


class _Handler(BaseHTTPRequestHandler):
    # Bounds how long an idle or slow client holds a worker thread.
    timeout = 30
    server_version = "Nest"
    sys_version = ""

    @property
    def app(self) -> AuthHTTPServer:
        return cast(AuthHTTPServer, self.server)

    def log_message(self, format, *args):
        # The default logs the request line, whose query holds the code and state.
        pass

    def _send(
        self,
        status: int,
        body: str = "",
        headers: dict[str, str] | None = None,
        content_type: str = "text/html; charset=utf-8",
    ):
        data = body.encode()
        self.send_response(status)
        for name, value in {**_SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(name, value)
        if self._set_cookies:
            for value in self._set_cookies:
                self.send_header("Set-Cookie", value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        logger.info(
            "Auth request method=%s path=%s status=%s",
            self.command,
            urlsplit(self.path).path[:200],
            status,
        )

    def _json(self, status: int, payload: dict[str, object], headers: dict[str, str] | None = None):
        self._send(status, json.dumps(payload), headers, content_type="application/json")

    def _fail(self, status: int, title: str, message: str):
        """Answers an error in the route's format: JSON for session routes."""
        path = urlsplit(self.path).path
        if (
            path in AUTH_SESSION_PATHS
            or path in CLI_API_PATHS
            or path == CLI_SESSION_PATH
            or _is_api(path)
        ):
            self._json(status, {"error": title.lower().replace(" ", "_")})
        else:
            self._page(status, title, message)

    def _page(self, status: int, title: str, message: str, *, retry: bool = False):
        link = f'<p><a href="{html.escape(LOGIN_PATH)}">Sign in again</a></p>' if retry else ""
        self._send(
            status,
            _PAGE.format(title=html.escape(title), message=html.escape(message), link=link),
        )

    def _dispatch(self, route):
        self._set_cookies: list[str] = []
        try:
            with audit_interface("http"):
                route()
        except DatabaseError:
            logger.error("Auth request failed: the database is unavailable")
            self._fail(
                HTTPStatus.SERVICE_UNAVAILABLE, "Unavailable", "Nest is unavailable. Try later."
            )
        except Exception:
            logger.exception("Auth request failed")
            self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, "Error", "Sign-in failed.")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == LOGIN_PATH:
            self._dispatch(self._login)
        elif path == self.app.callback_path:
            self._dispatch(self._callback)
        elif path == SESSION_PATH:
            self._dispatch(self._describe)
        elif path in (REFRESH_PATH, LOGOUT_PATH) or path in CLI_API_PATHS:
            self._dispatch(lambda: self._not_allowed("POST"))
        elif path == CLI_APPROVE_PATH:
            self._dispatch(self._cli_approve_page)
        elif path == CLI_SESSION_PATH:
            self._dispatch(self._cli_describe)
        elif path == INVITATION_PAGE_PATH:
            self._dispatch(self._invitation_page)
        elif _is_api(path):
            self._dispatch(lambda: self._api("GET", path))
        else:
            self._dispatch(
                lambda: self._fail(HTTPStatus.NOT_FOUND, "Not found", "There is nothing here.")
            )

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == REFRESH_PATH:
            self._dispatch(lambda: self._same_origin() and self._refresh())
        elif path == LOGOUT_PATH:
            self._dispatch(lambda: self._same_origin() and self._logout())
        elif path in CLI_API_PATHS:
            self._dispatch(lambda: self._cli_api(path))
        elif path == CLI_APPROVE_PATH:
            self._dispatch(lambda: self._same_origin(require_origin=True) and self._cli_decide())
        elif path == INVITATION_PAGE_PATH:
            self._dispatch(
                lambda: self._same_origin(require_origin=True) and self._invitation_decide()
            )
        elif _is_api(path):
            self._dispatch(lambda: self._api("POST", path))
        else:
            self.do_HEAD()

    def do_HEAD(self):
        path = urlsplit(self.path).path
        if path in (REFRESH_PATH, LOGOUT_PATH) or path in CLI_API_PATHS:
            allow = "POST"
        elif path in (CLI_APPROVE_PATH, INVITATION_PAGE_PATH) or _is_api(path):
            allow = "GET, POST"
        else:
            allow = "GET"
        self._dispatch(lambda: self._not_allowed(allow))

    # HEAD too: it would otherwise start a sign-in or consume a callback's state.
    do_PUT = do_DELETE = do_PATCH = do_HEAD

    def _not_allowed(self, allow: str):
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, headers={"Allow": allow})

    def _login(self):
        sign_in = self.app.sign_in
        try:
            started = sign_in.start()
        except SignInError as exc:
            self._page(exc.status, "Cannot sign in", str(exc))
            return
        self._set_cookies.append(
            _cookie(
                SIGN_IN_COOKIE,
                started.binding,
                path=self.app.callback_path,
                max_age=int(SIGN_IN_TTL),
                secure=self.app.secure,
            )
        )
        cli_code = normalize_user_code(self._query_value("cli_code"))
        if cli_code is not None:
            self._set_cookies.append(
                _cookie(
                    CLI_RETURN_COOKIE,
                    cli_code,
                    path=self.app.callback_path,
                    max_age=int(SIGN_IN_TTL),
                    secure=self.app.secure,
                )
            )
        elif self._query_value("next") == "invitation":
            self._set_cookies.append(
                _cookie(
                    INVITATION_RETURN_COOKIE,
                    "1",
                    path=self.app.callback_path,
                    max_age=int(SIGN_IN_TTL),
                    secure=self.app.secure,
                )
            )
        self._send(HTTPStatus.FOUND, headers={"Location": started.authorization_url})

    def _query_value(self, name: str) -> str | None:
        query = parse_qs(urlsplit(self.path).query, max_num_fields=20)
        values = query.get(name)
        return values[0] if values and len(values) == 1 else None

    def _callback(self):
        query = parse_qs(urlsplit(self.path).query, max_num_fields=20)

        def one(name: str) -> str | None:
            values = query.get(name)
            return values[0] if values and len(values) == 1 else None

        # Clear the sign-in cookie whatever happens: it is good for one callback.
        self._set_cookies.append(
            _cookie(
                SIGN_IN_COOKIE,
                "",
                path=self.app.callback_path,
                max_age=0,
                secure=self.app.secure,
            )
        )
        cli_code = normalize_user_code(self._read_cookie(CLI_RETURN_COOKIE))
        if cli_code is not None:
            self._set_cookies.append(
                _cookie(
                    CLI_RETURN_COOKIE,
                    "",
                    path=self.app.callback_path,
                    max_age=0,
                    secure=self.app.secure,
                )
            )
        to_invitation = self._read_cookie(INVITATION_RETURN_COOKIE) == "1"
        if to_invitation:
            self._set_cookies.append(
                _cookie(
                    INVITATION_RETURN_COOKIE,
                    "",
                    path=self.app.callback_path,
                    max_age=0,
                    secure=self.app.secure,
                )
            )
        try:
            result = self.app.sign_in.finish(
                state=one("state"),
                code=one("code"),
                binding=self._read_cookie(SIGN_IN_COOKIE),
                error="denied" if "error" in query else None,
            )
        except SignInError as exc:
            logger.info("Sign-in refused reason=%s", exc.reason)
            self._page(exc.status, "Sign-in failed", str(exc), retry=True)
            return
        self._set_session_cookies(
            result.access_token,
            result.access_expires_at,
            result.refresh_token,
            result.session.expires_at,
        )
        if cli_code is not None:
            # Only ever our own approval page, with a code that passed validation.
            location = f"{CLI_APPROVE_PATH}?code={quote(format_user_code(cli_code))}"
            self._send(HTTPStatus.SEE_OTHER, headers={"Location": location})
            return
        if to_invitation:
            self._send(HTTPStatus.SEE_OTHER, headers={"Location": INVITATION_PAGE_PATH})
            return
        joined = getattr(result, "joined", ())
        message = f"You are signed in to Nest as {result.identity.email}."
        if joined:
            count = len(joined)
            message += f" Your invitations added you to {count} Project{'s' * (count != 1)}."
        self._page(HTTPStatus.OK, "Signed in", message + " You can close this page.")

    # Browser sessions --------------------------------------------------------

    def _set_session_cookies(
        self,
        access_token: str,
        access_expires_at: datetime,
        refresh_token: str,
        refresh_expires_at: datetime,
    ):
        now = datetime.now(UTC)
        self._set_cookies.append(
            _cookie(
                self.app.session_cookie,
                access_token,
                path="/",
                max_age=max(1, round((access_expires_at - now).total_seconds())),
                secure=self.app.secure,
            )
        )
        self._set_cookies.append(
            _cookie(
                self.app.refresh_cookie,
                refresh_token,
                path=SESSION_PATH,
                max_age=max(1, round((refresh_expires_at - now).total_seconds())),
                secure=self.app.secure,
                # Only Nest's own pages refresh: never sent on cross-site requests.
                same_site="Strict",
            )
        )

    def _clear_session_cookies(self):
        self._set_cookies.append(
            _cookie(self.app.session_cookie, "", path="/", max_age=0, secure=self.app.secure)
        )
        self._set_cookies.append(
            _cookie(
                self.app.refresh_cookie,
                "",
                path=SESSION_PATH,
                max_age=0,
                secure=self.app.secure,
                same_site="Strict",
            )
        )

    def _same_origin(self, *, require_origin: bool = False) -> bool:
        """
        Refuses a cross-site POST. Browsers send `Origin` on every POST and
        `Sec-Fetch-Site` on most; a client that sends neither is not a browser that
        another site could drive. With `require_origin`, a POST without `Origin` is
        refused too, for routes only a browser form uses.
        """
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        if (
            (origin is not None and origin != self.app.origin)
            or (origin is None and require_origin)
            or (site is not None and site != "same-origin")
        ):
            logger.warning("Session request refused reason=cross_origin")
            self._json(HTTPStatus.FORBIDDEN, {"error": "cross_origin"})
            return False
        return True

    def _describe(self):
        grant = self._browser_grant()
        if grant is None:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthenticated"})
            return
        self._json(
            HTTPStatus.OK,
            {
                "user_id": str(grant.session.user_id),
                "access_expires_at": grant.access_expires_at.isoformat(),
                "refresh_expires_at": grant.session.expires_at.isoformat(),
                "session_expires_at": grant.session.family_expires_at.isoformat(),
            },
        )

    def _refresh(self):
        token = self._read_cookie(self.app.refresh_cookie)
        if not token:
            self._clear_session_cookies()
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthenticated"})
            return
        policy = self.app.sign_in.sessions
        new_refresh, new_access = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        try:
            rotation = self.app.sessions.rotate(
                hash_token(token),
                new_refresh_hash=hash_token(new_refresh),
                new_access_hash=hash_token(new_access),
                refresh_ttl=policy.refresh_ttl,
                access_ttl=policy.access_token_ttl,
                kind="browser",
            )
        except SessionRefreshError:
            # The reason is logged; the browser learns only that it must sign in.
            self._clear_session_cookies()
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthenticated"})
            return
        self._set_session_cookies(
            new_access, rotation.access_expires_at, new_refresh, rotation.session.expires_at
        )
        self._json(
            HTTPStatus.OK,
            {
                "user_id": str(rotation.session.user_id),
                "access_expires_at": rotation.access_expires_at.isoformat(),
                "refresh_expires_at": rotation.session.expires_at.isoformat(),
                "session_expires_at": rotation.session.family_expires_at.isoformat(),
            },
        )

    def _logout(self):
        session_id = None
        refresh = self._read_cookie(self.app.refresh_cookie)
        if refresh:
            found = self.app.sessions.find_by_refresh(hash_token(refresh), kind="browser")
            session_id = found.id if found else None
        if session_id is None:
            grant = self._browser_grant()
            session_id = grant.session.id if grant else None
        if session_id is not None:
            self.app.sign_in.identities.revoke_session(session_id, reason="logout")
            logger.info("Signed out session_id=%s", session_id)
        self._clear_session_cookies()
        self._json(HTTPStatus.OK, {"signed_out": True})

    def _browser_grant(self) -> AccessGrant | None:
        """The browser session of the presented session cookie, or `None`."""
        token = self._read_cookie(self.app.session_cookie)
        return self.app.sessions.authenticate(hash_token(token), kind="browser") if token else None

    # CLI sign-in: the browser's approval page --------------------------------

    def _cli_approve_page(self):
        raw = self._query_value("code")
        code = normalize_user_code(raw)
        grant = self._browser_grant()
        if grant is None:
            # Sign in with Google first; the callback comes back to this code.
            location = LOGIN_PATH + (f"?cli_code={quote(code)}" if code else "")
            self._send(HTTPStatus.SEE_OTHER, headers={"Location": location})
            return
        if code is None:
            message = (
                "Enter the code shown by nest login."
                if raw is None
                else "That is not a valid code. Check the code in your terminal."
            )
            self._send(
                HTTPStatus.OK if raw is None else HTTPStatus.BAD_REQUEST,
                _CODE_PAGE.format(message=html.escape(message), action=CLI_APPROVE_PATH),
            )
            return
        request = self.app.cli.find(code)
        if request is None:
            self._page(
                HTTPStatus.NOT_FOUND,
                "Unknown code",
                "This code is unknown, already used or expired. Run nest login again.",
            )
            return
        token = self._read_cookie(self.app.session_cookie) or ""
        scopes = "".join(
            f"<li>{html.escape(CLI_SCOPES.get(scope, scope))}</li>"
            for scope in request.scope.split()
        )
        self._send(
            HTTPStatus.OK,
            _APPROVE_PAGE.format(
                code=html.escape(format_user_code(code)),
                client=html.escape(request.client_name),
                scopes=scopes,
                action=CLI_APPROVE_PATH,
                csrf=_approval_csrf(token, request.id),
            ),
        )

    def _cli_decide(self):
        form = self._read_form()
        grant = self._browser_grant()
        token = self._read_cookie(self.app.session_cookie) or ""
        if grant is None:
            self._page(
                HTTPStatus.UNAUTHORIZED,
                "Not signed in",
                "Sign in to Nest, then open the approval link again.",
            )
            return
        code = normalize_user_code(form.get("code"))
        decision = form.get("decision")
        request = self.app.cli.find(code) if code else None
        if request is None or decision not in ("approve", "deny"):
            self._page(
                HTTPStatus.NOT_FOUND,
                "Unknown code",
                "This code is unknown, already used or expired. Run nest login again.",
            )
            return
        csrf = form.get("csrf") or ""
        if not hmac.compare_digest(csrf.encode(), _approval_csrf(token, request.id).encode()):
            logger.warning("CLI approval refused reason=csrf request_id=%s", request.id)
            self._page(
                HTTPStatus.FORBIDDEN,
                "Not approved",
                "This approval did not come from Nest's page. Open the approval link again.",
            )
            return
        approve = decision == "approve"
        try:
            decided = self.app.cli.decide(
                request,
                approve=approve,
                user_id=grant.session.user_id,
                session_id=grant.session.id,
            )
        except DecidingSessionNotActiveError, UserNotActiveError:
            # Signed out, revoked or disabled since the session was checked above.
            # CLISignIn already reports a disabled User as DecidingSessionNotActiveError;
            # UserNotActiveError is caught too for a replacement `cli` service.
            logger.warning("CLI approval refused reason=session_ended request_id=%s", request.id)
            self._page(
                HTTPStatus.UNAUTHORIZED,
                "Not signed in",
                "Your Nest session has ended. Sign in again, then open the approval link.",
            )
            return
        if decided is None:
            self._page(
                HTTPStatus.CONFLICT,
                "Not approved",
                "This sign-in is no longer waiting for approval. Run nest login again.",
            )
        elif approve:
            self._page(
                HTTPStatus.OK,
                "CLI sign-in approved",
                "The command line is now signed in. You can close this page and return "
                "to your terminal.",
            )
        else:
            self._page(
                HTTPStatus.OK,
                "CLI sign-in denied",
                "The command line was not signed in. You can close this page.",
            )

    def _read_body(self) -> bytes | None:
        """The request body, or `None` (after answering) when it is too large."""
        raw = self.headers.get("Content-Length") or "0"
        length = int(raw) if raw.isdecimal() else -1
        if not 0 <= length <= MAX_BODY_BYTES:
            self.close_connection = True
            self._fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Too large", "Request too large.")
            return None
        return self.rfile.read(length) if length else b""

    def _read_form(self) -> dict[str, str]:
        body = self._read_body() or b""
        try:
            fields = parse_qs(body.decode(), max_num_fields=10)
        except UnicodeDecodeError, ValueError:
            return {}
        return {name: values[0] for name, values in fields.items() if len(values) == 1}

    # Invitations: the browser's review and accept page ----------------------

    def _invitation_send(self, status: int, title: str, content: str, *, done: bool = False):
        self._send(
            status,
            _INVITATION_PAGE.format(
                title=html.escape(title), content=content, done=" data-done" if done else ""
            ),
            # A form POST from a `no-referrer` page carries `Origin: null`, which
            # the same-origin check refuses. `same-origin` keeps referrers off
            # every other site; the token is only ever in the fragment, which
            # browsers never put in a referrer.
            headers={"Content-Security-Policy": _INVITATION_CSP, "Referrer-Policy": "same-origin"},
        )

    def _invitation_message(self, status: int, title: str, message: str, *, done: bool = False):
        self._invitation_send(status, title, f"<p>{html.escape(message)}</p>", done=done)

    def _invitation_page(self):
        """
        The page an invitation link opens. It only shows a form: opening a link
        never joins a Project. Without a session it offers sign-in and keeps the
        token in this tab's sessionStorage for the way back.
        """
        if self._browser_grant() is None:
            login = html.escape(LOGIN_PATH + "?next=invitation")
            self._invitation_send(
                HTTPStatus.OK,
                "Nest invitation",
                "<p>Sign in to Nest to see this invitation. Use the Google account of "
                "the address the invitation was sent to.</p>"
                f'<p><a href="{login}">Sign in with Google</a></p>',
            )
            return
        self._invitation_send(
            HTTPStatus.OK,
            "Nest invitation",
            f'<form method="post" action="{INVITATION_PAGE_PATH}">'
            '<p><label>Invitation code: <input id="token" name="token" autocomplete="off" '
            'maxlength="200" required></label></p>'
            '<input type="hidden" name="step" value="review">'
            '<button type="submit">Review the invitation</button></form>',
        )

    def _invitation_decide(self):
        form = self._read_form()
        grant = self._browser_grant()
        if grant is None:
            self._invitation_message(
                HTTPStatus.UNAUTHORIZED,
                "Not signed in",
                "Sign in to Nest, then open the invitation link again.",
            )
            return
        principal = Principal.from_session(grant.session)
        access = self._read_cookie(self.app.session_cookie) or ""
        token = (form.get("token") or "").strip()
        step = form.get("step")
        if not token or len(token) > 200 or step not in ("review", "accept", "decline"):
            self._invitation_message(
                HTTPStatus.BAD_REQUEST, "Invitation not found", "Check the invitation link."
            )
            return
        service = self.app.invitations
        try:
            if step == "review":
                self._invitation_review(service.preview(principal, token), token, access)
                return
            csrf = form.get("csrf") or ""
            if not hmac.compare_digest(csrf.encode(), _invitation_csrf(access, token).encode()):
                logger.warning("Invitation decision refused reason=csrf")
                self._invitation_message(
                    HTTPStatus.FORBIDDEN,
                    "Not accepted",
                    "This decision did not come from Nest's page. Open the invitation again.",
                )
                return
            outcome = service.redeem(principal, token, accept=step == "accept")
        except InvitationNotFoundError:
            self._invitation_message(
                HTTPStatus.NOT_FOUND,
                "Invitation not found",
                "This invitation link is unknown or was replaced by a newer one.",
            )
            return
        except EmailMismatchError:
            self._invitation_message(
                HTTPStatus.FORBIDDEN,
                "Wrong account",
                "This invitation is for another email address. Sign in with the Google "
                "account of the address it was sent to.",
            )
            return
        except InvitationUnavailableError as exc:
            self._invitation_message(
                HTTPStatus.GONE,
                "Invitation not available",
                _UNAVAILABLE.get(exc.reason, "This invitation can no longer be used."),
            )
            return
        except DuplicateMemberError:
            self._invitation_message(
                HTTPStatus.CONFLICT, "Already a member", "You are already in this Project."
            )
            return
        if isinstance(outcome, Acceptance):
            role = outcome.role.capitalize()
            message = (
                f"You were already in this Project; your role is still {role}."
                if outcome.already_member
                else f"You joined the Project as {role}."
            )
            self._invitation_message(HTTPStatus.OK, "Invitation accepted", message, done=True)
        else:
            self._invitation_message(
                HTTPStatus.OK, "Invitation declined", "You did not join the Project.", done=True
            )

    def _invitation_review(self, preview: InvitationPreview, token: str, access: str):
        if preview.status not in ("pending", "active"):
            self._invitation_message(
                HTTPStatus.GONE,
                "Invitation not available",
                _UNAVAILABLE.get(preview.status, "This invitation can no longer be used."),
            )
            return
        decline = (
            ' <button type="submit" name="step" value="decline">Decline</button>'
            if preview.kind == "email"
            else ""
        )
        self._invitation_send(
            HTTPStatus.OK,
            "Join a Nest Project",
            f"<p>You are invited to join <strong>{html.escape(preview.project_name)}</strong> "
            f"as <strong>{html.escape(preview.role.capitalize())}</strong>.</p>"
            f"<p>The invitation expires {html.escape(preview.expires_at.isoformat())}.</p>"
            "<p>Joining gives you this Project role only. It grants no HPC, Slurm or "
            "Provider account.</p>"
            f'<form method="post" action="{INVITATION_PAGE_PATH}">'
            f'<input type="hidden" name="token" value="{html.escape(token)}">'
            f'<input type="hidden" name="csrf" value="{_invitation_csrf(access, token)}">'
            '<button type="submit" name="step" value="accept">Accept</button>'
            f"{decline}</form>",
        )

    # CLI sign-in: the CLI's JSON routes ---------------------------------------

    def _cli_api(self, path: str):
        """
        Checks and parses a CLI request, then runs its route. A browser always sends
        `Origin` on a POST, so a request with one is refused: no web page can drive
        these routes. They read no cookies; the CLI sends its secrets in the body
        or the `Authorization` header, never in the URL.
        """
        if self.headers.get("Origin") is not None or self.headers.get("Sec-Fetch-Site"):
            logger.warning("CLI request refused reason=browser_origin")
            self._json(HTTPStatus.FORBIDDEN, {"error": "cross_origin"})
            return
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "invalid_request"})
            return
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
            return
        {
            CLI_START_PATH: self._cli_start,
            CLI_TOKEN_PATH: self._cli_token,
            CLI_REFRESH_PATH: self._cli_refresh,
            CLI_LOGOUT_PATH: self._cli_logout,
        }[path](payload)

    def _cli_start(self, payload: dict):
        try:
            started = self.app.cli.start(
                scope=payload.get("scope"), client_name=payload.get("client_name")
            )
        except CLISignInError as exc:
            self._json(exc.status, {"error": exc.reason, "error_description": str(exc)})
            return
        verification = self.app.origin + CLI_APPROVE_PATH
        self._json(
            HTTPStatus.OK,
            {
                "device_code": started.device_code,
                "user_code": started.user_code,
                "verification_uri": verification,
                "verification_uri_complete": f"{verification}?code={quote(started.user_code)}",
                "expires_in": int(CLI_LOGIN_TTL),
                "interval": self.app.cli.poll_interval,
                "scope": started.request.scope,
            },
        )

    def _cli_session_payload(
        self, session: Session, access_expires_at: datetime, **secrets_: str
    ) -> dict[str, object]:
        now = datetime.now(UTC)
        return {
            **secrets_,
            "token_type": "Bearer",
            "expires_in": expires_in(access_expires_at, now),
            "user_id": str(session.user_id),
            "session_id": str(session.id),
            "scope": session.scope or "",
            "access_expires_at": access_expires_at.isoformat(),
            "refresh_expires_at": session.expires_at.isoformat(),
            "session_expires_at": session.family_expires_at.isoformat(),
        }

    def _cli_token(self, payload: dict):
        tokens = self.app.cli.redeem(payload.get("device_code"))
        poll = tokens.poll
        if poll.outcome != "issued":
            self._json(HTTPStatus.BAD_REQUEST, {"error": poll.outcome})
            return
        assert poll.session is not None and poll.access_expires_at is not None
        self._json(
            HTTPStatus.OK,
            self._cli_session_payload(
                poll.session,
                poll.access_expires_at,
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
            ),
        )

    def _cli_refresh(self, payload: dict):
        token = payload.get("refresh_token")
        if not isinstance(token, str) or not token:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthenticated"})
            return
        policy = self.app.sign_in.sessions
        new_refresh, new_access = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        try:
            rotation = self.app.sessions.rotate(
                hash_token(token),
                new_refresh_hash=hash_token(new_refresh),
                new_access_hash=hash_token(new_access),
                refresh_ttl=policy.refresh_ttl,
                access_ttl=policy.access_token_ttl,
                kind="cli",
            )
        except SessionRefreshError:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthenticated"})
            return
        self._json(
            HTTPStatus.OK,
            self._cli_session_payload(
                rotation.session,
                rotation.access_expires_at,
                access_token=new_access,
                refresh_token=new_refresh,
            ),
        )

    def _bearer_grant(self) -> AccessGrant | None:
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return self.app.sessions.authenticate(hash_token(token.strip()), kind="cli")

    def _cli_logout(self, payload: dict):
        session_id = None
        token = payload.get("refresh_token")
        if isinstance(token, str) and token:
            found = self.app.sessions.find_by_refresh(hash_token(token), kind="cli")
            session_id = found.id if found else None
        if session_id is None:
            grant = self._bearer_grant()
            session_id = grant.session.id if grant else None
        if session_id is not None:
            self.app.sign_in.identities.revoke_session(session_id, reason="logout")
            logger.info("CLI signed out session_id=%s", session_id)
        self._json(HTTPStatus.OK, {"signed_out": session_id is not None})

    def _cli_describe(self):
        grant = self._bearer_grant()
        if grant is None:
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "unauthenticated"},
                headers={"WWW-Authenticate": 'Bearer realm="nest"'},
            )
            return
        self._json(HTTPStatus.OK, self._cli_session_payload(grant.session, grant.access_expires_at))

    # Projects API ------------------------------------------------------------

    def _principal(self) -> Principal | None:
        """
        The caller: a CLI session by its bearer token when the request carries an
        `Authorization` header, otherwise the browser session cookie.
        """
        if self.headers.get("Authorization") is not None:
            grant = self._bearer_grant()
        else:
            grant = self._browser_grant()
        return Principal.from_session(grant.session) if grant else None

    def _api(self, method: str, path: str):
        """Authenticates, parses and routes a Projects API request."""
        principal = self._principal()
        if principal is None:
            self._json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "unauthenticated"},
                headers={"WWW-Authenticate": 'Bearer realm="nest"'},
            )
            return
        payload: dict = {}
        if method == "POST":
            # A browser session's change must come from Nest's own pages.
            if principal.session_kind == "browser" and not self._same_origin(require_origin=True):
                return
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "invalid_request"})
                return
            body = self._read_body()
            if body is None:
                return
            try:
                parsed = json.loads(body or b"{}")
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
                return
            payload = parsed
        try:
            self._api_call(method, path, principal, payload)
        except AccessDeniedError as exc:
            if exc.reason == "member_not_found":
                self._json(HTTPStatus.NOT_FOUND, {"error": "member_not_found"})
            elif exc.visible:
                self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden", "reason": exc.reason})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except ProjectNotFoundError, PlatformNotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except MemberNotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "member_not_found"})
        except UserNotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "user_not_found"})
        except DuplicateMemberError:
            self._json(HTTPStatus.CONFLICT, {"error": "already_member"})
        except AlreadyInvitedError as exc:
            self._json(
                HTTPStatus.CONFLICT,
                {"error": "already_invited", "invitation_id": exc.invitation_id},
            )
        except InvitationNotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "invitation_not_found"})
        except EmailMismatchError:
            self._json(HTTPStatus.FORBIDDEN, {"error": "email_mismatch"})
        except InvitationUnavailableError as exc:
            self._json(HTTPStatus.GONE, {"error": "invitation_unavailable", "reason": exc.reason})
        except OwnerRuleError:
            self._json(HTTPStatus.CONFLICT, {"error": "owner_rule"})
        except OrganizationRuleError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": "organization_rule", "reason": exc.reason})
        except LookupError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "user_not_found"})
        except ValueError, TypeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})

    def _api_call(self, method: str, path: str, who: Principal, body: dict):
        """Runs one Projects API route. Authorization happens in the service."""
        if path == INVITATIONS_API_PATH or path.startswith(INVITATIONS_API_PATH + "/"):
            self._own_invitations_api(method, path, who, body)
            return
        if path == PLATFORMS_API_PATH or path.startswith(PLATFORMS_API_PATH + "/"):
            self._platforms_api(method, path, who)
            return
        if path == ORGANIZATIONS_API_PATH or path.startswith(ORGANIZATIONS_API_PATH + "/"):
            self._organizations_api(method, path, who, body)
            return
        service = self.app.projects
        rest = path[len(PROJECTS_API_PATH) :].strip("/")
        parts = rest.split("/") if rest else []
        if len(parts) >= 2 and parts[1] in ("invitations", "invite-links"):
            self._project_invitations_api(method, parts, who, body)
            return
        if len(parts) >= 2 and parts[1] == "platforms":
            self._project_platforms_api(method, parts, who, body)
            return
        ids = [_parse_uuid(part) for part in parts[0:1] + parts[2:3]]
        pid, uid = (ids + [None, None])[:2]
        if None in ids:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        shape = tuple("{id}" if i in (0, 2) else part for i, part in enumerate(parts))
        allowed = _API_ROUTES.get(shape)
        if allowed is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if method not in allowed:
            self._not_allowed(", ".join(allowed))
            return
        if not parts:
            if method == "GET":
                projects = [_project_json(p, r) for p, r in service.list_projects(who)]
                self._json(HTTPStatus.OK, {"projects": projects})
            else:
                organization = body.get("organization_id")
                if organization is not None and not isinstance(organization, str):
                    raise TypeError("organization_id must be a string")
                project = service.create_project(
                    who, _string(body, "name"), organization_id=organization
                )
                self._json(HTTPStatus.CREATED, _project_json(project, "owner"))
            return
        assert pid is not None
        action = parts[-1] if len(parts) > 1 else ""
        if shape == ("{id}",):
            self._json(HTTPStatus.OK, _project_json(*service.get_project(who, pid)))
        elif action == "rename":
            project = service.rename_project(who, pid, _string(body, "name"))
            self._json(HTTPStatus.OK, _project_json(project, None))
        elif action == "delete":
            self._json(HTTPStatus.OK, _project_json(service.delete_project(who, pid), None))
        elif action == "transfer":
            previous = body.get("previous_owner_role", "admin")
            if not isinstance(previous, str):
                raise TypeError("previous_owner_role must be a string")
            project = service.transfer_ownership(
                who, pid, _string(body, "user_id"), previous_owner_role=previous
            )
            self._json(HTTPStatus.OK, _project_json(project, None))
        elif action == "members" and method == "GET":
            members = [_member_json(m) for m in service.members(who, pid)]
            self._json(HTTPStatus.OK, {"members": members})
        elif action == "members":
            member = service.add_member(who, pid, _string(body, "user_id"), _string(body, "role"))
            self._json(HTTPStatus.CREATED, _member_json(member))
        elif action == "audit":
            limit = int(self._query_value("limit") or 100)
            before = _optional_int(self._query_value("before"))
            events = service.audit_history(who, pid, limit=limit, before_id=before)
            self._json(HTTPStatus.OK, {"events": [_audit_json(e) for e in events]})
        else:
            assert uid is not None
            if action == "role":
                member = service.change_role(who, pid, uid, _string(body, "role"))
                self._json(HTTPStatus.OK, _member_json(member))
            else:
                service.remove_member(who, pid, uid)
                self._json(HTTPStatus.OK, {"removed": True})

    def _platforms_api(self, method: str, path: str, who: Principal):
        """`/api/platforms`: discovery of the caller's authorized HPC Platforms."""
        rest = path[len(PLATFORMS_API_PATH) :].strip("/")
        parts = rest.split("/") if rest else []
        if len(parts) > 1:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._route(method, ("GET",)):
            return
        catalog = self.app.platforms
        project_id = self._query_value("project_id")
        if project_id is not None and _parse_uuid(project_id) is None:
            raise ValueError("project_id is not a valid UUID")
        if parts:
            platform = catalog.resolve_platform(who, parts[0], project_id=project_id)
            summary = next(
                (
                    s
                    for s in catalog.list_platforms(who, project_id=project_id)
                    if s.platform.id == platform.id
                ),
                None,
            )
            if summary is None:  # Access ended between the two reads.
                raise PlatformNotFoundError("No such Platform")
            self._json(HTTPStatus.OK, _platform_json(summary))
            return
        summaries = catalog.list_platforms(who, project_id=project_id)
        try:
            default = str(catalog.resolve_platform(who, project_id=project_id).id)
        except PlatformResolutionError:
            default = None
        self._json(
            HTTPStatus.OK,
            {"platforms": [_platform_json(s) for s in summaries], "default_platform_id": default},
        )

    def _project_platforms_api(self, method: str, parts: list[str], who: Principal, body: dict):
        """`/api/projects/<id>/platforms[/default]`: Project-to-Platform eligibility."""
        pid = _parse_uuid(parts[0])
        shape = tuple(parts[1:])
        allowed = {("platforms",): ("GET",), ("platforms", "default"): ("POST",)}.get(shape)
        if pid is None:
            allowed = None
        if not self._route(method, allowed):
            return
        assert pid is not None
        catalog = self.app.platforms
        if shape == ("platforms",):
            self._json(HTTPStatus.OK, _project_platforms_json(catalog.project_platforms(who, pid)))
            return
        if "platform" not in body:
            raise TypeError("platform must be given, or null to clear the default")
        reference = body["platform"]
        if reference is not None and not isinstance(reference, str):
            raise TypeError("platform must be a string or null")
        view = catalog.set_project_default(who, pid, reference)
        self._json(HTTPStatus.OK, _project_platforms_json(view))

    def _organizations_api(self, method: str, path: str, who: Principal, body: dict):
        """`/api/organizations`: Organization CRUD and membership."""
        service = self.app.organizations
        rest = path[len(ORGANIZATIONS_API_PATH) :].strip("/")
        parts = rest.split("/") if rest else []
        ids = [_parse_uuid(part) for part in parts[0:1] + parts[2:3]]
        if None in ids:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        oid, uid = (ids + [None, None])[:2]
        shape = tuple("{id}" if i in (0, 2) else part for i, part in enumerate(parts))
        if not self._route(method, _ORGANIZATION_ROUTES.get(shape)):
            return
        if not parts:
            if method == "GET":
                items = [_organization_json(o, r) for o, r in service.list_organizations(who)]
                self._json(HTTPStatus.OK, {"organizations": items})
            else:
                organization = service.create_organization(who, _string(body, "name"))
                self._json(HTTPStatus.CREATED, _organization_json(organization, "org_admin"))
            return
        assert oid is not None
        action = parts[-1] if len(parts) > 1 else ""
        if shape == ("{id}",):
            self._json(HTTPStatus.OK, _organization_json(*service.get_organization(who, oid)))
        elif action == "rename":
            organization = service.rename_organization(who, oid, _string(body, "name"))
            self._json(HTTPStatus.OK, _organization_json(organization, None))
        elif action == "delete":
            service.delete_organization(who, oid)
            self._json(HTTPStatus.OK, {"deleted": True})
        elif action == "members" and method == "GET":
            members = [_org_member_json(m) for m in service.members(who, oid)]
            self._json(HTTPStatus.OK, {"members": members})
        elif action == "members":
            member = service.add_member(who, oid, _string(body, "user_id"), _string(body, "role"))
            self._json(HTTPStatus.CREATED, _org_member_json(member))
        else:
            assert uid is not None
            if action == "role":
                member = service.change_role(who, oid, uid, _string(body, "role"))
                self._json(HTTPStatus.OK, _org_member_json(member))
            else:
                service.remove_member(who, oid, uid)
                self._json(HTTPStatus.OK, {"removed": True})

    def _route(self, method: str, allowed: tuple[str, ...] | None) -> bool:
        """Whether a route exists and takes `method`, answering when not."""
        if allowed is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return False
        if method not in allowed:
            self._not_allowed(", ".join(allowed))
            return False
        return True

    def _project_invitations_api(self, method: str, parts: list[str], who: Principal, body: dict):
        """`/api/projects/<id>/invitations` and `/invite-links`, for Owners and Admins."""
        pid = _parse_uuid(parts[0])
        item = _parse_int_id(parts[2]) if len(parts) > 2 else None
        if pid is None or (len(parts) > 2 and item is None):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        shape = tuple("{n}" if i == 2 else part for i, part in enumerate(parts[1:], 1))
        if not self._route(method, _INVITATION_ROUTES.get(shape)):
            return
        service = self.app.invitations
        include_closed = self._query_value("include_closed") == "true"
        if shape == ("invitations",) and method == "GET":
            found = service.invitations(who, pid, include_closed=include_closed)
            self._json(HTTPStatus.OK, {"invitations": [_invitation_json(i) for i in found]})
        elif shape == ("invitations",):
            issued = service.invite(
                who,
                pid,
                _string(body, "email"),
                _string(body, "role"),
                ttl_seconds=_optional_number(body, "ttl_seconds"),
                send_email=_optional_bool(body, "send_email", True),
            )
            self._json(
                HTTPStatus.CREATED, _issued_json(issued.invitation, issued.link, issued.emailed)
            )
        elif shape == ("invitations", "{n}", "resend"):
            assert item is not None
            issued = service.resend(
                who, pid, item, send_email=_optional_bool(body, "send_email", True)
            )
            self._json(HTTPStatus.OK, _issued_json(issued.invitation, issued.link, issued.emailed))
        elif shape == ("invitations", "{n}", "revoke"):
            assert item is not None
            self._json(
                HTTPStatus.OK, {"invitation": _invitation_json(service.revoke(who, pid, item))}
            )
        elif shape == ("invite-links",) and method == "GET":
            links = service.links(who, pid, include_closed=include_closed)
            self._json(HTTPStatus.OK, {"invite_links": [_link_json(link) for link in links]})
        elif shape == ("invite-links",):
            max_uses = body.get("max_uses", 1)
            if not isinstance(max_uses, int) or isinstance(max_uses, bool):
                raise TypeError("max_uses must be an integer")
            created = service.create_link(
                who,
                pid,
                _string(body, "role"),
                max_uses=max_uses,
                ttl_seconds=_optional_number(body, "ttl_seconds"),
            )
            self._json(
                HTTPStatus.CREATED,
                {"invite_link": _link_json(created.invite_link), "link": created.link},
            )
        else:
            assert item is not None
            revoked = service.revoke_link(who, pid, item)
            self._json(HTTPStatus.OK, {"invite_link": _link_json(revoked)})

    def _own_invitations_api(self, method: str, path: str, who: Principal, body: dict):
        """`/api/invitations`: the caller's own pending invitations and redemption."""
        rest = path[len(INVITATIONS_API_PATH) :].strip("/")
        parts = rest.split("/") if rest else []
        item = _parse_int_id(parts[0]) if len(parts) == 2 else None
        if len(parts) == 2 and item is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        shape = tuple("{n}" if i == 0 and len(parts) == 2 else p for i, p in enumerate(parts))
        if not self._route(method, _OWN_INVITATION_ROUTES.get(shape)):
            return
        service = self.app.invitations
        if not parts:
            pending = [_pending_json(p) for p in service.pending(who)]
            self._json(HTTPStatus.OK, {"invitations": pending})
        elif shape == ("preview",):
            self._json(HTTPStatus.OK, _preview_json(service.preview(who, _string(body, "token"))))
        elif shape == ("redeem",):
            decision = body.get("decision", "accept")
            if decision not in ("accept", "decline"):
                raise ValueError("decision must be accept or decline")
            outcome = service.redeem(who, _string(body, "token"), accept=decision == "accept")
            if isinstance(outcome, Acceptance):
                self._json(HTTPStatus.OK, _acceptance_json(outcome))
            else:
                self._json(HTTPStatus.OK, {"invitation_id": outcome.id, "status": outcome.status})
        elif shape == ("{n}", "accept"):
            assert item is not None
            self._json(HTTPStatus.OK, _acceptance_json(service.accept(who, item)))
        else:
            assert item is not None
            declined = service.decline(who, item)
            self._json(HTTPStatus.OK, {"invitation_id": declined.id, "status": declined.status})

    def _read_cookie(self, name: str) -> str | None:
        raw = self.headers.get_all("Cookie") or []
        jar: SimpleCookie = SimpleCookie()
        for header in raw:
            try:
                jar.load(header)
            except CookieError:
                return None
        morsel = jar.get(name)
        return morsel.value if morsel is not None and morsel.value else None


_API_ROUTES: dict[tuple[str, ...], tuple[str, ...]] = {
    (): ("GET", "POST"),
    ("{id}",): ("GET",),
    ("{id}", "rename"): ("POST",),
    ("{id}", "delete"): ("POST",),
    ("{id}", "transfer"): ("POST",),
    ("{id}", "members"): ("GET", "POST"),
    ("{id}", "members", "{id}", "role"): ("POST",),
    ("{id}", "members", "{id}", "remove"): ("POST",),
    ("{id}", "audit"): ("GET",),
}
"""Projects API routes under `PROJECTS_API_PATH` and their methods."""

_ORGANIZATION_ROUTES: dict[tuple[str, ...], tuple[str, ...]] = {
    (): ("GET", "POST"),
    ("{id}",): ("GET",),
    ("{id}", "rename"): ("POST",),
    ("{id}", "delete"): ("POST",),
    ("{id}", "members"): ("GET", "POST"),
    ("{id}", "members", "{id}", "role"): ("POST",),
    ("{id}", "members", "{id}", "remove"): ("POST",),
}
"""Organizations API routes under `ORGANIZATIONS_API_PATH` and their methods."""


_INVITATION_ROUTES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("invitations",): ("GET", "POST"),
    ("invitations", "{n}", "resend"): ("POST",),
    ("invitations", "{n}", "revoke"): ("POST",),
    ("invite-links",): ("GET", "POST"),
    ("invite-links", "{n}", "revoke"): ("POST",),
}
"""Invitation routes under `PROJECTS_API_PATH/<id>/` and their methods."""

_OWN_INVITATION_ROUTES: dict[tuple[str, ...], tuple[str, ...]] = {
    (): ("GET",),
    ("preview",): ("POST",),
    ("redeem",): ("POST",),
    ("{n}", "accept"): ("POST",),
    ("{n}", "decline"): ("POST",),
}
"""Routes under `INVITATIONS_API_PATH` and their methods."""

_UNAVAILABLE = {
    "expired": "This invitation has expired. Ask a Project administrator for a new one.",
    "revoked": "This invitation was revoked.",
    "accepted": "This invitation was already accepted.",
    "declined": "This invitation was declined.",
    "exhausted": "This invite link has been used as many times as it allows.",
    "already_redeemed": "You have already used this invite link.",
    "issuer_not_authorized": "The person who invited you can no longer invite members.",
    "project_deleted": "This Project no longer exists.",
    "user_disabled": "Your Nest account is disabled.",
}
"""What the invitation page says for each [`db.InvitationUnavailableError`] reason."""


def _is_api(path: str) -> bool:
    return any(
        path == root or path.startswith(root + "/")
        for root in (
            PROJECTS_API_PATH,
            INVITATIONS_API_PATH,
            PLATFORMS_API_PATH,
            ORGANIZATIONS_API_PATH,
        )
    )


def _parse_int_id(value: str) -> int | None:
    return int(value) if value.isdecimal() and value.isascii() and len(value) <= 18 else None


def _optional_number(body: dict, name: str) -> float | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    return float(value)


def _optional_bool(body: dict, name: str, default: bool) -> bool:
    value = body.get(name, default)
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be true or false")
    return value


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value) if len(value) == 36 else None
    except ValueError:
        return None


def _string(body: dict, name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _optional_int(value: str | None) -> int | None:
    return int(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _id(value: UUID | None) -> str | None:
    return str(value) if value else None


def _project_json(project: Project, role: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(project.id),
        "name": project.name,
        "organization_id": _id(project.organization_id),
        "owner_user_id": str(project.owner_user_id),
        "created_by_user_id": str(project.created_by_user_id),
        "created_at": _iso(project.created_at),
        "deleted_at": _iso(project.deleted_at),
    }
    if role is not None:
        payload["role"] = role
    return payload


def _platform_json(summary: PlatformSummary) -> dict[str, object]:
    platform = summary.platform
    return {
        "id": str(platform.id),
        "slug": platform.slug,
        "display_name": platform.display_name,
        "operator_name": platform.operator_name,
        "scheduler": platform.scheduler,
        "is_default": platform.is_default,
        "transport_modes": list(summary.transport_modes),
        "login_node_count": summary.login_node_count,
    }


def _project_platforms_json(view: ProjectPlatforms) -> dict[str, object]:
    selection = view.selection
    platforms = []
    for summary in view.platforms:
        item = _platform_json(summary)
        item["is_project_default"] = summary.platform.id == view.project_default_platform_id
        platforms.append(item)
    return {
        "project_id": str(view.project_id),
        "platforms": platforms,
        "project_default_platform_id": _id(view.project_default_platform_id),
        "default_platform_id": str(selection.platform.id) if selection else None,
        "default_origin": selection.origin if selection else None,
        "unresolved_reason": view.unresolved_reason,
    }


def _organization_json(organization: Organization, role: str | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(organization.id),
        "name": organization.name,
        "created_by_user_id": _id(organization.created_by_user_id),
        "created_at": _iso(organization.created_at),
    }
    if role is not None:
        payload["role"] = role
    return payload


def _org_member_json(member: OrganizationMembership) -> dict[str, object]:
    return {
        "organization_id": str(member.organization_id),
        "user_id": str(member.user_id),
        "role": member.role,
        "created_at": _iso(member.created_at),
    }


def _member_json(member: Membership) -> dict[str, object]:
    return {
        "user_id": str(member.user_id),
        "role": member.role,
        "added_by_user_id": _id(member.added_by_user_id),
        "created_at": _iso(member.created_at),
    }


def _audit_json(event: ProjectAuditEvent) -> dict[str, object]:
    return {
        "id": event.id,
        "occurred_at": _iso(event.occurred_at),
        "event": event.event,
        "target_user_id": _id(event.target_user_id),
        "old_role": event.old_role,
        "new_role": event.new_role,
        "actor_user_id": _id(event.actor_user_id),
        "actor_operator": event.actor_operator,
    }


def _delivery_json(invitation: EmailInvitation) -> dict[str, object] | None:
    d = invitation.delivery
    if d is None:
        return None
    return {
        "status": d.status,
        "attempts": d.attempts,
        "max_attempts": d.max_attempts,
        "last_error": d.last_error,
        "last_error_kind": d.last_error_kind,
        "next_attempt_at": _iso(d.next_attempt_at),
        "sent_at": _iso(d.sent_at),
    }


def _invitation_json(invitation: EmailInvitation) -> dict[str, object]:
    return {
        "id": invitation.id,
        "project_id": str(invitation.project_id),
        "email": invitation.email,
        "role": invitation.role,
        "status": invitation.status,
        "invited_by_user_id": str(invitation.invited_by_user_id),
        "created_at": _iso(invitation.created_at),
        "expires_at": _iso(invitation.expires_at),
        "accepted_at": _iso(invitation.accepted_at),
        "accepted_by_user_id": _id(invitation.accepted_by_user_id),
        "declined_at": _iso(invitation.declined_at),
        "revoked_at": _iso(invitation.revoked_at),
        "delivery": _delivery_json(invitation),
    }


def _issued_json(invitation: EmailInvitation, link: str, emailed: bool) -> dict[str, object]:
    return {"invitation": _invitation_json(invitation), "link": link, "emailed": emailed}


def _link_json(link: InviteLink) -> dict[str, object]:
    return {
        "id": link.id,
        "project_id": str(link.project_id),
        "role": link.role,
        "status": link.status,
        "max_uses": link.max_uses,
        "use_count": link.use_count,
        "created_by_user_id": str(link.created_by_user_id),
        "created_at": _iso(link.created_at),
        "expires_at": _iso(link.expires_at),
        "revoked_at": _iso(link.revoked_at),
    }


def _pending_json(pending: PendingInvitation) -> dict[str, object]:
    return {
        "id": pending.id,
        "project_id": str(pending.project_id),
        "project_name": pending.project_name,
        "role": pending.role,
        "invited_by_name": pending.invited_by_name,
        "created_at": _iso(pending.created_at),
        "expires_at": _iso(pending.expires_at),
    }


def _preview_json(preview: InvitationPreview) -> dict[str, object]:
    return {
        "kind": preview.kind,
        "id": preview.id,
        "project_id": str(preview.project_id),
        "project_name": preview.project_name,
        "role": preview.role,
        "status": preview.status,
        "expires_at": _iso(preview.expires_at),
    }


def _acceptance_json(acceptance: Acceptance) -> dict[str, object]:
    return {
        "kind": acceptance.kind,
        "id": acceptance.invitation_id,
        "project_id": str(acceptance.project_id),
        "role": acceptance.role,
        "already_member": acceptance.already_member,
    }


class AuthHTTPServer(ThreadingHTTPServer):
    """
    HTTP server for the sign-in routes. Each request runs in its own daemon thread.

    Args:
        sign_in ([`auth.GoogleSignIn`]): The sign-in service.
        address (`tuple[str, int]`): Host and port to listen on.
        cli ([`auth.CLISignIn`], *optional*): CLI sign-in service. Defaults to one
            on the sign-in's database and session lifetimes.
        projects ([`authz.ProjectService`], *optional*): Projects API service.
            Defaults to one on the sign-in's database.
        platforms ([`authz.PlatformCatalog`], *optional*): Platforms API service.
            Defaults to one on the sign-in's database.
        invitations ([`authz.InvitationService`], *optional*): Invitation service.
            Defaults to one on the Projects service and `email`, whose links use
            `NEST_PUBLIC_URL` with email enabled and this listener's origin otherwise.
        email ([`mail.EmailService`], *optional*): Email delivery for invitations.
    """

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        sign_in: GoogleSignIn,
        address: tuple[str, int],
        *,
        cli: CLISignIn | None = None,
        projects: ProjectService | None = None,
        invitations: InvitationService | None = None,
        platforms: PlatformCatalog | None = None,
        email: EmailService | None = None,
        organizations: OrganizationService | None = None,
    ):
        self.sign_in = sign_in
        self.cli = cli or CLISignIn(sign_in.database, sign_in.sessions)
        config = sign_in.config
        self.callback_path = urlsplit(config.redirect_url).path
        self.secure = config.secure_cookies
        self.session_cookie = SESSION_COOKIE if self.secure else INSECURE_SESSION_COOKIE
        self.refresh_cookie = REFRESH_COOKIE if self.secure else INSECURE_REFRESH_COOKIE
        redirect = urlsplit(config.redirect_url)
        self.origin = f"{redirect.scheme}://{redirect.netloc}"
        self.sessions = sign_in.session_store
        self.projects = projects or ProjectService(sign_in.database)
        self.platforms = platforms or PlatformCatalog(sign_in.database, projects=self.projects)
        self.organizations = organizations or OrganizationService(
            sign_in.database, self.projects.authorizer
        )
        self.invitations = invitations or InvitationService(
            sign_in.database, self.projects, email, link_base_url=self.origin
        )
        super().__init__(address, _Handler)


@dataclass
class AuthService:
    """
    The Controller's sign-in service.

    Attributes:
        server ([`auth.AuthHTTPServer`] or `None`): The listener, or `None` when
            Google sign-in is not configured.
    """

    server: AuthHTTPServer | None = None
    thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        """Whether Google sign-in is served."""
        return self.server is not None

    @property
    def address(self) -> str | None:
        """The listener's bound `host:port`, or `None`."""
        if self.server is None:
            return None
        host, port = self.server.server_address[:2]
        return f"{host}:{port}"

    def stop(self) -> None:
        """Stops the listener and waits for its loop to exit."""
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=10)


def _split_bind(bind: str) -> tuple[str, int]:
    host, _, port = bind.rpartition(":")
    return host.strip("[]"), int(port)


def start_auth_service(
    config: GoogleAuthConfig | None,
    database: Database | None,
    *,
    sign_in=None,
    cli: CLISignIn | None = None,
    bootstrap_admin_email: str | None = None,
    email: EmailService | None = None,
) -> AuthService:
    """
    Starts the auth listener when Google sign-in is configured.

    Never contacts Google at startup: a Google outage cannot stop the Controller.

    Args:
        config ([`config.GoogleAuthConfig`] or `None`): Google settings, or `None`.
        database ([`db.Database`] or `None`): Open database; required when enabled.
        sign_in ([`auth.GoogleSignIn`], *optional*): Replaces the default service,
            for tests with a fake provider.
        cli ([`auth.CLISignIn`], *optional*): Replaces the default CLI sign-in
            service, for tests with a shorter poll interval.
        bootstrap_admin_email (`str`, *optional*): `NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL`,
            passed to the default [`auth.GoogleSignIn`].
        email ([`mail.EmailService`], *optional*): Email delivery for invitations;
            invitations are shared by link only without it.

    Raises:
        `OSError`: If `NEST_AUTH_HTTP_BIND` cannot be bound.
    """
    if config is None:
        logger.info("Google sign-in disabled")
        return AuthService()
    if database is None:
        raise RuntimeError("Google sign-in needs the database")
    sign_in = sign_in or GoogleSignIn(config, database, bootstrap_admin_email=bootstrap_admin_email)
    server = AuthHTTPServer(sign_in, _split_bind(config.http_bind), cli=cli, email=email)
    thread = threading.Thread(target=server.serve_forever, name="nest-auth-http", daemon=True)
    thread.start()
    service = AuthService(server, thread)
    policy = sign_in.sessions
    logger.info(
        "Google sign-in enabled listening=%s login_path=%s cli_approve_path=%s redirect_url=%s "
        "access_token_ttl=%g refresh_ttl=%g session_max_lifetime=%g",
        service.address,
        LOGIN_PATH,
        CLI_APPROVE_PATH,
        config.redirect_url,
        policy.access_token_ttl,
        policy.refresh_ttl,
        policy.max_lifetime,
    )
    return service
