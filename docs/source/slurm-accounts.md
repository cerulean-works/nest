# Map Slurm accounts to Projects

Nest keeps a registry of the Slurm accounts on each HPC Platform and of which
Projects may charge them. The server, never the client, chooses the account a job
charges, and every job record keeps which account was chosen and why.

The registry needs the PostgreSQL database and the
[HPC Platform Catalog](platforms), and is added by migration
`0013_slurm_account_registry`. Run `nest-db upgrade` first.

## Accounts belong to one Platform

A Slurm account exists only inside one Platform's namespace. The account `chem` on
`nano4` and the account `chem` on `f1` are two records, with their own IDs, Project
links, permissions and job history:

- Accounts are keyed by `(platform_id, account_name)` (`UNIQUE`), and every
  reference to an account (a Project link, a job record) carries the Platform. The
  schema refuses a reference to an account of another Platform.
- A Nest Project is never identified, renamed or merged by an account name.
  Projects keep their immutable IDs; two Projects may share an account and stay two
  Projects.
- Names are stored in lowercase, as Slurm stores them.
- Accounts are disabled, never deleted, so job records keep them.

Registering an account does not create it on the cluster, and linking a Project
grants no Slurm or UNIX permission: the cluster still decides whether an HPC
identity may charge the account.

| Account field | Meaning |
| --- | --- |
| `kind` | `standard`, or `chargeback` for an operator account charged through the Controller's own HPC identity. A chargeback account can only be linked for Mode A. |
| `status` | `active`, or `disabled`: never chosen, links kept. |
| `is_mode_a_default` | The Platform's operator-controlled Mode A default. At most one per Platform. |

A **link** (`project_platform_accounts`) lets a Project charge an account on a
Platform it is granted. It names the **modes** the account may be charged under for
that Project, `A` (the Controller's operator identity) and/or `B` (the User's own
HPC identity), and may be the Project's **default** on that Platform (at most one).
Revoking the Project's Platform grant removes its links on that Platform.

## Manage accounts

Operators use `nest-db account`, authenticated by the database credentials in
`NEST_DATABASE_*`. Every change takes `--operator` and `--reason` and is recorded in
the [security audit log](audit-log).

```bash
ops=(--operator ops-alice --reason "onboard the chemistry lab")
nest-db account register nano4 chem "${ops[@]}"
nest-db account register f1 chem "${ops[@]}"          # a different account
nest-db account register nano4 nchc-ops --kind chargeback "${ops[@]}"
nest-db account set-mode-a-default nano4 nchc-ops "${ops[@]}"

nest-db platform grant nano4 --project-id "$PROJECT" "${ops[@]}"
nest-db account link nano4 chem --project-id "$PROJECT" --modes A,B --default "${ops[@]}"
nest-db account link nano4 nchc-ops --project-id "$PROJECT" --modes A "${ops[@]}"
nest-db account native-default nano4 --project-id "$PROJECT" --allow "${ops[@]}"

nest-db account show nano4 --project-id "$PROJECT"
# project=... platform=nano4 granted=yes native_default=allowed
#   link chem id=1 modes=A,B default=yes status=active kind=standard
#   link nchc-ops id=3 modes=A default=no status=active kind=chargeback
```

`nest-db account list PLATFORM` lists a Platform's accounts. `disable` and `enable`
change an account's status; `unlink` and `clear-default` undo a link or a Project
default; `native-default --forbid` withdraws the native-default permission.

## How the account is chosen

[`authz.AccountResolver`] first resolves the Platform with
[`authz.PlatformCatalog`] (so the caller must be an active member of the Project and
the Project granted the Platform). It then considers only accounts linked to the
Project on that Platform, active, permitted for the job's mode, and, when the HPC
identity's binding lists entitlements, among them. The first step that applies
wins:

| Step | Mode A (Controller identity) | Mode B (User's own identity) | Origin recorded |
| --- | --- | --- | --- |
| 1 | The account the caller asked for | The account the caller asked for | `explicit` |
| 2 | The Project x Platform default | The Project x Platform default | `project_default` |
| 3 | The Platform's operator default | The identity's verified default account | `mode_a_operator_default` / `mode_b_user_default` |
| 4 | Never | Omit `--account` and let Slurm charge the User's default, if the Project's policy allows it | `native_default` |

Anything else is refused with [`authz.AccountResolutionError`], whose `reason` is
machine-readable:

- A requested account that is not linked, is linked for the other mode, is
  disabled, or is not among the identity's entitlements is refused. It never falls
  through to a default.
- The Controller's HPC identity is refused in Mode B, and a User identity in Mode
  A. Mode B never uses the Mode A operator default, and a chargeback account cannot
  be linked for Mode B, so neither is ever a Mode B fallback.
- An identity of another Platform is refused (`wrong_platform`).
- With no request and no applicable default, the result is `no_account`, never a
  silently chosen account.

Refusals are recorded as `slurm_account.resolve` with outcome `denied`. Resolutions
are logged with the Platform, account and origin; nothing here holds or logs an HPC
credential.

## Record the account with the job

[`authz.AccountResolution.job_fields`] gives the fields that
[`db.JobStore.begin`] records: `account`, `slurm_account_id` and
`slurm_account_origin`. The schema checks that the account belongs to the job's
Platform, that its name matches the registered account, and that the origin is
consistent (`native_default` names no account). Neither field changes after the
record is written.

```python
from nest.authz import AccountResolver, HPCIdentity

resolver = AccountResolver(database)
chosen = resolver.resolve(
    principal,
    project_id,
    platform="nano4",
    mode="A",
    identity=HPCIdentity.controller(nano4_id),
)
jobs = JobStore(database, provider_id, platform_id=chosen.platform_id)
jobs.begin(
    "batch",
    execution_id,
    project_id=project_id,
    submitted_by_user_id=principal.user_id,
    **chosen.job_fields(),
)
```

## Limitations

- The Controller still submits with the account of the request or its Platform
  Profile. Resolving every submission through [`authz.AccountResolver`] belongs to
  the durable execution registry (C01); this registry and its job-record columns
  are the contract it builds on.
- Per-User HPC identity bindings are not stored yet. [`authz.HPCIdentity`] is how
  the server describes the identity (its Platform, verified default account and
  optional entitlements) to the resolver; the binding records are follow-up work
  (Patch B06).
