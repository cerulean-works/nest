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

Reads the same `NEST_DATABASE_*` settings as the Controller. Exit status: 0 on
success, 1 when the schema is not current, a migration failed or an admin change
was not made, 2 for invalid configuration or arguments, 3 when the database is
unreachable.
"""

from __future__ import annotations

import argparse
import json
import sys
from uuid import UUID

import psycopg

from .._logging import configure_logging
from ..config import ConfigError, load_database_config
from .bootstrap import audit_events, bootstrap_state
from .connection import Database, DatabaseError
from .identity import IdentityStore
from .migrate import MigrationError, schema_status, upgrade, verify


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
    return parser


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
    if args.command == "admin":
        try:
            database.open()
        except DatabaseError as exc:
            print(exc, file=sys.stderr)
            return 3
        try:
            with database.connection() as conn:
                verify(conn)
            return _admin(database, args)
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
