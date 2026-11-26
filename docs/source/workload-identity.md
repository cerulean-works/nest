# Authenticate CI workloads with GitHub Actions OIDC

A CI pipeline can call protected Nest APIs without a browser. It doesn't need a
Nest User, a Google account or a long-lived secret. A **service principal** is a
non-human Nest caller, such as one pipeline. A GitHub Actions job proves which
workflow run it is with a short-lived GitHub OIDC token, and the Controller's auth
listener exchanges that token for a **service session**. A service session is an
opaque bearer token that lasts minutes and can't be refreshed.

This is B09. It sets up machine identity only:

- **Service principals get no HPC authority.** Every gRPC execution RPC (`Execute`,
  `SubmitJob`, `GetJobStatus`, `CancelJob`, `ListEligibleProjects`) refuses a
  service session with `PERMISSION_DENIED` and `nest-error-kind:
  noninteractive_auth_unavailable`. No grant, Project, Platform, mode or other
  metadata changes that, and nothing reaches the HPC Platform. Running CI jobs
  needs a platform-authorized noninteractive HPC credential and per-execution
  routing. Those are later work (D09/D10) and are never replaced by the
  Controller's credentials or a person's.
- **Human sign-in is unchanged.** Google, browser and CLI sessions, Project roles
  and Mode A work exactly as before. A service token is never accepted as a
  browser or CLI session, and a User's token is never accepted as a service
  session.
- **Don't store long-lived credentials in CI variables.** That covers Nest
  sessions, Google sessions, HPC passwords, SSH private keys and MFA secrets. The
  job's only credential is the GitHub OIDC token, which GitHub issues for that run.

## How a token is accepted

The exchange (`POST /auth/workload/token`) accepts a GitHub Actions OIDC token only
when every check below passes. Any failure is refused and nothing is issued.

| Check | Requirement |
| --- | --- |
| Signature | RS256, with a key from GitHub's published JWKS (`https://token.actions.githubusercontent.com/.well-known/jwks`). `alg: none`, HMAC and unknown keys are refused. |
| Issuer | Exactly `https://token.actions.githubusercontent.com`. |
| Audience | Exactly `NEST_WORKLOAD_OIDC_AUDIENCE`, a single audience. The trust policy must name the same value. |
| Time | `exp`, `iat` and `nbf` are present and hold, with 60 seconds of clock skew. The token lives at most an hour. |
| Replay | The token's `jti` has never been exchanged before, for any principal. Concurrent exchanges of one token issue at most one session. A used `jti` is kept at least until the token's `exp` plus the 60 seconds of skew, and 60 seconds more, so pruning can never make it acceptable again. |
| Trust policy | One active policy of the named principal matches exactly: `repository`, `repository_id`, `repository_owner_id`, `ref`, the workflow file in `workflow_ref` (which must name the same repository and ref), `environment` and `event_name`. |
| Principal | Active, and within its quotas: unexpired sessions at once (`max_active_sessions`, default 4) and exchanges in any rolling hour (`max_exchanges_per_hour`, default 60). |

Trust policies have no wildcards, prefixes or patterns. The numeric repository and
owner IDs are pinned, so a repository that is deleted and recreated, renamed, or
moved to another owner doesn't match. With no environment listed, only jobs
without a deployment environment match. With some listed, the job's environment
must be one of them.

A ref and workflow file alone don't keep fork pull requests out. Pull request refs
(`refs/pull/...`) can't be trusted at all, but GitHub runs `pull_request_target`,
`workflow_run` and `issue_comment` workflows in the base repository's context, on
its default branch, so a run that a fork's pull request triggers carries exactly
the `ref` and `workflow_ref` of a trusted push. Each trust policy therefore also
lists the triggering events it accepts, `push` by default, and only events a fork
cannot cause can be listed: `push`, `workflow_dispatch`, `schedule` and `release`.
A token whose `event_name` is anything else, or missing, never matches.

The answer is a `nest_svc_...` token valid for `NEST_WORKLOAD_TOKEN_TTL` seconds
(default 600, at most 3600). Only its SHA-256 digest is stored. Revoking the
session, its trust policy or its principal takes effect on the next request.

## Set up a service principal

The steps below create the principal, trust a workflow and grant capabilities,
then enable the exchange and use it from GitHub Actions.

1. As an operator, create the principal, trust one workflow, and grant it
   capabilities. Every change needs `--operator` and `--reason` and is recorded in
   the [security audit log](audit-log). Get the numeric IDs from
   `gh api repos/OWNER/NAME --jq '.id, .owner.id'`.

   ```bash
   nest-db service-principal create nest-ci --operator ops --reason "CI for pipeline"
   nest-db service-principal trust nest-ci \
       --audience https://auth.nest.example.org/workload \
       --repository example-org/pipeline --repository-id 100000001 \
       --repository-owner-id 200000001 \
       --ref refs/heads/main --workflow .github/workflows/nest.yml --event push \
       --operator ops --reason "pushes to main only"
   nest-db service-principal grant nest-ci --project-id "$PROJECT_ID" \
       --capabilities project.read --operator ops --reason "read Project metadata"
   ```

   Trust only a workflow file that untrusted code can't run in. Nest refuses
   tokens from fork-triggerable events, but a trusted `push` or
   `workflow_dispatch` run that checks out and runs code from a pull request, or
   that a pull request can change before it merges, hands that code the session.
   Protect the branch, and keep the token step in a workflow that runs only
   reviewed code.

2. Enable the exchange on the Controller. It needs Google sign-in (the auth
   listener serves the exchange), `NEST_DATABASE_URL` and
   `NEST_CONTROLLER_AUTH=session`:

   ```bash
   NEST_WORKLOAD_OIDC_AUDIENCE=https://auth.nest.example.org/workload
   ```

   Pick an audience unique to this deployment. GitHub lets any workflow request a
   token for any audience, so the audience together with the trust policy is what
   ties a token to this Nest.

3. Exchange the token in the workflow. The job needs `id-token: write` and nothing
   else:

   ```yaml
   permissions:
     contents: read
     id-token: write
   steps:
     - name: Get a Nest service session
       run: |
         oidc=$(curl -sSf -H "Authorization: Bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
           "$ACTIONS_ID_TOKEN_REQUEST_URL&audience=https://auth.nest.example.org/workload" \
           | jq -r .value)
         nest=$(jq -n --arg t "$oidc" --arg p "$NEST_SERVICE_PRINCIPAL_ID" \
             '{token: $t, service_principal_id: $p}' \
           | curl -sSf -H 'Content-Type: application/json' --data @- \
             https://auth.nest.example.org/auth/workload/token | jq -r .access_token)
         echo "::add-mask::$nest"
         curl -sSf -H "Authorization: Bearer $nest" \
           https://auth.nest.example.org/api/workload/projects
   ```

   `NEST_SERVICE_PRINCIPAL_ID` is the principal's ID from `nest-db service-principal
   show nest-ci`. It's an identifier, not a secret.

## What a service session may call

| Route | Answer |
| --- | --- |
| `GET /auth/workload/session` | The session: principal ID, session ID, expiry. |
| `POST /auth/workload/logout` | Revokes the session. |
| `GET /api/workload/projects` | Projects where the principal holds `project.read`, with its grants. |
| `GET /api/workload/projects/<id>` | One of them. Any other Project is `404`, so a principal can't probe for Projects. |
| Any other `/api/...` route | `403` with `reason: service_principal`. No User API (Projects, invitations, Console, administration) accepts a service session. |
| Any gRPC execution RPC | `PERMISSION_DENIED`, `noninteractive_auth_unavailable`. |

A service principal is never a Project member and has no Project, Organization or
platform role. It can do only what a grant in force allows. A grant is in force
when it is active, on a live Project, and for a Platform capability, on an active
Platform still granted to that Project. Grants are read on every request, never
cached.

| Capability | Scope | Effect in B09 |
| --- | --- | --- |
| `project.read` | Project | Read the Project's name and the principal's grants. |
| `platform.read` | Project and Platform | Recorded; shown with the grant. |
| `run.read`, `run.submit`, `run.cancel` | Project and Platform | Recorded only, for D09/D10. Every HPC-bound call is refused whatever is granted. |

## Revoke and audit

```bash
nest-db service-principal show nest-ci                  # policies, grants, live sessions
nest-db service-principal revoke-session SESSION_ID --operator ops --reason "leaked log"
nest-db service-principal revoke-grant GRANT_ID --operator ops --reason "project closed"
nest-db service-principal revoke-policy POLICY_ID --operator ops --reason "branch retired"
nest-db service-principal revoke nest-ci --operator ops --reason "pipeline removed"
nest-db service-principal prune                         # forget expired token uses
```

Revocation is final and nothing is deleted. Revoking a trust policy revokes the
sessions it issued. Revoking a principal revokes its policies, grants and sessions
in the same transaction. Principals, policies and grants are immutable apart from
revocation, so to change one, revoke it and create another. The database itself
refuses changes that don't name an operator.

The security audit log records each principal, policy and grant change
(`service_principal.*`, `workload_policy.*`, `service_grant.*`), each session
issued and revoked (`service_session.*`), each refused exchange of a token from a
trusted repository (`workload.exchange`, with the failed checks by name), and each
decision on a service session's request. These records leave `actor_user_id`
empty, because no User acted. They name the service session in `session_id` and
the principal in `details`. Tokens and claim values are never recorded. A
token that fails verification, or whose repository no policy of the principal
trusts, is only logged. Anyone can mint a validly signed GitHub token for a
repository of their own, so such tokens must not be able to fill the database.

## Upgrade and rollback

`nest-db upgrade` applies `0019_service_principals`. It only adds tables, changes
no existing row, and creates no principal, so nothing changes until an operator
creates one and sets `NEST_WORKLOAD_OIDC_AUDIENCE`.

- **Turn the exchange off** by unsetting `NEST_WORKLOAD_OIDC_AUDIENCE` and
  redeploying. `/auth/workload/token` then answers `404` and every service token
  is refused as unknown, sessions that haven't expired yet included: `401` over
  HTTP and `UNAUTHENTICATED` over gRPC, with nothing written to the database.
  Revoke the principals
  as well if they shouldn't come back when the exchange is enabled again.
- **Roll the Controller back** as for any migration: restore the backup taken
  before `nest-db upgrade` and deploy the previous image (see
  [Store Controller records in PostgreSQL](database)). Nest has no down-migrations,
  and an older Controller refuses to start on a schema with a migration it lacks.
