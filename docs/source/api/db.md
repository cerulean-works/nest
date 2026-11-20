# Database and migrations

`nest.db` connects the Controller to its externally managed PostgreSQL database,
applies and verifies the schema, records Provider Profiles and Slurm jobs, and
stores Users, identities, sessions, Projects and their memberships. See
[Store Controller records in PostgreSQL](../database) for deployment, the
`nest-db` command, and backups.

Apply and verify migrations from Python, for example in a deployment script:

```python
from nest.config import load_database_config
from nest.db import Database, upgrade, verify

database = Database(load_database_config(), application_name="deploy")
with database.connect() as conn:
    for migration in upgrade(conn):
        print("applied", migration.label)
    verify(conn)
```

## Connections

Settings are read by [`config.load_database_config`] into a [`config.DatabaseConfig`]
(see [Configuration loaders](config)).

[[autodoc]] db.Database

[[autodoc]] db.DatabaseError

## Migrations

[[autodoc]] db.upgrade

[[autodoc]] db.verify

[[autodoc]] db.schema_status

[[autodoc]] db.load_migrations

[[autodoc]] db.SchemaStatus

[[autodoc]] db.Migration

[[autodoc]] db.MigrationError

## Records

[[autodoc]] db.sync_providers

[[autodoc]] db.JobStore

The email outbox ([`db.EmailOutbox`]) is documented with [Email delivery](mail).

## HPC Platforms and login nodes

See [Register HPC Platforms and login nodes](../platforms). `PlatformStore` is the
operator-side API; users discover Platforms through [`authz.PlatformCatalog`].

```python
from nest.db import PlatformStore

platforms = PlatformStore(database)
ops = {"operator": "ops-alice", "reason": "onboard nano4"}
nano4 = platforms.create_platform("nano4", "Nano 4", operator_name="NCHC", **ops)
platforms.add_login_node(
    nano4.id, "ln01.nano4.example", transport_modes=["B"], host_key_pin_ref=pin, **ops
)
platforms.grant_project(nano4.id, project_id, **ops)
```

[[autodoc]] db.PlatformStore

[[autodoc]] db.Platform

[[autodoc]] db.PlatformLoginNode

[[autodoc]] db.LoginEndpoint

[[autodoc]] db.order_login_nodes

[[autodoc]] db.PlatformError

[[autodoc]] db.PlatformNotFoundError

[[autodoc]] db.DuplicatePlatformError

[[autodoc]] db.LoginNodeNotFoundError

[[autodoc]] db.DuplicateLoginNodeError

[[autodoc]] db.EnvironmentManagedError

[[autodoc]] db.NoLoginNodeError

[[autodoc]] db.EndpointNotApprovedError

## Users and identities

See [Users, Projects and memberships](../database#users-projects-and-memberships)
for the data model.

```python
from datetime import UTC, datetime, timedelta
from nest.db import IdentityStore, hash_token

identities = IdentityStore(database)
# Atomic: concurrent first sign-ins with the same subject create one User.
user, identity, created = identities.sign_in(
    issuer="https://accounts.google.com",
    subject=claims["sub"],
    email=claims["email"],
    email_verified=claims["email_verified"],
    display_name=claims["name"],
)
# Refused with UserNotActiveError if the User is disabled.
identities.create_session(
    user.id,
    kind="browser",
    token_hash=hash_token(secret),
    expires_at=datetime.now(UTC) + timedelta(hours=8),
)
```

[[autodoc]] db.IdentityStore

[[autodoc]] db.User

[[autodoc]] db.Identity

[[autodoc]] db.Session

[[autodoc]] db.UserNotActiveError

[[autodoc]] db.hash_token

[[autodoc]] db.IdentityConflictError

## Platform administrator bootstrap

See [Bootstrap and recover platform administration](../platform-admin).

[[autodoc]] db.PlatformAdminBootstrap

[[autodoc]] db.BootstrapOutcome

[[autodoc]] db.BootstrapState

[[autodoc]] db.bootstrap_state

[[autodoc]] db.AuditEvent

[[autodoc]] db.audit_events

[[autodoc]] db.normalize_email

## Projects and memberships

```python
from nest.db import ProjectStore

projects = ProjectStore(database)
project = projects.create_project("llm-training", created_by_user_id=alice)  # Alice is Owner
projects.add_member(project.id, bob, "admin", added_by_user_id=alice)
projects.transfer_ownership(project.id, actor_user_id=alice, new_owner_user_id=bob)
projects.role_of(project.id, alice)  # "admin"
projects.audit_history(project.id)  # newest first
```

`ProjectStore` applies the one-Owner rules for every caller but not who may call
it; interfaces go through [`authz.ProjectService`] instead (see
[Authorization](authz)).

[[autodoc]] db.ProjectStore

[[autodoc]] db.Project

[[autodoc]] db.Membership

[[autodoc]] db.ProjectAuditEvent

[[autodoc]] db.ProjectError

[[autodoc]] db.ProjectNotFoundError

[[autodoc]] db.UserNotFoundError

[[autodoc]] db.MemberNotFoundError

[[autodoc]] db.DuplicateMemberError

[[autodoc]] db.OwnerRuleError

## Invitations and Invite Links

`InvitationStore` keeps invitation records, memberships and delivery state apart
and makes every redemption transactional; interfaces go through
[`authz.InvitationService`] instead (see [Authorization](authz)).

[[autodoc]] db.InvitationStore

[[autodoc]] db.EmailInvitation

[[autodoc]] db.InviteLink

[[autodoc]] db.PendingInvitation

[[autodoc]] db.InvitationPreview

[[autodoc]] db.Acceptance

[[autodoc]] db.normalize_invited_email

[[autodoc]] db.InvitationError

[[autodoc]] db.InvitationNotFoundError

[[autodoc]] db.InvitationUnavailableError

[[autodoc]] db.EmailMismatchError

[[autodoc]] db.AlreadyInvitedError
