#!/usr/bin/env bash
# Public gRPC gateway on a Dokploy-like multi-network host (#109, H08).
#
# On the staging host the Controller and Envoy both joined the external edge
# network (dokploy-network) as well as the internal grpc-origin network, and the
# gateway dialled the generic `controller` name. Docker resolved it to the
# Controller's edge address, so the Controller saw the gateway's edge address and
# refused every call (PERMISSION_DENIED, origin_not_allowed). This test rebuilds
# that topology with compose.dokploy.yaml plus tests/compose.dokploy-edge-fixture.yaml
# (the Controller also on the edge network, a stand-in for Traefik's PROXY v2
# passthrough, an outside client network) and runs real gRPC through the real
# Envoy, origin guard, Controller and PostgreSQL:
#
#   1. old configuration (upstream controller:50051, no resolution check): the
#      runtime doctor fails, with socket evidence of the edge path;
#   2. old upstream with the new entrypoint: the gateway refuses to start;
#   3. current configuration: the doctor passes, and from outside the verifier gets
#      UNAUTHENTICATED with `nest login` guidance, verified TLS/h2, wrong name and
#      HTTP/1.1 refused, the health path refused, and raw origin ports unreachable.
#      The Controller attributes the call to the outside client's address. Direct
#      and spoofed x-forwarded-for calls from the edge network are refused, and the
#      private Tailnet-bridge path still works. All of this holds again after a
#      restart and after the Controller and the gateway are recreated.
#
# Needs Docker, openssl and the image in NEST_CONTROLLER_IMAGE. Contacts no real
# host, DNS name, certificate authority or Tailnet.
set -euo pipefail

cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
export COMPOSE_PROJECT_NAME=nest-grpc-smoke
export NEST_CONTROLLER_IMAGE=${NEST_CONTROLLER_IMAGE:-nest-controller:test}
project=$COMPOSE_PROJECT_NAME
edge_network=nest-grpc-smoke-edge
edge_subnet=10.231.0.0/24
edge_proxy=10.231.0.7
origin_subnet=10.253.250.0/28
gateway_origin=10.253.250.3
base=(docker compose -f compose.dokploy.yaml -f tests/compose.dokploy-edge-fixture.yaml
    --env-file "$fixture/.env" --profile local-db --profile migrate --profile public-grpc)
compose=("${base[@]}")
doctor=(python3 scripts/nest-deploy-doctor.py)

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        "${compose[@]}" logs --no-color --tail 80 controller grpc-gateway edge-proxy || true
    fi
    docker ps --all --quiet --filter "label=nest-smoke=$project" | xargs -r docker rm -f >/dev/null 2>&1 || true
    "${compose[@]}" down --timeout 10 --volumes --remove-orphans >/dev/null 2>&1 || true
    docker network rm "$edge_network" >/dev/null 2>&1 || true
    rm -rf "$fixture"
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# --- Fixture: an external edge network, a staging CA, the Dokploy .env -----------
docker network create --subnet "$edge_subnet" "$edge_network" >/dev/null

openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=nest-smoke-ca \
    -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign \
    -keyout "$fixture/ca.key" -out "$fixture/ca.pem" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj /CN=grpc.smoke.test \
    -keyout "$fixture/privkey.pem" -out "$fixture/leaf.csr" 2>/dev/null
printf 'subjectAltName=DNS:grpc.smoke.test\nextendedKeyUsage=serverAuth\n' > "$fixture/leaf.ext"
openssl x509 -req -in "$fixture/leaf.csr" -CA "$fixture/ca.pem" -CAkey "$fixture/ca.key" \
    -CAcreateserial -days 2 -extfile "$fixture/leaf.ext" -out "$fixture/fullchain.pem" 2>/dev/null
ssh-keygen -q -t ed25519 -N '' -C '' -f "$fixture/id_ed25519"

write_env() {
    {
        printf "NEST_DATABASE_URL='postgresql://nest@postgres:5432/nest'\n"
        printf "NEST_DATABASE_PASSWORD='%s'\n" "smoke-$(openssl rand -hex 8)"
        printf "NEST_DATABASE_REQUIRED='true'\n"
        printf "NEST_TAILSCALE_AUTHKEY=''\n"
        printf "NEST_EXECUTOR='slurm'\n"
        printf "NEST_SSH_CONFIG='%s'\n" 'Host fixture-hpc\n    HostName fixture.example\n    User svc-nest\n    IdentityFile ~/.ssh/id_nest\n    IdentitiesOnly yes\n    BatchMode yes\n    StrictHostKeyChecking yes'
        printf "NEST_SSH_PRIVATE_KEY='%s'\n" "$(awk '{printf "%s\\n", $0}' "$fixture/id_ed25519")"
        printf "NEST_SSH_KNOWN_HOSTS_DATA='fixture.example %s'\n" "$(cat "$fixture/id_ed25519.pub")"
        printf "NEST_PLATFORMS_ENV='%s'\n" 'NEST_PLATFORMS=lab\nNEST_PLATFORM_LAB_NAME=Lab cluster\nNEST_PLATFORM_LAB_OPERATOR=example-university\nNEST_PLATFORM_LAB_LOGIN_NODES=fixture-hpc\nNEST_PLATFORM_LAB_SSH_USER=svc-nest\nNEST_PLATFORM_LAB_ACCOUNT=lab\nNEST_PLATFORM_LAB_WORKDIR=/home/svc-nest/nest'
        # The bounded pilot, exactly as docs/source/public-grpc-gateway.md asks.
        printf "NEST_PUBLIC_PILOT='true'\n"
        printf "NEST_CONTROLLER_AUTH='session'\n"
        printf "NEST_EXECUTION_REGISTRY='required'\n"
        printf "NEST_LIMIT_BATCH_SCRIPTS='refuse'\n"
        printf "NEST_CONTROLLER_ALLOW_PLAINTEXT='true'\n"
        printf "NEST_ENVIRONMENT_TIMEOUT='540'\n"
        printf "NEST_GRPC_ORIGIN_SUBNET='%s'\n" "$origin_subnet"
        printf "NEST_GRPC_GATEWAY_ADDRESS='%s'\n" "$gateway_origin"
        printf "NEST_GRPC_TRUSTED_PROXIES='%s/32'\n" "$gateway_origin"
        printf "NEST_GRPC_ALLOWED_PEERS='%s'\n" "$1"
        printf "NEST_GRPC_EDGE_NETWORK='%s'\n" "$edge_network"
        printf "NEST_GRPC_EDGE_PROXY='%s/32'\n" "$edge_proxy"
        printf "NEST_GRPC_PUBLIC_HOST='grpc.smoke.test'\n"
    } > "$fixture/.env"
    chmod 600 "$fixture/.env"
}

# Networks first, so the Tailnet bridge's subnet can go into the allowlist.
write_env "$gateway_origin/32"
"${compose[@]}" up --no-start --no-build --pull missing --no-deps postgres controller grpc-gateway edge-proxy >/dev/null 2>&1
tailnet_subnet=$(docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "${project}_tailnet")
[ -n "$tailnet_subnet" ] || fail "no Tailnet bridge subnet"
outside_subnet=$(docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "${project}_outside")
# Exactly the gateway and the Tailnet bridge: never the edge network.
write_env "$gateway_origin/32,$tailnet_subnet"

# The gateway's certificate, as the ACME client installs it (group 101, 0640 key).
docker run --rm --user 0 --entrypoint sh -v "${project}_grpc-certificates:/certs" \
    -v "$fixture:/src:ro" envoyproxy/envoy:v1.36.2 -euc '
        cp /src/fullchain.pem /src/privkey.pem /certs/
        chown 0:101 /certs/fullchain.pem /certs/privkey.pem
        chmod 0644 /certs/fullchain.pem
        chmod 0640 /certs/privkey.pem'

"${compose[@]}" up -d --no-build --pull never --wait --wait-timeout 120 postgres
"${compose[@]}" run --rm --no-deps --pull never migrate >/dev/null

start_controller() {
    "${compose[@]}" up -d --no-build --pull never --no-deps --wait --wait-timeout 90 "$@" controller
}

# The external client: on the outside network only, as from the Internet.
outside() {
    docker run --rm --network "${project}_outside" \
        -v "$PWD/scripts:/scripts:ro" -v "$fixture/ca.pem:/fixture/ca.pem:ro" \
        --entrypoint python "$NEST_CONTROLLER_IMAGE" "$@"
}

# One credential-free call straight to the Controller from a container on `network`.
probe() {
    local network=$1
    shift
    docker run --rm --network "$network" -v "$PWD/tests:/tests:ro" \
        --entrypoint python "$NEST_CONTROLLER_IMAGE" /tests/grpc_origin_probe.py "$@"
}

container_ip() { # service network
    docker inspect --format "{{(index .NetworkSettings.Networks \"$2\").IPAddress}}" \
        "$("${compose[@]}" ps --quiet "$1")"
}

# --- 1. The old configuration reproduces the staging failure ----------------------
cat > "$fixture/old-upstream.yaml" <<'EOF'
services:
  grpc-gateway:
    environment:
      NEST_GRPC_UPSTREAM: controller:50051
      NEST_GRPC_ORIGIN_SUBNET: ""
EOF
compose=("${base[@]}" -f "$fixture/old-upstream.yaml")
start_controller
"${compose[@]}" up -d --no-build --pull never --no-deps grpc-gateway edge-proxy
sleep 3
controller_edge=$(container_ip controller "$edge_network")
if "${doctor[@]}" gateway-runtime --project "$project" > "$fixture/old-doctor.txt"; then
    cat "$fixture/old-doctor.txt"
    fail "the doctor passed the old upstream configuration"
fi
cat "$fixture/old-doctor.txt"
grep -q '^FAIL upstream_setting: ' "$fixture/old-doctor.txt" || fail "old upstream not flagged"
grep -q '^FAIL upstream_alias: controller is a Controller name on ' "$fixture/old-doctor.txt" \
    || fail "the generic name on the edge network was not flagged"
if grep -q "^FAIL upstream_dns: .*$controller_edge" "$fixture/old-doctor.txt"; then
    # The staging failure, reproduced: Docker resolved `controller` to the edge address.
    outside /scripts/verify-public-grpc.py grpc.smoke.test:443 --ca-file /fixture/ca.pem \
        > "$fixture/old-verify.txt" && fail "the old configuration passed external verification"
    grep -q 'expected UNAUTHENTICATED, got PERMISSION_DENIED' "$fixture/old-verify.txt" \
        || { cat "$fixture/old-verify.txt"; fail "expected the staging PERMISSION_DENIED"; }
    grep -q '^FAIL private_path: the gateway reaches the Controller from ' "$fixture/old-doctor.txt" \
        || fail "no socket evidence of the edge path"
    echo "PASS: old configuration reproduces #109 (edge address, PERMISSION_DENIED)"
else
    echo "PASS: old configuration flagged by the doctor (this Docker resolved the generic name to grpc-origin)"
fi

# --- 2. The old upstream with the new entrypoint never starts ---------------------
cat > "$fixture/old-upstream.yaml" <<'EOF'
services:
  grpc-gateway:
    environment:
      NEST_GRPC_UPSTREAM: controller:50051
EOF
"${compose[@]}" up -d --no-build --pull never --no-deps --force-recreate grpc-gateway
gateway=$("${compose[@]}" ps --all --quiet grpc-gateway)
for _ in $(seq 30); do
    [ "$(docker inspect --format '{{.RestartCount}}' "$gateway")" -ge 1 ] && break
    sleep 1
done
docker logs "$gateway" 2>&1 | grep -q "resolves to .*outside grpc-origin ($origin_subnet)\|resolves to $controller_edge" \
    || { docker logs "$gateway" 2>&1 | tail -5; fail "the gateway did not refuse a non-origin upstream"; }
echo "PASS: an upstream resolving outside grpc-origin stops the gateway before Envoy starts"

# --- 3. The current configuration ---------------------------------------------------
compose=("${base[@]}")
"${compose[@]}" up -d --no-build --pull never --no-deps --force-recreate grpc-gateway

check_current() {
    local label=$1 report since
    # Nanoseconds: the previous phase's Tailnet probe can end in the same second.
    since=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
    report="$fixture/doctor-$label.txt"
    for _ in $(seq 20); do
        "${doctor[@]}" gateway-runtime --project "$project" > "$report" && break
        sleep 1
    done
    cat "$report"
    grep -q '^FAIL' "$report" && fail "[$label] the runtime doctor failed"
    grep -q "^PASS private_path: gateway $gateway_origin -> Controller " "$report" \
        || fail "[$label] no socket evidence of the private path"

    local controller_edge controller_origin gateway_edge
    controller_edge=$(container_ip controller "$edge_network")
    controller_origin=$(container_ip controller "${project}_grpc-origin")
    gateway_edge=$(container_ip grpc-gateway "$edge_network")
    outside /scripts/verify-public-grpc.py grpc.smoke.test:443 --ca-file /fixture/ca.pem \
        --origin "$controller_edge:50051" --origin "$controller_origin:50051" \
        --origin "$gateway_edge:8443" --timeout 5 > "$fixture/verify-$label.txt" \
        || { cat "$fixture/verify-$label.txt"; fail "[$label] external verification failed"; }
    cat "$fixture/verify-$label.txt"
    python3 - "$fixture/verify-$label.txt" "$controller_edge" "$controller_origin" "$gateway_edge" <<'PY'
import json, sys
path, controller_edge, controller_origin, gateway_edge = sys.argv[1:]
results = {}
for line in open(path):
    record = json.loads(line)
    if "check" in record:
        results[record["check"]] = record["passed"]
expected = {
    "tls", "tls_wrong_name_refused", "tls_http1_only_not_h2", "unauthenticated_refused",
    "non_nest_path_refused", f"origin_unreachable_{controller_edge}:50051",
    f"origin_unreachable_{controller_origin}:50051", f"origin_unreachable_{gateway_edge}:8443",
}
missing = expected - {name for name, passed in results.items() if passed}
assert not missing, f"checks not passed: {sorted(missing)}"
PY

    # The Controller attributed the call to the outside client (the address Traefik's
    # PROXY header named, forwarded by Envoy), not to the gateway or the edge proxy.
    "${compose[@]}" logs --no-color --since "$since" controller > "$fixture/controller-$label.log"
    python3 - "$fixture/controller-$label.log" "$outside_subnet" <<'PY'
import ipaddress, re, sys
log, subnet = open(sys.argv[1]).read(), ipaddress.ip_network(sys.argv[2])
clients = re.findall(r"RPC started method=ListEligibleProjects client=(\S+)", log)
assert clients, "no ListEligibleProjects call reached the Controller"
for client in clients:
    assert ipaddress.ip_address(client) in subnet, f"call attributed to {client}, not {subnet}"
print(f"PASS: calls attributed to the outside client: {sorted(set(clients))}")
PY

    # Direct calls from the edge network, with and without a forged x-forwarded-for.
    local direct spoofed tailnet
    direct=$(probe "$edge_network" "controller:50051")
    spoofed=$(probe "$edge_network" "controller:50051" --forwarded-for "$gateway_origin")
    for answer in "$direct" "$spoofed"; do
        [ "$answer" = '{"code": "PERMISSION_DENIED", "kind": "origin_not_allowed", "login_guidance": false}' ] \
            || fail "[$label] a direct edge-network call was not refused: $answer"
    done
    # The private Tailnet-bridge path (Tailscale Serve's `controller:50051`) still works.
    tailnet=$(probe "${project}_tailnet" "controller:50051")
    [ "$tailnet" = '{"code": "UNAUTHENTICATED", "kind": "", "login_guidance": true}' ] \
        || fail "[$label] the Tailnet bridge path changed: $tailnet"
    echo "PASS: [$label] gateway path, refusals and Tailnet path"
}

check_current deploy
grep -q "RPC refused: connection from outside NEST_GRPC_ALLOWED_PEERS" \
    <<<"$("${compose[@]}" logs --no-color controller)" \
    || fail "the direct edge-network calls left no refusal in the Controller log"

"${compose[@]}" restart controller >/dev/null
start_controller
check_current restart
# A recreated Controller with a different grpc-origin address: nothing pins it, and
# Envoy follows the alias (STRICT_DNS) without a restart.
previous=$(container_ip controller "${project}_grpc-origin")
"${compose[@]}" stop --timeout 10 controller >/dev/null
placeholder=$(docker run -d --label "nest-smoke=$project" --network "${project}_grpc-origin" --ip "$previous" \
    --entrypoint sleep envoyproxy/envoy:v1.36.2 600)
start_controller --force-recreate
current=$(container_ip controller "${project}_grpc-origin")
[ "$current" != "$previous" ] || fail "the recreated Controller kept $previous"
echo "PASS: recreated Controller moved from $previous to $current on grpc-origin"
check_current recreate-controller
docker rm -f "$placeholder" >/dev/null
"${compose[@]}" up -d --no-build --pull never --no-deps --force-recreate grpc-gateway
check_current recreate-gateway

echo "PASS: public gRPC gateway on a Dokploy-like multi-network host"
