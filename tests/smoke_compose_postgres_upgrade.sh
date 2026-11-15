#!/usr/bin/env bash
# Compose smoke test for the documented PostgreSQL major-version upgrade of the
# local-db profile (docs/source/database.md, "Upgrade PostgreSQL"): 17 to 18 by
# dump, fresh volume and restore, then the rollback to the saved 17 volume.
# Checks that records and the Nest schema survive each step, and that the new
# image refuses the old volume instead of touching it.
set -euo pipefail

cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
export COMPOSE_PROJECT_NAME=nest-postgres-upgrade-smoke
export NEST_CONTROLLER_IMAGE=${NEST_CONTROLLER_IMAGE:-nest-controller:test}
export NEST_EXECUTOR=local
export NEST_CONTROLLER_HOST_IP=127.0.0.1
export NEST_CONTROLLER_PORT=0
export NEST_POSTGRES_PORT=0
export NEST_SSH_DIR="$fixture"
export NEST_DATABASE_URL=postgresql://nest@postgres:5432/nest
export NEST_DATABASE_PASSWORD_FILE=/home/nest/.ssh/nest-postgres-password
unset COMPOSE_PROFILES NEST_POSTGRES_PASSWORD_FILE NEST_POSTGRES_DATA_DIR
old_image=postgres:17.12-bookworm
new_image=postgres:18.7-bookworm
export NEST_POSTGRES_IMAGE=$old_image
volume="${COMPOSE_PROJECT_NAME}_postgres-data"
saved="${volume}-pg17"
local_db=(docker compose --profile local-db)

cleanup() {
    "${local_db[@]}" logs --no-color || true
    "${local_db[@]}" down --timeout 20 --volumes || true
    docker volume rm "$saved" >/dev/null 2>&1 || true
    rm -rf "$fixture"
}
trap cleanup EXIT

psql() {
    "${local_db[@]}" exec -T postgres psql -U nest -d nest -tA -c "$1"
}

copy_volume() {
    docker run --rm -v "$1:/from:ro" -v "$2:/to" --entrypoint cp "$old_image" -a /from/. /to/
}

# Everything a restore must keep: the schema history and a record.
snapshot() {
    psql "SELECT string_agg(version || ':' || name || ':' || checksum, ',' ORDER BY version)
          FROM schema_migrations"
    psql "SELECT id, ssh_mode, array_to_string(login_nodes, ','), account FROM provider_profiles
          WHERE id = 'upgrade'"
}

verify_schema() {
    grep -q 'Schema is current' <<<"$(docker compose run --rm --no-deps controller nest-db verify)"
}

(umask 077 && openssl rand -hex 32 > "$fixture/nest-postgres-password")

# A PostgreSQL 17 deployment with records.
"${local_db[@]}" up -d --pull missing --wait --wait-timeout 120 postgres
docker compose run --rm --no-deps controller nest-db upgrade
docker compose up -d --no-build --pull never --wait --wait-timeout 60 controller
psql "INSERT INTO provider_profiles (id, ssh_mode, login_nodes, account, partition, time_limit)
      VALUES ('upgrade', 'A', ARRAY['login1', 'login2'], 'acct', 'dev', '00:05:00')"
test "$(psql 'SHOW server_version_num')" -lt 180000
before=$(snapshot)

# 1. Stop the Controller and dump with the old server.
docker compose stop controller
"${local_db[@]}" exec -T postgres pg_dump -U nest -d nest --format=custom > "$fixture/nest-pg17.dump"
test -s "$fixture/nest-pg17.dump"

# 2. Stop the database and copy the volume aside.
"${local_db[@]}" stop postgres
docker volume create "$saved" >/dev/null
copy_volume "$volume" "$saved"

# The new image refuses the old volume at either mount target and changes nothing.
for target in /var/lib/postgresql/data /var/lib/postgresql; do
    if docker run --rm -e POSTGRES_PASSWORD_FILE=/run/pw \
        -v "$fixture/nest-postgres-password:/run/pw:ro" \
        -v "$volume:$target" "$new_image" >"$fixture/refused.log" 2>&1; then
        cat "$fixture/refused.log"
        echo "FAIL: $new_image started on the PostgreSQL 17 volume at $target"
        exit 1
    fi
done
docker run --rm -v "$volume:/data:ro" --entrypoint cat "$old_image" /data/PG_VERSION | grep -qx 17

"${local_db[@]}" rm -f postgres
docker volume rm "$volume" >/dev/null

# 3-4. Start the new, empty server, restore, and verify.
export NEST_POSTGRES_IMAGE=$new_image NEST_POSTGRES_DATA_DIR=/var/lib/postgresql
"${local_db[@]}" up -d --pull missing --wait --wait-timeout 120 postgres
test "$(psql 'SHOW server_version_num')" -ge 180000
"${local_db[@]}" exec -T postgres pg_restore -U nest -d nest --no-owner --exit-on-error \
    < "$fixture/nest-pg17.dump"
verify_schema
docker compose up -d --no-build --pull never --wait --wait-timeout 60 controller
grep -q 'Database connected' <<<"$(docker compose logs --no-color controller)"
test "$(snapshot)" = "$before"

# Rollback: restore the old image and the saved volume.
export NEST_POSTGRES_IMAGE=$old_image
unset NEST_POSTGRES_DATA_DIR
docker compose stop controller
"${local_db[@]}" rm -sf postgres
docker volume rm "$volume" >/dev/null
docker volume create "$volume" >/dev/null
copy_volume "$saved" "$volume"
"${local_db[@]}" up -d --wait --wait-timeout 120 postgres
test "$(psql 'SHOW server_version_num')" -lt 180000
verify_schema
docker compose up -d --no-build --pull never --wait --wait-timeout 60 controller
test "$(snapshot)" = "$before"

echo "PASS: PostgreSQL 17 to 18 upgrade by dump and restore, refused in-place start, and rollback"
