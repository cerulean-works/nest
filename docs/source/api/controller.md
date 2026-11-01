# Execution service and RPCs

The Controller implements the `nest.ExecutionService` gRPC service defined in
[`execution.proto`](https://github.com/cerulean-works/nest/blob/main/src/nest/proto/execution.proto).
It starts with four RPC worker threads and an insecure gRPC listener. See
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
must be nonempty. Slurm job operations require a configured backend, even when
`Execute` uses local mode. Cancellation requests do not wait for a job to stop.

## Service handlers

[[autodoc]] controller.ExecutionService

## Server entry point

[[autodoc]] controller.main
