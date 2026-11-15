"""
Versioned SQL migrations for the Nest control-plane schema.

Nest has one ordered migration chain for its whole PostgreSQL schema. Each file in
`nest/db/migrations` is named `NNNN_description.sql`, numbered from `0001` without
gaps, and applied once, in order, in its own transaction. The `schema_migrations`
table records each applied version with the SHA-256 checksum of its file, so an
applied migration that was edited, or a database newer than the code, is detected.

Only `nest-db upgrade` applies migrations; the Controller only verifies them.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import psycopg

from .._logging import get_logger
from .connection import DatabaseError

logger = get_logger("nest.db.migrate")

MIGRATION_LOCK_ID = 0x4E455354_4D494752
"""`pg_advisory_lock` key held while migrations run ("NESTMIGR")."""

_FILENAME = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    name        text NOT NULL,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    duration_ms integer NOT NULL
)
"""


class MigrationError(DatabaseError):
    """A migration could not be applied, or the applied history is inconsistent."""


@dataclass(frozen=True)
class Migration:
    """
    One versioned schema change.

    Attributes:
        version (`int`): Position in the chain, starting at 1.
        name (`str`): Description from the file name.
        sql (`str`): SQL statements. Must not contain transaction control.
        checksum (`str`): SHA-256 of the file's bytes.
    """

    version: int
    name: str
    sql: str
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str


@dataclass
class SchemaStatus:
    """
    Comparison between the code's migrations and a database's history.

    Attributes:
        migrations (`list[Migration]`): Every migration in the code.
        applied (`dict[int, AppliedMigration]`): Rows of `schema_migrations`.
        initialized (`bool`): Whether `schema_migrations` exists.
    """

    migrations: list[Migration]
    applied: dict[int, AppliedMigration] = field(default_factory=dict)
    initialized: bool = True

    @property
    def pending(self) -> list[Migration]:
        """Migrations not yet applied."""
        return [m for m in self.migrations if m.version not in self.applied]

    @property
    def modified(self) -> list[Migration]:
        """Applied migrations whose file no longer matches the recorded checksum."""
        return [
            m
            for m in self.migrations
            if m.version in self.applied and self.applied[m.version].checksum != m.checksum
        ]

    @property
    def unknown(self) -> list[AppliedMigration]:
        """Applied versions this code does not have (the database is newer)."""
        known = {m.version for m in self.migrations}
        return [a for v, a in sorted(self.applied.items()) if v not in known]

    @property
    def current_version(self) -> int:
        return max(self.applied, default=0)

    @property
    def target_version(self) -> int:
        return max((m.version for m in self.migrations), default=0)

    @property
    def problems(self) -> list[str]:
        """Actionable descriptions of everything that makes the schema unusable."""
        if not self.initialized:
            return ["The database has no Nest schema; run `nest-db upgrade`"]
        problems = [
            f"Migration {m.label} was changed after it was applied (checksum mismatch); "
            "restore the original file and add a new migration instead"
            for m in self.modified
        ]
        problems += [
            f"The database has migration {a.version:04d}_{a.name}, which this Nest version "
            "does not have; upgrade Nest or restore a matching backup"
            for a in self.unknown
        ]
        if self.pending:
            problems.append(
                f"The schema is at version {self.current_version} but this Nest needs "
                f"{self.target_version}; run `nest-db upgrade` (pending: "
                + ", ".join(m.label for m in self.pending)
                + ")"
            )
        return problems

    @property
    def ok(self) -> bool:
        return not self.problems


def load_migrations(directory: Path | None = None) -> list[Migration]:
    """
    Reads and validates the migration files.

    Args:
        directory (`Path`, *optional*): Directory of `NNNN_name.sql` files. Defaults
            to the migrations packaged with Nest.

    Returns:
        `list[Migration]`: Migrations ordered by version.

    Raises:
        [`db.MigrationError`]: If a file is misnamed or versions are not `1..N`.
    """
    root = directory if directory is not None else resources.files(__package__) / "migrations"
    migrations = []
    for entry in root.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        match = _FILENAME.fullmatch(entry.name)
        if not match:
            raise MigrationError(
                f"Migration file {entry.name!r} must be named NNNN_lowercase_name.sql"
            )
        data = entry.read_bytes()
        migrations.append(
            Migration(
                version=int(match[1]),
                name=match[2],
                sql=data.decode("utf-8"),
                checksum=hashlib.sha256(data).hexdigest(),
            )
        )
    migrations.sort(key=lambda m: m.version)
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError(
            f"Migration versions must run from 0001 without gaps or duplicates, got {versions}"
        )
    return migrations


def _applied(conn: psycopg.Connection) -> tuple[bool, dict[int, AppliedMigration]]:
    exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()
    if not exists or not exists[0]:
        return False, {}
    rows = conn.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    return True, {row[0]: AppliedMigration(*row) for row in rows}


def schema_status(conn: psycopg.Connection, migrations: list[Migration] | None = None):
    """
    Reads the applied history and compares it with the code. Changes nothing.

    Args:
        conn (`psycopg.Connection`): Open connection.
        migrations (`list[Migration]`, *optional*): Defaults to [`db.load_migrations`].

    Returns:
        [`db.SchemaStatus`]: The comparison.
    """
    migrations = load_migrations() if migrations is None else migrations
    initialized, applied = _applied(conn)
    if not conn.autocommit:
        conn.rollback()
    return SchemaStatus(migrations, applied, initialized)


def _lock(conn: psycopg.Connection, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (MIGRATION_LOCK_ID,)).fetchone()
        if row and row[0]:
            return
        if time.monotonic() >= deadline:
            raise MigrationError(
                f"Another migration holds the lock after {timeout:g} seconds; wait for it to "
                "finish, or check pg_locks for a stuck session"
            )
        time.sleep(0.2)


def upgrade(
    conn: psycopg.Connection,
    migrations: list[Migration] | None = None,
    *,
    lock_timeout: float = 60.0,
) -> list[Migration]:
    """
    Applies pending migrations in order, each in its own transaction.

    Holds a PostgreSQL advisory lock for the whole run, so concurrent upgrades
    wait instead of racing. Before applying anything, refuses to continue when an
    applied migration was modified or the database has versions this code lacks.
    A failing migration is rolled back completely and stops the run; migrations
    applied before it stay applied.

    Args:
        conn (`psycopg.Connection`): Connection in autocommit mode.
        migrations (`list[Migration]`, *optional*): Defaults to [`db.load_migrations`].
        lock_timeout (`float`, *optional*, defaults to 60): Seconds to wait for the lock.

    Returns:
        `list[Migration]`: The migrations applied by this call.

    Raises:
        [`db.MigrationError`]: If the history is inconsistent or a migration fails.
    """
    if not conn.autocommit:
        raise ValueError("upgrade() needs an autocommit connection")
    migrations = load_migrations() if migrations is None else migrations
    _lock(conn, lock_timeout)
    try:
        conn.execute(_CREATE_TABLE)
        status = schema_status(conn, migrations)
        if status.modified or status.unknown:
            # Pending migrations are what upgrade fixes; everything else blocks it.
            raise MigrationError("; ".join(status.problems[: -1 if status.pending else None]))
        applied = []
        for migration in status.pending:
            started = time.monotonic()
            logger.info("Applying migration %s", migration.label)
            try:
                with conn.transaction():
                    conn.execute(migration.sql.encode())
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name, checksum, duration_ms) "
                        "VALUES (%s, %s, %s, %s)",
                        (
                            migration.version,
                            migration.name,
                            migration.checksum,
                            round((time.monotonic() - started) * 1000),
                        ),
                    )
            except psycopg.Error as exc:
                detail = " ".join(str(exc).split())
                raise MigrationError(
                    f"Migration {migration.label} failed and was rolled back: {detail}"
                ) from None
            applied.append(migration)
        return applied
    finally:
        if not conn.closed:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))


def verify(conn: psycopg.Connection, migrations: list[Migration] | None = None) -> None:
    """
    Checks that every migration is applied unchanged and none is unknown.

    Raises:
        [`db.MigrationError`]: Listing each problem and how to fix it.
    """
    status = schema_status(conn, migrations)
    if not status.ok:
        raise MigrationError("; ".join(status.problems))
