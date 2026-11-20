# Handle remote errors

Exceptions raised during remote function execution are returned as `RemoteExecutionError`.

```python
import nest
from nest.errors import RemoteExecutionError


@nest.remote(gpu=1)
def compute():
    """Raises an error to demonstrate remote exception handling."""
    raise ValueError("Something went wrong")


try:
    compute.remote()
except RemoteExecutionError as exc:
    print(exc.remote_type)
    print(exc.remote_message)
    print(exc.remote_traceback)
```

The original exception type, message, and remote traceback are preserved.

## Connection and Controller failures

A remote Python exception is different from a failure to reach or run the
Controller:

| Exception | What to inspect |
| --- | --- |
| `RemoteExecutionError` | The function's remote type, message, and traceback. |
| `ConnectionError` | Tailscale connectivity, Controller address, access rules, and whether the Controller is running. |
| `InfrastructureError` subclasses | SSH, Slurm, or deadline failures; see the table below. |
| `grpc.RpcError` | `exc.code()` and `exc.details()` for any other Controller failure. |
| `OSError` | Whether the local project's `pyproject.toml` and `uv.lock` can be read. |
| `ValueError` | Negative `gpu` or `cpu` counts, or an invalid `NEST_RPC_TIMEOUT`. |

## Infrastructure failures and timeouts

The Controller classifies SSH and Slurm failures. It reports each one with a gRPC
status and an `nest-error-kind` trailing-metadata value, plus `nest-job-id` when
a Slurm job is involved. The client raises the matching class from `nest.errors`.
Every class is a `RuntimeError` and a `grpc.RpcError`, so existing
`except grpc.RpcError` handlers and `exc.code()` keep working. The timeout classes
are also `TimeoutError`s.

| Exception | `nest-error-kind` | gRPC status | Meaning | Safe to call again? |
| --- | --- | --- | --- | --- |
| `SSHTimeoutError` | `ssh_timeout` | `DEADLINE_EXCEEDED` | An SSH command or file transfer took longer than `NEST_SSH_TIMEOUT` (60 s). | Yes if `exc.job_id` is `None`: nothing was submitted. Otherwise the job was submitted and SSH failed while Nest waited for it. Check that job first. |
| `QueueTimeoutError` | `queue_timeout` | `DEADLINE_EXCEEDED` | The job (`exc.job_id`) never started before the polling deadline. The message and `exc.pending_reason` give Slurm's last pending reason. | Check or cancel `exc.job_id` first. It is still queued. |
| `JobBlockedError` | `job_blocked` | `FAILED_PRECONDITION` | The job stayed pending for a reason it cannot outwait, such as `PartitionConfig`, so Nest stopped waiting early. A `QueueTimeoutError` subclass. See [Pending jobs](#pending-jobs). | Fix the cause first. The job is still queued unless the message says Nest cancelled it. |
| `ExecutionTimeoutError` | `execution_timeout` | `DEADLINE_EXCEEDED` | The job started but did not finish before the polling deadline, or Slurm ended it with `TIMEOUT`. | Check `exc.job_id` first. It may still be running. |
| `RpcTimeoutError` | (client-side) | `DEADLINE_EXCEEDED` | The client's `Execute` deadline (`NEST_RPC_TIMEOUT`, 900 s) expired. | Not blindly. The Controller may still have submitted or be waiting for a job. |
| `SubmissionUnknownError` | `submission_unknown` | `UNKNOWN` | `sbatch` may have submitted a job, but Nest got no job ID: SSH timed out or dropped after `sbatch` started, or `sbatch` printed no ID. | Not blindly. See below. |
| `SSHError` | `ssh_error` | `UNAVAILABLE` | SSH could not reach or log in to the login node before running the command. | Yes, after fixing connectivity, unless `exc.job_id` names a job that was already submitted. |
| `RemoteCommandError` | `remote_command_error` | `INTERNAL` (`FAILED_PRECONDITION` for `SubmitJob` and `CancelJob`) | A remote command, such as a rejected `sbatch`, exited unsuccessfully. | Yes, after fixing the cause. |
| `DatabaseUnavailableError` | `database_unavailable` | `UNAVAILABLE` | The Controller's PostgreSQL database could not record the request, so it was refused before anything ran. | Yes. Nothing was submitted. |
| `SlurmJobError` | `slurm_job_failed` | `INTERNAL` | The job ended in a failure state such as `FAILED`, `NODE_FAIL`, or `OUT_OF_MEMORY`. | Yes, after reading the job's output file. |

A Controller that has not been upgraded sends no `nest-error-kind`. Its timeouts
arrive as plain `grpc.RpcError`s with `INTERNAL` status.

Timeouts never cancel Slurm jobs. Nest never retries an RPC or a submission.

```python
import nest
from nest.errors import (
    ExecutionTimeoutError,
    QueueTimeoutError,
    RpcTimeoutError,
    SubmissionUnknownError,
)


@nest.remote(cpu=4)
def train():
    """Runs a long training step on four CPUs."""


try:
    train.remote()
except QueueTimeoutError as exc:
    print(f"Job {exc.job_id} is still queued; cancel it or wait longer.")
except ExecutionTimeoutError as exc:
    print(f"Job {exc.job_id} is still running or hit its Slurm time limit.")
except SubmissionUnknownError as exc:
    print(f"Check the queue before resubmitting: {exc}")
except RpcTimeoutError:
    print("The Controller did not reply in time; its job may still exist.")
```

### Pending jobs

A queued job is not a failure: Slurm reports why it has not started, and Nest
keeps that reason. Every state and reason change is logged with the job's
partition, QoS, and account, and the last reason appears in the error message
and in the `nest-pending-reason` trailing metadata (`exc.pending_reason`).
Nest sorts reasons into three groups:

| Group | Example reasons | What Nest does |
| --- | --- | --- |
| Queue congestion | `Priority`, `Resources`, `Dependency`, unrecognized reasons | Waits until `NEST_RESULT_TIMEOUT`, then raises `QueueTimeoutError`. |
| QoS or association usage limit | `MaxGRESPerAccount`, `QOSMaxGRESPerUser`, `AssocGrpGRES` | Logs a warning with the requested resources and a suggested fix, and waits until `NEST_RESULT_TIMEOUT`, because the limit clears as the account's other jobs finish. Set `NEST_PENDING_LIMIT_TIMEOUT` to stop sooner with `JobBlockedError`. |
| Cannot start without a change | `PartitionConfig`, `PartitionTimeLimit`, `BadConstraints`, `InvalidAccount`, `InvalidQOS`, `*PerJobLimit`, `JobHeldUser` | Logs a warning, and raises `JobBlockedError` once the job has been pending for such a reason for `NEST_PENDING_BLOCKED_TIMEOUT` (120 s). |

For example, a job blocked by the account's GPU limit ends with:

```text
Slurm job 4242 did not start within 600 seconds. Last state: PENDING. Pending
reason: MaxGRESPerAccount (partition=h200_256gpu qos=normal account=acct
requested=cpu=8,node=1,gres/gpu=8). Slurm is enforcing a QoS or association
limit ... The job was not cancelled.
```

`JobBlockedError` leaves the job queued, so an administrator can still release a
hold or fix a limit, unless `NEST_CANCEL_BLOCKED_JOBS=true`, in which case Nest
cancels it with `scancel`. Nest never resubmits it either way.

### Ambiguous submissions

An SSH timeout during `sbatch` does not tell Nest whether Slurm accepted the job.
Nest raises `SubmissionUnknownError` and does **not** resubmit, because a retry
could run the function twice. The message names the execution ID and the
`output.pkl` path that a submitted worker writes. Before calling again:

1. Run `squeue --me --name=nest-worker` on the login node, or check
   `worker-<job_id>.out` files in `NEST_SLURM_WORKDIR`.
2. If a job exists, wait for it or `scancel` it.
3. Call `.remote()` again only once no job is running for that execution.

`SSHError` (SSH failed before the remote command started) and
`RemoteCommandError` (`sbatch` rejected the job) are not ambiguous: no job was
submitted.

See [Errors](api/errors), [Client execution](api/client), and
[Environment variables and defaults](configuration).
