"""
`nest`: sign the command line in to Nest.

Usage:

    nest login  [--url URL] [--no-browser]   # approve a CLI sign-in in a browser
    nest logout [--url URL] [--local]        # revoke the session and delete it
    nest auth status [--url URL] [--check] [--json]
    nest platforms list [--url URL] [--project-id UUID] [--json]
    nest platforms show SLUG [--url URL] [--project-id UUID] [--json]
    nest platforms default --project-id UUID [--url URL] [--json]
    nest projects list [--json]              # Projects you may run in on this Controller
    nest projects current [--json]           # the Project the SDK would use, and why
    nest projects select                     # choose it by name, on a terminal
    nest projects use UUID                   # save the Project the SDK uses here
    nest projects forget                     # forget the saved Project

Every command talks to the Nest named by `--url`, else `NEST_AUTH_URL`, else the
Cerulean-hosted Nest at `https://auth.nest.cerulean.works`.

`nest login` is optional: commands that need a Nest session sign in the same way
when there is none. Sessions are kept in `NEST_CLI_CREDENTIALS_FILE` (default
`~/.config/nest/credentials.json`), one per Nest, readable only by you. No command takes a token
or password as an argument.

`nest platforms` lists only the HPC Platforms granted to your Projects.
`nest platforms default` shows which of a Project's eligible Platforms a job that
names none runs on, and why; a Project Owner or Admin chooses it in a browser.

`nest projects` asks the Controller the SDK is configured for (the same
`NEST_PROFILE`, `NEST_CONTROLLER_ADDRESS` and `NEST_AUTH_URL` settings) which Nest
Projects you may run in, and keeps the SDK's saved choice for that Controller and
account in `NEST_PREFERENCES_FILE`. `NEST_PROJECT_ID` always overrides it.
`nest projects select` lists them by name and role and saves your choice; it needs
a terminal, and scripts use `nest projects use UUID` instead.

Exit status: 0 on success, 1 when not signed in or the sign-in was denied or
expired, the Platform or Project is not available, or no Project can be chosen,
2 for invalid configuration or arguments, 3 when Nest is unreachable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from uuid import UUID

from .config import (
    HOSTED_AUTH_URL,
    ConfigError,
    is_hosted_controller,
    load_cli_auth_config,
    load_client_config,
)
from .errors import ProjectSelectionError
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
from .platforms import (
    PlatformInfo,
    PlatformNotAvailableError,
    get_platform,
    list_platforms,
    project_platforms,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nest", description="Sign in to Nest and discover HPC Platforms."
    )
    url = argparse.ArgumentParser(add_help=False)
    url.add_argument(
        "--url",
        metavar="URL",
        help=(
            "Nest's sign-in address, such as https://nest.example.com "
            f"(default: NEST_AUTH_URL, else {HOSTED_AUTH_URL})"
        ),
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
    default_platform = platform_actions.add_parser(
        "default", parents=[url], help="show the Platform a Project's jobs default to"
    )
    default_platform.add_argument(
        "--project-id", type=UUID, metavar="UUID", required=True, help="the Project"
    )
    default_platform.add_argument("--json", action="store_true", help="print JSON")
    projects = commands.add_parser(
        "projects", help="choose the Nest Project the SDK runs in on this Controller"
    )
    project_actions = projects.add_subparsers(dest="action", required=True)
    as_json = argparse.ArgumentParser(add_help=False)
    as_json.add_argument("--json", action="store_true", help="print JSON")
    project_actions.add_parser(
        "list", parents=[as_json], help="list the Projects you may run in here"
    )
    project_actions.add_parser(
        "current", parents=[as_json], help="show the Project the SDK would run in, and why"
    )
    project_actions.add_parser(
        "select", help="choose the Project the SDK runs in here from a list, by name"
    )
    use = project_actions.add_parser("use", help="save the Project the SDK runs in here")
    use.add_argument("project_id", type=UUID, metavar="UUID", help="an eligible Project's ID")
    project_actions.add_parser("forget", help="forget the saved Project")
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
    if args.action == "default":
        view = project_platforms(args.project_id, config)
        if args.json:
            payload = {
                "project_id": str(view.project_id),
                "platforms": [_platform_json(i) for i in view.platforms],
                "project_default_platform_id": _optional_id(view.project_default_platform_id),
                "default_platform_id": _optional_id(view.default_platform_id),
                "default_origin": view.default_origin,
                "unresolved_reason": view.unresolved_reason,
            }
            print(json.dumps(payload, indent=2))
            return 0
        chosen = next((i for i in view.platforms if i.id == view.default_platform_id), None)
        if chosen is not None:
            print(f"{chosen.slug} ({view.default_origin})")
            return 0
        reasons = {
            "none_authorized": "No HPC Platform is granted to this Project.",
            "ambiguous": "Several Platforms are eligible and none is the default; name one.",
            "default_unavailable": "The Project's default Platform is unavailable.",
        }
        print(reasons.get(view.unresolved_reason or "", "No default Platform."), file=sys.stderr)
        return 1
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


def _optional_id(value: UUID | None) -> str | None:
    return str(value) if value else None


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


def _warn_custom_controller(config) -> None:
    """Says when the hosted session being created will not reach the configured
    Controller, which receives only the session of the origin NEST_AUTH_URL names."""
    address = os.environ.get("NEST_CONTROLLER_ADDRESS", "").strip()
    if config.url_source == "hosted default" and address and not is_hosted_controller(address):
        print(
            f"nest: NEST_CONTROLLER_ADDRESS={address} is not the hosted Controller, so the "
            "SDK will not send it this hosted session. For a self-hosted Nest, set "
            "NEST_AUTH_URL to its sign-in address.",
            file=sys.stderr,
        )


def _projects(args) -> int:
    """`nest projects`: Project selection against the SDK's configured Controller."""
    import grpc

    from . import project_selection as selection
    from .client import _channel, _controller_error

    client = load_client_config()
    if client.auth_url is None:
        print(
            f"nest: the SDK sends no Nest session to {client.address}, so no Project "
            "applies there. Set NEST_AUTH_URL to its sign-in address, or use "
            "NEST_PROFILE=hosted.",
            file=sys.stderr,
        )
        return 2
    from .login import ensure_session

    store = selection.store_for(client)
    session = ensure_session(load_cli_auth_config(url=client.auth_url))
    key = selection.preference_key(client, session.user_id)
    if args.action == "forget":
        removed = store is not None and key is not None and store.remove(key)
        print("Forgot the saved Project." if removed else "No Project was saved here.")
        return 0
    try:
        with _channel(client) as channel:
            if args.action == "current":
                choice = selection.select_project(
                    client, channel, session.access_token, session.user_id, interactive=False
                )
                if choice is None:
                    print("This Controller needs no Project.")
                    return 0
                if args.json:
                    print(json.dumps({"project_id": choice.project_id, "source": choice.source}))
                else:
                    print(f"{choice.project_id} ({choice.source})")
                return 0
            eligible = selection.list_eligible(channel, session.access_token)
            if args.action == "select":
                return _select(client, channel, session.access_token, eligible, store, key)
    except ProjectSelectionError as exc:
        print(f"nest: {exc}", file=sys.stderr)
        return 1
    except grpc.RpcError as exc:
        error = _controller_error(
            exc,
            client.address,
            selection.LIST_TIMEOUT,
            auth_url=client.auth_url,
            hosted=client.profile == "hosted",
        )
        print(f"nest: {error or exc}", file=sys.stderr)
        return 3 if isinstance(error, ConnectionError) else 1
    if eligible is None:
        print("This Controller needs no Project.")
        return 0
    saved = store.get(key) if store is not None and key is not None else None
    if args.action == "use":
        wanted = str(args.project_id)
        if not any(p.id == wanted for p in eligible):
            print(
                f"nest: Project {wanted} is not one you may run in on {client.address}; "
                "see `nest projects list`.",
                file=sys.stderr,
            )
            return 1
        if store is None or key is None:
            return 2
        store.set(key, wanted)
        print(f"The SDK now runs in {wanted} on {client.address} (unless NEST_PROJECT_ID is set).")
        return 0
    if args.json:
        payload = {
            "controller": client.address,
            "saved_project_id": saved,
            "projects": [{"id": p.id, "name": p.name, "role": p.role} for p in eligible],
        }
        print(json.dumps(payload, indent=2))
    elif not eligible:
        print(
            "You have no Project that may run on this Controller; ask a Project Owner or "
            "Admin to invite you.",
            file=sys.stderr,
        )
    else:
        for project in eligible:
            marker = "*" if project.id == saved else " "
            print(f"{marker} {selection.describe(project)}")
    return 0


def _select(client, channel, token, eligible, store, key) -> int:
    """
    `nest projects select` (E07a): choose the saved Project by name on a terminal.

    The choice is checked against a fresh listing before it is saved, so a Project
    revoked while the prompt was open is never saved; a cancelled prompt changes
    nothing. `NEST_PROJECT_ID`, when set, keeps overriding the saved choice, and this
    says so instead of pretending the choice takes effect.
    """
    from . import project_selection as selection

    if eligible is None:
        print("This Controller needs no Project.")
        return 0
    if not eligible:
        print(
            f"nest: you have no Project that may run on {client.address}. Ask a Project "
            "Owner or Admin to invite you, or to have the Project approved for this "
            "Controller's HPC Platform. Nothing was saved.",
            file=sys.stderr,
        )
        return 1
    if store is None or key is None:
        return 2
    override = client.project_id
    if override is not None:
        print(
            f"nest: NEST_PROJECT_ID is set to {override}, so the SDK runs in that Project "
            "whatever you choose here. Unset it to use the Project you save.",
            file=sys.stderr,
        )
    if not selection.is_interactive():
        listed = "\n".join(f"  {selection.describe(p)}" for p in eligible)
        print(
            "nest: `nest projects select` needs a terminal to ask. Save a Project with "
            "`nest projects use <ID>`, or set NEST_PROJECT_ID. Nothing was saved.\n" + listed,
            file=sys.stderr,
        )
        return 1
    saved = store.get(key)
    chosen = selection.choose_project(
        eligible,
        sys.stderr,
        input,
        saved=saved,
        cancellable=True,
        heading=f"Nest Projects you may run in on {client.address}:",
        question="Select a Project",
    )
    if chosen is None:
        print("nest: cancelled; nothing was saved.", file=sys.stderr)
        return 1
    # Authorized again now: the list may be stale after a slow answer.
    current = selection.list_eligible(channel, token) or []
    if not any(p.id == chosen.id for p in current):
        print(
            f"nest: Project {chosen.id} is no longer one you may run in on {client.address}; "
            "nothing was saved. See `nest projects list`.",
            file=sys.stderr,
        )
        return 1
    store.set(key, chosen.id)
    label = selection.labels([chosen])[0]
    print(f"The SDK now runs in {label} [{chosen.id}] on {client.address}.")
    if override is not None:
        print("NEST_PROJECT_ID still overrides it while it is set.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Runs `nest`. Returns the exit status."""
    args = _parser().parse_args(argv)
    if args.command == "projects":
        try:
            return _projects(args)
        except ConfigError as exc:
            print(f"nest: {exc}", file=sys.stderr)
            return 2
        except NestUnavailableError as exc:
            print(f"nest: {exc}", file=sys.stderr)
            return 3
        except AuthError as exc:
            print(f"nest: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("\nnest: cancelled", file=sys.stderr)
            return 130
    try:
        config = load_cli_auth_config(url=args.url)
    except ConfigError as exc:
        print(f"nest: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "login":
            _warn_custom_controller(config)
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
