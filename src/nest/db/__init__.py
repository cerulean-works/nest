"""
PostgreSQL persistence for the Nest Controller.

The database is externally managed and opt-in (`NEST_DATABASE_URL`). Schema changes
are applied only by `nest-db upgrade`; the Controller verifies the schema at startup
and refuses to serve when it is unreachable or out of date.
"""

from .bootstrap import (
    GOOGLE_ISSUERS,
    AuditEvent,
    BootstrapOutcome,
    BootstrapState,
    PlatformAdminBootstrap,
    audit_events,
    bootstrap_state,
)
from .cli_logins import (
    CLILoginRequest,
    CLILoginStore,
    CLIPoll,
    TooManyPendingCLILoginsError,
    UserCodeCollisionError,
)
from .connection import Database, DatabaseError
from .identity import (
    Identity,
    IdentityConflictError,
    IdentityStore,
    Session,
    User,
    UserNotActiveError,
    hash_token,
    normalize_email,
)
from .login_flows import LoginFlow, LoginFlowStore, TooManyPendingSignInsError
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
from .sessions import AccessGrant, Rotation, SessionRefreshError, SessionStore
from .store import JobStore, sync_providers

__all__ = [
    "ASSIGNABLE_ROLES",
    "GOOGLE_ISSUERS",
    "PROJECT_ROLES",
    "AccessGrant",
    "AuditEvent",
    "BootstrapOutcome",
    "BootstrapState",
    "CLILoginRequest",
    "CLILoginStore",
    "CLIPoll",
    "Database",
    "DatabaseError",
    "DeliveryStatus",
    "DuplicateMemberError",
    "EmailOutbox",
    "Identity",
    "IdentityConflictError",
    "IdentityStore",
    "JobStore",
    "LoginFlow",
    "LoginFlowStore",
    "MemberNotFoundError",
    "Membership",
    "Migration",
    "MigrationError",
    "OwnerRuleError",
    "PlatformAdminBootstrap",
    "Project",
    "ProjectError",
    "ProjectNotFoundError",
    "ProjectStore",
    "QueuedEmail",
    "Rotation",
    "SchemaStatus",
    "Session",
    "SessionRefreshError",
    "SessionStore",
    "TooManyPendingCLILoginsError",
    "TooManyPendingSignInsError",
    "User",
    "UserCodeCollisionError",
    "UserNotActiveError",
    "UserNotFoundError",
    "audit_events",
    "bootstrap_state",
    "hash_token",
    "load_migrations",
    "normalize_email",
    "schema_status",
    "sync_providers",
    "upgrade",
    "verify",
]
