# Onboard a Project

**This page is for operators** of an authenticated (M1) Controller: the people
who hold its database credentials and deployment settings. Users never run these
commands; they follow [Run your first remote function](quickstart) and send you
their Project ID.

A Project role never grants HPC access. Before members of a Project can run
anything, you approve it in three independent places, each recorded in the
[security audit log](audit-log):

| Step | Grants | Interface | Refusal a user sees without it |
| --- | --- | --- | --- |
| 1. Provider approval | The Controller's Provider accepts the Project's Runs | `NEST_PROVIDER_ALLOWED_PROJECTS`, read at start | `PERMISSION_DENIED` `provider_denied` |
| 2. Platform grant | The Project may use an HPC Platform | `nest-db platform grant` | `NOT_FOUND` *Platform not found*, or `FAILED_PRECONDITION` `none_authorized` |
| 3. Slurm account link | Which Slurm account the Project's jobs charge | `nest-db account register` and `link` | `PERMISSION_DENIED` `no_account` |

## Before you start

The Controller must already run with session authentication: a migrated
PostgreSQL database (`NEST_DATABASE_URL`, `nest-db upgrade`), Google sign-in
(`NEST_GOOGLE_*`, served through HTTPS), and gRPC over TLS or an approved
encrypted network (`NEST_CONTROLLER_ALLOW_PLAINTEXT=true` inside a Tailnet). See
[Deploy on Dokploy from environment variables](dokploy), or
[Deploy with Docker Compose](docker-compose) with
[Store Controller records in PostgreSQL](database) and
[Sign in with Google](authentication).

Give users these values; none of them is a secret:

- `NEST_CONTROLLER_ADDRESS` and whether to set `NEST_CONTROLLER_TLS=true` or `false`;
- `NEST_AUTH_URL`: the origin of `NEST_GOOGLE_REDIRECT_URL`, such as
  `https://nest.example.com`;
- how to reach the Controller over Tailscale (Tailnet membership or a node share;
  see [Share a Controller over Tailscale](tailscale-shared)).

Users are created when they first sign in with Google; there is no per-user
provisioning. A Project is created by a signed-in User, who becomes its Owner,
and its Owner and Admins invite the other members (see
[Share Projects and manage roles](projects)).

Run `nest-db` where the Controller's `NEST_DATABASE_*` settings are available: a
Dokploy terminal of the `controller` service, `docker compose exec controller
nest-db ...`, or a host with the same environment. Every change takes
`--operator` and `--reason`, which the audit log keeps.

## 1. Approve the Project for the Provider

Every Run executes as the Controller's single HPC account, so accepting a Project
is the operator's decision. Add its ID to the comma-separated list and restart or
redeploy the Controller:

```ini
NEST_PROVIDER_ALLOWED_PROJECTS='<project-id-1>,<project-id-2>'
```

The Controller reads the list only at start. `*` accepts every Project; prefer an
explicit list. With the list empty, every submission is refused, and the
Controller logs a warning at start.

## 2. Grant an HPC Platform

List the Platforms and grant the one this Controller serves. A Platform from the
environment (`NEST_PLATFORMS`, or the `NEST_SLURM_*` form, whose Platform is
`default`) has the profile ID as its slug:

```bash
ops=(--operator ops-alice --reason "onboard the chemistry lab")
nest-db platform list
nest-db platform grant campus --project-id "$PROJECT" "${ops[@]}"
```

Each Controller submits to one Platform. A Project whose selected Platform is
another one is refused with `platform_not_served`, never run elsewhere. With
several eligible Platforms, set a server default (`nest-db platform set-default`)
or let a Project Owner or Admin choose the Project's default; see
[Register HPC Platforms and login nodes](platforms).

## 3. Register and link a Slurm account

Register each Slurm account once per Platform, then link it to the Project for the
Controller's SSH mode. With `--default`, jobs that name no account charge it:

```bash
nest-db account register campus chem "${ops[@]}"
nest-db account link campus chem --project-id "$PROJECT" --modes A --default "${ops[@]}"
nest-db account show campus --project-id "$PROJECT"
```

```text
project=... platform=campus granted=yes native_default=forbidden
  link chem id=1 modes=A default=yes status=active kind=standard
```

`NEST_SLURM_ACCOUNT` and a Platform Profile's `ACCOUNT` are not charged unless
registered and linked like this, or set as the Platform's Mode A operator default
(`nest-db account set-mode-a-default`). Mode A also needs the operator's UNIX user
(`NEST_PLATFORM_<ID>_SSH_USER` or `NEST_SLURM_SSH_USER`). See
[Map Slurm accounts to Projects](slurm-accounts) and
[Admit submissions through the Execution Registry](execution-registry).

## Verify

Ask a Project member to run:

```bash
nest auth status --check
nest platforms default --project-id "<project-id>"
```

and then the [quickstart](quickstart#6-run-a-function) example. A refusal names
its reason; see [Access and onboarding errors](remote-errors#access-and-onboarding-errors).
`nest-db audit list --project-id "$PROJECT"` shows every decision and refusal
for the Project.

## Offboard

Each approval is withdrawn on its own, from the next call:

```bash
nest-db account unlink campus chem --project-id "$PROJECT" "${ops[@]}"
nest-db platform revoke campus --project-id "$PROJECT" "${ops[@]}"
```

Remove the ID from `NEST_PROVIDER_ALLOWED_PROJECTS` and restart. To remove one
person, a Project Owner or Admin removes their membership; to stop a User
everywhere, revoke their sessions with `nest-db sessions revoke --user-id ...`
(see [Revoke sessions](authentication#revoke-sessions)).

## What not to do

- Do not set `NEST_CONTROLLER_AUTH=none` on a shared Controller to work around a
  refusal: it removes every Nest access check for everyone who can reach it.
- Do not ask users for their Nest session file or tokens, and never put a
  credential in an issue, chat or the repository.
- Approval in Nest does not isolate Projects on the cluster: all Runs share the
  Controller's HPC account and work directory, and run trusted, deserialized
  Python. See [Security and limitations](security).
