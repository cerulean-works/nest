#!/usr/bin/env bash
# Controller-to-Login-Node SSH through the Tailnet Compose deployment, offline.
#
# Runs the real compose.tailscale.yaml Controller and docker/ssh_config.tailscale.example
# against a disposable sshd that only a SOCKS5 stand-in for the sidecar can reach.
# Needs Docker and a built image, but no Auth Key, Tailnet, HPC access, or Slurm:
# the fixture Login Node has no Slurm, so no job can be submitted.
set -euo pipefail

cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
ssh_dir="$fixture/ssh"
login_dir="$fixture/login"
mkdir -m 700 "$ssh_dir" "$login_dir"

export COMPOSE_PROJECT_NAME=nest-tailnet-ssh-smoke
export NEST_CONTROLLER_IMAGE=${NEST_CONTROLLER_IMAGE:-nest-controller:test}
export NEST_EXECUTOR=slurm
export NEST_SLURM_HOST=hpc
export NEST_SLURM_ACCOUNT=fixture-account
export NEST_SLURM_WORKDIR=/home/hpc-user/nest
export NEST_CONTROLLER_HOST_IP=127.0.0.1
export NEST_CONTROLLER_PORT=0
export NEST_SSH_DIR="$ssh_dir"
export NEST_TAILSCALE_AUTHKEY_FILE="$fixture/unused-authkey"
export NEST_LOGIN_FIXTURE_DIR="$login_dir"
compose=(docker compose -f compose.tailscale.yaml -f tests/compose.tailnet-fixture.yaml)

cleanup() {
    "${compose[@]}" logs --no-color || true
    "${compose[@]}" down --timeout 20 --volumes || true
    rm -rf "$fixture"
}
trap cleanup EXIT

# Compose requires the secret file to exist; the stand-in proxy never reads it.
printf 'placeholder, not a credential\n' > "$NEST_TAILSCALE_AUTHKEY_FILE"

ssh-keygen -q -t ed25519 -N '' -C '' -f "$ssh_dir/id_ed25519"
ssh-keygen -q -t ed25519 -N '' -C '' -f "$login_dir/ssh_host_ed25519_key"
cp "$ssh_dir/id_ed25519.pub" "$login_dir/authorized_keys"
rm "$ssh_dir/id_ed25519.pub"
chmod 644 "$login_dir"/*.pub "$login_dir/authorized_keys"
# The fixture container copies these as root; the host key never leaves the fixture.
chmod 644 "$login_dir/ssh_host_ed25519_key"
printf 'login %s\n' "$(cut -d' ' -f1,2 "$login_dir/ssh_host_ed25519_key.pub")" > "$ssh_dir/known_hosts"
# Use the deployment template as documented, changing only its placeholders.
sed -e 's/login\.YOUR-TAILNET\.ts\.net/login/' -e 's/YOUR-HPC-USER/hpc-user/' \
    docker/ssh_config.tailscale.example > "$ssh_dir/config"

"${compose[@]}" config --quiet
"${compose[@]}" build login
"${compose[@]}" up -d --no-build --wait --wait-timeout 90

controller() { "${compose[@]}" exec -T --user nest controller "$@"; }

# Only the proxy can reach the Login Node, as in the Tailnet deployment.
controller python -c '
import socket, sys
try:
    socket.getaddrinfo("login", 22)
except socket.gaierror:
    print("PASS: Controller cannot resolve the Login Node without the proxy")
else:
    sys.exit("FAIL: Controller reached the Login Node network directly")
'

# The same alias checks as scripts/verify-tailnet.sh.
controller sh -c 'ssh -G "$NEST_SLURM_HOST"' > "$fixture/resolved"
grep -qx 'proxycommand /usr/bin/nc -X 5 -x tailscale:1055 %h %p' "$fixture/resolved"
grep -Eqx 'stricthostkeychecking (yes|true)' "$fixture/resolved"
grep -qx 'batchmode yes' "$fixture/resolved"

for attempt in $(seq 1 20); do
    if output=$(controller sh -c 'ssh "$NEST_SLURM_HOST" "hostname && id -un"' 2>&1); then
        break
    fi
    test "$attempt" -lt 20 || { echo "$output"; exit 1; }
    sleep 1
done
test "$output" = "$(printf 'login-node-fixture\nhpc-user')" || { echo "$output"; exit 1; }
echo "PASS: SSH reached the Login Node through SOCKS5 with a verified host key"

# Nest's own SSH call path, configured only from the Controller's environment.
controller python - <<'PY'
import os

from nest.backends.slurm import SlurmBackend, SlurmConfig

config = SlurmConfig(ssh_host=os.environ["NEST_SLURM_HOST"], account=os.environ["NEST_SLURM_ACCOUNT"])
assert SlurmBackend(config)._run_remote("hostname") == "login-node-fixture"
print("PASS: SlurmBackend SSH reached the Login Node")
PY

"${compose[@]}" logs --no-color tailscale | grep -q 'CONNECT login:22'

# Host-key verification stays enforced: an unknown or changed key is refused
# before authentication. Only disposable copies in the container's tmpfs change.
controller sh -eu -c '
    : > /tmp/empty_known_hosts
    ssh-keygen -q -t ed25519 -N "" -C "" -f /tmp/impostor
    printf "login %s\n" "$(cut -d" " -f1,2 /tmp/impostor.pub)" > /tmp/wrong_known_hosts
    for database in /tmp/empty_known_hosts /tmp/wrong_known_hosts; do
        if ssh -o UserKnownHostsFile="$database" "$NEST_SLURM_HOST" true 2>/tmp/ssh.err; then
            echo "FAIL: SSH accepted an unverified host key ($database)"
            exit 1
        fi
        grep -q "Host key verification failed" /tmp/ssh.err || { cat /tmp/ssh.err; exit 1; }
    done
'
echo "PASS: unknown and changed Login Node host keys are rejected"

# The Slurm-mode Controller answers the no-job RPC probe.
controller python - controller:50051 < scripts/verify-grpc.py
