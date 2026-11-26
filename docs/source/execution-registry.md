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
   then resolves the execution authority (`controller_owned` or `user_owned`)
   from the deployment, Platform and Project policies and, for `user_owned`, the
   caller's verified HPC binding. An authority the policies do not permit is
   refused, never replaced, and `user_owned` never falls back to the Controller's
   credentials (see [Authorize execution authority and HPC account bindings](hpc-access));
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
   the login-node snapshot, the mode, the execution authority and the policy
   versions it was admitted under, the UNIX user, the account and its origin,
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
| Failed (including a function that raised), or reconciled as not submitted | A new attempt, in its own artifact directory |
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
| The job completed, but the function raised | `failed`, `slurm_state` `COMPLETED`, `remote_application_error` | The remote error record (`RemoteExecutionError`), unchanged |
| The job completed, but its outcome record could not be read, or is missing | stays `submitted` until a status query or the settle sweep reads it (see [Application failures](#application-failures)) | The result, unchanged |
| The Platform's worker script predates the outcome record | `failed`, `worker_protocol_unsupported`, before anything is staged | `FAILED_PRECONDITION`, `nest-error-kind: worker_protocol_unsupported` |
| Slurm accepted the job, but its job ID could not be stored (after brief retries) | `submitting`; the Controller records the kept job ID at its next recovery sweep, also after the attempt was marked `unknown` | `UNKNOWN`, `nest-error-kind: submission_unknown`, with the job ID in `nest-job-id`; never a success |
| A timeout or lost status query while waiting | stays `submitted` | The classified error; the job is not cancelled |

An `unknown` attempt is never resubmitted. At start, and every five minutes, the
Controller marks `submitting` attempts older than
`NEST_ENVIRONMENT_TIMEOUT + 4 × NEST_SSH_TIMEOUT + 5 minutes` as `unknown`
(`error_kind` `controller_interrupted`): their Controller stopped between recording
them and receiving a job ID.

## Application failures

A function that raises is still a job Slurm completed: the worker writes the
remote error record as the result (`output.pkl`) and exits successfully. To tell
the two apart without unpickling that result, which would run code the caller
controls inside the Controller, the worker also writes a plain-JSON outcome
record, `outcome.json`, next to it:

```json
{"nest_outcome": 1, "status": "raised", "error_type": "builtins.RuntimeError"}
```

### Worker protocol and per-attempt artifacts

Before staging an `Execute`, the Controller reads the protocol that the
Platform's deployed worker script declares (`# nest-worker-protocol: 2` in
`project/scripts/worker.sbatch`) and pins it on the attempt (`worker_protocol`,
migration `0020_attempt_worker_protocol`), before `sbatch`. The pinned value, not
whatever is deployed later, decides how the attempt settles.

A protocol 2 attempt has its own artifact directory,
`<work root>/<execution ID>/attempts/<attempt number>/`, holding its
`input.pkl`, `output.pkl` and `outcome.json`; the worker publishes both result
files atomically (written beside their name, then renamed), and the job only
completes when both exist. A retry under the same idempotency key is a new
attempt in a new directory, so it can never change, clear or truncate an earlier
attempt's result or outcome record, even while that attempt's `Execute` is still
downloading it. Each attempt's `output_ref` points at its own `output.pkl`.

A worker script that declares no protocol predates the outcome record. By
default the Controller refuses it (`NEST_LEGACY_WORKERS=refuse`): the `Execute`
fails with `FAILED_PRECONDITION` (`worker_protocol_unsupported`) before anything
is staged, and the attempt is `failed` with that kind, so the call can be retried
once the Platform's Nest checkout is updated. During a rollout an operator can set
`NEST_LEGACY_WORKERS=allow`: such an attempt (`worker_protocol` 1) runs in the
shared execution directory, as before, and its completion without a record is an
**unverified** success. Anything that depends on a verified success must exclude
attempts whose `worker_protocol` is not 2 or more. Attempts recorded before
migration 0020 have no `worker_protocol` and settle the same way.

### How a completed `Execute` settles

When Slurm reports `COMPLETED`, the Controller reads at most 1 KiB of the
attempt's own `outcome.json` over SSH and parses it strictly. The same rule
applies however the completion is seen, by `Execute`, `GetJobStatus` or the
settle sweep, so none of them overwrites another's record:

| Outcome record | Attempt | `error_kind` | Execution |
| --- | --- | --- | --- |
| `returned` | `succeeded` | — | `succeeded` (closed) |
| `raised` | `failed`, `slurm_state` `COMPLETED` | `remote_application_error` | `failed`, open for a retry |
| Malformed | `failed`, `slurm_state` `COMPLETED` | `remote_outcome_invalid` | `failed`, open for a retry |
| Not a readable regular file (a directory, a dangling link, no permission) | `failed`, `slurm_state` `COMPLETED` | `remote_outcome_unreadable` | `failed`, open for a retry |
| Missing, protocol 2 | stays `submitted` or `running`, marked `remote_outcome_pending`, keeping its reservation | then `remote_outcome_missing`, `failed` | `failed`, open for a retry |
| Missing, legacy worker (`worker_protocol` 1 or none) | `succeeded`, unverified | — | `succeeded` (closed) |
| Could not be read now (SSH failed) | left as it was; read again later | — | unchanged |

A missing record is never a success for a protocol 2 attempt: its worker always
writes one, so a missing record means the shared filesystem has not caught up,
or the file was removed. `Execute` and `GetJobStatus` only mark such an attempt
`remote_outcome_pending`. The settle sweep reads it again once the attempt has
been unchanged for a full settle threshold after that, and fails it
(`remote_outcome_missing`) only if the record is still missing then; a record
that appears in the meantime settles the attempt as usual.

The attempt's `error_message` names only the exception type, or a fixed reason;
it never quotes the record. The exception message and traceback belong to the
caller and are delivered to the client unchanged in the result; the Controller
does not store them. A failed attempt releases its admission reservation like
any other failure. Slurm and transport failures keep their own kinds, such as
`slurm_job_failed`. Batch jobs (`SubmitJob`) have no outcome record: `COMPLETED`
is a success.

The legacy path (`NEST_EXECUTION_REGISTRY=legacy`) runs each `Execute` once in
its own execution directory and records the same outcome in `jobs`: `failed`
with `remote_application_error`. It has no settle sweep, so a record that cannot
be read makes the row `unknown` (`remote_outcome_unreadable`), and a protocol 2
worker that left none makes it `unknown` (`remote_outcome_missing`): never
`succeeded`, and distinct from a job still in flight. `NEST_LEGACY_WORKERS` does
not apply to it.

### Roll out

1. Update each Platform's Nest checkout (the worker script and worker) first, or
   set `NEST_LEGACY_WORKERS=allow` for the rollout.
2. Run `nest-db upgrade` (migration `0020_attempt_worker_protocol` is additive),
   then update the Controller.
3. Once every Platform's worker declares protocol 2, unset `NEST_LEGACY_WORKERS`.

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

The first makes the attempt `submitted`, and status queries follow it (a
completed execution then settles from its outcome record, as above); the second
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
