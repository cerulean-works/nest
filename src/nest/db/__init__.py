"""
PostgreSQL persistence for the Nest Controller.

The database is externally managed and opt-in (`NEST_DATABASE_URL`). Schema changes
are applied only by `nest-db upgrade`; the Controller verifies the schema at startup
and refuses to serve when it is unreachable or out of date.
"""

from .connection import Database, DatabaseError
from .migrate import (
    Migration,
    MigrationError,
    SchemaStatus,
    load_migrations,
    schema_status,
    upgrade,
    verify,
)
from .store import JobStore, sync_providers

__all__ = [
    "Database",
    "DatabaseError",
    "JobStore",
    "Migration",
    "MigrationError",
    "SchemaStatus",
    "load_migrations",
    "schema_status",
    "sync_providers",
    "upgrade",
    "verify",
]
