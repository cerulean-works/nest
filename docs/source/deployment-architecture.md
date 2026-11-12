# Deployment validation and roadmap

This page records how the Docker Compose and Tailscale deployments are
validated, and the changes still needed to align them with Nest's
environment-variable-first configuration. For setup steps, see
[Deploy with Docker Compose](docker-compose) and
[Share a Controller over Tailscale](tailscale-shared).

## What is checked automatically

Every pull request runs these checks in CI. None needs credentials, a Tailnet,
HPC access, or Slurm, and none submits a job.

| Check | Command | What it proves |
| --- | --- | --- |
| Compose rendering | `python tests/check_shared_compose.py` | All four Compose files render; the Controller definitions pass the same `NEST_*` settings, never receive the Auth Key, and mount SSH files read-only; the shared deployment publishes no host ports. |
| Controller image | `docker run … -m unittest discover -s /tests` | RPCs, shutdown, the SSH SOCKS5 template, and the no-job gRPC probe work inside the image. |
| Local Compose deployment | `bash tests/smoke_compose.sh` | `compose.yaml` builds, starts healthy, copies SSH files privately without changing the host copy, executes calls, and stops cleanly. |
| Controller-to-Login-Node SSH | `bash tests/smoke_tailnet_ssh.sh` | See below. |
| Repository hygiene | `uv run pytest tests/test_deployment_hygiene.py` | No private keys, Tailscale keys, or tokens are tracked; credential locations are ignored by Git and excluded from the image; no tracked file disables SSH host-key verification. |

### The SSH path test

`tests/smoke_tailnet_ssh.sh` starts the real `compose.tailscale.yaml` Controller
in Slurm mode with an SSH directory made from `docker/ssh_config.tailscale.example`
(only its placeholders replaced). The test override
`tests/compose.tailnet-fixture.yaml` makes two substitutions:

- The `tailscale` sidecar becomes a small SOCKS5 proxy on the same `tailscale:1055`
  address, standing in for userspace `tailscaled`.
- A disposable OpenSSH server plays the Login Node. It is on an internal network
  that only the proxy joins, as only `tailscaled` can reach the Tailnet. It has a
  stock `sshd` and an authorized key, and no Slurm.

The test then checks that:

1. The Controller cannot resolve the Login Node directly.
2. The SSH alias uses the SOCKS5 proxy, `BatchMode`, and strict host-key checking.
3. `ssh hpc 'hostname && id -un'` and `SlurmBackend`'s own SSH call reach the
   Login Node through the proxy as the `nest` user.
4. An empty or wrong `known_hosts` is refused with
   `Host key verification failed`, before authentication.
5. The Slurm-mode Controller answers `scripts/verify-grpc.py`, which never
   executes code or contacts Slurm.

## Validation record (2026-11-12)

Validated on macOS with Docker Engine 29.8.2 (OrbStack), against `main` at
`ed5e825`, for ticket A02.

| Acceptance criterion | Verified here | Still needs a real deployment |
| --- | --- | --- |
| Docker Compose deployment works locally | `docker build`, `smoke_compose.sh` and the in-image tests pass. `smoke_compose.sh` used GNU `stat` and failed on macOS; it is now portable. | — |
| Controller-to-HPC SSH connectivity | `smoke_tailnet_ssh.sh` passes through the real Compose Controller, SSH template, and a SOCKS5 sidecar stand-in. | `bash scripts/verify-tailnet.sh` with a real Auth Key, Tailnet policy, and Login Node. |
| No change to the HPC side | The fixture Login Node is a stock `sshd` with an authorized key. Nothing Tailscale- or Nest-specific runs there. | The Controller's public key must already be authorized for the HPC user, as for any SSH deployment. |
| Host-key verification stays enabled | Template, `verify-tailnet.sh`, and the SSH path test require `StrictHostKeyChecking yes`; unknown and changed keys are rejected; the hygiene test forbids weakening it anywhere in the repository. | The empty-`known_hosts` negative check in [Share a Controller](tailscale-shared#start-migrate-state-and-verify) on the real host. |
| No credentials are committed | The hygiene test scans tracked files and paths; `.gitignore` and `.dockerignore` are checked. | Keep Dokploy File Mounts and `secrets/` outside Git. |
| Connectivity tests submit no Slurm jobs | `verify-tailnet.sh` runs only `hostname && id -un`; `verify-grpc.py` stops before execution; the fixture Login Node has no Slurm at all. | — |

Not verified here: Tailscale enrollment and tags, Tailnet grants, Node Sharing,
Serve, Dokploy, and a real Login Node. These need the operator's Tailnet and HPC
credentials; follow the checks in the deployment guides.

## Roadmap: environment-variable-first deployment

The Controller reads its configuration only from `NEST_*` environment variables.
The deployment mostly follows that. These are the remaining gaps, most important first.

### Expose the hardcoded Slurm defaults (done)

`NEST_SLURM_PARTITION` and `NEST_SLURM_TIME` (and the per-profile
`NEST_PROVIDER_<ID>_PARTITION` / `_TIME`) default to `dev` and `00:05:00`, are
validated at startup, and pass through every Compose file. The `--time` option
overrides the `#SBATCH --time` line still in `scripts/worker.sbatch`.

### Keep the Controller definitions from drifting

The Controller service is defined three times: in `compose.yaml`,
`compose-nop.yaml`, and `compose.tailscale-shared.yaml`.
`compose.tailscale.yaml` extends `compose.yaml`. The shared file is self-contained
because Dokploy rewrites YAML before Compose sees it. Each file passes an explicit
allowlist of `NEST_*` variables, so a new setting must be added to all of them.
`NEST_LOG_LEVEL` had been left out of all of them; it is now passed through, and
`tests/check_shared_compose.py` fails if their settings differ. Until a single
definition is possible:

- Add every new Controller variable to all Compose files and to
  `CONTROLLER_SETTINGS` in the check. The `NEST_PROVIDER_<ID>_*` family cannot be
  listed in advance, so multi-provider deployments add those in an override file.
- Decide what `compose-nop.yaml` is for. It is the default deployment without a
  published port, but nothing documents or runs it. Document it as the
  no-host-port variant, or remove it.

Keep the explicit allowlist rather than `env_file:`. It stops unrelated host or
Dokploy variables from reaching the Controller.

### Make SSH settings explicit (Mode B done)

`NEST_SLURM_HOST` names an alias in the mounted `ssh/config` (Mode A). The host,
user, port, proxy, and host-key database are in that file, not in the environment.
Mode B, managed SSH ([configuration](configuration)), now builds the whole SSH
command line from the environment with `-F none` and `StrictHostKeyChecking=yes`,
and accepts `NEST_SSH_KNOWN_HOSTS` and `NEST_SSH_IDENTITY_FILE` paths or inline
values. Remaining for Mode A: have `SlurmBackend` pass
`-o StrictHostKeyChecking=yes` too. Today a permissive `ssh/config` could weaken
verification, and the hygiene test only covers files in this repository. This
changes behavior for non-container hosts that rely on `accept-new`, so it needs
its own release note.

### Report SSH readiness

The image healthcheck proves only that the gRPC listener is up. It now reads its
port from `NEST_CONTROLLER_BIND`. A Controller with a broken SSH path stays
healthy until the first call fails. Options:

- A startup preflight, enabled by a variable such as `NEST_SSH_PREFLIGHT=1`, that
  runs `true` over SSH and logs a clear error.
- A standard `grpc.health.v1` service. `scripts/verify-grpc.py` then would not
  need to rely on rejecting an empty environment.

### Remove the Tailnet-specific client default (done)

The SDK's default `NEST_CONTROLLER_ADDRESS` is now `127.0.0.1:50051`. Team users set
the Tailnet address explicitly; nothing in the SDK or Mode B depends on Tailscale.

### Check sidecar preferences in both modes

`TS_AUTH_ONCE=true` does not reapply `TS_EXTRA_ARGS` to existing state.
`verify-tailnet.sh --shared` checks the persisted preferences, but the outbound
mode does not check that `ShieldsUp` is still `true`. Add that check. A plain
`compose.yaml` deployment on a host already running Tailscale has no script at
all; give it a `--host` mode that runs only the SSH checks.

### Publish versioned images

Each host builds the image from its checkout. `NEST_CONTROLLER_IMAGE` already
allows a prebuilt image. Publishing tagged images to GHCR and pinning by digest
would make deployments reproducible and remove the build from the host.

### Authenticate callers

gRPC is plaintext unless `NEST_TLS_CERT_FILE` and `NEST_TLS_KEY_FILE` are set
(clients then set `NEST_CONTROLLER_TLS` or `NEST_CONTROLLER_TLS_CA_FILE`). Without
TLS it is protected only by the network, such as Tailscale, between client and host.
Nest has no application-level authentication, and every caller uses one HPC SSH
identity (see [Security](security)). `NEST_AUTH_TOKEN_FILE` and `NEST_DATABASE_URL`
are reserved for this work and rejected at startup until it lands. A multi-user
deployment needs per-caller authentication and authorization, and eventually
per-user HPC identities.
