# Execution service and RPCs

The Controller implements the `nest.ExecutionService` gRPC service defined in
[`execution.proto`](https://github.com/cerulean-works/nest/blob/main/src/nest/proto/execution.proto).
It starts with four RPC worker threads and a plaintext gRPC listener, or a TLS
listener when `NEST_TLS_CERT_FILE` and `NEST_TLS_KEY_FILE` are set. See
[Deploy a Slurm Controller](../deploy) for setup and
[Security and limitations](../security) before granting access.

## RPC messages

| RPC | Request fields | Response fields |
| --- | --- | --- |
| `Execute` | `payload` (bytes), `gpu` and `cpu` (int32), optional `environment` | `result` (bytes) |
| `GetJobStatus` | `job_id` (int64) | `state` (string) |
| `SubmitJob` | `workdir`, `script`, `account`, `partition`, `time` (strings), `gpu` (int32) | `job_id` (int64) |
| `CancelJob` | `job_id` (int64) | Empty response |

`environment` contains `pyproject_toml` and `uv_lock` bytes. When supplied, both
must be nonempty. `gpu` and `cpu` must not be negative. Slurm job operations
require a configured backend, even when `Execute` uses local mode. Cancellation
requests do not wait for a job to stop.

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

A `DEADLINE_EXCEEDED` status without `nest-error-kind` is the caller's own RPC
deadline. The Controller never retries `sbatch`. See
[Handle remote errors](../remote-errors).

## Service handlers

[[autodoc]] controller.ExecutionService

## Server entry point

[[autodoc]] controller.main

[[autodoc]] controller.build_backend
