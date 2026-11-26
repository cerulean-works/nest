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
| `GET /console/nest-mark.svg` | The Nest mark |
| `GET /api/connect` | Connection profiles and the caller's Project eligibility (G02c) |

Its Content Security Policy admits only the listener's own script, stylesheet
and JSON routes (`'self'`), so no inline script, no framing and no cross-origin
request; the other routes keep their own stricter policy. The sign-in, CLI
approval and invitation pages share the stylesheet (and nothing else) so every
browser page looks and behaves alike.

The *Connect to Project* commands are built here, on the server, from the
operator's [`config.ConsoleConnectConfig`] and this listener's own origin, every
value quoted for a POSIX shell. The browser only adds a Project ID it has checked
is a UUID; no name, label or other free text ever enters a command.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from importlib.resources import files

from .. import config as _config
from ..config import (
    CONSOLE_PATH,
    HOSTED_AUTH_URL,
    HOSTED_CONTROLLER_ALIASES,
    ConsoleConnectConfig,
    is_hosted_controller,
)

__all__ = [
    "CONSOLE_ASSETS",
    "CONSOLE_CSP",
    "CONSOLE_HEADERS",
    "CONSOLE_INDEX_PATHS",
    "INSTALL_COMMAND",
    "PAGE_CSP",
    "connect_payload",
    "connection_profiles",
    "console_asset",
    "console_page",
]

CONSOLE_INDEX_PATHS = (CONSOLE_PATH, CONSOLE_PATH + "/")
"""Paths that serve the Console page."""

CONSOLE_ASSETS = {
    "console.js": "text/javascript; charset=utf-8",
    "console.css": "text/css; charset=utf-8",
    "nest-mark.svg": "image/svg+xml",
}
"""The Console's static files under `CONSOLE_PATH/`, with their content types."""

CONSOLE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
"""The Console page's Content Security Policy: same-origin script, style and fetch only."""

PAGE_CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
"""The Content Security Policy of the listener's own HTML pages (sign-in results, CLI
approval, invitations): the Console's stylesheet and mark, and no script at all
(the invitation page adds the hash of its one script)."""

INSTALL_COMMAND = shlex.join(
    ["uv", "add", "nest @ git+https://github.com/cerulean-works/nest@main"]
)
"""The one-time install step the Console shows: Nest from its repository, as in
*Install Nest*."""

_SETUP_COMMANDS = (
    ("Sign in", "uv run nest login"),
    ("Check the sign-in", "uv run nest auth status --check"),
)

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


def _exports(settings: Iterable[tuple[str, str]]) -> list[str]:
    return [f"export {name}={shlex.quote(value)}" for name, value in settings]


def connection_profiles(connect: ConsoleConnectConfig, origin: str) -> list[dict[str, object]]:
    """
    The connection profiles the Console offers, private network first.

    Each profile has the `settings` a user exports (already validated by
    [`config.load_controller_config`]) and the same as shell lines in `exports`.
    The hosted profile appears only with `NEST_CONSOLE_HOSTED_PROFILE=pilot` on the
    hosted sign-in origin, labelled `pilot` until [`config.HOSTED_PROFILE_DEFAULT`]
    (H09) makes it the SDK's `default`, when it needs no setting at all.

    Args:
        connect ([`config.ConsoleConnectConfig`]): The operator's settings.
        origin (`str`): This listener's sign-in origin.
    """
    profiles: list[dict[str, object]] = []
    address = connect.controller_address
    if address is not None:
        settings = [
            ("NEST_AUTH_URL", origin),
            ("NEST_CONTROLLER_ADDRESS", address),
            ("NEST_CONTROLLER_TLS", "true" if connect.controller_tls else "false"),
        ]
        hosted_target = is_hosted_controller(address) or address.lower() in (
            alias.lower() for alias in HOSTED_CONTROLLER_ALIASES
        )
        if origin == HOSTED_AUTH_URL and not hosted_target:
            # A custom address receives the hosted session only when named twice.
            settings.append(("NEST_HOSTED_SESSION_ADDRESS", address))
        private = not connect.controller_tls
        profiles.append(
            {
                "id": "private",
                "label": "Private network" if private else "Direct (TLS)",
                "status": "available",
                "transport": "private-network" if private else "tls",
                "summary": (
                    f"Connect to {address} over your organization's private network "
                    "(for example its Tailnet). The connection is plaintext gRPC that only "
                    "the private network encrypts: use it only from a device on that network."
                    if private
                    else f"Connect to {address} over verified TLS."
                ),
                "settings": [list(pair) for pair in settings],
                "exports": _exports(settings),
            }
        )
    if connect.hosted_profile == "pilot" and origin == HOSTED_AUTH_URL:
        # Read at call time: H09 activation is the one reviewed change of this flag.
        default = _config.HOSTED_PROFILE_DEFAULT
        settings = [] if default else [("NEST_PROFILE", "hosted")]
        profiles.append(
            {
                "id": "hosted",
                "label": "Hosted gateway",
                "status": "default" if default else "pilot",
                "transport": "tls",
                "summary": (
                    "Connect through the Cerulean-hosted public gateway over verified TLS, "
                    "without a private network. "
                    + (
                        "It is the SDK's default."
                        if default
                        else "It is a pilot: use it only if your operator invited you to it."
                    )
                ),
                "settings": [list(pair) for pair in settings],
                "exports": _exports(settings),
            }
        )
    return profiles


def connect_payload(
    connect: ConsoleConnectConfig, origin: str, eligibility: list[dict[str, object]] | None
) -> dict[str, object]:
    """
    The `GET /api/connect` answer: install and sign-in steps, connection profiles,
    and, when this server can evaluate it, the caller's Projects with their
    default-execution eligibility (`None` when it cannot).
    """
    return {
        "auth_url": origin,
        "auth_export": _exports([("NEST_AUTH_URL", origin)])[0],
        "install": INSTALL_COMMAND,
        "setup": [{"label": label, "command": command} for label, command in _SETUP_COMMANDS],
        "profiles": connection_profiles(connect, origin),
        "eligibility": {
            "evaluated": eligibility is not None,
            "projects": eligibility or [],
        },
    }
