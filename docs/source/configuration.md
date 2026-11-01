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
