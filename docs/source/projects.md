# Share Projects and manage roles

A Nest Project is the unit of collaboration and authorization. Whoever creates a
Project is its Owner; the Owner and Admins add other Users with a role, and each
User can belong to any number of Projects. A Project's ID is a generated UUID that
never changes, whoever owns it and whatever it is called, and it is independent of
any Slurm or Provider account.

Every permission is decided on the Controller by one deny-by-default policy,
[`authz.decide`], enforced by [`authz.ProjectService`] and [`authz.Authorizer`]
([reference](api/authz)). Clients, including the Web UI, never decide permissions:
hiding a button changes nothing on the server.

## Roles

Each member has exactly one of four fixed roles, and every Project has exactly one
Owner.

| Action | Owner | Admin | Member | Viewer |
| --- | --- | --- | --- | --- |
| See the Project, its members, Runs and ordinary artifacts | yes | yes | yes | yes |
| Change settings (the name), read the audit history | yes | yes | no | no |
| Invite or add an `admin`, `member` or `viewer` | yes | yes | no | no |
| Change a non-Owner member's role among those three | yes | yes | no | no |
| Remove a non-Owner member | yes | yes | no | no |
| Leave the Project | no, transfer first | yes | yes | yes |
| Transfer ownership, delete the Project | yes | no | no | no |
| Submit a Run, if the Provider permits it | yes | yes | yes | no |
| Cancel one's own Run, if the Provider permits it | yes | yes | yes | no |
| Read artifacts marked sensitive | yes | yes | of own Runs | no |

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
Projects.

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
| `POST /api/projects` | `name`, optional `organization_id` | Creates a Project you own |
| `GET /api/projects/<id>` | | Describes a Project |
| `POST /api/projects/<id>/rename` | `name` | Renames it |
| `POST /api/projects/<id>/delete` | | Deletes it |
| `POST /api/projects/<id>/transfer` | `user_id`, optional `previous_owner_role` | Transfers ownership |
| `GET /api/projects/<id>/members` | | Lists members, Owner first |
| `POST /api/projects/<id>/members` | `user_id`, `role` | Adds an existing User |
| `POST /api/projects/<id>/members/<user_id>/role` | `role` | Changes a role |
| `POST /api/projects/<id>/members/<user_id>/remove` | | Removes a member, or leaves |
| `GET /api/projects/<id>/audit?limit=&before=` | | Audit history, newest first |

```bash
curl -s -H "Authorization: Bearer $NEST_ACCESS_TOKEN" \
    https://nest.example.com/api/projects
```

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

Email invitations, Invite Links and onboarding are separate; they check
[`authz.ProjectService.authorize_invitation`] before creating an invitation.

The gRPC execution service does not yet authenticate callers (see
[Security](security)), so it does not apply Project permissions. When it does, it
must call [`authz.Authorizer`] like every other interface.
