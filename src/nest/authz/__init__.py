"""
Centralized, deny-by-default authorization for Nest Projects.

The fixed Owner/Admin/Member/Viewer matrix is [`authz.decide`]; [`authz.Authorizer`]
and [`authz.ProjectService`] enforce it on the server for every interface, and
[`authz.ExecutionAccess`] applies the same checks to the gRPC execution service.
See *Share Projects and manage roles* and *Security and limitations*.
"""

from .execution import (
    AllowedProjects,
    ExecutionAccess,
    ProjectRequiredError,
    UnauthenticatedError,
)
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
    "AllowedProjects",
    "Authorizer",
    "Decision",
    "DenyAllProviders",
    "ExecutionAccess",
    "Principal",
    "ProjectRequiredError",
    "ProjectService",
    "ProviderAuthority",
    "Target",
    "UnauthenticatedError",
    "decide",
    "may_create_project",
    "session_allows",
]
