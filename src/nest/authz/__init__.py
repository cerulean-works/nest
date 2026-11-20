"""
Centralized, deny-by-default authorization for Nest Projects.

The fixed Owner/Admin/Member/Viewer matrix is [`authz.decide`]; [`authz.Authorizer`]
and [`authz.ProjectService`] enforce it on the server for every interface, and
[`authz.ExecutionAccess`] applies the same checks to the gRPC execution service,
and [`authz.InvitationService`] to email invitations and Invite Links.
[`authz.PlatformCatalog`] authorizes HPC Platform discovery and login-node selection,
and [`authz.AccountResolver`] chooses the Slurm account a job charges.
See *Share Projects and manage roles* and *Security and limitations*.
"""

from .accounts import (
    AccountResolution,
    AccountResolutionError,
    AccountResolver,
    HPCIdentity,
    resolve_account,
)
from .execution import (
    AllowedProjects,
    ExecutionAccess,
    ProjectRequiredError,
    UnauthenticatedError,
)
from .invitations import (
    DEFAULT_INVITATION_TTL,
    DEFAULT_LINK_MAX_USES,
    InvitationService,
    IssuedInvitation,
    IssuedInviteLink,
)
from .platforms import (
    PLATFORM_SCOPES,
    PlatformCatalog,
    PlatformResolutionError,
    PlatformSummary,
    select_endpoint,
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
    "DEFAULT_INVITATION_TTL",
    "DEFAULT_LINK_MAX_USES",
    "PLATFORM_SCOPES",
    "PROVIDER_ACTIONS",
    "READ_ACTIONS",
    "AccessDeniedError",
    "AccountResolution",
    "AccountResolutionError",
    "AccountResolver",
    "Action",
    "AllowedProjects",
    "Authorizer",
    "Decision",
    "DenyAllProviders",
    "ExecutionAccess",
    "HPCIdentity",
    "InvitationService",
    "IssuedInvitation",
    "IssuedInviteLink",
    "PlatformCatalog",
    "PlatformResolutionError",
    "PlatformSummary",
    "Principal",
    "ProjectRequiredError",
    "ProjectService",
    "ProviderAuthority",
    "Target",
    "UnauthenticatedError",
    "decide",
    "may_create_project",
    "resolve_account",
    "select_endpoint",
    "session_allows",
]
