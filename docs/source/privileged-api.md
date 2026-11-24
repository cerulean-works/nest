# Use the privileged management API

**This page is for platform administrators and Console developers.** The
privileged management API lets an active `platform_admin` run the routine
onboarding steps through Nest instead of with database credentials: find Users,
create Projects with an explicit Owner, grant Platforms, link Slurm accounts,
approve Projects for a Provider, and read the audit log. It is the backend for the
G02a Console; Nest itself serves no page for it yet.

It is served by the Controller's auth listener under `/api/admin`, next to the
Projects API (see [Share Projects and manage roles](projects)).

## Who may call it

Every route is deny by default. A request is served only when all of these hold:

- it carries a **browser** session cookie. CLI sessions are refused with
  `403 {"error": "forbidden", "reason": "browser_only"}`, whatever their scopes;
- the User is an **active `platform_admin`** now. The role is read from the
  database on every request, in the same transaction as the change and
  share-locked, so a revoked or disabled administrator is refused from the next
  request on, and a revocation racing a change either waits for it or refuses it
  (`403 ... "reason": "platform_admin_required"`);
- a `POST` passes the same-origin (CSRF) check of the Projects API: an `Origin`
  equal to the Nest origin, `Sec-Fetch-Site: same-origin` when sent, and
  `Content-Type: application/json` (`403 {"error": "cross_origin"}` and `415`
  otherwise);
- a change names a `reason` of 1 to 1000 characters (`400` otherwise).

Without a session, every route answers `401`. Refusals are recorded in the
[security audit log](audit-log) with the actor, the action (such as
`admin.provider.approve`) and the reason.

`platform_admin` still grants no Project role, Platform, Slurm account, Provider
approval or HPC identity. Creating a Project for someone else does not make the
administrator a member, and nothing here runs a job.

## Routes

Listings take `limit` (1 to 200, default 50) and return `next_after`, the cursor
to pass as `after` for the next page, or `null` on the last one. No response
contains a token, session, identity subject, login-node host, host key or HPC
credential.

| Method | Route | Body or query | Answer |
| --- | --- | --- | --- |
| `GET` | `/api/admin/users` | `limit`, `after`, `email` (exact, any case) | `{"users": [...], "next_after"}` |
| `GET` | `/api/admin/users/<user-id>` | | One User |
| `GET` | `/api/admin/projects` | `limit`, `after`, `include_deleted=true` | `{"projects": [...], "next_after"}` |
| `POST` | `/api/admin/projects` | `name`, `owner_user_id` (default: you), `organization_id`, `reason` | `201` the Project |
| `GET` | `/api/admin/projects/<id>/access` | | `{"project", "platforms", "accounts", "provider_approvals"}` |
| `POST` | `/api/admin/projects/<id>/platforms/grant`, `/revoke` | `platform` (slug or ID), `reason` | `{"changed": bool}` |
| `POST` | `/api/admin/projects/<id>/accounts/link` | `platform`, `account`, `modes` (`["A"]`), `default`, `reason` | The link |
| `POST` | `/api/admin/projects/<id>/accounts/unlink` | `platform`, `account`, `reason` | `{"changed": true}` |
| `GET` | `/api/admin/platforms` | | Every Platform, including disabled ones |
| `GET` | `/api/admin/platforms/<platform>/accounts` | | Its registered Slurm accounts |
| `GET` | `/api/admin/provider-approvals` | `provider`, `project_id`, `status`, `limit`, `after` | `{"provider_approvals": [...], "next_after"}` |
| `POST` | `/api/admin/provider-approvals/approve`, `/revoke` | `provider`, `project_id`, `reason` | `{"changed": bool}` |
| `GET` | `/api/admin/audit` | `limit`, `before`, `event`, `project_id`, `actor_user_id` | `{"events": [...], "next_after"}` (newest first; pass `next_after` as `before`) |

A User in the directory has only `id`, `display_name`, `email` (the last verified
address, informational: an address never identifies a User by itself), `status`,
`created_at`, `disabled_at` and `platform_roles`.

Errors use the Projects API's shapes: `404 not_found`, `404 user_not_found` (the
Owner is missing or disabled), `404 account_not_found`, `409 platform_not_granted`
(link an account only after granting its Platform), `409 account_rule`, and `400
invalid_request`.

`provider` is the Provider key a Controller records with each decision: its
default Platform Profile ID (`default` for the `NEST_SLURM_*` form, otherwise the
`NEST_PLATFORMS` ID), or `local`. An approval applies only to Controllers with
`NEST_PROVIDER_APPROVALS=database` (see
[Onboard a Project](project-onboarding#1-approve-the-project-for-the-provider)).

## Create a Project

Only an active `platform_admin` may create Projects, through either
`POST /api/admin/projects` or the existing `POST /api/projects`, which now takes
the same optional `owner_user_id` and `reason`. The Project and its first Owner
membership are written in one transaction; if the Owner is not an active User,
nothing is created. The Project's audit history records `project_created` and the
Owner's `member_added` with the administrator as actor and your reason. An
ordinary signed-in User is refused with `platform_admin_required`.

The Owner then manages the Project exactly as before: invitations, roles,
transfer and deletion are unchanged and need no administrator.

## Audit

Every grant, revocation, account link and approval made here is recorded by the
database's triggers in `security_audit_events`, with `actor_user_id` (the
administrator), `note` (your reason) and `database_role`. Read them with
`GET /api/admin/audit`, `nest-db audit list`, or
[`audit.AuditLog.events`].

## What stays in the operator CLI

Registering, changing or disabling Platforms and login nodes, registering or
disabling Slurm accounts, the Mode A operator default, native-default policy,
granting or revoking `platform_admin`, session revocation and Execution Registry
reconciliation stay with `nest-db` and database credentials (see
[Register HPC Platforms and login nodes](platforms),
[Map Slurm accounts to Projects](slurm-accounts) and
[Bootstrap and recover platform administration](platform-admin)). The database
enforces this too: an administrator acting through Nest may change only Platform
grants, Project account links and Provider approvals; every other operator-managed
table still needs an operator name (migration `0017_provider_project_approvals`).

## Compatibility

- `POST /api/projects` keeps its request and answer. `role` is present in the
  answer only when you are the Owner.
- Existing Projects, memberships, invitations and Invite Links are untouched.
- The routes are additive: clients of the Projects, Platforms, Organizations and
  invitations APIs need no change.
