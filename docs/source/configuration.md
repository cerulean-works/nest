# Environment variables and defaults

## Client

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_CONTROLLER_ADDRESS` | `nest-controller.tail8e5ba1.ts.net:50051` | Host and port for each remote call. An empty value also uses the default. |

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

Slurm execution requires nonempty host, account, and work-directory values.
The Controller rejects missing settings and a nonabsolute work directory before
starting the listener.

## Docker Compose

The Compose deployment sets the container listener to `0.0.0.0:50051` and defaults
to Slurm mode. These additional variables configure Compose itself:

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_CONTROLLER_IMAGE` | `nest-controller:local` | Local tag assigned to the image built from this checkout. |
| `NEST_CONTROLLER_HOST_IP` | `127.0.0.1` | Host interface for the published gRPC port. Use the host's Tailscale IPv4 address for Tailnet clients. |
| `NEST_CONTROLLER_PORT` | `50051` | Published host port; the container always listens on port 50051. |
| `NEST_SSH_DIR` | `./ssh` | Existing host directory containing the deployment's SSH configuration, key, and verified host keys. |

See [Deploy with Docker Compose](docker-compose) for setup and operating commands.

The optional `compose.tailscale.yaml` also accepts:

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_TAILSCALE_AUTHKEY_FILE` | `./secrets/tailscale-authkey` | Private host file containing an official Tailscale Auth Key; mounted only into the sidecar as a Compose Secret. Dokploy: `../files/tailscale/authkey`. |
| `NEST_TAILSCALE_HOSTNAME` | `nest-controller` | Sidecar's name in your personal Tailnet. |
| `NEST_TAILSCALE_IMAGE` | `tailscale/tailscale:v1.104.4` | Official sidecar image, pinned to a version; may be overridden with a reviewed version or digest. |

The sidecar uses userspace SOCKS5 on `tailscale:1055`, a persistent state volume,
and `tag:nest-controller`. Configure the SSH alias using the supplied template
and restrict that tag to the Login Node's SSH port in your Tailnet policy.
These settings do not affect the default Compose or local Python development.

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
