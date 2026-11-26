# Authorization

`nest.authz` is the Controller's single, deny-by-default authorization policy for
Projects. See [Share Projects and manage roles](../projects) for the role matrix,
revocation, the audit history and the Projects API.

Check one action, for example before streaming a Run's logs:

```python
from nest.authz import Action, Authorizer, Principal, Target

authorizer = Authorizer(database, providers=provider_policy)
principal = Principal.from_session(grant.session)  # grant from SessionStore.authenticate
authorizer.require(principal, Action.RUN_CANCEL, project_id, Target(run_submitted_by=submitter))
```

Change a Project as its caller; the check and the change run in one transaction:

```python
from nest.authz import AccessDeniedError, ProjectService

projects = ProjectService(database)
try:
    projects.change_role(principal, project_id, member_id, "viewer")
except AccessDeniedError as exc:
    respond(403 if exc.visible else 404, exc.reason)
```

## Policy

[[autodoc]] authz.decide

[[autodoc]] authz.may_create_project

[[autodoc]] authz.session_allows

[[autodoc]] authz.Action

[[autodoc]] authz.Principal

[[autodoc]] authz.Target

[[autodoc]] authz.Decision

[[autodoc]] authz.decide_organization

[[autodoc]] authz.OrganizationAction

## Enforcement

[[autodoc]] authz.Authorizer

[[autodoc]] authz.ProjectService

[[autodoc]] authz.AccessDeniedError

## Organizations

See [Share Projects and manage roles](../projects#organizations).

[[autodoc]] authz.OrganizationService

[[autodoc]] authz.Organization

[[autodoc]] authz.OrganizationMembership

[[autodoc]] authz.OrganizationRuleError

## Invitations

See [Invite people to a Project](../invitations).

```python
from nest.authz import InvitationService

invitations = InvitationService(database, projects, email_service)
issued = invitations.invite(principal, project_id, "ada@example.org", "member")
share(issued.link)  # returned once; only its hash is stored
invitations.pending(invitee)  # the invitee's own pending invitations
invitations.accept(invitee, issued.invitation.id)
```

[[autodoc]] authz.InvitationService

[[autodoc]] authz.IssuedInvitation

[[autodoc]] authz.IssuedInviteLink

## HPC Platforms

See [Register HPC Platforms and login nodes](../platforms).

```python
from nest.authz import PlatformCatalog

catalog = PlatformCatalog(database)
catalog.list_platforms(principal)  # only Platforms granted to the caller's Projects
endpoint = catalog.select_login_node(principal, mode="B")  # the server's default Platform
try:
    connect(endpoint.host, endpoint.port, endpoint.host_key_pin_ref)
except OSError:
    endpoint = catalog.failover(principal, endpoint)  # same Platform ID, next node

chosen = catalog.select_platform(principal, project_id, requested)  # eligible only
chosen.platform.slug, chosen.origin  # "nano4", "project_default"
catalog.set_project_default(owner, project_id, "nano4")  # Owners and Admins only
```

[[autodoc]] authz.PlatformCatalog

[[autodoc]] authz.PlatformSelection

[[autodoc]] authz.ProjectPlatforms

[[autodoc]] authz.PlatformSummary

[[autodoc]] authz.PlatformResolutionError

[[autodoc]] authz.select_endpoint

## Slurm account selection

See [Map Slurm accounts to Projects](../slurm-accounts).

```python
from nest.authz import AccountResolver, HPCIdentity

resolver = AccountResolver(database)
chosen = resolver.resolve(
    principal,
    project_id,
    platform="nano4",
    mode="B",
    identity=HPCIdentity.user(nano4_id, principal.user_id, verified_default_account="chem"),
)
chosen.origin  # "explicit", "project_default", "mode_b_user_default", ...
```

[[autodoc]] authz.AccountResolver

[[autodoc]] authz.resolve_account

[[autodoc]] authz.HPCIdentity

[[autodoc]] authz.AccountResolution

[[autodoc]] authz.AccountResolutionError

## Execution authority (D01, D02)

See [Authorize execution authority and HPC account bindings](../hpc-access).

```python
from nest.authz import AuthorityRefusedError, AuthorityResolver

resolver = AuthorityResolver(database, deployment=config.execution_authorities)
try:
    decision = resolver.resolve(principal, project_id, platform_id, requested="user_owned")
except AuthorityRefusedError as exc:
    exc.reason  # "authority_not_permitted", "hpc_binding_unverified", ...
decision.pin()  # what the execution attempt records
```

[[autodoc]] authz.AuthorityResolver

[[autodoc]] authz.AuthorityDecision

[[autodoc]] authz.AuthorityRefusedError

## Platform administration

See [Use the privileged management API](../privileged-api).

```python
from nest.authz import AdminService

admin = AdminService(database)
project = admin.create_project(
    principal, "Chemistry", owner_user_id=owner_id, reason="onboard chemistry; ticket OPS-12"
)
admin.grant_platform(principal, project.id, "nano4", reason="OPS-12")
admin.approve(principal, "nano4", project.id, reason="OPS-12")
admin.project_access(principal, project.id)  # grants, account links, approvals
```

[[autodoc]] authz.AdminService

[[autodoc]] authz.DirectoryUser

[[autodoc]] authz.ProjectAccess

[[autodoc]] authz.Page

## Provider authority

[[autodoc]] authz.ProviderAuthority

[[autodoc]] authz.DenyAllProviders

[[autodoc]] authz.DatabaseProviderApprovals

[[autodoc]] authz.AllowedProjects

## Execution service

[[autodoc]] authz.ExecutionAccess

[[autodoc]] authz.EligibleProject

[[autodoc]] authz.UnauthenticatedError

[[autodoc]] authz.ProjectRequiredError
