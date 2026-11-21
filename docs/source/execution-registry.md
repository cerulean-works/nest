# Admit submissions through the Execution Registry

With session authentication (`NEST_CONTROLLER_AUTH=session`), the Controller admits
every Slurm submission, from both `Execute` and `SubmitJob`, through the durable
Execution Registry (`NEST_EXECUTION_REGISTRY=required`, the default). The registry,
not the legacy `jobs` table, is the record of these runs. This page covers what the
Controller does before `sbatch`, how to provision a Platform for it, how failures
are handled, and the explicit legacy path.

## What happens before `sbatch`

For each submission the Controller, on the server side and in this order:

1. authenticates the Nest CLI session and checks `run.submit` in the Project named
   by `x-nest-project-id`, recording the decision in the security audit log, as
   before;
2. selects the HPC Platform with the central Project-to-Platform policy: the
   Platform named in `x-nest-platform`, which must be eligible, or else the
   Project's default, the server default or the only eligible Platform (see
   [Register HPC Platforms and login nodes](platforms)). An ineligible or disabled
   Platform is refused, never replaced, and a Platform this Controller does not
   serve is refused (`platform_not_served`) instead of running elsewhere;
3. chooses an approved login node of that Platform for its transport mode;
4. takes the HPC identity from its own configuration: in Mode A, the operator's
   UNIX user (`SSH_USER` of the Platform Profile). Nothing the client sends can
   name a UNIX user, an HPC binding or an entitlement;
5. resolves the Slurm account with the Platform-scoped account registry (see
   [Map Slurm accounts to Projects](slurm-accounts)): the requested account, which
   must be linked to the Project, or the Project default, or the Platform's Mode A
   operator default;
6. registers the execution and records its `submitting` attempt, committing both:
   the requested options, every resolved option with its origin (`request` or the
   default's layer: `project`, `platform`, `server` or `catalog`), the Platform,
   the login-node snapshot, the mode, the UNIX user, the account and its origin,
   and the digests of the serialized call and of the environment files.

Only then does it stage files and run `sbatch`, with the recorded account,
partition and time limit. If any step refuses, or the database cannot commit,
nothing reaches the cluster, and the Controller never falls back to the legacy
path or to another Platform or account. Grants, links and node approvals are read
again in the transaction that records the attempt, so a revocation either wins
outright or waits for it.

Each successful response, and each error after admission, carries the execution ID
in the `nest-execution-id` trailing metadata.

## Provision a Platform

The Controller infers no authority from its environment. Grant the environment
Platform and link an account explicitly, with an operator name and reason that the
audit log keeps:

```bash
nest-db platform grant default --project-id "$PROJECT" --operator ops --reason "C01 rollout"
nest-db account register default proj --operator ops --reason "C01 rollout"
nest-db account link default proj --project-id "$PROJECT" --modes A --default \
  --operator ops --reason "C01 rollout"
```

and set the operator's UNIX user, which every attempt records as its HPC identity:

```bash
NEST_PLATFORM_DEFAULT_SSH_USER=nestop   # or NEST_SLURM_SSH_USER for the NEST_SLURM_* form
```

`NEST_SLURM_ACCOUNT` (the Platform Profile's account) is not charged for
registry-backed runs unless it is registered and linked like any other account,
for example as the Platform's Mode A operator default
(`nest-db account set-mode-a-default`).

## Idempotency and retries

Send `x-nest-idempotency-key` (1 to 200 printable ASCII characters) to make a
submission idempotent for its owner and Project:

| Earlier request with the same key | Result |
| --- | --- |
| Submitted (`SubmitJob`) | The same `job_id`, without a new `sbatch` |
| Submitted (`Execute`) | `ALREADY_EXISTS` |
| Still being submitted, or outcome `unknown` | `ABORTED`; nothing is submitted |
| Failed, or reconciled as not submitted | A new attempt |
| A different request | `FAILED_PRECONDITION` (`idempotency_conflict`) |

Concurrent requests with one key submit at most once: the registry allows a single
attempt in flight per execution.

## Failures

| What happened | Attempt | Client sees |
| --- | --- | --- |
| Refused by policy (Platform, login node, identity, account) | None | `NOT_FOUND`, `PERMISSION_DENIED` or `FAILED_PRECONDITION` with the reason |
| Database unavailable or commit failed before `sbatch` | None | `UNAVAILABLE`, `nest-error-kind: database_unavailable` |
| SSH or `sbatch` refused the job | `failed` | The classified error |
| `sbatch` may have run, but no job ID arrived | `unknown` | `UNKNOWN`, `nest-error-kind: submission_unknown` |
| Slurm accepted the job | `submitted`, then `running`, `succeeded`, `failed` or `cancelled` as `Execute` or `GetJobStatus` observe it | The result or job ID |
| The job ID could not be stored after `sbatch` | stays `submitting` until recovery marks it `unknown` | The job ID, which is logged |
| A timeout or lost status query while waiting | stays `submitted` | The classified error; the job is not cancelled |

An `unknown` attempt is never resubmitted. At start, and every five minutes, the
Controller marks `submitting` attempts older than
`NEST_ENVIRONMENT_TIMEOUT + 4 × NEST_SSH_TIMEOUT + 5 minutes` as `unknown`
(`error_kind` `controller_interrupted`): their Controller stopped between recording
them and receiving a job ID.

## Reconcile an unknown attempt

Check the cluster (for example with `sacct` for the attempt's account, and the
execution directory named in the error), then record what you found:

```bash
nest-db execution list
nest-db execution reconcile ATTEMPT_ID --slurm-job-id 123456 \
  --operator ops --reason "sacct shows job 123456 for this execution directory"
nest-db execution reconcile ATTEMPT_ID --not-submitted \
  --operator ops --reason "no job in sacct; execution directory has no output"
```

The first makes the attempt `submitted`, and status queries follow it; the second
marks it `failed` (`reconciled_not_submitted`), so a retry with the same
idempotency key may start a new attempt. Only an `unknown` attempt can be
reconciled, and the operator and evidence are recorded as an
`execution.reconciled` event in the security audit log.

## Job status and cancellation

`GetJobStatus` and `CancelJob` find a job by its Slurm job ID on the served
Platform only, and authorize against the newest record with that ID, a registry
attempt or a legacy `jobs` row, so a job ID that Slurm reused never authorizes
against an older record, and a job of another Project is `NOT_FOUND`. The B07
rules are unchanged: any member may read, only the submitter cancels.

## Legacy path

`NEST_EXECUTION_REGISTRY=legacy` keeps the trusted Mode A behavior from before the
registry: submissions are recorded only in `jobs` and charge the Platform
Profile's account. It is an explicit choice, logged at start as a warning and
recorded in the `controller.started` audit event. It is refused for a Mode B
Platform under session authentication, and it is the only behavior without
session authentication (`NEST_CONTROLLER_AUTH=none`), where no Nest User could own
an execution. Historical `jobs` rows are never migrated or changed, and remain
readable through `GetJobStatus`.

`Execute` with `NEST_EXECUTOR=local` runs on the Controller itself and does not
use the registry.

## Limitations

- **Mode B.** Nest has no verified per-User HPC binding or per-User transport
  yet. A Mode B Platform is refused (`hpc_binding_unavailable`); even with a
  binding, the Controller refuses (`mode_b_transport_unavailable`) rather than use
  its own SSH credentials. Mode B end-to-end submission is not supported.
- **One served Platform.** A Controller submits only to its default Platform
  Profile's environment Platform; a Project whose request resolves elsewhere is
  refused.
- **Nodes, usage and runtime digest.** Allocated compute nodes, usage records and
  the runtime (container image) digest are not collected yet; `output_ref` is
  recorded for `Execute` (`<execution directory>/output.pkl`).
- `GetJobStatus` and `CancelJob` take a Slurm job ID, so they cannot address an
  `unknown` attempt, which has none; reconcile it first.
