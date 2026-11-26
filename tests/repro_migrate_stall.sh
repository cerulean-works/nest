#!/bin/bash
# Reproduces the Compose stall seen on Dokploy on 2026-11-26 with a disposable
# fixture (busybox only: no Nest image, database or credential), and checks that
# scripts/nest-deploy-doctor.py detects and recovers it.
#
#   tests/repro_migrate_stall.sh
#
# The fixture mirrors compose.dokploy.yaml's start order: a healthy sidecar and
# database, a one-shot `migrate` (profile migrate), and a Controller that requires
# `migrate` to complete successfully. Steps:
#
#   1. A deploy whose network subnet overlaps an existing network fails at network
#      creation, and leaves the running containers as they were.
#   2. A deploy that is killed while Compose recreates `migrate` leaves a container
#      named `<12 hex>_<project>-migrate-1` in state Created (PID 0, never started).
#      (Compose recreates by creating the replacement under that temporary name,
#      removing the old container, then renaming the replacement.)
#   3. The next `docker compose up -d` adopts that container, removes the old one,
#      and waits on it forever: it never starts it. `--wait --wait-timeout` bounds
#      the wait but still fails.
#   4. `nest-deploy-doctor.py recover-migrate --apply` removes the never-started
#      container; the next deploy runs migrate again and starts the Controller.
#
# Step 2 is a race against Compose; the script retries it. Exits 0 when every step
# behaved as described, 1 otherwise. Prints the Docker and Compose versions.
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
DOCTOR="python3 $ROOT/scripts/nest-deploy-doctor.py"
# The fixture's stuck container is seconds old; in production the doctor waits
# --min-age (300 s) before calling a never-started container stuck.
AGE="--min-age 0"
project=nest-stall-$$
work=$(mktemp -d)
squatter=$project-squatter
cleanup() {
    docker compose -p "$project" -f "$work/compose.yaml" --profile migrate --profile local-db \
        down --volumes --remove-orphans >/dev/null 2>&1 || true
    docker ps -aq --filter "label=com.docker.compose.project=$project" | xargs -r docker rm -f >/dev/null 2>&1
    docker network rm "$squatter" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
fail() { echo "FAIL $*"; exit 1; }
pass() { echo "PASS $*"; }

echo "docker $(docker version --format '{{.Server.Version}}'), $(docker compose version)"
cat > "$work/compose.yaml" <<'YAML'
services:
  controller:
    image: busybox:1.37
    command: ["sh", "-c", "sleep 3600"]
    init: true
    depends_on:
      sidecar: {condition: service_healthy}
      postgres: {condition: service_healthy, required: false}
      migrate: {condition: service_completed_successfully, required: false}
    networks: [tailnet, grpc-origin]
  migrate:
    profiles: [migrate]
    image: busybox:1.37
    command: ["sh", "-c", "echo schema current ${REV:-0}"]
    restart: "no"
    depends_on:
      postgres: {condition: service_healthy, required: false}
    networks: [tailnet]
  postgres:
    profiles: [local-db]
    image: busybox:1.37
    command: ["sh", "-c", "touch /tmp/ok; sleep 3600"]
    healthcheck: {test: ["CMD", "test", "-f", "/tmp/ok"], interval: 1s, retries: 30}
    volumes: [data:/data]
    networks: [tailnet]
  sidecar:
    image: busybox:1.37
    command: ["sh", "-c", "touch /tmp/ok; sleep 3600"]
    healthcheck: {test: ["CMD", "test", "-f", "/tmp/ok"], interval: 1s, retries: 30}
    networks: [tailnet]
networks:
  tailnet: {driver: bridge}
  grpc-origin:
    driver: bridge
    internal: true
    ipam:
      config:
        - subnet: ${NEST_GRPC_ORIGIN_SUBNET}
volumes:
  data:
YAML
compose() { docker compose -p "$project" -f "$work/compose.yaml" "$@"; }
export COMPOSE_PROFILES=local-db,migrate REV=0
docker image inspect busybox:1.37 >/dev/null 2>&1 || docker pull -q busybox:1.37 >/dev/null

# A free /28 for the fixture and a /16 network that overlaps another one.
docker network create --subnet 10.231.0.0/16 "$squatter" >/dev/null || fail "cannot create the squatter network"
export NEST_GRPC_ORIGIN_SUBNET=10.231.250.0/28
compose up -d >"$work/up1.log" 2>&1 && fail "a deploy into an overlapping subnet succeeded"
grep -q "overlaps" "$work/up1.log" || fail "the overlap error was not reported"
pass "1. overlapping subnet: the deploy fails at network creation"
report=$(NEST_GRPC_GATEWAY_ADDRESS=10.231.250.2 $DOCTOR preflight --project "$project")
[ $? -eq 1 ] && grep -q "FAIL origin_subnet_free" <<<"$report" || fail "preflight did not report the overlap"
pass "1. preflight reports the overlapping network"

export NEST_GRPC_ORIGIN_SUBNET=10.232.250.0/28
compose up -d >/dev/null 2>&1 || fail "the deploy with a free subnet failed"
stuck=
for i in $(seq 1 60); do
    export REV=$i
    compose up -d >"$work/run.log" 2>&1 &
    pid=$!
    for _ in $(seq 1 400); do grep -q 'migrate-1 Recreate' "$work/run.log" && break; sleep 0.005; done
    sleep "0.0$((i % 10))"
    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    # Requests the daemon had already accepted may still finish: require the state
    # to persist for two seconds.
    for _ in 1 2; do
        stuck=$(docker ps -a --filter "label=com.docker.compose.project=$project" --format '{{.Names}} {{.State}}' \
            | grep -E '^[0-9a-f]{12}_.*migrate.* created$' || true)
        [ -n "$stuck" ] || break
        sleep 1
    done
    [ -n "$stuck" ] && break
done
[ -n "$stuck" ] || fail "could not interrupt a recreate in 60 attempts (not reproduced)"
pass "2. interrupted recreate left: $stuck"

report=$($DOCTOR migrate-status --project "$project" $AGE)
code=$?
[ $code -eq 3 ] || { echo "$report"; fail "migrate-status exited $code, not 3 (stuck)"; }
pass "2. migrate-status reports stuck-created (exit 3)"

timeout 30 docker compose -p "$project" -f "$work/compose.yaml" up -d >"$work/hang.log" 2>&1
[ $? -eq 124 ] || { cat "$work/hang.log"; fail "the retry did not hang"; }
pass "3. the retry waits on the never-started container (killed after 30 s)"

report=$($DOCTOR recover-migrate --project "$project" $AGE --settle 2)
grep -q "would run: docker rm" <<<"$report" || fail "dry run printed no command"
$DOCTOR migrate-status --project "$project" $AGE >/dev/null
code=$?
[ $code -eq 3 ] || fail "the dry run changed something (migrate-status exited $code)"
pass "4. recover-migrate without --apply changes nothing"
$DOCTOR recover-migrate --project "$project" $AGE --settle 2 --apply >/dev/null || fail "recover-migrate --apply failed"
compose up -d --wait --wait-timeout 60 >/dev/null 2>&1 || fail "the deploy after recovery failed"
$DOCTOR migrate-status --project "$project" >/dev/null || fail "migrate did not complete after recovery"
[ "$(docker inspect "$project-controller-1" --format '{{.State.Status}}')" = running ] || fail "the Controller is not running"
docker volume inspect "${project}_data" >/dev/null || fail "the named volume was lost"
pass "4. after recover-migrate --apply the deploy runs migrate, starts the Controller and keeps the volume"
