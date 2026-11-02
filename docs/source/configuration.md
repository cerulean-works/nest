# Environment variables and defaults

## Client

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_CONTROLLER_ADDRESS` | `services.tail8e5ba1.ts.net:50051` | Host and port for each remote call. An empty value also uses the default. |

Set the address in the process running your Nest application. It does not
configure where the Controller listens.

## Controller

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_CONTROLLER_BIND` | `127.0.0.1:50051` | Listener address read when the Controller starts. |
| `NEST_EXECUTOR` | `local` | Only the exact value `slurm` selects Slurm execution; other values use a local subprocess. |
| `NEST_SLURM_HOST` | Unset | SSH host or alias used by the Slurm backend. |
| `NEST_SLURM_ACCOUNT` | Unset | Default Slurm account. Both this and the host must be nonempty to create a backend. |
| `NEST_SLURM_WORKDIR` | Unset | Absolute shared HPC directory containing `project/`, `images/`, `environments/`, and `jobs/`. Required for Controller-managed Slurm worker execution. |

The Controller creates its backend with partition `dev` and a Slurm time limit of
`00:05:00`. These are currently code defaults, without environment-variable
overrides. Direct backend users can set [`backends.slurm.SlurmConfig`]. Review
the deployed batch scripts for CPU, memory, and cluster-specific settings.

## Deadlines and resources

| Operation | Current value | Notes |
| --- | --- | --- |
| Client `Execute` RPC | 900 seconds | Covers the entire RPC, including environment preparation and queueing. |
| Controller Slurm result polling | 600 seconds | Starts after worker submission; remote queries and download can extend total time. |
| Environment build polling | 1,800 seconds | Backend default; this can exceed the client's RPC deadline. |
| SSH commands and file transfer subprocesses | 60 seconds | A timeout does not prove the remote operation stopped. |
| `@nest.remote` resources | `gpu=0`, `cpu=1` | The Slurm Controller forwards `gpu` but currently ignores `cpu`. |

These deadlines are currently set in code. RPC or polling timeouts do not
automatically cancel submitted Slurm jobs. For deployment steps, see
[Deploy a Slurm Controller](deploy); for local execution, see
[Run a local Controller](local-controller).

## Logging and troubleshooting

The Controller and worker commands write logs to **stderr** at `INFO` by default.
Set `NEST_LOG_LEVEL` to `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` before
starting the Controller. Invalid values produce a warning and fall back to `INFO`.
`DEBUG` adds file sizes, SSH timings and runtime readiness checks; it does not
print serialized calls or dependency file contents.

```bash
NEST_LOG_LEVEL=DEBUG uv run python -m nest.controller 2>controller.log
```

Command log lines include a UTC timestamp, severity, module, process ID and
`request_id`. Execution stages also record elapsed milliseconds. Startup and
shutdown messages use `request_id=-` because they are outside any request.

### Enable client logs

Importing Nest does not configure application logging. In your client program,
configure the `nest` logger before calling `.remote()`:

```python
import logging
import nest

handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s %(message)s",
    defaults={"request_id": "-"},
))
logger = logging.getLogger("nest")
logger.setLevel(logging.INFO)  # Use DEBUG for additional detail.
logger.addHandler(handler)
logger.propagate = False
```

`NEST_LOG_LEVEL` configures the Controller and worker commands. The SDK follows
your application's Python logging configuration. Neither import nor the command
logging setup replaces the application's root logger handlers.

### Follow an execution

1. Find the `request_id` in the client's `Remote execution started` message.
2. Search the Controller log for that ID. RPC entries show the method and elapsed
   time. Errors include the original exception traceback; rejected RPCs show their
   gRPC status. Older clients without an ID receive a new one in the Controller log.
3. For Slurm execution, find `Execution scheduled` to map the request to its
   `job_id`, `execution_id`, `environment_id` and `job_dir`. Environment preparation
   separately records its build job ID and cache hits or misses.
4. Inspect `worker-<job_id>.out` or `env-<build_job_id>.out` in
   `NEST_SLURM_WORKDIR`. These filenames come from the deployed batch scripts.
   The worker and isolated user interpreter receive the request ID and log level
   explicitly, including when Apptainer clears the host environment.

Deploy the updated Python runtime **and** `scripts/worker.sbatch` together to
preserve the request ID and log level across Slurm workers. Existing three-argument
worker script invocations still work; their worker generates its own request ID.

Slurm polling records the initial state and later state changes at `INFO`, so an
unchanged queue state does not flood the log. A timeout records the job ID; it does
not cancel the job. A completed worker or RPC means a result was produced or
transported: a user function exception can still be returned as
[`errors.RemoteExecutionError`], with an error log at the worker and client.

Logs record metadata rather than function arguments, return values, serialized
payloads or the contents of `pyproject.toml` and `uv.lock`. Exception messages,
tracebacks and remote command error output can contain application-specific data.
