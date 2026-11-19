"""
Centralized, deny-by-default authorization for Nest Projects.

The fixed Owner/Admin/Member/Viewer matrix is [`authz.decide`]; [`authz.Authorizer`]
and [`authz.ProjectService`] enforce it on the server for every interface. See
*Share Projects and manage roles*.
"""

from .policy import (
    CLI_ACTION_SCOPES,
    PROVIDER_ACTIONS,
    READ_ACTIONS,
    Action,
    Decision,
    Principal,
    Target,
    decide,
    may_create_project,
    session_allows,
)
from .service import (
    AccessDeniedError,
    Authorizer,
    DenyAllProviders,
    ProjectService,
    ProviderAuthority,
)

__all__ = [
    "CLI_ACTION_SCOPES",
    "PROVIDER_ACTIONS",
    "READ_ACTIONS",
    "AccessDeniedError",
    "Action",
    "Authorizer",
    "Decision",
    "DenyAllProviders",
    "Principal",
    "ProjectService",
    "ProviderAuthority",
    "Target",
    "decide",
    "may_create_project",
    "session_allows",
]
