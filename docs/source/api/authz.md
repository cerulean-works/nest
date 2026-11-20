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

## Enforcement

[[autodoc]] authz.Authorizer

[[autodoc]] authz.ProjectService

[[autodoc]] authz.AccessDeniedError

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
```

[[autodoc]] authz.PlatformCatalog

[[autodoc]] authz.PlatformSummary

[[autodoc]] authz.PlatformResolutionError

[[autodoc]] authz.select_endpoint

## Slurm account selection

See [Map Slurm accounts to Projects](../slurm-accounts).

```python
from nest.authz import AccountResolver, HPCIdentity

resolver = AccountResolver(database)
chosen = resolver.resolve(
    principal, project_id, platform="nano4", mode="B",
    identity=HPCIdentity.user(nano4_id, principal.user_id, verified_default_account="chem"),
)
chosen.origin  # "explicit", "project_default", "mode_b_user_default", ...
```

[[autodoc]] authz.AccountResolver

[[autodoc]] authz.resolve_account

[[autodoc]] authz.HPCIdentity

[[autodoc]] authz.AccountResolution

[[autodoc]] authz.AccountResolutionError

## Provider authority

[[autodoc]] authz.ProviderAuthority

[[autodoc]] authz.DenyAllProviders

[[autodoc]] authz.AllowedProjects

## Execution service

[[autodoc]] authz.ExecutionAccess

[[autodoc]] authz.UnauthenticatedError

[[autodoc]] authz.ProjectRequiredError
