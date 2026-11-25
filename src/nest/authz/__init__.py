"""
Centralized, deny-by-default authorization for Nest Projects.

The fixed Owner/Admin/Member/Viewer matrix is [`authz.decide`]; [`authz.Authorizer`]
and [`authz.ProjectService`] enforce it on the server for every interface, and
[`authz.ExecutionAccess`] applies the same checks to the gRPC execution service,
and [`authz.InvitationService`] to email invitations and Invite Links.
[`authz.PlatformCatalog`] authorizes HPC Platform discovery, Project-to-Platform
eligibility, default Platform selection, login-node selection and every
Platform-bound operation (`authorize_platform_action`),
[`authz.OrganizationService`] Organization changes, and [`authz.AccountResolver`]
chooses the Slurm account a job charges.
See *Share Projects and manage roles* and *Security and limitations*.
"""

from .accounts import (
    AccountResolution,
    AccountResolutionError,
    AccountResolver,
    HPCIdentity,
    resolve_account,
)
from .admin import AdminService, DirectoryUser, Page, ProjectAccess, ProviderPolicy
from .execution import (
    MAX_ELIGIBLE_PROJECTS,
    AllowedProjects,
    DatabaseProviderApprovals,
    EligibleProject,
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
from .organizations import (
    Organization,
    OrganizationMembership,
    OrganizationRuleError,
    OrganizationService,
)
from .platforms import (
    PLATFORM_SCOPES,
    SELECTION_ORIGINS,
    PlatformCatalog,
    PlatformResolutionError,
    PlatformSelection,
    PlatformSummary,
    ProjectPlatforms,
    select_endpoint,
)
from .policy import (
    CLI_ACTION_SCOPES,
    ORGANIZATION_READ_ACTIONS,
    PROVIDER_ACTIONS,
    READ_ACTIONS,
    Action,
    Decision,
    OrganizationAction,
    Principal,
    Target,
    decide,
    decide_organization,
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
    "MAX_ELIGIBLE_PROJECTS",
    "ORGANIZATION_READ_ACTIONS",
    "PLATFORM_SCOPES",
    "PROVIDER_ACTIONS",
    "READ_ACTIONS",
    "SELECTION_ORIGINS",
    "AccessDeniedError",
    "AccountResolution",
    "AccountResolutionError",
    "AccountResolver",
    "Action",
    "AdminService",
    "AllowedProjects",
    "Authorizer",
    "DatabaseProviderApprovals",
    "Decision",
    "DenyAllProviders",
    "DirectoryUser",
    "EligibleProject",
    "ExecutionAccess",
    "HPCIdentity",
    "InvitationService",
    "IssuedInvitation",
    "IssuedInviteLink",
    "Organization",
    "OrganizationAction",
    "OrganizationMembership",
    "OrganizationRuleError",
    "OrganizationService",
    "Page",
    "PlatformCatalog",
    "PlatformResolutionError",
    "PlatformSelection",
    "PlatformSummary",
    "Principal",
    "ProjectAccess",
    "ProjectPlatforms",
    "ProjectRequiredError",
    "ProjectService",
    "ProviderAuthority",
    "ProviderPolicy",
    "Target",
    "UnauthenticatedError",
    "decide",
    "decide_organization",
    "may_create_project",
    "resolve_account",
    "select_endpoint",
    "session_allows",
]
