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

The listener speaks plain HTTP. Put it behind a TLS-terminating reverse proxy that
serves `NEST_GOOGLE_REDIRECT_URL` and forwards paths unchanged.
"""

from __future__ import annotations

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
from urllib.parse import parse_qs, urlsplit

from .._logging import get_logger
from ..config import AUTH_SESSION_PATHS, GoogleAuthConfig
from ..db import Database, DatabaseError, SessionRefreshError, hash_token
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
        if urlsplit(self.path).path in AUTH_SESSION_PATHS:
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
        elif path in (REFRESH_PATH, LOGOUT_PATH):
            self._dispatch(lambda: self._not_allowed("POST"))
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
        else:
            self.do_HEAD()

    def do_HEAD(self):
        path = urlsplit(self.path).path
        allow = "POST" if path in (REFRESH_PATH, LOGOUT_PATH) else "GET"
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
        self._send(HTTPStatus.FOUND, headers={"Location": started.authorization_url})

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

    def _same_origin(self) -> bool:
        """
        Refuses a cross-site POST. Browsers send `Origin` on every POST and
        `Sec-Fetch-Site` on most; a client that sends neither is not a browser that
        another site could drive.
        """
        origin = self.headers.get("Origin")
        site = self.headers.get("Sec-Fetch-Site")
        if (origin is not None and origin != self.app.origin) or (
            site is not None and site != "same-origin"
        ):
            logger.warning("Session request refused reason=cross_origin")
            self._json(HTTPStatus.FORBIDDEN, {"error": "cross_origin"})
            return False
        return True

    def _describe(self):
        token = self._read_cookie(self.app.session_cookie)
        grant = self.app.sessions.authenticate(hash_token(token)) if token else None
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
            found = self.app.sessions.find_by_refresh(hash_token(refresh))
            session_id = found.id if found else None
        if session_id is None:
            access = self._read_cookie(self.app.session_cookie)
            grant = self.app.sessions.authenticate(hash_token(access)) if access else None
            session_id = grant.session.id if grant else None
        if session_id is not None:
            self.app.sign_in.identities.revoke_session(session_id, reason="logout")
            logger.info("Signed out session_id=%s", session_id)
        self._clear_session_cookies()
        self._json(HTTPStatus.OK, {"signed_out": True})

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
    """

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, sign_in: GoogleSignIn, address: tuple[str, int]):
        self.sign_in = sign_in
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
    server = AuthHTTPServer(sign_in, _split_bind(config.http_bind))
    thread = threading.Thread(target=server.serve_forever, name="nest-auth-http", daemon=True)
    thread.start()
    service = AuthService(server, thread)
    policy = sign_in.sessions
    logger.info(
        "Google sign-in enabled listening=%s login_path=%s redirect_url=%s "
        "access_token_ttl=%g refresh_ttl=%g session_max_lifetime=%g",
        service.address,
        LOGIN_PATH,
        config.redirect_url,
        policy.access_token_ttl,
        policy.refresh_ttl,
        policy.max_lifetime,
    )
    return service
