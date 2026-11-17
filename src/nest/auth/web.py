"""
The Controller's auth listener: a small HTTP server for the browser side of
Google sign-in.

It serves two routes and nothing else:

- `GET /auth/google/login` stores a pending sign-in, sets the sign-in cookie, and
  redirects to Google.
- `GET <path of NEST_GOOGLE_REDIRECT_URL>` is Google's callback. It finishes the
  sign-in and sets the browser session cookie.

The listener speaks plain HTTP. Put it behind a TLS-terminating reverse proxy that
serves `NEST_GOOGLE_REDIRECT_URL` and forwards paths unchanged.
"""

from __future__ import annotations

import html
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import parse_qs, urlsplit

from .._logging import get_logger
from ..config import GoogleAuthConfig
from ..db import Database, DatabaseError
from .google import SIGN_IN_TTL, GoogleSignIn, SignInError

logger = get_logger("nest.auth.web")

LOGIN_PATH = "/auth/google/login"
"""Route that starts a Google sign-in."""

SIGN_IN_COOKIE = "nest_google_signin"
"""Cookie binding a pending sign-in to the browser that started it."""

SESSION_COOKIE = "__Host-nest_session"
"""Browser session cookie when the redirect URL is `https://`."""

INSECURE_SESSION_COOKIE = "nest_session"
"""Browser session cookie for an `http://localhost` redirect URL, where browsers
refuse the `__Host-` prefix."""

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


def _cookie(name: str, value: str, *, path: str, max_age: int, secure: bool) -> str:
    attributes = [f"{name}={value}", f"Path={path}", f"Max-Age={max_age}", "HttpOnly"]
    attributes.append("SameSite=Lax")  # sent on Google's top-level redirect back
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

    def _send(self, status: int, body: str = "", headers: dict[str, str] | None = None):
        data = body.encode()
        self.send_response(status)
        for name, value in {**_SECURITY_HEADERS, **(headers or {})}.items():
            self.send_header(name, value)
        if self._set_cookies:
            for value in self._set_cookies:
                self.send_header("Set-Cookie", value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        logger.info(
            "Auth request method=%s path=%s status=%s",
            self.command,
            urlsplit(self.path).path[:200],
            status,
        )

    def _page(self, status: int, title: str, message: str, *, retry: bool = False):
        link = f'<p><a href="{html.escape(LOGIN_PATH)}">Sign in again</a></p>' if retry else ""
        self._send(
            status,
            _PAGE.format(title=html.escape(title), message=html.escape(message), link=link),
        )

    def do_GET(self):
        self._set_cookies: list[str] = []
        path = urlsplit(self.path).path
        try:
            if path == LOGIN_PATH:
                self._login()
            elif path == self.app.callback_path:
                self._callback()
            else:
                self._page(HTTPStatus.NOT_FOUND, "Not found", "There is nothing here.")
        except DatabaseError:
            logger.error("Auth request failed: the database is unavailable")
            self._page(
                HTTPStatus.SERVICE_UNAVAILABLE, "Unavailable", "Nest is unavailable. Try later."
            )
        except Exception:
            logger.exception("Auth request failed")
            self._page(HTTPStatus.INTERNAL_SERVER_ERROR, "Error", "Sign-in failed.")

    def do_POST(self):
        self._set_cookies = []
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, headers={"Allow": "GET"})

    # HEAD too: it would otherwise start a sign-in or consume a callback's state.
    do_HEAD = do_PUT = do_DELETE = do_PATCH = do_POST

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
        self._set_cookies.append(
            _cookie(
                self.app.session_cookie,
                result.session_token,
                path="/",
                max_age=int(self.app.sign_in.session_ttl.total_seconds()),
                secure=self.app.secure,
            )
        )
        self._page(
            HTTPStatus.OK,
            "Signed in",
            f"You are signed in to Nest as {result.identity.email}. You can close this page.",
        )

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
    logger.info(
        "Google sign-in enabled listening=%s login_path=%s redirect_url=%s",
        service.address,
        LOGIN_PATH,
        config.redirect_url,
    )
    return service
