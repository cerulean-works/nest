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

The listener speaks plain HTTP. Put it behind a TLS-terminating reverse proxy that
serves `NEST_GOOGLE_REDIRECT_URL` and forwards paths unchanged.
"""

from __future__ import annotations

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
from typing import cast
from urllib.parse import parse_qs, quote, urlsplit

from .._logging import get_logger
from ..config import AUTH_CLI_PREFIX, AUTH_SESSION_PATHS, CLI_SCOPES, GoogleAuthConfig
from ..db import (
    AccessGrant,
    Database,
    DatabaseError,
    DecidingSessionNotActiveError,
    Session,
    SessionRefreshError,
    UserNotActiveError,
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
        if path in AUTH_SESSION_PATHS or path in CLI_API_PATHS or path == CLI_SESSION_PATH:
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
        else:
            self.do_HEAD()

    def do_HEAD(self):
        path = urlsplit(self.path).path
        if path in (REFRESH_PATH, LOGOUT_PATH) or path in CLI_API_PATHS:
            allow = "POST"
        elif path == CLI_APPROVE_PATH:
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
        self._page(
            HTTPStatus.OK,
            "Signed in",
            f"You are signed in to Nest as {result.identity.email}. You can close this page.",
        )

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


class AuthHTTPServer(ThreadingHTTPServer):
    """
    HTTP server for the sign-in routes. Each request runs in its own daemon thread.

    Args:
        sign_in ([`auth.GoogleSignIn`]): The sign-in service.
        address (`tuple[str, int]`): Host and port to listen on.
        cli ([`auth.CLISignIn`], *optional*): CLI sign-in service. Defaults to one
            on the sign-in's database and session lifetimes.
    """

    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        sign_in: GoogleSignIn,
        address: tuple[str, int],
        *,
        cli: CLISignIn | None = None,
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

    Raises:
        `OSError`: If `NEST_AUTH_HTTP_BIND` cannot be bound.
    """
    if config is None:
        logger.info("Google sign-in disabled")
        return AuthService()
    if database is None:
        raise RuntimeError("Google sign-in needs the database")
    sign_in = sign_in or GoogleSignIn(config, database, bootstrap_admin_email=bootstrap_admin_email)
    server = AuthHTTPServer(sign_in, _split_bind(config.http_bind), cli=cli)
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
