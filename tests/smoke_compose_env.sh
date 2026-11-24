#!/usr/bin/env bash
# Environment-only deployment (compose.dokploy.yaml): every credential comes from a
# Dokploy-style .env file and nothing is bind-mounted except the repository's own
# reviewed serve.json. Checks PostgreSQL with a URL-hostile password, the migrate
# profile, runtime-generated SSH and TLS files, Google sign-in from a direct
# secret, restarts, recreation, and that Tailscale and PostgreSQL state survive.
#
# Needs Docker, the image in NEST_CONTROLLER_IMAGE and openssl/ssh-keygen. No
# Tailnet or HPC access: the sidecar never enrolls, so only its wrapper and state
# volume are checked.
set -euo pipefail

cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
export COMPOSE_PROJECT_NAME=nest-env-smoke
export NEST_CONTROLLER_IMAGE=${NEST_CONTROLLER_IMAGE:-nest-controller:test}
compose=(docker compose -f compose.dokploy.yaml --env-file "$fixture/.env" --profile local-db --profile migrate)

cleanup() {
    "${compose[@]}" logs --no-color controller migrate || true
    "${compose[@]}" down --timeout 20 --volumes || true
    rm -rf "$fixture"
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Everything an operator would paste into the Dokploy Environment panel.
password='p@ss:w/rd?#%&= "\;x$HOME'
ssh-keygen -q -t ed25519 -N '' -C '' -f "$fixture/id_ed25519"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
    -addext subjectAltName=DNS:localhost -keyout "$fixture/tls.key" -out "$fixture/tls.crt" 2>/dev/null
google_secret="GOCSPX-env-smoke-$(openssl rand -hex 8)"
# One line per value: single quotes keep `$`, quotes and backslashes literal; `\n`
# stands for a line break in multiline values. TLS uses a double-quoted multiline
# value instead, the other form Compose accepts.
{
    printf "NEST_DATABASE_URL='postgresql://nest@postgres:5432/nest'\n"
    printf "NEST_DATABASE_PASSWORD='%s'\n" "$password"
    printf "NEST_DATABASE_REQUIRED='true'\n"
    printf "NEST_EXECUTOR='slurm'\n"
    printf "NEST_TAILSCALE_AUTHKEY=''\n"
    printf "NEST_TAILSCALE_SERVE_CONFIG='%s'\n" '{"TCP":{"50051":{"TCPForward":"controller:50051"}}}'
    printf "NEST_SSH_CONFIG='%s'\n" 'Host fixture-hpc\n    HostName fixture.example\n    User svc-nest\n    IdentityFile ~/.ssh/id_nest\n    IdentitiesOnly yes\n    BatchMode yes\n    StrictHostKeyChecking yes'
    printf "NEST_SSH_PRIVATE_KEY='%s'\n" "$(awk '{printf "%s\\n", $0}' "$fixture/id_ed25519")"
    printf "NEST_SSH_KNOWN_HOSTS_DATA='fixture.example %s'\n" "$(cat "$fixture/id_ed25519.pub")"
    printf "NEST_PLATFORMS_ENV='%s'\n" 'NEST_PLATFORMS=lab\nNEST_PLATFORM_LAB_NAME=Lab cluster\nNEST_PLATFORM_LAB_OPERATOR=example-university\nNEST_PLATFORM_LAB_LOGIN_NODES=fixture-hpc\nNEST_PLATFORM_LAB_SSH_USER=svc-nest\nNEST_PLATFORM_LAB_ACCOUNT=lab\nNEST_PLATFORM_LAB_WORKDIR=/home/svc-nest/nest'
    printf 'NEST_TLS_CERT="%s"\n' "$(cat "$fixture/tls.crt")"
    printf "NEST_TLS_KEY='%s'\n" "$(awk '{printf "%s\\n", $0}' "$fixture/tls.key")"
    printf "NEST_GOOGLE_CLIENT_ID='1234-env-smoke.apps.googleusercontent.com'\n"
    printf "NEST_GOOGLE_CLIENT_SECRET='%s'\n" "$google_secret"
    printf "NEST_GOOGLE_REDIRECT_URL='https://nest.example.com/auth/google/callback'\n"
} > "$fixture/.env"
chmod 600 "$fixture/.env"

"${compose[@]}" config --quiet
rendered=$("${compose[@]}" config --format json)
python3 - "$rendered" <<'PY'
import json, sys
config = json.loads(sys.argv[1])
controller = config["services"]["controller"]
assert not controller.get("volumes"), "no host mounts for the Controller"
assert not any(s.get("ports") for s in config["services"].values()), "no host ports"
print("PASS: rendered environment-only deployment has no Controller mounts or host ports")
PY

# PostgreSQL initializes with the password from the environment-sourced secret.
"${compose[@]}" up -d --no-build --wait --wait-timeout 120 postgres
container=$("${compose[@]}" ps --quiet postgres)
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" | grep -q '^POSTGRES_PASSWORD=' \
    && fail "PostgreSQL received its password in the environment"

# Migrations, then verification, through the migrate profile and nest-db.
"${compose[@]}" run --rm --no-deps --pull never migrate
"${compose[@]}" run --rm --no-deps --pull never migrate nest-db verify
echo "PASS: nest-db upgrade and verify with a direct NEST_DATABASE_PASSWORD"

# The Controller, without the sidecar (it cannot enroll here).
"${compose[@]}" up -d --no-build --pull never --no-deps --wait --wait-timeout 90 controller

check_controller() {
    "${compose[@]}" exec -T --user nest controller sh -euc '
        test "$(stat -c %a ~/.ssh)" = 700
        for f in config known_hosts id_nest; do test "$(stat -c %a:%U ~/.ssh/$f)" = 600:nest; done
        ssh-keygen -y -f ~/.ssh/id_nest >/dev/null
        ssh -G fixture-hpc | grep -qx "hostname fixture.example"
        ssh -G fixture-hpc | grep -qx "stricthostkeychecking true"
        test ! -e /run/nest-ssh
        python -m nest.healthcheck
        python - <<PY
import http.client
conn = http.client.HTTPConnection("127.0.0.1", 8080, timeout=5)
conn.request("GET", "/auth/google/login")
response = conn.getresponse()
assert response.status in (302, 303), response.status
assert response.getheader("Location", "").startswith("https://accounts.google.com/"), "redirect"
PY
    '
}
check_controller
echo "PASS: SSH files generated privately, TLS healthcheck, Google sign-in from a direct secret"

# Secrets stay out of the logs, and the inline TLS key is not on disk anywhere but
# the runtime files Nest never writes for gRPC (it reads PEM bytes directly).
logs=$("${compose[@]}" logs --no-color controller migrate postgres)
for secret in "$password" "$google_secret" "$(sed -n 2p "$fixture/id_ed25519")" "$(sed -n 2p "$fixture/tls.key")"; do
    grep -qF -- "$secret" <<<"$logs" && fail "a secret appeared in the logs"
done
"${compose[@]}" exec -T controller sh -c 'grep -rlF -- "$1" /tmp /home/nest 2>/dev/null' _ "$(sed -n 2p "$fixture/tls.key")" \
    && fail "the inline TLS key was written to disk"
echo "PASS: no secret in logs; inline TLS key never written to disk"

# A restart reuses the container (tmpfs emptied) and must regenerate the files.
"${compose[@]}" restart controller
"${compose[@]}" up -d --no-build --pull never --no-deps --wait --wait-timeout 90 controller
check_controller
# A redeploy recreates the container; the database keeps the Platform registry.
"${compose[@]}" up -d --no-build --pull never --no-deps --force-recreate --wait --wait-timeout 90 controller
check_controller
"${compose[@]}" up -d --no-build --pull never --force-recreate --wait --wait-timeout 120 postgres
"${compose[@]}" run --rm --no-deps --pull never migrate nest-db verify
platforms=$("${compose[@]}" exec -T postgres psql -U nest -d nest -tAc "SELECT count(*) FROM hpc_platforms WHERE slug = 'lab'")
test "$platforms" = 1 || fail "the Platform registry did not survive recreation"
echo "PASS: restart and redeploy regenerate runtime files; PostgreSQL data persists"

# Tailscale sidecar: the wrapper writes the inline Serve configuration, passes no
# auth key when it is empty, and the node identity in tailscale-state survives a
# redeploy.
"${compose[@]}" up -d --no-build --no-deps tailscale
state_key() {
    for _ in $(seq 30); do
        if "${compose[@]}" exec -T tailscale sh -c 'test -s /var/lib/tailscale/tailscaled.state' 2>/dev/null; then
            "${compose[@]}" exec -T tailscale sh -c 'grep -o "\"_machinekey\": *\"[^\"]*\"" /var/lib/tailscale/tailscaled.state | sha256sum'
            return
        fi
        sleep 1
    done
    fail "tailscaled wrote no state"
}
first=$(state_key)
"${compose[@]}" exec -T tailscale sh -euc '
    test "$(cat /run/nest-serve/serve.json)" = "{\"TCP\":{\"50051\":{\"TCPForward\":\"controller:50051\"}}}"
    test "$(stat -c %a /run/nest-serve/serve.json)" = 600
    tr "\0" "\n" < /proc/1/environ > /tmp/env
    grep -qx "TS_SERVE_CONFIG=/run/nest-serve/serve.json" /tmp/env
    ! grep -q "^NEST_TAILSCALE_SERVE_CONFIG=" /tmp/env
    ! grep -q "^TS_AUTHKEY=" /tmp/env
'
"${compose[@]}" up -d --no-build --pull never --no-deps --force-recreate tailscale
second=$(state_key)
test "$first" = "$second" || fail "the Tailscale node identity changed on redeploy"
echo "PASS: sidecar Serve configuration from the environment; node identity persists"

"${compose[@]}" stop --timeout 20 controller
container=$("${compose[@]}" ps --all --quiet controller)
test "$(docker inspect --format '{{.State.ExitCode}}' "$container")" = 0
echo "PASS: environment-only deployment smoke test"
