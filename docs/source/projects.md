# Share Projects and manage roles

A Nest Project is the unit of collaboration and authorization. Whoever creates a
Project is its Owner; the Owner and Admins add other Users with a role, and each
User can belong to any number of Projects. A Project's ID is a generated UUID that
never changes, whoever owns it and whatever it is called, and it is independent of
any Slurm or Provider account.

Every permission is decided on the Controller by one deny-by-default policy,
[`authz.decide`], enforced by [`authz.ProjectService`] and [`authz.Authorizer`]
([reference](api/authz)). Clients, including the
[Management Console](console), never decide permissions: hiding a button changes
nothing on the server.

## Roles

Each member has exactly one of four fixed roles, and every Project has exactly one
Owner.

| Action | Owner | Admin | Member | Viewer |
| --- | --- | --- | --- | --- |
| See the Project, its members, Runs and ordinary artifacts | yes | yes | yes | yes |
| Change settings (the name), read the audit history | yes | yes | no | no |
| Invite or add an `admin`, `member` or `viewer` | yes | yes | no | no |
| List, resend and revoke invitations and Invite Links | yes | yes | no | no |
| Change a non-Owner member's role among those three | yes | yes | no | no |
| Remove a non-Owner member | yes | yes | no | no |
| Leave the Project | no, transfer first | yes | yes | yes |
| Transfer ownership, delete the Project | yes | no | no | no |
| Submit a Run, if the Provider permits it | yes | yes | yes | no |
| Cancel one's own Run, if the Provider permits it | yes | yes | yes | no |
| Read artifacts marked sensitive | yes | yes | of own Runs | no |
| See the eligible HPC Platforms and the default Platform | yes | yes | yes | yes |
| Choose the default among eligible Platforms | yes | yes | no | no |

- `owner` is never granted by an invitation, by adding a member or by a role change.
  It moves only when the current Owner transfers ownership, which demotes them
  (to `admin` by default) in the same transaction. An Admin therefore cannot make
  themselves Owner, and nobody can remove or demote the Owner, so a Project always
  has its one Owner.
- Deleting a Project keeps its rows, Runs and history, but nobody has a role in it
  any more and it cannot be restored. The database refuses a hard `DELETE` or
  `TRUNCATE` of `projects` from any writer, so deletion is always this audited
  soft delete.
- Everything not allowed above is refused, including any action by a User who is
  not a member, by a disabled User, or on a deleted Project.

**Platform and Organization roles grant nothing here.** A `platform_admin` manages
the Nest service and has no role in any Project they are not a member of. Being in
an Organization lets a User file new Projects under it, and grants no role in its
Projects. Which HPC Platforms a Project may use is the operator's grant; see
[Project eligibility and default Platform](platforms#project-eligibility-and-default-platform).

Project and Organization IDs are tenant keys: the database refuses to change them
(`projects_id_immutable`, `organizations_id_immutable`). Two Projects that share a
Slurm account, or an account string, stay two Projects with their own members,
Owner, Runs and permissions; see [Map Slurm accounts to Projects](slurm-accounts).

## Organizations

An Organization is an optional grouping of Projects with a generated, immutable ID.
[`authz.OrganizationService`] authorizes every Organization change on the server
with [`authz.decide_organization`]:

| Action | `org_admin` | `member` |
| --- | --- | --- |
| See the Organization and its members | yes | yes |
| File a new Project under it | yes | yes |
| Rename it, or delete it once no Project is filed under it | yes | no |
| Add an existing User, change a member's role | yes | no |
| Remove a member | yes | only themselves |

Whoever creates an Organization is its first `org_admin`, and it always keeps at
least one: the last `org_admin` cannot be demoted, removed or leave. Reads need a
browser session or a CLI session with `projects:read`; changes need a browser
session. Organization roles grant no Project role, no Platform and no HPC
authority, and an Organization with Projects filed under it, live or deleted,
cannot be deleted. Changes are recorded in the security audit log
(`organization.created`, `organization.renamed`, `organization.deleted`,
`organization.member.added`, `organization.member.role_changed` and
`organization.member.removed`).

| Route | Body | Does |
| --- | --- | --- |
| `GET /api/organizations` | | Lists your Organizations with your role |
| `POST /api/organizations` | `name` | Creates one, with you as `org_admin` |
| `GET /api/organizations/<id>` | | Describes one |
| `POST /api/organizations/<id>/rename` | `name` | Renames it |
| `POST /api/organizations/<id>/delete` | | Deletes it |
| `GET /api/organizations/<id>/members` | | Lists members, `org_admin`s first |
| `POST /api/organizations/<id>/members` | `user_id`, `role` | Adds an existing User |
| `POST /api/organizations/<id>/members/<user_id>/role` | `role` | Changes a role |
| `POST /api/organizations/<id>/members/<user_id>/remove` | | Removes a member, or leaves |

They answer like the Projects API below, and a broken Organization rule is `409
{"error": "organization_rule", "reason": "last_org_admin" | "not_empty" |
"already_member"}`.

## Provider and HPC authority

A Project role never grants HPC authority. Submitting or cancelling a Run needs both
the role above and the permission of the Run's Provider, asked through an
[`authz.ProviderAuthority`] after the role check passes. The Provider decides from
its own policy, such as whether the User has a linked Provider Account, never from
Project membership, so Project Owners and Platform Admins gain no UNIX or Slurm
access by their role. Until Provider account policy is configured, the default
[`authz.DenyAllProviders`] refuses every Run action. The Controller's gRPC
execution service uses [`authz.AllowedProjects`] instead: its operator lists the
Projects whose Runs its Provider accepts in `NEST_PROVIDER_ALLOWED_PROJECTS` (see
[Authenticated execution](security#authenticated-execution)).

## Sessions and scopes

Changes to a Project or its members need a browser session. A CLI session (see
[Sign in from the command line](cli-login)) is limited to its scopes:
`projects:read` reads Projects and members, and `jobs:run` acts on Runs and reads
artifacts. A CLI session cannot create, change or delete Projects or memberships.

## Revocation and caching

Nest caches no permission. Every check reads the caller's membership, the User's
status and the Project's deletion from the database in one query, so removing a
member, lowering a role, disabling a User or deleting a Project is refused from
the very next API request, with the same access token. Long-lived responses, such
as an event stream, must check again before each event they send, and stop when
the check fails.

Each change locks the Project row, reads the caller's and the target member's
current roles, decides and changes in one transaction. A change that races with
the caller's own demotion or removal waits for it and is then refused.

## Audit history

Migration `0008_project_audit` records every Project creation, rename and deletion
and every membership addition, role change and removal in
`project_audit_events`, from database triggers, so changes made through
`nest-db` or plain SQL are recorded too. Each entry names the member, the old and
new roles, and the acting User (or operator and reason, set as for
[platform administration](platform-admin)). Entries cannot be updated, deleted or
truncated, and outlive the Users and Projects they name. Owners and Admins read
them with `GET /api/projects/<id>/audit`; operators read them with
[`db.ProjectStore.audit_history`].

## Projects API

The auth listener (see [Sign in with Google](authentication)) serves a JSON API
under `/api/projects`. Browsers authenticate with the session cookie, and their
`POST`s must be same-origin, carrying `Origin`. The CLI authenticates with
`Authorization: Bearer <access token>`. Every `POST` body is JSON
(`Content-Type: application/json`).

| Route | Body | Does |
| --- | --- | --- |
| `GET /api/projects` | | Lists your Projects with your role |
| `POST /api/projects` | `name`, optional `organization_id` | Creates a Project you own (browser session only; see below) |
| `GET /api/projects/<id>` | | Describes a Project |
| `POST /api/projects/<id>/rename` | `name` | Renames it |
| `POST /api/projects/<id>/delete` | | Deletes it |
| `POST /api/projects/<id>/transfer` | `user_id`, optional `previous_owner_role` | Transfers ownership |
| `GET /api/projects/<id>/members` | | Lists members, Owner first |
| `POST /api/projects/<id>/members` | `user_id`, `role` | Adds an existing User |
| `POST /api/projects/<id>/members/<user_id>/role` | `role` | Changes a role |
| `POST /api/projects/<id>/members/<user_id>/remove` | | Removes a member, or leaves |
| `GET /api/projects/<id>/audit?limit=&before=` | | Audit history, newest first |
| `GET /api/projects/<id>/platforms` | | Eligible Platforms and the default |
| `POST /api/projects/<id>/platforms/default` | `platform` (slug, ID or `null`) | Chooses or clears the default |

```bash
curl -s -H "Authorization: Bearer $NEST_ACCESS_TOKEN" \
    https://nest.example.com/api/projects
```

Nest serves no page that calls `POST /api/projects` yet, and every page and JSON
response on the Nest origin carries `Content-Security-Policy: default-src 'none'`,
which also blocks `fetch()` typed into the browser's developer console there. A
CLI session is refused (`browser_only`). Until a creation page or command exists,
operators create Projects for Users as described in
[Create a Project for a User](project-onboarding#create-a-project-for-a-user).
Restricting creation to platform administrators is planned, not enforced.

Errors are JSON `{"error": ...}`:

| Status | `error` | Meaning |
| --- | --- | --- |
| `401` | `unauthenticated` | No valid session |
| `403` | `forbidden`, with a `reason` | You are a member but your role or session does not allow it |
| `403` | `cross_origin` | A browser `POST` from another site, or without `Origin` |
| `404` | `not_found` | No such Project, or you are not a member: the two look the same |
| `404` | `member_not_found`, `user_not_found` | The named User is not a member, or does not exist |
| `409` | `already_member`, `owner_rule` | The User is already a member, or the change would break the one-Owner rule |
| `400` | `invalid_request` | A malformed body or value |

Email invitations and Invite Links have their own routes under
`/api/projects/<id>/invitations` and `/api/projects/<id>/invite-links`; see
[Invite people to a Project](invitations).

The gRPC execution service authorizes Project access with
[`authz.ExecutionAccess`] (see [Security](security)). When it chooses a Platform per
request (C01 Part 2), it must call [`authz.PlatformCatalog.select_platform`] like
every other interface.
