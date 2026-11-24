"""Creating Projects in tests the way Nest now requires: as a `platform_admin` (G03).

Only an active `platform_admin` may create a Project, with an explicit Owner. Tests
that only need "a Project owned by this User" call `owned_project`, which creates
one administrator per test database and has it create the Project for the Owner.
The administrator becomes no member of the Project.
"""

import uuid

from nest.authz import Principal, ProjectService
from nest.db import Database, IdentityStore, Project

_ADMINS: dict[int, tuple[Database, uuid.UUID]] = {}


def platform_admin(database: Database) -> Principal:
    """A browser session of this test database's `platform_admin`, created once."""
    found = _ADMINS.get(id(database))
    if found is None or found[0] is not database:
        identities = IdentityStore(database)
        admin = identities.create_user(display_name="test platform admin").id
        identities.grant_platform_role(admin, operator="tests", reason="create test Projects")
        found = (database, admin)
        _ADMINS[id(database)] = found
    return Principal(found[1], "browser")


def owned_project(service: ProjectService, owner: uuid.UUID, name: str, **kwargs) -> Project:
    """A Project with `owner` as its Owner, created by the test administrator."""
    return service.create_project(
        platform_admin(service.database), name, owner_user_id=owner, **kwargs
    )
