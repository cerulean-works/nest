# Database and migrations

`nest.db` connects the Controller to its externally managed PostgreSQL database,
applies and verifies the schema, and records Provider Profiles and Slurm jobs. See
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
