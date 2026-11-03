# Deploy with Docker Compose

Clone Nest and run the Controller with Docker Engine and Docker Compose v2.20
or newer. The Controller needs network access to the HPC login node and a working
noninteractive SSH identity. For a Login Node on your personal Tailscale Tailnet,
use the [optional outbound Tailscale sidecar](#personal-tailnet-access-with-a-sidecar).
For members using separate Tailnets, use the
[shared Controller deployment](tailscale-shared), including private inbound
TCP forwarding, minimal grants, and Dokploy File Mount instructions.
Python, uv, Slurm, and Apptainer do not need to be
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
authorized Tailnet; the default `compose.yaml` does not install or authenticate
Tailscale. The optional sidecar below provides outbound HPC access independently
of this inbound client listener.

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

For a network-only check, use `ssh hpc 'hostname && id -un'` instead. This does
not submit a Slurm job. The quickstart below executes a real job in Slurm mode
and is not needed to validate Tailnet connectivity.

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

## Personal Tailnet access with a sidecar

Use `compose.tailscale.yaml` **instead of** `compose.yaml` for this deployment.
It inherits the existing Controller using Compose `extends`, adds the official
Tailscale container, and waits for its health check before starting Nest. Plain
`docker compose` and non-Docker local development remain unchanged.

```text
Nest / OpenSSH -> nc SOCKS5 -> tailscale:1055 -> personal Tailnet -> Login Node SSH
```

This follows the official [Docker guide](https://tailscale.com/docs/features/containers/docker/how-to/connect-docker-container)
with [userspace networking](https://tailscale.com/docs/concepts/userspace-networking)
and the documented [Docker parameters](https://tailscale.com/docs/features/containers/docker/docker-params).
Userspace mode needs no `/dev/net/tun`, `NET_ADMIN`, `NET_RAW`, host networking,
or shared container network namespace. SSH's `ProxyCommand` uses OpenBSD `nc`
to send the target name through SOCKS5, so Docker does not need to resolve the
Login Node's MagicDNS name. `ALL_PROXY` alone would not configure OpenSSH.
SlurmBackend continues to use the same system SSH commands and alias.

This is ordinary SSH over Tailscale, using the Login Node's already configured
SSH endpoint and key. It does not enable the separate Tailscale SSH server
feature, change the Login Node, or use a custom control server.

### Restrict the Sidecar's Tailnet identity

Before enrolling Nest, define `tag:nest-controller` in your **personal Tailnet's**
Access controls and scope it to the Login Node's actual personal Tailscale IPv4
address and SSH TCP port. You do not need to tag or reconfigure the Login Node.
For example, adapt this fragment in your existing policy (replace every
placeholder; use the actual SSH port if different from 22):

```json
{
  "tagOwners": {
    "tag:nest-controller": ["autogroup:admin"]
  },
  "hosts": {
    "nest-hpc-login": "REPLACE_WITH_PERSONAL_TAILSCALE_IPV4",
    "nest-other-device": "REPLACE_WITH_ANOTHER_PERSONAL_TAILSCALE_IPV4"
  },
  "grants": [
    {
      "src": ["tag:nest-controller"],
      "dst": ["nest-hpc-login"],
      "ip": ["tcp:22"]
    }
  ],
  "tests": [
    {
      "src": "tag:nest-controller",
      "proto": "tcp",
      "accept": ["nest-hpc-login:22"],
      "deny": ["nest-hpc-login:80", "nest-other-device:22"]
    }
  ]
}
```

Grants and legacy ACLs are **additive**: adding this narrow grant does not
restrict an existing `* -> *` allow rule. Review and narrow any broader rules
that match this tag, and run the policy tests in the admin console before
enrollment. The `deny` entries above are test assertions, not firewall deny
rules. See [Grants syntax](https://tailscale.com/docs/reference/syntax/grants)
and [policy tests](https://tailscale.com/docs/reference/syntax/policy-file#tests).
These policy changes are operator steps; the repository does not apply them.

### Provide an Auth Key and SSH files

Generate an official **Auth Key** in your personal Tailscale admin console,
tagged `tag:nest-controller`, with **Ephemeral off**. A single-use, short-lived
key is suitable for this persistent node; pre-approve it if your Tailnet
requires device approval. Do not use an API key or OAuth client secret here.

Create a private directory and write the key into `secrets/tailscale-authkey`
using a secure editor or secret manager. Do not put the key in a command line,
Compose YAML, `.env`, or Git:

```bash
mkdir -m 700 secrets
install -m 600 /dev/null secrets/tailscale-authkey
# Populate secrets/tailscale-authkey securely before starting the service.
```

The Compose Secret mounts the file only into the sidecar. `TS_AUTHKEY` contains
the path `file:/run/secrets/tailscale_authkey`, supported by the official
[container entrypoint](https://github.com/tailscale/tailscale/blob/v1.104.4/cmd/containerboot/main.go).
Compose file secrets are read-only host files, not an encrypted secret store;
protect them and any backups with host filesystem permissions. Nest itself
receives neither the Auth Key nor the Tailscale state.

Copy the SSH template into the already documented SSH directory:

```bash
cp docker/ssh_config.tailscale.example ssh/config
```

Replace `HostName` with the Login Node's **personal Tailnet IP or full MagicDNS
name**, and set its existing SSH user and port. Keep `NEST_SLURM_HOST=hpc` to
select this alias. Do not use an address from another Tailnet. The relevant
connection setting is:

```ssh-config
ProxyCommand /usr/bin/nc -X 5 -x tailscale:1055 %h %p
```

Keep the dedicated `id_ed25519` and verified `known_hosts` in `NEST_SSH_DIR` as
before. For a new Tailnet hostname/IP, the trusted host key must be associated
with that target (use `[target]:port` for a nonstandard SSH port). Verify its
fingerprint against a trusted existing record or administrator-provided key.
An unauthenticated `ssh-keyscan` result alone is not verification. Keep
`StrictHostKeyChecking yes`; never use `/dev/null` as the host-key database.

### Start and check, without submitting a job

After completing the policy, Auth Key, SSH configuration, and existing Slurm
settings, run these commands on the deployment host:

```bash
docker compose -f compose.tailscale.yaml config --quiet
docker compose -f compose.tailscale.yaml up -d --build --wait
bash scripts/verify-tailnet.sh
```

The check confirms a running Tailscale backend with a Tailnet IP and the
expected tag, checks the configured SSH proxy and host-key verification, then
runs only `hostname && id -un` over SSH as the same container user Nest uses.
It does not call Nest RPCs or submit a job. Also confirm `nest-controller`
appears with the expected tag in your personal Tailnet's Machines page.

For diagnosis:

```bash
docker compose -f compose.tailscale.yaml logs --tail 100 tailscale
docker compose -f compose.tailscale.yaml exec tailscale tailscale status
docker compose -f compose.tailscale.yaml exec tailscale tailscale ip -4
docker compose -f compose.tailscale.yaml exec --user nest controller ssh -G hpc
docker compose -f compose.tailscale.yaml exec --user nest controller ssh -v hpc 'hostname && id -un'
```

The sidecar health check confirms it has a Tailnet IP, not Login Node reachability
or SSH authorization. Use the SSH check to prove the complete path. Normal
container `ping` does not use userspace networking. A proxy connection error
usually means the sidecar is unavailable; an SSH timeout may mean the wrong
target/port, an offline Login Node, or a Tailnet policy denial. Host-key errors
require trusted key verification, while `Permission denied (publickey)` means
the SSH identity/user is wrong. Do not disable host-key verification to fix it.

### Persist and operate

The `tailscale-state` named volume and `TS_AUTH_ONCE=true` preserve this node's
identity on restart and avoid consuming a fresh Auth Key on every boot. Treat
the state as sensitive device credentials, restrict its backups, and never
copy it between simultaneous instances. Keep the same Compose project name
across redeployments so Compose reuses the same volume. Losing the state,
logging out, or node-key expiry requires a fresh valid Auth Key; replacing a
key file alone does not reauthenticate an already logged-in node. Check node
expiry settings in the admin console as part of operations.

For restart verification, compare `tailscale ip -4` and the machine identity
before and after `docker compose -f compose.tailscale.yaml restart tailscale`,
then rerun the check script. To reload SSH files, restart `controller` using
the same `-f compose.tailscale.yaml` option.

Stop this deployment with `docker compose -f compose.tailscale.yaml down`.
Do not add `--volumes` unless you intend to discard the Tailscale identity.
Always use this file for lifecycle commands; plain `docker compose` selects
the original deployment without the sidecar.

The proxy has no published host port and lives on a project-scoped bridge.
Only trusted services should share its Docker networks: SOCKS5 has no client
authentication. `--shields-up` blocks incoming Tailnet connections to the
sidecar; it does not limit outbound access, which requires the grants above.
No subnet routes, exit node, Serve, Funnel, or Tailnet SSH server is enabled.
The gRPC listener's existing host binding remains independent of this outbound
connection; the sidecar does not publish gRPC into your personal Tailnet.

### Dokploy

Use the existing repository deployment with **Compose Type: Docker Compose**
(not Docker Stack) and set **Compose Path: `./compose.tailscale.yaml`**. The
inherited `compose.yaml` must remain in the checkout. No custom deployment
command or replacement Dockerfile is required.

In Dokploy's Environment settings, keep the existing Nest variables and set:

```text
NEST_SSH_DIR=../files/nest-ssh
NEST_TAILSCALE_AUTHKEY_FILE=../files/tailscale/authkey
NEST_TAILSCALE_HOSTNAME=nest-controller
```

Provision those files securely on the deployment server in the persistent
`../files` location (relative to the Compose checkout), or through Dokploy's
File Mount facilities. Keep the entire SSH directory mounted at `/run/nest-ssh`
and the Auth Key provided as the Compose Secret; avoid overlapping mounts.
Do not store credentials under the Git checkout, which Dokploy replaces during
redeployments. The SSH directory and Auth Key file must exist before startup.
Limit access to the Dokploy UI and these files to deployment administrators.

Keep the same Dokploy app/project name so the named state volume persists.
Enable **Isolated Deployments**, keep other applications out of this project's
networks, and configure no Domains/Traefik routes or host ports for `tailscale`.
Dokploy adds its isolated network to services; review its rendered Compose to
ensure the proxy is not attached to a network shared with unrelated apps.
The gRPC service retains its existing private host binding and access model.
See Dokploy's [Compose configuration and persistent files](https://docs.dokploy.com/docs/core/docker-compose)
and [Isolated Deployments](https://docs.dokploy.com/docs/core/docker-compose/utilities).

Run the same checks using Dokploy's service terminals or the deployed checkout.
For the script, pass `-p YOUR_DOKPLOY_APP_NAME` if Dokploy uses a different Compose
project name. Do not run `up` manually alongside a Dokploy-managed stack.

## Build and test

The `Controller container` workflow builds the image, runs Controller integration
tests inside it, and checks a complete Compose deployment on pull requests and
pushes to `main`. Run the same checks locally with:

```bash
docker build -t nest-controller:test .
docker run --rm --volume "$PWD/tests:/tests:ro" --volume "$PWD/docker:/docker:ro" --volume "$PWD/scripts:/scripts:ro" --entrypoint python nest-controller:test -m unittest discover -s /tests -v
bash tests/smoke_compose.sh
```

The build installs the non-development dependencies from `uv.lock`. The image
contains the SSH client and Controller package; it never includes `.env` or
deployment SSH credentials.

The offline proxy tests use a local SOCKS5 fixture to verify that OpenSSH sends
the destination hostname and SSH stream through the proxy without local DNS,
and that the template retains strict host-key verification. They need no Auth
Key, Login Node, or Slurm access. Actual Tailnet enrollment and SSH authentication
must still be checked with `scripts/verify-tailnet.sh` on the deployment host.
