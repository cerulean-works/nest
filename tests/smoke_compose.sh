#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
export COMPOSE_PROJECT_NAME=nest-controller-smoke
export NEST_CONTROLLER_IMAGE=${NEST_CONTROLLER_IMAGE:-nest-controller:test}
export NEST_EXECUTOR=local
export NEST_CONTROLLER_HOST_IP=127.0.0.1
export NEST_CONTROLLER_PORT=0
export NEST_SSH_DIR="$fixture"

cleanup() {
    docker compose logs --no-color controller || true
    docker compose down --timeout 20 --volumes || true
    rm -rf "$fixture"
}
trap cleanup EXIT

# Host-side metadata; GNU and BSD stat disagree on flags.
host_metadata() {
    python3 -c 'import os, sys; s = os.stat(sys.argv[1]); print(f"{s.st_mode & 0o777:o}:{s.st_uid}:{s.st_gid}")' "$1"
}

ssh-keygen -q -t ed25519 -N '' -C '' -f "$fixture/id_ed25519"
cat > "$fixture/config" <<'EOF'
Host fixture-hpc
    HostName fixture.example
    User test-user
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    StrictHostKeyChecking yes
EOF
printf 'fixture.example %s\n' "$(cat "$fixture/id_ed25519.pub")" > "$fixture/known_hosts"
original_key_permissions=$(host_metadata "$fixture/id_ed25519")

docker compose config --quiet
docker compose up -d --no-build --pull never --wait --wait-timeout 60
test "$(docker run --rm --volume "$fixture:/run/nest-ssh:ro" "$NEST_CONTROLLER_IMAGE" id -u)" = 10001
docker compose exec -T --user nest controller sh -c \
    'test "$(stat -c %a ~/.ssh)" = 700 && test "$(stat -c %a ~/.ssh/id_ed25519)" = 600 && ssh-keygen -y -f ~/.ssh/id_ed25519 >/dev/null'
docker compose exec -T --user nest controller ssh -G fixture-hpc > "$fixture/resolved-config"
grep -q '^hostname fixture.example$' "$fixture/resolved-config"
test "$(host_metadata "$fixture/id_ed25519")" = "$original_key_permissions"

address=$(docker compose port controller 50051)
docker run --rm -i --network host --env "NEST_CONTROLLER_ADDRESS=$address" "$NEST_CONTROLLER_IMAGE" python - <<'PY'
import nest
from nest.errors import RemoteExecutionError

@nest.remote()
def double(value):
    return value * 2

assert double.remote(21) == 42

@nest.remote()
def fail():
    raise ValueError("compose worker failure")

try:
    fail.remote()
except RemoteExecutionError as error:
    assert error.remote_type == "builtins.ValueError"
    assert error.remote_message == "compose worker failure"
else:
    raise AssertionError("Remote worker error was not returned")
print("PASS: Compose Controller executes calls and returns remote errors")
PY

docker compose stop --timeout 20
container=$(docker compose ps --all --quiet controller)
test "$(docker inspect --format '{{.State.ExitCode}}' "$container")" = 0
