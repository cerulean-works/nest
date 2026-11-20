"""
`nest`: sign the command line in to Nest.

Usage:

    nest login  [--url URL] [--no-browser]   # approve a CLI sign-in in a browser
    nest logout [--url URL] [--local]        # revoke the session and delete it
    nest auth status [--url URL] [--check] [--json]
    nest platforms list [--url URL] [--project-id UUID] [--json]
    nest platforms show SLUG [--url URL] [--project-id UUID] [--json]

`nest login` is optional: commands that need a Nest session sign in the same way
when there is none. The session is kept in `NEST_CLI_CREDENTIALS_FILE` (default
`~/.config/nest/credentials.json`), readable only by you. No command takes a token
or password as an argument.

`nest platforms` lists only the HPC Platforms granted to your Projects.

Exit status: 0 on success, 1 when not signed in or the sign-in was denied or
expired or the Platform is not available, 2 for invalid configuration or arguments,
3 when Nest is unreachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID

from .config import ConfigError, load_cli_auth_config
from .login import (
    AuthError,
    CredentialsFileError,
    LoginDeniedError,
    LoginExpiredError,
    NestUnavailableError,
    login,
    logout,
    status,
)
from .platforms import PlatformInfo, PlatformNotAvailableError, get_platform, list_platforms


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nest", description="Sign in to Nest and discover HPC Platforms."
    )
    url = argparse.ArgumentParser(add_help=False)
    url.add_argument(
        "--url",
        metavar="URL",
        help="Nest's address, such as https://nest.example.com (default: NEST_AUTH_URL)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    signin = commands.add_parser(
        "login", parents=[url], help="sign in by approving this CLI in a browser"
    )
    signin.add_argument("--no-browser", action="store_true", help="only print the approval link")
    signout = commands.add_parser(
        "logout", parents=[url], help="revoke the Nest session and delete it"
    )
    signout.add_argument(
        "--local", action="store_true", help="only delete it here, without contacting Nest"
    )
    auth = commands.add_parser("auth", help="inspect or clear the Nest session")
    actions = auth.add_subparsers(dest="action", required=True)
    show = actions.add_parser("status", parents=[url], help="show the stored Nest session")
    show.add_argument("--check", action="store_true", help="also ask Nest whether it is valid")
    show.add_argument("--json", action="store_true", help="print JSON")
    clear = actions.add_parser("logout", parents=[url], help="same as nest logout")
    clear.add_argument("--local", action="store_true", help=argparse.SUPPRESS)
    platforms = commands.add_parser("platforms", help="list the HPC Platforms available to you")
    platform_actions = platforms.add_subparsers(dest="action", required=True)
    scope = argparse.ArgumentParser(add_help=False)
    scope.add_argument(
        "--project-id", type=UUID, metavar="UUID", help="only Platforms granted to this Project"
    )
    scope.add_argument("--json", action="store_true", help="print JSON")
    platform_actions.add_parser(
        "list", parents=[url, scope], help="list the Platforms available to you"
    )
    show_platform = platform_actions.add_parser(
        "show", parents=[url, scope], help="describe one available Platform"
    )
    show_platform.add_argument("platform", metavar="SLUG", help="the Platform's slug or ID")
    return parser


def _platform_json(info: PlatformInfo) -> dict[str, object]:
    return {
        "id": str(info.id),
        "slug": info.slug,
        "display_name": info.display_name,
        "operator_name": info.operator_name,
        "scheduler": info.scheduler,
        "is_default": info.is_default,
        "transport_modes": list(info.transport_modes),
        "login_node_count": info.login_node_count,
    }


def _platforms(config, args) -> int:
    if args.action == "show":
        info = get_platform(args.platform, config, project_id=args.project_id)
        if args.json:
            print(json.dumps(_platform_json(info), indent=2))
        else:
            print(f"{info.slug}: {info.display_name}")
            print(f"  id:          {info.id}")
            print(f"  operator:    {info.operator_name or '-'}")
            print(f"  scheduler:   {info.scheduler}")
            print(f"  ssh modes:   {','.join(info.transport_modes) or '-'}")
            print(f"  login nodes: {info.login_node_count}")
            print(f"  default:     {'yes' if info.is_default else 'no'}")
        return 0
    infos, default = list_platforms(config, project_id=args.project_id)
    if args.json:
        payload = {
            "platforms": [_platform_json(i) for i in infos],
            "default_platform_id": str(default) if default else None,
        }
        print(json.dumps(payload, indent=2))
    elif not infos:
        print("No HPC Platform is available to you.", file=sys.stderr)
    else:
        for info in infos:
            marker = "*" if info.id == default else " "
            operator = f" ({info.operator_name})" if info.operator_name else ""
            print(f"{marker} {info.slug:<16} {info.display_name}{operator}  id={info.id}")
    return 0


def _status(config, args) -> int:
    info = status(config, check=args.check)
    if args.json:
        print(json.dumps(info, indent=2))
    elif "user_id" not in info:
        print(f"Not signed in to {info['auth_url']}.")
    else:
        state = "Signed in" if info["signed_in"] else "Session ended; run nest login"
        print(f"{state}: {info['auth_url']}")
        print(f"  user:          {info['user_id']}")
        print(f"  session:       {info['session_id']}")
        print(f"  scope:         {info['scope']}")
        print(f"  access token:  expires {info['access_expires_at']}")
        print(f"  refresh:       expires {info['refresh_expires_at']}")
        print(f"  sign-in ends:  {info['session_expires_at']}")
        if "accepted" in info:
            print(f"  accepted by Nest: {'yes' if info['accepted'] else 'no'}")
        print(f"  stored in:     {info['credentials_file']}")
    return 0 if info["signed_in"] else 1


def main(argv: list[str] | None = None) -> int:
    """Runs `nest`. Returns the exit status."""
    args = _parser().parse_args(argv)
    try:
        config = load_cli_auth_config(url=args.url)
    except ConfigError as exc:
        print(f"nest: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "login":
            login(config, open_browser=not args.no_browser)
            return 0
        if args.command == "platforms":
            return _platforms(config, args)
        if args.command == "logout" or (args.command == "auth" and args.action == "logout"):
            result = logout(config, revoke=not args.local)
            if not result.removed:
                print("Not signed in to Nest.", file=sys.stderr)
            elif result.revoked is None and not args.local:
                print(
                    "Signed out here, but Nest could not be reached to revoke the session; "
                    "it ends when it expires.",
                    file=sys.stderr,
                )
            else:
                print("Signed out of Nest.", file=sys.stderr)
            return 0
        return _status(config, args)
    except (LoginDeniedError, LoginExpiredError) as exc:
        print(f"nest: {exc}", file=sys.stderr)
        return 1
    except PlatformNotAvailableError as exc:
        print(f"nest: {exc}", file=sys.stderr)
        return 1
    except NestUnavailableError as exc:
        print(f"nest: {exc}", file=sys.stderr)
        return 3
    except CredentialsFileError as exc:
        print(f"nest: {exc}", file=sys.stderr)
        return 2
    except AuthError as exc:
        print(f"nest: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nnest: cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
