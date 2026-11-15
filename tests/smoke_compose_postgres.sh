#!/usr/bin/env bash
# Compose smoke test for PostgreSQL persistence, using the development-only
# database in compose.postgres-dev.yaml: migrations run as a separate one-shot
# service, the Controller verifies the schema, records survive a full restart,
# and an unreachable database stops the Controller instead of being ignored.
set -euo pipefail

cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
export COMPOSE_PROJECT_NAME=nest-postgres-smoke
export NEST_CONTROLLER_IMAGE=${NEST_CONTROLLER_IMAGE:-nest-controller:test}
export NEST_EXECUTOR=local
export NEST_CONTROLLER_HOST_IP=127.0.0.1
export NEST_CONTROLLER_PORT=0
export NEST_SSH_DIR="$fixture"
export NEST_DEV_POSTGRES_PASSWORD=smoke-dev-password
compose=(docker compose -f compose.yaml -f compose.postgres-dev.yaml)

cleanup() {
    "${compose[@]}" logs --no-color || true
    "${compose[@]}" down --timeout 20 --volumes || true
    rm -rf "$fixture"
}
trap cleanup EXIT

psql() {
    "${compose[@]}" exec -T postgres psql -U nest -d nest -tA -c "$1"
}

"${compose[@]}" config --quiet
"${compose[@]}" up -d --no-build --pull missing --wait --wait-timeout 120

# Migrations ran once, outside the Controller, and the Controller verified them.
# Capture output before grepping: with pipefail, grep -q exiting early would
# kill the producer with SIGPIPE and fail the pipeline.
grep -q 'applied 0001_providers_and_jobs' <<<"$("${compose[@]}" logs --no-color migrate)"
grep -q 'Schema is current' <<<"$("${compose[@]}" exec -T controller nest-db verify)"
grep -q 'Database connected' <<<"$("${compose[@]}" logs --no-color controller)"
test "$(psql 'SELECT count(*) FROM schema_migrations')" = 1

# A record written before a full restart is still there afterwards. The
# Controller has no Provider Profiles, so its startup sync marks the row inactive.
psql "INSERT INTO provider_profiles (id, ssh_mode, login_nodes, account, partition, time_limit)
      VALUES ('smoke', 'A', ARRAY['login'], 'acct', 'dev', '00:05:00')"
"${compose[@]}" down --timeout 20
"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120
test "$(psql "SELECT active FROM provider_profiles WHERE id = 'smoke'")" = f
grep -q 'already current' <<<"$("${compose[@]}" logs --no-color migrate)"

# With the database gone, a Controller start fails fast and never logs the password.
"${compose[@]}" stop postgres
status=0
output=$("${compose[@]}" run --rm --no-deps -e NEST_DATABASE_CONNECT_TIMEOUT=3 \
    controller python -m nest.controller 2>&1) || status=$?
echo "$output"
test "$status" = 3
grep -q 'Cannot connect to PostgreSQL at postgresql://nest:\*\*\*@postgres:5432/nest' <<<"$output"
if grep -q "$NEST_DEV_POSTGRES_PASSWORD" <<<"$output"; then
    echo "FAIL: database password appeared in Controller output"
    exit 1
fi

echo "PASS: Compose PostgreSQL migrations, verification, restart persistence, and fail-fast"
