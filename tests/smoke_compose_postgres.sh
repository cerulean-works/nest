#!/usr/bin/env bash
# Compose smoke test for the optional Docker-managed PostgreSQL (the local-db
# profile in compose.yaml):
# - the default deployment starts no database and never falls back to one;
# - the profile starts PostgreSQL alone, with a file-based password;
# - `nest-db upgrade` runs explicitly, and the Controller verifies the schema;
# - Controller and database start, stop, and restart independently;
# - data survives `down` / `up` without --volumes;
# - the documented backup and restore commands work;
# - an unreachable database stops the Controller without logging the password.
set -euo pipefail

cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
export COMPOSE_PROJECT_NAME=nest-postgres-smoke
export NEST_CONTROLLER_IMAGE=${NEST_CONTROLLER_IMAGE:-nest-controller:test}
export NEST_EXECUTOR=local
export NEST_CONTROLLER_HOST_IP=127.0.0.1
export NEST_CONTROLLER_PORT=0
export NEST_POSTGRES_PORT=0
export NEST_SSH_DIR="$fixture"
unset COMPOSE_PROFILES NEST_POSTGRES_PASSWORD_FILE NEST_DATABASE_URL NEST_DATABASE_PASSWORD_FILE
volume="${COMPOSE_PROJECT_NAME}_postgres-data"
local_db=(docker compose --profile local-db)
database_url=postgresql://nest@postgres:5432/nest
password_file=/home/nest/.ssh/nest-postgres-password

cleanup() {
    "${local_db[@]}" logs --no-color || true
    "${local_db[@]}" down --timeout 20 --volumes || true
    rm -rf "$fixture"
}
trap cleanup EXIT

psql() {
    "${local_db[@]}" exec -T postgres psql -U nest -d nest -tA -c "$1"
}

running() {
    "${local_db[@]}" ps --status running --format '{{.Service}}' | sort | tr '\n' ' '
}

with_database() {
    NEST_DATABASE_URL=$database_url NEST_DATABASE_PASSWORD_FILE=$password_file "$@"
}

(umask 077 && openssl rand -hex 32 > "$fixture/nest-postgres-password")
password=$(cat "$fixture/nest-postgres-password")

docker compose config --quiet
"${local_db[@]}" config --quiet

# 1. Default deployment: no database service, container, or volume.
docker compose up -d --no-build --pull never --wait --wait-timeout 60
test "$(running)" = "controller "
test -z "$("${local_db[@]}" ps --all --format '{{.Service}}' | grep -x postgres || true)"
if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "FAIL: the default deployment created $volume"
    exit 1
fi
# Pointed at the profile's database without the profile, the Controller fails
# fast instead of starting PostgreSQL or running without it.
status=0
output=$(with_database docker compose run --rm --no-deps -e NEST_DATABASE_CONNECT_TIMEOUT=3 \
    controller python -m nest.controller 2>&1) || status=$?
echo "$output"
test "$status" = 3
grep -q 'Cannot connect to PostgreSQL' <<<"$output"
docker compose down --timeout 20

# 2. The profile starts the database on its own; the Controller stays stopped.
"${local_db[@]}" up -d --pull missing --wait --wait-timeout 120 postgres
test "$(running)" = "postgres "
docker volume inspect "$volume" >/dev/null
# The password file is the only credential: a wrong one is rejected over TCP.
"${local_db[@]}" exec -T -e PGPASSWORD="$password" postgres \
    psql -h 127.0.0.1 -U nest -d nest -tAc 'SELECT 1' | grep -qx 1
if "${local_db[@]}" exec -T -e PGPASSWORD=wrong postgres \
    psql -h 127.0.0.1 -U nest -d nest -tAc 'SELECT 1' 2>/dev/null; then
    echo "FAIL: PostgreSQL accepted a wrong password"
    exit 1
fi

# 3. Migrations run explicitly, then the Controller verifies them.
# Capture output before grepping: with pipefail, grep -q exiting early would
# kill the producer with SIGPIPE and fail the pipeline.
output=$(with_database docker compose run --rm --no-deps controller nest-db upgrade)
grep -q 'applied 0001_providers_and_jobs' <<<"$output"
with_database docker compose up -d --no-build --pull never --wait --wait-timeout 60 controller
test "$(running)" = "controller postgres "
grep -q 'Database connected' <<<"$(docker compose logs --no-color controller)"
grep -q 'Schema is current' <<<"$(docker compose exec -T controller nest-db verify)"
test "$(psql 'SELECT count(*) FROM schema_migrations')" = 1

# 4. Independent lifecycles: stopping or replacing one leaves the other running.
controller_id=$(docker compose ps --quiet controller)
"${local_db[@]}" up -d --force-recreate --wait --wait-timeout 120 postgres
test "$(docker compose ps --quiet controller)" = "$controller_id"
test "$(running)" = "controller postgres "
docker compose stop controller
test "$(running)" = "postgres "
with_database docker compose up -d --no-build --pull never --wait --wait-timeout 60 controller

# 5. Data survives `down` / `up` without --volumes. The Controller has no
# Provider Profiles, so its startup sync marks the row inactive.
psql "INSERT INTO provider_profiles (id, ssh_mode, login_nodes, account, partition, time_limit)
      VALUES ('smoke', 'A', ARRAY['login'], 'acct', 'dev', '00:05:00')"
"${local_db[@]}" down --timeout 20
test -z "$(running)"
docker volume inspect "$volume" >/dev/null
with_database "${local_db[@]}" up -d --no-build --pull never --wait --wait-timeout 120
test "$(psql "SELECT active FROM provider_profiles WHERE id = 'smoke'")" = f
grep -q 'Schema is current' <<<"$(docker compose exec -T controller nest-db verify)"
grep -q 'already current' <<<"$(with_database docker compose run --rm --no-deps controller nest-db upgrade)"

# 6. The documented backup and restore: dump with the server's own pg_dump,
# restore into an empty database, and verify it with nest-db.
"${local_db[@]}" exec -T postgres pg_dump -U nest -d nest --format=custom > "$fixture/nest.dump"
test -s "$fixture/nest.dump"
"${local_db[@]}" exec -T postgres createdb -U nest nest_restored
"${local_db[@]}" exec -T postgres pg_restore -U nest -d nest_restored --no-owner --exit-on-error \
    < "$fixture/nest.dump"
output=$(NEST_DATABASE_URL=postgresql://nest@postgres:5432/nest_restored \
    NEST_DATABASE_PASSWORD_FILE=$password_file \
    docker compose run --rm --no-deps controller nest-db verify)
grep -q 'Schema is current' <<<"$output"
test "$("${local_db[@]}" exec -T postgres psql -U nest -d nest_restored -tA \
    -c "SELECT count(*) FROM provider_profiles WHERE id = 'smoke'")" = 1

# 7. With the database gone, a Controller start fails fast and never logs the password.
"${local_db[@]}" stop postgres
test "$(running)" = "controller "
status=0
output=$(NEST_DATABASE_URL="postgresql://nest:$password@postgres:5432/nest" \
    docker compose run --rm --no-deps -e NEST_DATABASE_CONNECT_TIMEOUT=3 \
    controller python -m nest.controller 2>&1) || status=$?
echo "$output"
test "$status" = 3
grep -q 'Cannot connect to PostgreSQL at postgresql://nest:\*\*\*@postgres:5432/nest' <<<"$output"
if grep -q "$password" <<<"$output"; then
    echo "FAIL: database password appeared in Controller output"
    exit 1
fi

echo "PASS: Compose default without a database, local-db profile, explicit migrations," \
    "independent lifecycles, persistence across down/up, backup/restore, and fail-fast"
