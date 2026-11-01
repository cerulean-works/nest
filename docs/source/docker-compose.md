# Deploy with Docker Compose

Clone Nest and run the Controller with Docker Engine and Docker Compose v2.20
or newer. The deployment host needs network access to the HPC login node and a working
noninteractive SSH identity. Python, uv, Slurm, and Apptainer do not need to be
installed on the Controller host.

The HPC runtime, batch scripts, and Apptainer image must already be deployed as
described in [Deploy a Slurm Controller](deploy#hpc-work-directory). Compose
deploys the Controller service; its work directory remains on the HPC cluster.

## Clone and configure

Clone the repository and prepare settings and SSH files next to `compose.yaml`:

```bash
git clone https://github.com/cerulean-works/nest.git
cd nest
cp .env.example .env
mkdir -m 700 ssh
```

Edit `.env` for your cluster:

```text
NEST_EXECUTOR=slurm
NEST_SLURM_HOST=hpc
NEST_SLURM_ACCOUNT=your-slurm-account
NEST_SLURM_WORKDIR=/home/your-user/nest
NEST_SSH_DIR=./ssh
NEST_CONTROLLER_HOST_IP=127.0.0.1
NEST_CONTROLLER_PORT=50051
```

`NEST_SLURM_WORKDIR` is an absolute path on the HPC cluster, not a local Docker
volume. The Controller rejects missing Slurm settings and relative work paths
at startup. The `.env` file is a convenience: the same variables may be exported
in the shell or supplied directly in `compose.yaml`.

Compose builds the Controller image from this checkout and tags it
`nest-controller:local`. No private image registry or registry login is needed.
The Docker build downloads its base images and the locked Python dependencies.
For a fixed deployment, check out a commit or release containing the Docker
deployment files before building.

## Provide SSH access

Prepare a self-contained `ssh/` directory:

```text
ssh/
├── config
├── id_ed25519
└── known_hosts
```

Use a dedicated private key authorized on the HPC login node. The key must work
without an interactive password or passphrase prompt. Put the login node's
verified host keys in `known_hosts`, using the cluster administrator's trusted
keys or entries you have already verified on the host.

Example `ssh/config`:

```ssh-config
Host hpc
    HostName login.example.org
    User your-hpc-user
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    BatchMode yes
    StrictHostKeyChecking yes
```

`NEST_SLURM_HOST=hpc` selects this alias. Use paths inside the container's
`/home/nest/.ssh` directory for identity and included configuration files.
Include any jump-host configuration, keys, and verified host keys in the same
directory. Host-specific SSH agent sockets and files outside this directory
are not mounted automatically.

Compose mounts this directory read-only. Startup copies its contents to a
private in-memory directory, sets directory permissions to `700` and file
permissions to `600`, and runs the Controller as UID `10001` (`nest`). It does
not modify the host's credentials or require matching host and container UIDs.
Restart the service after updating keys or SSH settings.

## Select who can connect

The default host binding is `127.0.0.1:50051`. For the team's Tailscale deployment,
run `tailscale ip -4` on the Docker host and put that IPv4 address into
`NEST_CONTROLLER_HOST_IP` in `.env`. The host must already be connected to the
authorized Tailnet; Compose does not install or authenticate Tailscale.

The listener inside the container binds `0.0.0.0:50051`. The host address and
published port control client access. Keep the existing Tailscale Grants / ACLs
and [trusted-user access model](security) in place.

## Start and verify

```bash
docker compose up -d --build --wait
docker compose ps
docker compose logs --tail 100 controller
```

The health check verifies that the gRPC listener is ready. It does not contact
Slurm. Verify SSH separately:

```bash
docker compose exec --user nest controller ssh -o BatchMode=yes hpc 'command -v sbatch && command -v squeue && command -v sacct'
```

Then run the [quickstart](quickstart) from a trusted client. Set
`NEST_CONTROLLER_ADDRESS` to the Docker host's selected address and published
port. A successful remote function call verifies the full HPC execution path.

The container restarts automatically unless you stop it explicitly. Its root
filesystem is read-only, with temporary files and the private SSH copy in
memory. Logs rotate at 10 MB with three files retained.

## Update or stop

To update a deployment that follows `main`, pull the source and rebuild:

```bash
git pull --ff-only
docker compose up -d --build --wait
```

To reload SSH configuration:

```bash
docker compose restart controller
```

To stop and remove the Controller:

```bash
docker compose down
```

SIGTERM stops new RPCs and gives active calls ten seconds to finish. Compose
allows twenty seconds before forcing the container to stop. Stopping or
replacing the Controller does not cancel existing Slurm jobs or remove files
from the HPC cluster.

## Test without HPC

Keep the `ssh/` directory present (it can be empty), set `NEST_EXECUTOR=local`,
and start the same Compose service. Calls then run in subprocesses inside the
Controller container. Install additional dependencies in a custom image if the
function needs packages beyond Nest's runtime dependencies; `project=` does not
install environments in local mode.

## Build and test

The `Controller container` workflow builds the image, runs Controller integration
tests inside it, and checks a complete Compose deployment on pull requests and
pushes to `main`. Run the same checks locally with:

```bash
docker build -t nest-controller:test .
docker run --rm --volume "$PWD/tests:/tests:ro" --entrypoint python nest-controller:test -m unittest discover -s /tests -v
bash tests/smoke_compose.sh
```

The build installs the non-development dependencies from `uv.lock`. The image
contains the SSH client and Controller package; it never includes `.env` or
deployment SSH credentials.
