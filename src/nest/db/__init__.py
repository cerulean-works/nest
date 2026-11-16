"""
PostgreSQL persistence for the Nest Controller.

The database is externally managed and opt-in (`NEST_DATABASE_URL`). Schema changes
are applied only by `nest-db upgrade`; the Controller verifies the schema at startup
and refuses to serve when it is unreachable or out of date.
"""

from .connection import Database, DatabaseError
from .identity import (
    Identity,
    IdentityConflictError,
    IdentityStore,
    Session,
    User,
    UserNotActiveError,
    hash_token,
)
from .migrate import (
    Migration,
    MigrationError,
    SchemaStatus,
    load_migrations,
    schema_status,
    upgrade,
    verify,
)
from .outbox import DeliveryStatus, EmailOutbox, QueuedEmail
from .projects import (
    ASSIGNABLE_ROLES,
    PROJECT_ROLES,
    DuplicateMemberError,
    MemberNotFoundError,
    Membership,
    OwnerRuleError,
    Project,
    ProjectError,
    ProjectNotFoundError,
    ProjectStore,
    UserNotFoundError,
)
from .store import JobStore, sync_providers

__all__ = [
    "ASSIGNABLE_ROLES",
    "PROJECT_ROLES",
    "Database",
    "DatabaseError",
    "DeliveryStatus",
    "DuplicateMemberError",
    "EmailOutbox",
    "Identity",
    "IdentityConflictError",
    "IdentityStore",
    "JobStore",
    "MemberNotFoundError",
    "Membership",
    "Migration",
    "MigrationError",
    "OwnerRuleError",
    "Project",
    "ProjectError",
    "ProjectNotFoundError",
    "ProjectStore",
    "QueuedEmail",
    "SchemaStatus",
    "Session",
    "User",
    "UserNotActiveError",
    "UserNotFoundError",
    "hash_token",
    "load_migrations",
    "schema_status",
    "sync_providers",
    "upgrade",
    "verify",
]
