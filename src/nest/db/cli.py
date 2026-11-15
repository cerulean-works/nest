"""
`nest-db`: apply and verify the Controller's PostgreSQL schema.

Usage:

    nest-db upgrade   # apply pending migrations (advisory-locked, transactional)
    nest-db verify    # exit 0 only if the schema matches this Nest version
    nest-db status    # list applied and pending migrations

Reads the same `NEST_DATABASE_*` settings as the Controller. Exit status: 0 on
success, 1 when the schema is not current or a migration failed, 2 for invalid
configuration, 3 when the database is unreachable.
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from .._logging import configure_logging
from ..config import ConfigError, load_database_config
from .connection import Database, DatabaseError
from .migrate import MigrationError, schema_status, upgrade


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging()
    try:
        config = load_database_config()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    database = Database(config, application_name="nest-db")
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
            print(f"Database error: {' '.join(str(exc).split())}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
