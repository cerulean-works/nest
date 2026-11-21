"""
`nest-db`: apply and verify the Controller's PostgreSQL schema.

Usage:

    nest-db upgrade   # apply pending migrations (advisory-locked, transactional)
    nest-db verify    # exit 0 only if the schema matches this Nest version
    nest-db status    # list applied and pending migrations

    # Platform administrator recovery (see "Recover platform administration"):
    nest-db admin status
    nest-db admin find-user --email ADDRESS
    nest-db admin grant  --user-id UUID --operator NAME --reason TEXT
    nest-db admin revoke --user-id UUID --operator NAME --reason TEXT
    nest-db admin audit  [--user-id UUID] [--limit N]

    # Browser and CLI sessions (see "Revoke sessions"):
    nest-db sessions list   --user-id UUID
    nest-db sessions revoke --user-id UUID | --session-id UUID

    # The HPC Platform Catalog and Login-Node Registry (see "HPC Platforms"):
    nest-db platform list
    nest-db platform show     PLATFORM
    nest-db platform create   --slug SLUG --name NAME [--operated-by NAME] [--default]
    nest-db platform update   PLATFORM [--name NAME] [--operated-by NAME | --clear-operated-by]
    nest-db platform disable | enable PLATFORM
    nest-db platform set-default PLATFORM | --none
    nest-db platform add-node PLATFORM --host HOST [--port N] --modes A,B
                              [--priority N] [--host-key-pin REF]
    nest-db platform update-node NODE_ID [--modes A,B] [--priority N] [--port N]
                              [--host-key-pin REF] [--status active|disabled]
    nest-db platform remove-node NODE_ID
    nest-db platform grant | revoke PLATFORM --project-id UUID
    (every change also takes --operator NAME --reason TEXT)

    # The Platform-scoped Slurm Account Registry (see "Slurm accounts"):
    nest-db account list     PLATFORM
    nest-db account show     PLATFORM --project-id UUID
    nest-db account register PLATFORM NAME [--kind standard|chargeback] [--description TEXT]
    nest-db account disable | enable PLATFORM NAME
    nest-db account set-mode-a-default PLATFORM NAME | --none
    nest-db account link     PLATFORM NAME --project-id UUID --modes A,B [--default]
    nest-db account unlink   PLATFORM NAME --project-id UUID
    nest-db account clear-default  PLATFORM --project-id UUID
    nest-db account native-default PLATFORM --project-id UUID --allow | --forbid
    (every change also takes --operator NAME --reason TEXT)

    # The security audit log (see "Security audit log"):
    nest-db audit list   [--user-id UUID] [--project-id UUID] [--event NAME] [--limit N]
    nest-db audit verify # seal new records, then check the hash chain

Reads the same `NEST_DATABASE_*` settings as the Controller. Exit status: 0 on
success, 1 when the schema is not current, a migration failed or an admin change
or revocation or Platform or Slurm account change was not made or the audit chain
is broken, 2 for invalid configuration or arguments, 3 when the database is
unreachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from uuid import UUID

import psycopg

from .._logging import configure_logging
from ..audit import AuditLog
from ..config import ConfigError, load_database_config
from .accounts import ProjectAccountLink, SlurmAccount, SlurmAccountStore
from .bootstrap import audit_events, bootstrap_state
from .connection import Database, DatabaseError
from .identity import IdentityStore
from .migrate import MigrationError, schema_status, upgrade, verify
from .platforms import (
    LoginNodeNotFoundError,
    Platform,
    PlatformError,
    PlatformLoginNode,
    PlatformNotFoundError,
    PlatformStore,
)
from .projects import ProjectNotFoundError
from .sessions import SessionStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nest-db",
        description="Apply and verify the Nest Controller's PostgreSQL schema.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("upgrade", help="apply pending migrations")
    up.add_argument(
        "--lock-timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="how long to wait for a concurrent upgrade (default: 60)",
    )
    commands.add_parser("verify", help="check that the schema is current and unmodified")
    commands.add_parser("status", help="list applied and pending migrations")

    admin = commands.add_parser(
        "admin",
        help="inspect and recover platform administration",
        description=(
            "Operator recovery for platform_admin. Authenticated by the database "
            "credentials in NEST_DATABASE_*; every change is audited with the "
            "operator name and the PostgreSQL role."
        ),
    )
    actions = admin.add_subparsers(dest="action", required=True)
    actions.add_parser("status", help="show the bootstrap and the current administrators")
    find = actions.add_parser("find-user", help="list Users whose sign-in reported an address")
    find.add_argument("--email", required=True)
    for name, verb in (("grant", "grant platform_admin to"), ("revoke", "revoke it from")):
        change = actions.add_parser(name, help=f"{verb} a User")
        change.add_argument("--user-id", required=True, type=UUID, metavar="UUID")
        change.add_argument(
            "--operator", required=True, help="who is making the change, for the audit log"
        )
        change.add_argument("--reason", required=True, help="why, for the audit log")
    audit = actions.add_parser("audit", help="show the platform audit log, newest first")
    audit.add_argument("--user-id", type=UUID, metavar="UUID", help="only entries about this User")
    audit.add_argument("--limit", type=int, default=50)

    sessions = commands.add_parser(
        "sessions",
        help="list and revoke Users' sessions",
        description=(
            "Administrative session revocation. Authenticated by the database "
            "credentials in NEST_DATABASE_*. Revocation takes effect at once: the "
            "session's access tokens and refresh secret are refused from then on."
        ),
    )
    session_actions = sessions.add_subparsers(dest="action", required=True)
    listing = session_actions.add_parser("list", help="list a User's active sessions")
    listing.add_argument("--user-id", required=True, type=UUID, metavar="UUID")
    revoke = session_actions.add_parser("revoke", help="revoke one session or all of a User's")
    target = revoke.add_mutually_exclusive_group(required=True)
    target.add_argument("--user-id", type=UUID, metavar="UUID", help="every session of this User")
    target.add_argument(
        "--session-id",
        type=UUID,
        metavar="UUID",
        help="this session and every session rotated from the same sign-in",
    )

    _platform_parser(commands)
    _account_parser(commands)

    security = commands.add_parser(
        "audit",
        help="read and verify the security audit log",
        description=(
            "The append-only security audit log (security_audit_events). Records never "
            "contain tokens, codes, email addresses or credentials."
        ),
    )
    audit_actions = security.add_subparsers(dest="action", required=True)
    events = audit_actions.add_parser("list", help="show security audit records, newest first")
    events.add_argument("--user-id", type=UUID, metavar="UUID", help="only this actor's records")
    events.add_argument("--project-id", type=UUID, metavar="UUID", help="only this Project's")
    events.add_argument("--event", help="only this event, such as run.submit")
    events.add_argument("--limit", type=int, default=50)
    audit_actions.add_parser("verify", help="seal new records, then check the hash chain")
    return parser


def _platform_parser(commands) -> None:
    platform = commands.add_parser(
        "platform",
        help="manage HPC Platforms, their login nodes and Project grants",
        description=(
            "The HPC Platform Catalog and Login-Node Registry. Authenticated by the "
            "database credentials in NEST_DATABASE_*; every change is audited with the "
            "operator name, the reason and the PostgreSQL role. PLATFORM is a slug or ID."
        ),
    )
    actions = platform.add_subparsers(dest="action", required=True)
    change = argparse.ArgumentParser(add_help=False)
    change.add_argument(
        "--operator", required=True, help="who is making the change, for the audit log"
    )
    change.add_argument("--reason", required=True, help="why, for the audit log")
    actions.add_parser("list", help="list every Platform")
    show = actions.add_parser("show", help="show a Platform, its login nodes and grants")
    show.add_argument("platform", metavar="PLATFORM")
    create = actions.add_parser("create", parents=[change], help="register a Platform")
    create.add_argument("--slug", required=True, help="unique, permanent name such as nano4")
    create.add_argument("--name", required=True, help="display name")
    create.add_argument("--operated-by", metavar="NAME", help="operator metadata, such as NCHC")
    create.add_argument("--default", action="store_true", help="make it the default Platform")
    update = actions.add_parser("update", parents=[change], help="change a Platform's metadata")
    update.add_argument("platform", metavar="PLATFORM")
    update.add_argument("--name", help="display name")
    operated = update.add_mutually_exclusive_group()
    operated.add_argument("--operated-by", metavar="NAME", help="operator metadata")
    operated.add_argument("--clear-operated-by", action="store_true")
    for name, verb in (("disable", "disable"), ("enable", "re-enable")):
        status = actions.add_parser(name, parents=[change], help=f"{verb} a Platform")
        status.add_argument("platform", metavar="PLATFORM")
    default = actions.add_parser(
        "set-default", parents=[change], help="set or clear the default Platform"
    )
    target = default.add_mutually_exclusive_group(required=True)
    target.add_argument("platform", metavar="PLATFORM", nargs="?")
    target.add_argument("--none", action="store_true", help="clear the default")
    node = actions.add_parser("add-node", parents=[change], help="approve a login node")
    node.add_argument("platform", metavar="PLATFORM")
    node.add_argument("--host", required=True)
    node.add_argument("--port", type=int)
    node.add_argument("--modes", required=True, help="approved transport modes: A, B or A,B")
    node.add_argument("--priority", type=int, default=100, help="lower is tried first")
    node.add_argument(
        "--host-key-pin", metavar="REF", help="vetted host-key reference; required for Mode B"
    )
    edit = actions.add_parser("update-node", parents=[change], help="change a login node")
    edit.add_argument("node_id", type=int, metavar="NODE_ID")
    edit.add_argument("--modes")
    edit.add_argument("--priority", type=int)
    edit.add_argument("--port", type=int)
    edit.add_argument("--host-key-pin", metavar="REF")
    edit.add_argument("--status", choices=("active", "disabled"))
    remove = actions.add_parser("remove-node", parents=[change], help="remove a login node")
    remove.add_argument("node_id", type=int, metavar="NODE_ID")
    for name, verb in (("grant", "let a Project use"), ("revoke", "stop a Project using")):
        grant = actions.add_parser(name, parents=[change], help=f"{verb} a Platform")
        grant.add_argument("platform", metavar="PLATFORM")
        grant.add_argument("--project-id", required=True, type=UUID, metavar="UUID")


def _account_parser(commands) -> None:
    account = commands.add_parser(
        "account",
        help="manage Platform-scoped Slurm accounts and their Project links",
        description=(
            "The Slurm Account Registry. An account belongs to one Platform: the same name "
            "on two Platforms is two accounts. Every change is audited with the operator "
            "name, the reason and the PostgreSQL role. PLATFORM is a slug or ID."
        ),
    )
    actions = account.add_subparsers(dest="action", required=True)
    change = argparse.ArgumentParser(add_help=False)
    change.add_argument(
        "--operator", required=True, help="who is making the change, for the audit log"
    )
    change.add_argument("--reason", required=True, help="why, for the audit log")
    project = argparse.ArgumentParser(add_help=False)
    project.add_argument("--project-id", required=True, type=UUID, metavar="UUID")
    listing = actions.add_parser("list", help="list a Platform's Slurm accounts")
    listing.add_argument("platform", metavar="PLATFORM")
    show = actions.add_parser(
        "show", parents=[project], help="show a Project's accounts and policy on a Platform"
    )
    show.add_argument("platform", metavar="PLATFORM")
    register = actions.add_parser("register", parents=[change], help="register an account")
    register.add_argument("platform", metavar="PLATFORM")
    register.add_argument("name", metavar="NAME")
    register.add_argument("--kind", choices=("standard", "chargeback"), default="standard")
    register.add_argument("--description")
    for name, verb in (("disable", "disable"), ("enable", "re-enable")):
        status = actions.add_parser(name, parents=[change], help=f"{verb} an account")
        status.add_argument("platform", metavar="PLATFORM")
        status.add_argument("name", metavar="NAME")
    default = actions.add_parser(
        "set-mode-a-default", parents=[change], help="set or clear the Mode A operator default"
    )
    default.add_argument("platform", metavar="PLATFORM")
    target = default.add_mutually_exclusive_group(required=True)
    target.add_argument("name", metavar="NAME", nargs="?")
    target.add_argument("--none", action="store_true", help="clear the default")
    link = actions.add_parser(
        "link", parents=[change, project], help="let a Project charge an account"
    )
    link.add_argument("platform", metavar="PLATFORM")
    link.add_argument("name", metavar="NAME")
    link.add_argument("--modes", required=True, help="permitted modes: A, B or A,B")
    link.add_argument("--default", action="store_true", help="make it the Project default")
    unlink = actions.add_parser(
        "unlink", parents=[change, project], help="stop a Project charging an account"
    )
    unlink.add_argument("platform", metavar="PLATFORM")
    unlink.add_argument("name", metavar="NAME")
    clear = actions.add_parser(
        "clear-default", parents=[change, project], help="clear a Project's default account"
    )
    clear.add_argument("platform", metavar="PLATFORM")
    native = actions.add_parser(
        "native-default",
        parents=[change, project],
        help="allow or forbid Mode B jobs to use the User's native Slurm default",
    )
    native.add_argument("platform", metavar="PLATFORM")
    choice = native.add_mutually_exclusive_group(required=True)
    choice.add_argument("--allow", action="store_true")
    choice.add_argument("--forbid", action="store_true")


def _print_account(account: SlurmAccount) -> None:
    print(
        f"{account.account_name} id={account.id} platform={account.platform_id} "
        f"kind={account.kind} status={account.status} "
        f"mode_a_default={'yes' if account.is_mode_a_default else 'no'}"
        + (f" description={json.dumps(account.description)}" if account.description else "")
    )


def _print_link(link: ProjectAccountLink) -> None:
    print(
        f"  link {link.account.account_name} id={link.account.id} "
        f"modes={','.join(link.modes)} default={'yes' if link.is_default else 'no'} "
        f"status={link.account.status} kind={link.account.kind}"
    )


def _account(database: Database, args: argparse.Namespace) -> int:
    """Runs one `nest-db account` action on an open, verified database."""
    store = SlurmAccountStore(database)
    audit: dict[str, Any] = {
        "operator": getattr(args, "operator", None),
        "reason": getattr(args, "reason", None),
    }
    try:
        if args.action == "list":
            for account in store.list_accounts(args.platform):
                _print_account(account)
        elif args.action == "show":
            mapping = store.mapping(args.project_id, args.platform)
            print(
                f"project={mapping.project_id} platform={mapping.platform.slug} "
                f"granted={'yes' if mapping.granted else 'no'} "
                f"native_default={'allowed' if mapping.allow_native_default else 'forbidden'}"
            )
            for link in mapping.links:
                _print_link(link)
        elif args.action == "register":
            _print_account(
                store.register_account(
                    args.platform,
                    args.name,
                    kind=args.kind,
                    description=args.description,
                    **audit,
                )
            )
        elif args.action in ("disable", "enable"):
            status = "disabled" if args.action == "disable" else "active"
            _print_account(store.update_account(args.platform, args.name, status=status, **audit))
        elif args.action == "set-mode-a-default":
            account = store.set_mode_a_default(
                args.platform, None if args.none else args.name, **audit
            )
            if account is None:
                print("No Mode A default account")
            else:
                _print_account(account)
        elif args.action == "link":
            _print_link(
                store.link_project(
                    args.project_id,
                    args.platform,
                    args.name,
                    modes=args.modes.split(","),
                    default=args.default,
                    **audit,
                )
            )
        elif args.action == "unlink":
            store.unlink_project(args.project_id, args.platform, args.name, **audit)
            print(f"Project {args.project_id} no longer charges {args.name} on {args.platform}")
        elif args.action == "clear-default":
            if not store.clear_project_default(args.project_id, args.platform, **audit):
                print("No change: the Project has no default account", file=sys.stderr)
                return 1
            print(f"Project {args.project_id} has no default account on {args.platform}")
        else:
            store.set_native_default(args.project_id, args.platform, args.allow, **audit)
            print(
                f"Native Slurm default {'allowed' if args.allow else 'forbidden'} for Mode B "
                f"jobs of Project {args.project_id} on {args.platform}"
            )
        return 0
    except PlatformError as exc:
        print(exc, file=sys.stderr)
        return 1
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2


def _print_platform(platform: Platform) -> None:
    print(
        f"{platform.slug} id={platform.id} status={platform.status} source={platform.source}"
        f" default={'yes' if platform.is_default else 'no'}"
        f" operated_by={json.dumps(platform.operator_name) if platform.operator_name else '-'}"
        f" name={json.dumps(platform.display_name)}"
    )


def _print_node(node: PlatformLoginNode) -> None:
    print(
        f"  node {node.id} {node} modes={','.join(node.transport_modes)} "
        f"priority={node.priority} status={node.status} health={node.health} "
        f"host_key_pin={node.host_key_pin_ref or '-'}"
    )


def _platform(database: Database, args: argparse.Namespace) -> int:
    """Runs one `nest-db platform` action on an open, verified database."""
    store = PlatformStore(database)
    audit: dict[str, Any] = {
        "operator": getattr(args, "operator", None),
        "reason": getattr(args, "reason", None),
    }
    try:
        if args.action == "list":
            for platform in store.list_platforms():
                _print_platform(platform)
            return 0
        if args.action == "show":
            platform = store.get_platform(args.platform)
            if platform is None:
                print("No such Platform", file=sys.stderr)
                return 1
            _print_platform(platform)
            for node in store.login_nodes(platform.id):
                _print_node(node)
            for project_id in store.granted_projects(platform.id):
                print(f"  grant project={project_id}")
            return 0
        if args.action == "create":
            _print_platform(
                store.create_platform(
                    args.slug,
                    args.name,
                    operator_name=args.operated_by,
                    default=args.default,
                    **audit,
                )
            )
        elif args.action == "update":
            options: dict[str, Any] = {}
            if args.operated_by is not None or args.clear_operated_by:
                options["operator_name"] = args.operated_by
            _print_platform(
                store.update_platform(args.platform, display_name=args.name, **options, **audit)
            )
        elif args.action in ("disable", "enable"):
            status = "disabled" if args.action == "disable" else "active"
            _print_platform(store.set_status(args.platform, status, **audit))
        elif args.action == "set-default":
            platform = store.set_default(None if args.none else args.platform, **audit)
            if platform is None:
                print("No default Platform")
            else:
                _print_platform(platform)
        elif args.action == "add-node":
            _print_node(
                store.add_login_node(
                    args.platform,
                    args.host,
                    port=args.port,
                    transport_modes=args.modes.split(","),
                    priority=args.priority,
                    host_key_pin_ref=args.host_key_pin,
                    **audit,
                )
            )
        elif args.action == "update-node":
            options = {}
            if args.port is not None:
                options["port"] = args.port
            if args.host_key_pin is not None:
                options["host_key_pin_ref"] = args.host_key_pin
            _print_node(
                store.update_login_node(
                    args.node_id,
                    transport_modes=args.modes.split(",") if args.modes else None,
                    priority=args.priority,
                    status=args.status,
                    **options,
                    **audit,
                )
            )
        elif args.action == "remove-node":
            store.remove_login_node(args.node_id, **audit)
            print(f"Removed login node {args.node_id}")
        else:
            change = store.grant_project if args.action == "grant" else store.revoke_project
            if not change(args.platform, args.project_id, **audit):
                print(
                    "No change: the Project "
                    + ("is already granted" if args.action == "grant" else "has no grant"),
                    file=sys.stderr,
                )
                return 1
            print(
                f"Project {args.project_id} "
                + ("granted" if args.action == "grant" else "no longer granted")
                + f" Platform {args.platform}"
            )
        return 0
    except (PlatformNotFoundError, LoginNodeNotFoundError, ProjectNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 1
    except PlatformError as exc:
        print(exc, file=sys.stderr)
        return 1
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2


def _audit(database: Database, args: argparse.Namespace) -> int:
    """Runs one `nest-db audit` action on an open, verified database."""
    log = AuditLog(database)
    if args.action == "verify":
        log.seal()
        status = log.verify()
        if not status.intact:
            print(
                f"Audit chain BROKEN at chain_seq={status.first_broken}: a sealed record was "
                f"changed, removed or reordered ({status.sealed} sealed records checked)",
                file=sys.stderr,
            )
            return 1
        print(
            f"Audit chain intact: {status.sealed} sealed records"
            + (f", {status.unsealed} not sealed yet" if status.unsealed else "")
        )
        return 0
    if not 1 <= args.limit <= 1000:
        print("--limit must be between 1 and 1000", file=sys.stderr)
        return 2
    for event in log.events(
        limit=args.limit,
        event=args.event,
        actor_user_id=args.user_id,
        project_id=args.project_id,
    ):
        fields = {
            "outcome": event.outcome,
            "interface": event.interface,
            "actor": event.actor_user_id or event.actor_operator,
            "session": event.session_id,
            "project": event.project_id,
            "target": event.target_user_id,
            "resource": (
                f"{event.resource_type}:{event.resource_id}" if event.resource_type else None
            ),
            "reason": event.reason,
            "request_id": event.request_id,
            "db_role": event.database_role,
        }
        print(
            f"{event.occurred_at.isoformat()} {event.event} "
            + " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
            + (f" note={json.dumps(event.note)}" if event.note else "")
            + (f" details={json.dumps(event.details, sort_keys=True)}" if event.details else "")
        )
    return 0


def _sessions(database: Database, args: argparse.Namespace) -> int:
    """Runs one `nest-db sessions` action on an open, verified database."""
    if args.action == "list":
        for session in SessionStore(database).active_sessions(args.user_id):
            print(
                f"{session.id} kind={session.kind} family={session.family_id} "
                f"created={session.created_at.isoformat()} "
                f"expires={session.expires_at.isoformat()} "
                f"family_expires={session.family_expires_at.isoformat()}"
            )
        return 0
    identities = IdentityStore(database)
    if args.session_id is not None:
        if not identities.revoke_session(args.session_id, reason="admin_revoked"):
            print("No change: no active session in that session's family", file=sys.stderr)
            return 1
        print(f"Revoked the session family of {args.session_id}")
        return 0
    count = identities.revoke_user_sessions(args.user_id, reason="admin_revoked")
    if not count:
        print("No change: the User has no active session", file=sys.stderr)
        return 1
    print(f"Revoked {count} session(s) of {args.user_id}")
    return 0


def _admin(database: Database, args: argparse.Namespace) -> int:
    """Runs one `nest-db admin` action on an open, verified database."""
    identities = IdentityStore(database)
    if args.action == "status":
        state = bootstrap_state(database)
        if state is None:
            print("bootstrap: open (no platform_admin has ever been granted)")
        else:
            print(
                f"bootstrap: completed {state.completed_at.isoformat()} "
                f"method={state.method} user_id={state.user_id}"
            )
        admins = identities.platform_admins()
        print(f"platform_admin: {len(admins)}")
        for user_id, granted_at in admins:
            user = identities.get_user(user_id)
            status = user.status if user else "missing"
            print(f"  {user_id} granted={granted_at.isoformat()} status={status}")
        return 0
    if args.action == "find-user":
        matches = identities.find_users_by_email(args.email)
        if not matches:
            print("No User has signed in with that address", file=sys.stderr)
            return 1
        for user, identity in matches:
            print(
                f"{user.id} status={user.status} issuer={identity.issuer} "
                f"subject={identity.subject} email_verified={identity.email_verified} "
                f"last_login={identity.last_login_at.isoformat() if identity.last_login_at else '-'}"
            )
        return 0
    if args.action in ("grant", "revoke"):
        try:
            if args.action == "grant":
                changed = identities.grant_platform_role(
                    args.user_id, operator=args.operator, reason=args.reason
                )
            else:
                changed = identities.revoke_platform_role(
                    args.user_id, operator=args.operator, reason=args.reason
                )
        except LookupError as exc:
            print(exc, file=sys.stderr)
            return 1
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        if not changed:
            print(
                f"No change: the User {'already has' if args.action == 'grant' else 'does not have'}"
                " platform_admin",
                file=sys.stderr,
            )
            return 1
        print(
            f"platform_admin {'granted to' if args.action == 'grant' else 'revoked from'} "
            f"{args.user_id}"
        )
        return 0
    if args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2
    for event in reversed(audit_events(database, target_user_id=args.user_id, limit=args.limit)):
        actor = event.actor_operator or (str(event.actor_user_id) if event.actor_user_id else "-")
        print(
            f"{event.occurred_at.isoformat()} {event.event} target={event.target_user_id or '-'} "
            f"actor={actor} db_role={event.database_role}"
            + (f" reason={json.dumps(event.reason)}" if event.reason else "")
            + (f" details={json.dumps(event.details, sort_keys=True)}" if event.details else "")
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging()
    try:
        config = load_database_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    database = Database(config, application_name="nest-db")
    if args.command in ("admin", "sessions", "audit", "platform", "account"):
        try:
            database.open()
        except DatabaseError as exc:
            print(exc, file=sys.stderr)
            return 3
        try:
            with database.connection() as conn:
                verify(conn)
            action = {
                "admin": _admin,
                "sessions": _sessions,
                "audit": _audit,
                "platform": _platform,
                "account": _account,
            }[args.command]
            return action(database, args)
        except MigrationError as exc:
            print(f"{exc}; run nest-db upgrade first", file=sys.stderr)
            return 1
        except DatabaseError as exc:
            print(exc, file=sys.stderr)
            return 3
        except psycopg.Error as exc:
            print(f"Database error: {database.scrub(str(exc))}", file=sys.stderr)
            return 3
        finally:
            database.close()
    try:
        conn = database.connect()
    except DatabaseError as exc:
        print(exc, file=sys.stderr)
        return 3
    with conn:
        try:
            if args.command == "upgrade":
                applied = upgrade(conn, lock_timeout=args.lock_timeout)
                status = schema_status(conn)
                for migration in applied:
                    print(f"applied {migration.label}")
                print(
                    f"Schema is at version {status.current_version} on {database.describe()}"
                    + ("" if applied else " (already current)")
                )
                return 0
            status = schema_status(conn)
            if args.command == "status":
                for migration in status.migrations:
                    state = "applied" if migration.version in status.applied else "pending"
                    if migration in status.modified:
                        state = "MODIFIED"
                    elif migration in status.renamed:
                        state = "RENAMED"
                    print(f"{state:8} {migration.label}")
                for unknown in status.unknown:
                    print(f"{'UNKNOWN':8} {unknown.version:04d}_{unknown.name}")
            for problem in status.problems:
                print(problem, file=sys.stderr)
            if status.ok and args.command == "verify":
                print(
                    f"Schema is current at version {status.current_version} on "
                    f"{database.describe()}"
                )
            return 0 if status.ok else 1
        except MigrationError as exc:
            print(exc, file=sys.stderr)
            return 1
        except DatabaseError as exc:
            print(exc, file=sys.stderr)
            return 3
        except psycopg.Error as exc:
            print(f"Database error: {database.scrub(str(exc))}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
