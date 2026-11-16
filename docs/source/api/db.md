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

## Users and identities

See [Users, Projects and memberships](../database#users-projects-and-memberships)
for the data model.

```python
from datetime import UTC, datetime, timedelta
from nest.db import IdentityStore, hash_token

identities = IdentityStore(database)
user = identities.find_user(issuer="https://accounts.google.com", subject=claims["sub"])
if user is None:
    user = identities.create_user(display_name=claims["name"], email=claims["email"])
identities.link_identity(
    user.id,
    issuer="https://accounts.google.com",
    subject=claims["sub"],
    email=claims["email"],
    email_verified=claims["email_verified"],
)
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

[[autodoc]] db.hash_token

[[autodoc]] db.IdentityConflictError

## Projects and memberships

```python
from nest.db import ProjectStore

projects = ProjectStore(database)
project = projects.create_project("llm-training", created_by_user_id=alice)  # Alice is Owner
projects.add_member(project.id, bob, "admin", added_by_user_id=alice)
projects.transfer_ownership(project.id, actor_user_id=alice, new_owner_user_id=bob)
projects.role_of(project.id, alice)  # "admin"
```

[[autodoc]] db.ProjectStore

[[autodoc]] db.Project

[[autodoc]] db.Membership

[[autodoc]] db.ProjectError

[[autodoc]] db.ProjectNotFoundError

[[autodoc]] db.UserNotFoundError

[[autodoc]] db.MemberNotFoundError

[[autodoc]] db.DuplicateMemberError

[[autodoc]] db.OwnerRuleError
