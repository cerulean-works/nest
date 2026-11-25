# Execution service and RPCs

The Controller implements the `nest.ExecutionService` gRPC service defined in
[`execution.proto`](https://github.com/cerulean-works/nest/blob/main/src/nest/proto/execution.proto).
It starts with four RPC worker threads (`NEST_GRPC_MAX_WORKERS`) and a plaintext
gRPC listener, or a TLS listener when `NEST_TLS_CERT_FILE` and `NEST_TLS_KEY_FILE`
are set, bounded and guarded as described in
[Public API security and abuse controls](../public-api-security). See
[Deploy a Slurm Controller](../deploy) for setup and
[Security and limitations](../security) before granting access.

## RPC messages

| RPC | Request fields | Response fields |
| --- | --- | --- |
| `Execute` | `payload` (bytes), `gpu` and `cpu` (int32), optional `environment` | `result` (bytes) |
| `GetJobStatus` | `job_id` (int64) | `state` (string) |
| `SubmitJob` | `workdir`, `script`, `account`, `partition`, `time` (strings), `gpu` and `cpu` (int32), `script_args` (repeated string) | `job_id` (int64) |
| `CancelJob` | `job_id` (int64) | Empty response |
| `ListEligibleProjects` | Empty | `projects` (repeated `id`, `name`, `role`), `projects_required` (bool) |

`environment` contains `pyproject_toml` and `uv_lock` bytes. When supplied, both
must be nonempty. `gpu` and `cpu` must not be negative; `0` omits the Slurm flag.
`SubmitJob` passes each `script_args` element to the script, after its path,
verbatim as one argument, so an element is never an `sbatch` option. Slurm job
operations require a configured backend, even when `Execute` uses local mode.
Cancellation requests do not wait for a job to stop.

`ListEligibleProjects` answers which of the caller's Projects this Controller would
accept a submission from now (role, Provider approval and the served Platform's
grant), for the SDK's [Project selection](../project-selection). It only reads,
records no per-Project refusal, and is advice: every submission is authorized
again. Without session authentication it answers `projects_required: false`.

## Request metadata

| Key | Value |
| --- | --- |
| `authorization` | `Bearer <access token>` of a Nest CLI session. Required with `NEST_CONTROLLER_AUTH=session`. |
| `x-nest-project-id` | The Project a submission (`Execute`, `SubmitJob`) runs in. Required with session authentication. The SDK sends `NEST_PROJECT_ID`, or the Project it [selected](../project-selection). |
| `x-forwarded-for` | Read only from a peer in `NEST_GRPC_TRUSTED_PROXIES` (the public gateway), for rate limits, logs and the `client_address` of audit records. |
| `x-nest-request-id` | Optional 32-character hex correlation ID, used in logs and audit records. |
| `x-nest-platform` | Optional HPC Platform slug or ID for a registry-backed submission. It must be eligible for the Project; it is refused, never replaced. |
| `x-nest-idempotency-key` | Optional key (1 to 200 printable ASCII characters) that makes a registry-backed submission idempotent for its owner and Project. |

A registry-backed submission (`NEST_EXECUTION_REGISTRY=required`) answers with its
execution ID in the `nest-execution-id` trailing metadata. See
[Admit submissions through the Execution Registry](../execution-registry) for its
refusals and failure statuses.

With session authentication, a call without an accepted session fails with
`UNAUTHENTICATED`, a submission without a Project with `INVALID_ARGUMENT`, a
Project or job the caller cannot see with `NOT_FOUND`, and a refusal by role,
scope or Provider with `PERMISSION_DENIED`. See
[Authenticated execution](../security#authenticated-execution).

## Error statuses

Classified SSH and Slurm failures set the `nest-error-kind` trailing metadata, and
`nest-job-id` when a job is involved. Clients can tell timeouts apart without
parsing messages:

| `nest-error-kind` | Status |
| --- | --- |
| `ssh_timeout`, `queue_timeout`, `execution_timeout` | `DEADLINE_EXCEEDED` |
| `submission_unknown` | `UNKNOWN` |
| `ssh_error` | `UNAVAILABLE` |
| `remote_command_error` | `INTERNAL` for `Execute` and `GetJobStatus`; `FAILED_PRECONDITION` for `SubmitJob` and `CancelJob` |
| `slurm_job_failed`, `infrastructure_error` | `INTERNAL` |
| `rate_limited` (with `retry-after` seconds) | `RESOURCE_EXHAUSTED` |
| `origin_not_allowed` | `PERMISSION_DENIED` |
| `limit_environment_builds` | `RESOURCE_EXHAUSTED` |
| `client_gone` | `CANCELLED` |
| `result_too_large` | `RESOURCE_EXHAUSTED` |

Admission limits ([Admission limits](../admission-limits)) answer
`RESOURCE_EXHAUSTED` or `FAILED_PRECONDITION` with the limit's reason in the
status message.

A `DEADLINE_EXCEEDED` status without `nest-error-kind` is the caller's own RPC
deadline. The Controller never retries `sbatch`. See
[Handle remote errors](../remote-errors).

## Service handlers

[[autodoc]] controller.ExecutionService

## Server entry point

[[autodoc]] controller.main

[[autodoc]] controller.build_backend

[[autodoc]] controller.open_database

[[autodoc]] controller.build_access

[[autodoc]] controller.build_admission

[[autodoc]] controller.recovery_threshold

[[autodoc]] controller.settle_threshold

[[autodoc]] controller.build_server

[[autodoc]] controller.record_start

## Registry-backed admission

[[autodoc]] admission.RegistryAdmission

[[autodoc]] admission.SubmissionRequest

[[autodoc]] admission.Admission

[[autodoc]] admission.HPCIdentitySource

[[autodoc]] admission.ControllerIdentitySource

[[autodoc]] admission.AdmissionRefusedError

[[autodoc]] admission.DuplicateSubmissionError

[[autodoc]] admission.RecoverySweeper

[[autodoc]] admission.BuildSlot

## Abuse controls

[[autodoc]] rpc_guard.RpcGuard

[[autodoc]] rpc_guard.TokenBuckets

[[autodoc]] rpc_guard.client_address

[[autodoc]] rpc_guard.server_options
