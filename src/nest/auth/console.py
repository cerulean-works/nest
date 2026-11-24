"""
The browser Management Console (G02a), served same-origin by the auth listener.

The Console is a static page and one script, with no build step and no third-party
code. It holds no secret of its own: it calls the same JSON routes as the CLI
(`/auth/session*`, `/api/projects`, `/api/invitations` and, when the server has
it, `/api/admin`) with the browser's `HttpOnly` session cookies, and every
authorization decision stays on the server. Its pages render only the actions a
caller's role allows, but a refused request is always answered by the server.

| Route | Serves |
| --- | --- |
| `GET /console` | The Console page |
| `GET /console/console.js`, `/console/console.css` | Its script and stylesheet |

Its Content Security Policy admits only the listener's own script, stylesheet
and JSON routes (`'self'`), so no inline script, no framing and no cross-origin
request; the other routes keep their own stricter policy.
"""

from __future__ import annotations

from importlib.resources import files

from ..config import CONSOLE_PATH

__all__ = [
    "CONSOLE_ASSETS",
    "CONSOLE_CSP",
    "CONSOLE_HEADERS",
    "CONSOLE_INDEX_PATHS",
    "console_asset",
    "console_page",
]

CONSOLE_INDEX_PATHS = (CONSOLE_PATH, CONSOLE_PATH + "/")
"""Paths that serve the Console page."""

CONSOLE_ASSETS = {
    "console.js": "text/javascript; charset=utf-8",
    "console.css": "text/css; charset=utf-8",
}
"""The Console's static files under `CONSOLE_PATH/`, with their content types."""

CONSOLE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
"""The Console page's Content Security Policy: same-origin script, style and fetch only."""

CONSOLE_HEADERS = {
    "Content-Security-Policy": CONSOLE_CSP,
    # A same-origin fetch POST from a `no-referrer` page carries `Origin: null`,
    # which the API's same-origin check refuses. `same-origin` still sends no
    # referrer to any other site.
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}
"""Headers added to the Console's responses, over the listener's security headers."""

_PACKAGE = files(__package__).joinpath("console_assets")


def _read(name: str) -> str:
    return _PACKAGE.joinpath(name).read_text(encoding="utf-8")


def console_page(*, privileged_api: bool) -> str:
    """
    The Console page.

    Args:
        privileged_api (`bool`): Whether this server serves the privileged management
            API (`/api/admin`, G03). Without it the Console shows no platform-admin
            feature at all; with it, the Console still asks the server whether the
            caller is a platform admin before showing one.
    """
    state = "available" if privileged_api else "unavailable"
    return _read("index.html").replace("{privileged_api}", state)


def console_asset(name: str) -> tuple[str, str] | None:
    """A static Console file as `(content, content type)`, or `None` if there is none."""
    content_type = CONSOLE_ASSETS.get(name)
    if content_type is None:
        return None
    return _read(name), content_type
