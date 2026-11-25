# Admission limits for the trusted pilot (F02a)

A registry-backed Controller (`NEST_EXECUTION_REGISTRY=required`, see
[Admit submissions through the Execution Registry](execution-registry)) bounds
what each User, Project and Platform may have in flight, server-side, for
`Execute`, `SubmitJob` and environment builds. The limits sit on top of RBAC, the
Provider approval, the Platform grant and the Slurm account registry, and replace
none of them.

These are **Nest quotas**, enforced before Nest asks Slurm for anything. They are
not Slurm policy: Slurm's own associations, QoS, partition limits and fairshare
still apply to every job Nest submits, and a job Nest admits can still wait or be
refused in Slurm. Nest controls only the jobs it submits; it claims no control over
anything a User runs with their own downstream SSH access (Mode B or otherwise).
Full GPU-hour accounting and showback are F02b (M3).

## What is limited

| Limit | Default | Scope | Refused as |
| --- | --- | --- | --- |
| `NEST_LIMIT_ACTIVE_PER_USER` | 8 | live attempts owned by one User, across Projects and Platforms | `RESOURCE_EXHAUSTED` (`limit_active_per_user`) |
| `NEST_LIMIT_ACTIVE_PER_PROJECT` | 32 | live attempts of one Project | `RESOURCE_EXHAUSTED` (`limit_active_per_project`) |
| `NEST_LIMIT_ACTIVE_PER_PLATFORM` | 128 | live attempts on one Platform | `RESOURCE_EXHAUSTED` (`limit_active_per_platform`) |
| `NEST_LIMIT_CPU_PER_PROJECT` | 512 | CPUs requested by a Project's live attempts | `RESOURCE_EXHAUSTED` (`limit_cpu_per_project`) |
| `NEST_LIMIT_GPU_PER_PROJECT` | 16 | GPUs requested by a Project's live attempts | `RESOURCE_EXHAUSTED` (`limit_gpu_per_project`) |
| `NEST_LIMIT_SUBMISSIONS_PER_MINUTE` | 30 | new attempts by one User in any 60 s, finished or not | `RESOURCE_EXHAUSTED` (`limit_submissions_per_minute`) |
| `NEST_LIMIT_CPU_PER_REQUEST` | 128 | `cpu` of one request | `FAILED_PRECONDITION` (`limit_cpu_per_request`) |
| `NEST_LIMIT_GPU_PER_REQUEST` | 8 | `gpu` of one request | `FAILED_PRECONDITION` (`limit_gpu_per_request`) |
| `NEST_LIMIT_WALLTIME` | `1-00:00:00` | the `--time` of one request (or the Platform default) | `FAILED_PRECONDITION` (`limit_walltime`; `limit_walltime_unbounded` for `UNLIMITED` or no time) |
| `NEST_LIMIT_ENVIRONMENT_BUILDS` | 2 | environment builds one Controller runs at once | `RESOURCE_EXHAUSTED` (`limit_environment_builds`) |
| `NEST_LIMIT_BATCH_SCRIPTS` | `trusted` | how `SubmitJob` batch scripts are admitted; see [Batch scripts](#batch-scripts) | `FAILED_PRECONDITION` (`limit_batch_scripts_unbounded`) with `refuse` |

Every limit is a positive bound of at most 1,000,000 (`NEST_LIMIT_WALLTIME` is a
bounded Slurm time); none can be switched off, and an invalid value stops the
Controller at start with every problem listed. A live attempt is one that is
`submitting`, `submitted`, `running` or `unknown`.

## What is reserved

Limits count what Slurm is asked to allocate, not only the counts in the RPC.

- **`Execute`.** The Controller pins the worker job with `--nodes=1 --ntasks=1` and
  an explicit `--cpus-per-task`, which override the worker script's own counts.
  It reserves the request's GPUs per node and CPUs, or the worker script's
  defaults where the request asks for `0`: **1 GPU and 1 CPU**. A CPU-only call
  therefore holds, and is charged, one GPU, because `scripts/worker.sbatch`
  requests one and Nest does not pass `--gpus-per-node=0`, whose meaning varies
  between sites. That GPU is not visible to the function (see
  [Run functions on GPUs](gpus)). Changing this is E04's site-supported
  zero-GPU policy.
- **`Execute` with an environment.** A cache miss runs the build job
  (`scripts/environment-smoke.sbatch`: one node, 1 GPU, 12 CPUs, 10 minutes)
  before the worker, within the same attempt. Since admission cannot know in
  advance whether the cache is warm, it reserves the larger of the two
  allocations: at least 1 GPU, 12 CPUs and 10 minutes.
- **Account.** The environment build is charged to the same Slurm account and partition that admission authorized and recorded for the attempt, never to the Platform Profile's default account.
- **`SubmitJob`.** See [Batch scripts](#batch-scripts).

The per-request bounds (`*_PER_REQUEST`, `WALLTIME`) apply to these effective
values too, so a request that asks for nothing can still exceed a per-request
CPU bound below 12 when it needs an environment build.

### Batch scripts

A `SubmitJob` script is a file on the cluster whose own `#SBATCH` directives
(`--nodes`, `--ntasks-per-node`, `--gres`, `--gpus`, `--array`, `--mem`, ...)
can allocate far more than the request's `gpu` and `cpu`. Command-line flags
override only the same options, and Nest does not read the script, so no
admission limit can bound its allocation. Nest therefore makes no claim to:

- `NEST_LIMIT_BATCH_SCRIPTS=trusted` (the default, which keeps the existing
  private deployment's `SubmitJob` working): batch jobs are admitted. They count
  against the active-attempt and per-minute limits, and reserve the counts they
  asked for, but the reservation is recorded with `bounded = false` and the
  audit record with `quota_enforced: false`. The CPU and GPU sums do not bound
  what those jobs hold.
- `NEST_LIMIT_BATCH_SCRIPTS=refuse`: every `SubmitJob` is refused before anything
  is registered. **Set this for a bounded pilot.** `Execute` is unaffected.

Memory is not limited: Nest's API has no memory request, so Slurm's partition
defaults decide it. Repeated requests are bounded by the per-minute limit and the
[per-User RPC rate limit](public-api-security); an idempotent retry of a request
Slurm already accepted is answered with the existing job and reserves nothing.

The status message names the limit, the current use and the bound, and says that
nothing was submitted. The SDK does not retry `RESOURCE_EXHAUSTED`.

## Reservations

Admission and reservation are one step. In the transaction that records the
`submitting` attempt ([`db.ExecutionRegistry.begin_attempt`]) the Controller:

1. checks the per-request bounds (also before registering anything);
2. rechecks that the submitter's session is still valid (a session rotated by a
   refresh counts while its family is live), the User active, and their
   membership one that may submit, together with the Platform grant, the login
   node, the Slurm account link and the Provider approval, share-locking each;
3. takes the exclusive `nest.admission` advisory lock, so admissions on every
   replica sharing the database apply one after another;
4. counts the reservations held by live attempts and refuses if this one would
   exceed a limit;
5. records the attempt's reservation (`execution_reservations`: owner, Project,
   Platform, CPUs, GPUs, walltime and the limits in force) and commits.

Only then does the Controller build the environment, stage the payload and run
`sbatch`. A refused or failed admission rolls back: no attempt, no reservation,
nothing on the cluster. When the database cannot answer, admission fails closed
with `UNAVAILABLE` (`database_unavailable`). A request refused at step 4 leaves
its execution registered and `pending`, so a retry with the same idempotency key
can proceed once there is room.

### Release

A reservation is never released by a counter. It is held exactly while its
attempt is live, so it is released once, when the attempt reaches a final state,
however many times that state is observed:

| Outcome | Attempt | Reservation |
| --- | --- | --- |
| Job completed, failed, timed out in Slurm, or was cancelled, as Slurm reports | `succeeded`, `failed`, `cancelled` | released |
| Refused by Slurm, or SSH failed before `sbatch` | `failed` | released |
| Job completed, but its result exceeds the RPC message bound | `failed` (`result_too_large`) | released |
| Environment build failed in Slurm, or nothing was built (staging or readiness check failed before a build job) | `failed` | released |
| Environment build timed out, its status could not be read, or its `sbatch` outcome is unknown | `unknown` (`environment_build_unknown`) | **held** until reconciled: the build job may still run |
| Client cancelled or its deadline passed before a side effect | `failed` (`client_gone`) | released |
| `sbatch` may have run but no job ID arrived | `unknown` | **held** until `nest-db execution reconcile` |
| Controller stopped before a job ID arrived | `unknown` after recovery | **held** until reconciled |
| `Execute` timed out, or the client went away, after `sbatch` | `submitted` | **held** until Slurm reports the job finished |
| `CancelJob` | unchanged until Slurm reports `CANCELLED` | **held** until then |
| Status query fails, or Slurm no longer knows the job | unchanged | **held** |

No timeout, RPC disconnect or restart releases a reservation whose job may be
running. To release reservations of jobs that finished while nobody asked for
their status, the recovery sweeper (every 5 minutes) also queries Slurm for
`submitted` and `running` attempts of the served Platform that have not changed
for `NEST_RESULT_TIMEOUT` + 2 × `NEST_SSH_TIMEOUT` + 60 s and that no RPC of this
Controller is waiting on, at most 50 per sweep ([`admission.RegistryAdmission.settle`]).

### Remaining lifecycle limitations

- A job Slurm has forgotten (past `MinJobAge`, with no accounting) keeps its
  reservation until an operator reconciles or finishes the attempt. `nest-db
  execution list` shows such attempts.
- An application error in the user's function completes the Slurm job and is
  recorded `succeeded` (issue #73); the reservation is released either way.
- Cancelling the Slurm job of an abandoned `Execute`, and short RPCs that do not
  hold a thread while a job runs, are C04 (detached execution).
- Environment-build slots are per Controller replica and held in memory, and are
  freed once the build call returns. What bounds builds across replicas and
  restarts is the attempt's durable reservation, which covers the build's
  allocation and stays held (`unknown`) whenever the build may still be running.
  The build's Slurm job ID is in the attempt's `error_message` and in the
  `execution.environment_build_unknown` audit record. Once Slurm shows that job
  finished, reconcile with `nest-db execution reconcile ATTEMPT --not-submitted`
  (the worker never ran).
- The legacy path (`NEST_EXECUTION_REGISTRY=legacy`) enforces no admission
  limits; the Controller says so at start. Use it only for trusted Mode A.

## Audit and observability

Every admission writes an `execution.admit` record to the
[security audit log](audit-log): `succeeded` with the execution, attempt,
Platform, account, CPUs, GPUs and walltime, or `denied` with the reason
(`limit_*`, `submitter_not_authorized`, `provider_denied`, ...) and, for a
limit, `limit`, `current` and `requested`. Records carry the actor, session and
Project, never a token or a payload. The Controller's start record lists the
limits in force (`admission_limits`), and the log line `Submission admitted`
carries the reservation.

To see what is held now:

```sql
SELECT r.project_id, r.owner_user_id, a.status, r.cpu, r.gpu, r.walltime_seconds
FROM execution_reservations r JOIN execution_attempts a ON a.id = r.attempt_id
WHERE a.status IN ('submitting', 'submitted', 'running', 'unknown');
```

[`db.ExecutionRegistry.held_reservations`] gives the totals.

## Upgrade and rollback

`nest-db upgrade` applies `0018_admission_reservations`. It adds the
`execution_reservations` table and backfills a reservation (CPUs and GPUs, no
walltime) for every attempt that is live at the time, so the first admission
after the upgrade counts them. It changes no existing row.

To loosen limits, raise the `NEST_LIMIT_*` values and redeploy; no data changes.
To roll the Controller back, take a backup before `nest-db upgrade` as usual:
Nest has no down-migrations, and an older Controller refuses to start on a schema
with a migration it lacks, so restore that backup and deploy the previous image
(see [Store Controller records in PostgreSQL](database)). The older release
ignores the `NEST_LIMIT_*` variables.
