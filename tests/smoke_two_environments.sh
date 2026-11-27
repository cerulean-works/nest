#!/usr/bin/env bash
# Two Nest deployments on one Dokploy-like host: Main and Release (#118).
#
# Both run compose.dokploy.yaml, as two Compose projects (two Dokploy applications),
# with the Controller on the shared external edge network as Dokploy attaches it, and
# one real Traefik on that network routing both public gRPC names by SNI with TLS
# passthrough and PROXY v2, into each project's real Envoy, origin guard, Controller
# and PostgreSQL:
#
#   1. the old, fixed edge alias: both gateways answer to `nest-grpc-gateway` on the
#      edge network (two addresses), the doctor fails both deployments, and two
#      default route files share router, service and transport names, so Traefik
#      keeps one and leaves a name unrouted: the collision this issue reports;
#   2. per-deployment aliases and `render.sh --instance` route files, for each
#      Traefik family (pre-v3.5 loadBalancer PROXY v2, v3.5+ serversTransport): the
#      doctor passes both, each name reaches only its own gateway and Controller with
#      its own certificate, an unknown name is refused, calls are attributed to the
#      outside client (PROXY v2), neither gateway can reach the other's Controller,
#      and no container publishes a host port;
#   3. Main is recreated, then taken down and brought back (its rollback), and its
#      route removed and restored, while Release keeps serving every handshake and
#      its containers, route file and database are untouched; Main's data survives;
#   4. negative preflights: a colliding origin subnet, edge alias or HPC workdir, and
#      a gateway alias that is a Compose service name.
#
# Needs Docker, openssl and the image in NEST_CONTROLLER_IMAGE. Contacts no real host,
# DNS name, certificate authority, Tailnet or HPC cluster; every credential is a
# throwaway fixture. NEST_SMOKE_TRAEFIK_TAGS picks the Traefik images.
set -euo pipefail

cd "$(dirname "$0")/.."
fixture=$(mktemp -d)
export NEST_CONTROLLER_IMAGE=${NEST_CONTROLLER_IMAGE:-nest-controller:test}
read -r -a traefik_tags <<<"${NEST_SMOKE_TRAEFIK_TAGS:-v3.3 v3.6.2}"
label=nest-multi-smoke
edge=nest-multi-smoke-edge
outside=nest-multi-smoke-outside
edge_proxy=10.232.0.7
traefik=nest-multi-smoke-traefik
dyn=$fixture/dynamic
doctor=(python3 scripts/nest-deploy-doctor.py)
declare -A project=([main]=nest-main-smoke [release]=nest-release-smoke)
declare -A subnet=([main]=10.253.250.0/28 [release]=10.253.251.0/28)
declare -A gateway_ip=([main]=10.253.250.3 [release]=10.253.251.3)
declare -A host=([main]=grpc.main.smoke.test [release]=grpc.release.smoke.test)
# One throwaway password per deployment, kept across every rewrite of its .env.
declare -A password=([main]="smoke-main-$(openssl rand -hex 8)" [release]="smoke-release-$(openssl rand -hex 8)")
mkdir -p "$dyn"

compose() { # env args...
    local name=$1
    shift
    docker compose -p "${project[$name]}" -f compose.dokploy.yaml -f tests/compose.multi-env-fixture.yaml \
        --env-file "$fixture/$name.env" --profile local-db --profile migrate --profile public-grpc "$@"
}

cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then
        for name in main release; do
            compose "$name" logs --no-color --tail 40 controller grpc-gateway 2>/dev/null || true
        done
        docker logs --tail 40 "$traefik" 2>&1 || true
    fi
    docker ps --all --quiet --filter "label=$label" | xargs -r docker rm -f >/dev/null 2>&1 || true
    for name in main release; do
        compose "$name" down --timeout 10 --volumes --remove-orphans >/dev/null 2>&1 || true
    done
    docker network rm "$edge" "$outside" >/dev/null 2>&1 || true
    rm -rf "$fixture"
}
trap cleanup EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# --- Fixture: the shared edge and outside networks, one CA, a leaf per name --------
docker network create --subnet 10.232.0.0/24 --label "$label" "$edge" >/dev/null
docker network create --label "$label" "$outside" >/dev/null
outside_subnet=$(docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "$outside")

openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=nest-multi-smoke-ca \
    -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign \
    -keyout "$fixture/ca.key" -out "$fixture/ca.pem" 2>/dev/null
for name in main release; do
    mkdir -p "$fixture/certs-$name"
    openssl req -newkey rsa:2048 -nodes -subj "/CN=${host[$name]}" \
        -keyout "$fixture/certs-$name/privkey.pem" -out "$fixture/$name.csr" 2>/dev/null
    printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "${host[$name]}" > "$fixture/$name.ext"
    openssl x509 -req -in "$fixture/$name.csr" -CA "$fixture/ca.pem" -CAkey "$fixture/ca.key" \
        -CAcreateserial -days 2 -extfile "$fixture/$name.ext" -out "$fixture/certs-$name/fullchain.pem" 2>/dev/null
done
ssh-keygen -q -t ed25519 -N '' -C '' -f "$fixture/id_ed25519"

write_env() { # env alias(empty: the default) allowed_peers [workdir]
    local name=$1 alias=$2 peers=$3 workdir=${4:-/home/svc-nest/nest-$1}
    {
        printf "COMPOSE_PROFILES='local-db,migrate,public-grpc'\n"
        printf "NEST_DATABASE_URL='postgresql://nest@postgres:5432/nest'\n"
        printf "NEST_DATABASE_PASSWORD='%s'\n" "${password[$name]}"
        printf "NEST_DATABASE_REQUIRED='true'\n"
        printf "NEST_TAILSCALE_AUTHKEY=''\n"
        printf "NEST_TAILSCALE_HOSTNAME='nest-controller-%s'\n" "$name"
        printf "NEST_EXECUTOR='slurm'\n"
        printf "NEST_SSH_CONFIG='%s'\n" 'Host fixture-hpc\n    HostName fixture.example\n    User svc-nest\n    IdentityFile ~/.ssh/id_nest\n    IdentitiesOnly yes\n    BatchMode yes\n    StrictHostKeyChecking yes'
        printf "NEST_SSH_PRIVATE_KEY='%s'\n" "$(awk '{printf "%s\\n", $0}' "$fixture/id_ed25519")"
        printf "NEST_SSH_KNOWN_HOSTS_DATA='fixture.example %s'\n" "$(cat "$fixture/id_ed25519.pub")"
        printf "NEST_PLATFORMS_ENV='%s'\n" "NEST_PLATFORMS=lab\nNEST_PLATFORM_LAB_NAME=Lab cluster\nNEST_PLATFORM_LAB_OPERATOR=example-university\nNEST_PLATFORM_LAB_LOGIN_NODES=fixture-hpc\nNEST_PLATFORM_LAB_SSH_USER=svc-nest\nNEST_PLATFORM_LAB_ACCOUNT=lab\nNEST_PLATFORM_LAB_WORKDIR=$workdir"
        printf "NEST_LIMIT_ACTIVE_PER_PLATFORM='2'\n"
        printf "NEST_PUBLIC_PILOT='true'\n"
        printf "NEST_CONTROLLER_AUTH='session'\n"
        printf "NEST_EXECUTION_REGISTRY='required'\n"
        printf "NEST_LIMIT_BATCH_SCRIPTS='refuse'\n"
        printf "NEST_CONTROLLER_ALLOW_PLAINTEXT='true'\n"
        printf "NEST_ENVIRONMENT_TIMEOUT='540'\n"
        printf "NEST_GRPC_ORIGIN_SUBNET='%s'\n" "${subnet[$name]}"
        printf "NEST_GRPC_GATEWAY_ADDRESS='%s'\n" "${gateway_ip[$name]}"
        printf "NEST_GRPC_TRUSTED_PROXIES='%s/32'\n" "${gateway_ip[$name]}"
        printf "NEST_GRPC_ALLOWED_PEERS='%s'\n" "$peers"
        printf "NEST_GRPC_EDGE_NETWORK='%s'\n" "$edge"
        printf "NEST_GRPC_EDGE_PROXY='%s/32'\n" "$edge_proxy"
        printf "NEST_GRPC_PUBLIC_HOST='%s'\n" "${host[$name]}"
        [ -z "$alias" ] || printf "NEST_GRPC_EDGE_ALIAS='%s'\n" "$alias"
    } > "$fixture/$name.env"
    chmod 600 "$fixture/$name.env"
}

declare -A peers alias
alias=([main]="" [release]="")
for name in main release; do
    write_env "$name" "" "${gateway_ip[$name]}/32"
    compose "$name" up --no-start --no-build --pull missing --no-deps postgres controller grpc-gateway tailscale >/dev/null 2>&1
    tailnet=$(docker network inspect --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' "${project[$name]}_tailnet")
    [ -n "$tailnet" ] || fail "no Tailnet bridge for $name"
    # Exactly this deployment's gateway and its own Tailnet bridge.
    peers[$name]="${gateway_ip[$name]}/32,$tailnet"
    write_env "$name" "" "${peers[$name]}"
    docker run --rm --user 0 --entrypoint sh -v "${project[$name]}_grpc-certificates:/certs" \
        -v "$fixture/certs-$name:/src:ro" envoyproxy/envoy:v1.36.2 -euc '
            cp /src/fullchain.pem /src/privkey.pem /certs/
            chown 0:101 /certs/fullchain.pem /certs/privkey.pem
            chmod 0644 /certs/fullchain.pem
            chmod 0640 /certs/privkey.pem'
    compose "$name" up -d --no-build --pull never --wait --wait-timeout 120 postgres >/dev/null
    compose "$name" run --rm --no-deps --pull never migrate >/dev/null
    compose "$name" up -d --no-build --pull never --no-deps --wait --wait-timeout 90 controller >/dev/null
    compose "$name" up -d --no-build --pull never --no-deps grpc-gateway >/dev/null
done

start_traefik() { # tag
    docker rm -f "$traefik" >/dev/null 2>&1 || true
    docker run -d --name "$traefik" --label "$label" --network "$edge" --ip "$edge_proxy" \
        -v "$dyn:/dyn:ro" "traefik:$1" --entryPoints.websecure.address=:443 \
        --providers.file.directory=/dyn --providers.file.watch=true >/dev/null
    docker network connect --alias "${host[main]}" --alias "${host[release]}" \
        --alias grpc.unknown.smoke.test "$outside" "$traefik"
}

traefik_version() { docker run --rm "traefik:$1" version | awk '/^Version/{print $2}'; }

render_routes() { # version mode(default|shared-alias|instance)
    rm -f "$dyn"/*.yml
    for name in main release; do
        case $2 in
            default) deploy/public-grpc/traefik/render.sh "${host[$name]}" "v$1" ;;
            shared-alias) deploy/public-grpc/traefik/render.sh "${host[$name]}" "v$1" \
                --instance "$name" --edge-alias nest-grpc-gateway ;;
            instance) deploy/public-grpc/traefik/render.sh "${host[$name]}" "v$1" --instance "$name" ;;
        esac > "$dyn/nest-grpc-$name.yml.tmp"
        mv "$dyn/nest-grpc-$name.yml.tmp" "$dyn/nest-grpc-$name.yml"
    done
}

# TLS handshakes from the outside network, through Traefik, for one name.
handshakes() { # name count
    docker run --rm --network "$outside" -v "$PWD/tests:/tests:ro" -v "$fixture/ca.pem:/ca.pem:ro" \
        --entrypoint python "$NEST_CONTROLLER_IMAGE" /tests/tls_sni_probe.py "$1:443" "$1" \
        --ca-file /ca.pem --count "$2" --timeout 3
}

field() { python3 -c "import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])" "$1" "$2"; }

wait_routed() { # name
    for _ in $(seq 30); do
        [ "$(field "$(handshakes "$1" 1)" ok)" = 1 ] && return 0
        sleep 1
    done
    fail "$1 is not routed"
}

preflight() { # env [extra args]
    local name=$1
    shift
    "${doctor[@]}" preflight --project "${project[$name]}" --env-file "$fixture/$name.env" \
        --traefik-container "$traefik" --traefik-dynamic-dir "$dyn" "$@"
}

runtime() { "${doctor[@]}" gateway-runtime --project "${project[$1]}"; }

container_id() { compose "$1" ps --all --quiet "$2"; }
container_ip() { # env service network
    docker inspect --format "{{(index .NetworkSettings.Networks \"$3\").IPAddress}}" "$(container_id "$1" "$2")"
}

# --- 1. The fixed alias: the collision ----------------------------------------------
first_version=$(traefik_version "${traefik_tags[0]}")
render_routes "$first_version" default
start_traefik "${traefik_tags[0]}"
sleep 3

resolved=$(docker run --rm --network "$edge" --entrypoint getent "$NEST_CONTROLLER_IMAGE" ahosts nest-grpc-gateway \
    | awk '{print $1}' | sort -u | tr '\n' ' ')
[ "$(wc -w <<<"$resolved")" -eq 2 ] || fail "expected nest-grpc-gateway to name both gateways, got: $resolved"
echo "PASS: [collision] nest-grpc-gateway on the edge network resolves to both gateways: $resolved"

for name in main release; do
    preflight "$name" > "$fixture/collision-preflight-$name.txt" && fail "[collision] preflight passed $name"
    grep -q '^FAIL edge_alias: nest-grpc-gateway on .* is also the gateway alias of' "$fixture/collision-preflight-$name.txt" \
        || { cat "$fixture/collision-preflight-$name.txt"; fail "[collision] shared alias not flagged for $name"; }
    grep -q '^FAIL route_names: .*router nest-grpc in nest-grpc-main.yml, nest-grpc-release.yml' \
        "$fixture/collision-preflight-$name.txt" || fail "[collision] duplicate route names not flagged"
    runtime "$name" > "$fixture/collision-runtime-$name.txt" && fail "[collision] gateway-runtime passed $name"
    grep -q '^FAIL edge_alias: nest-grpc-gateway on .* also names' "$fixture/collision-runtime-$name.txt" \
        || { cat "$fixture/collision-runtime-$name.txt"; fail "[collision] runtime alias collision not flagged"; }
done
echo "PASS: [collision] preflight and gateway-runtime fail both deployments (alias and route names)"

# Traefik keeps one definition of each duplicated name, so one of the two names is
# not routed at all; the other may still land on the wrong gateway (shared alias).
unrouted=0
for name in main release; do
    result=$(handshakes "${host[$name]}" 10)
    [ "$(field "$result" ok)" = 0 ] && unrouted=$((unrouted + 1))
    echo "INFO: [collision] ${host[$name]} with duplicate router names: $result"
done
[ "$unrouted" -ge 1 ] || fail "[collision] both names routed although their router names are defined twice"
echo "PASS: [collision] duplicate route names leave $unrouted of 2 names unrouted"

# Unique route names, but both still dial the shared alias: a name can land on the
# other deployment's gateway, which refuses it. Docker's resolver decides, so this
# is reported, not required.
render_routes "$first_version" shared-alias
sleep 3
crossed=0
for name in main release; do
    result=$(handshakes "${host[$name]}" 20)
    crossed=$((crossed + $(field "$result" failed)))
    echo "INFO: [collision] ${host[$name]} via the shared alias: $result"
done
echo "INFO: [collision] $crossed of 40 handshakes reached the other deployment's gateway"

# --- 2. Per-deployment aliases and route files ----------------------------------------
alias=([main]=nest-grpc-gateway-main [release]=nest-grpc-gateway-release)
for name in main release; do
    write_env "$name" "${alias[$name]}" "${peers[$name]}"
    compose "$name" up -d --no-build --pull never --no-deps --force-recreate grpc-gateway >/dev/null
done

check_environment() { # env other tag
    local name=$1 other=$2 tag=$3 since report
    report="$fixture/preflight-$name-$tag.txt"
    preflight "$name" > "$report" || { cat "$report"; fail "[$tag] preflight failed for $name"; }
    for check in edge_alias public_host tailnet_hostname database_host hpc_workdir \
        route_names route_sni route_target origin_subnet_free trusted_proxies allowed_peers edge_proxy; do
        grep -q "^PASS $check: " "$report" || { cat "$report"; fail "[$tag] $check did not pass for $name"; }
    done
    grep -q '^WARN hpc_account: ' "$report" || fail "[$tag] the shared Slurm account was not reported"
    for _ in $(seq 20); do
        runtime "$name" > "$fixture/runtime-$name-$tag.txt" && break
        sleep 1
    done
    grep -q '^FAIL' "$fixture/runtime-$name-$tag.txt" && { cat "$fixture/runtime-$name-$tag.txt"; fail "[$tag] gateway-runtime failed for $name"; }
    grep -q "^PASS edge_alias: ${alias[$name]} names only this gateway" "$fixture/runtime-$name-$tag.txt" \
        || fail "[$tag] edge alias not verified for $name"

    # Every handshake for this name gets this deployment's own certificate.
    result=$(handshakes "${host[$name]}" 20)
    [ "$result" = "{\"ok\": 20, \"failed\": 0, \"names\": [\"${host[$name]}\"], \"errors\": []}" ] \
        || fail "[$tag] ${host[$name]} handshakes: $result"

    # The full credential-free verification, and the call reaches this Controller only.
    since=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
    docker run --rm --network "$outside" -v "$PWD/scripts:/scripts:ro" -v "$fixture/ca.pem:/ca.pem:ro" \
        --entrypoint python "$NEST_CONTROLLER_IMAGE" /scripts/verify-public-grpc.py "${host[$name]}:443" \
        --ca-file /ca.pem --timeout 5 \
        --origin "$(container_ip "$name" controller "$edge"):50051" \
        --origin "$(container_ip "$name" grpc-gateway "$edge"):8443" \
        > "$fixture/verify-$name-$tag.txt" || { cat "$fixture/verify-$name-$tag.txt"; fail "[$tag] verification failed for $name"; }
    sleep 1
    compose "$name" logs --no-color --since "$since" controller > "$fixture/own-$name-$tag.log"
    compose "$other" logs --no-color --since "$since" controller > "$fixture/other-$name-$tag.log"
    python3 - "$fixture/own-$name-$tag.log" "$fixture/other-$name-$tag.log" "$outside_subnet" <<'PY'
import ipaddress, re, sys
own, other, subnet = open(sys.argv[1]).read(), open(sys.argv[2]).read(), ipaddress.ip_network(sys.argv[3])
pattern = r"RPC started method=ListEligibleProjects client=(\S+)"
clients = re.findall(pattern, own)
assert clients, "the call did not reach this deployment's Controller"
for client in clients:
    assert ipaddress.ip_address(client) in subnet, f"attributed to {client}, not the outside client"
assert not re.findall(pattern, other), "the other deployment's Controller received the call"
PY
    echo "PASS: [$tag] ${host[$name]} -> ${project[$name]} only, own certificate, client attributed via PROXY v2"
}

check_isolation_paths() {
    local name other target
    for name in main release; do
        other=release
        [ "$name" = main ] || other=main
        target=$(container_ip "$other" controller "${project[$other]}_grpc-origin")
        if docker exec "$(container_id "$name" grpc-gateway)" bash -c "timeout 3 bash -c '</dev/tcp/$target/50051'" 2>/dev/null; then
            fail "$name's gateway reached $other's Controller at $target"
        fi
    done
    unknown=$(handshakes grpc.unknown.smoke.test 3)
    [ "$(field "$unknown" ok)" = 0 ] || fail "an unknown name was served: $unknown"
    for name in main release; do
        for id in $(compose "$name" ps --all --quiet); do
            ports=$(docker inspect --format '{{json .HostConfig.PortBindings}}' "$id")
            case $ports in "{}" | null) ;; *) fail "$name container $id publishes $ports" ;; esac
        done
    done
    echo "PASS: no cross-deployment origin path, unknown name refused, no host ports"
}

for tag in "${traefik_tags[@]}"; do
    version=$(traefik_version "$tag")
    render_routes "$version" instance
    start_traefik "$tag"
    wait_routed "${host[main]}"
    wait_routed "${host[release]}"
    check_environment main release "traefik-$version"
    check_environment release main "traefik-$version"
done
check_isolation_paths

# --- 3. Redeploy, rollback and route removal of Main only -------------------------
main_pg=$(container_id main postgres)
release_pg=$(container_id release postgres)
docker exec "$main_pg" psql -q -U nest -d nest -c "CREATE TABLE smoke_marker (environment text); INSERT INTO smoke_marker VALUES ('main');"
release_before=$(for s in postgres controller grpc-gateway; do docker inspect --format '{{.Id}} {{.State.StartedAt}}' "$(container_id release "$s")"; done)
release_route=$(sha256sum "$dyn/nest-grpc-release.yml")

during_main_change() { # description command...
    local description=$1 result
    shift
    handshakes "${host[release]}" 60 > "$fixture/during.json" &
    local watcher=$!
    "$@"
    wait "$watcher"
    result=$(cat "$fixture/during.json")
    [ "$(field "$result" failed)" = 0 ] && [ "$(field "$result" ok)" = 60 ] \
        || fail "Release was disrupted while Main $description: $result"
    echo "PASS: Release served 60/60 handshakes while Main $description"
}

recreate_main() {
    compose main up -d --no-build --pull never --no-deps --force-recreate --wait --wait-timeout 90 controller >/dev/null
    compose main up -d --no-build --pull never --no-deps --force-recreate grpc-gateway >/dev/null
}
rollback_main() {
    compose main down --timeout 10 >/dev/null
    # The Tailscale sidecar is created, never started (no Tailnet in this test).
    compose main up --no-start --no-build --no-deps tailscale >/dev/null 2>&1
    compose main up -d --no-build --pull never --wait --wait-timeout 120 postgres >/dev/null
    compose main run --rm --no-deps --pull never migrate >/dev/null
    compose main up -d --no-build --pull never --no-deps --wait --wait-timeout 90 controller >/dev/null
    compose main up -d --no-build --pull never --no-deps grpc-gateway >/dev/null
}
remove_main_route() {
    mv "$dyn/nest-grpc-main.yml" "$fixture/nest-grpc-main.yml.removed"
    sleep 3
    result=$(handshakes "${host[main]}" 3)
    [ "$(field "$result" ok)" = 0 ] || fail "Main still routed after its route was removed: $result"
    mv "$fixture/nest-grpc-main.yml.removed" "$dyn/nest-grpc-main.yml"
}

during_main_change "was recreated" recreate_main
during_main_change "was taken down and redeployed (rollback)" rollback_main
during_main_change "lost and regained its route" remove_main_route

release_after=$(for s in postgres controller grpc-gateway; do docker inspect --format '{{.Id}} {{.State.StartedAt}}' "$(container_id release "$s")"; done)
[ "$release_before" = "$release_after" ] || fail "a Release container was restarted or replaced"
[ "$release_route" = "$(sha256sum "$dyn/nest-grpc-release.yml")" ] || fail "Release's route file changed"
[ "$(docker exec "$(container_id main postgres)" psql -tA -U nest -d nest -c 'SELECT environment FROM smoke_marker')" = main ] \
    || fail "Main's data did not survive its rollback"
[ "$(docker exec "$release_pg" psql -tA -U nest -d nest -c "SELECT to_regclass('smoke_marker') IS NULL")" = t ] \
    || fail "Main's table appeared in Release's database"
volumes_main=$(docker volume ls --quiet --filter "label=com.docker.compose.project=${project[main]}" | sort)
volumes_release=$(docker volume ls --quiet --filter "label=com.docker.compose.project=${project[release]}" | sort)
[ -z "$(comm -12 <(echo "$volumes_main") <(echo "$volumes_release"))" ] || fail "the deployments share a volume"
echo "PASS: Release untouched (containers, route, database); Main's data survived; no shared volume"
wait_routed "${host[main]}"
check_environment main release "after-rollback"

# --- 4. Negative preflights -----------------------------------------------------------
cp "$fixture/release.env" "$fixture/release.env.good"
negative() { # description expected-line sed-expression
    sed "$3" "$fixture/release.env.good" > "$fixture/release.env"
    if preflight release > "$fixture/negative.txt"; then
        cat "$fixture/negative.txt"
        fail "preflight passed $1"
    fi
    grep -q "$2" "$fixture/negative.txt" || { cat "$fixture/negative.txt"; fail "$1 not flagged"; }
    echo "PASS: preflight refuses $1"
}
negative "Main's origin subnet" "^FAIL origin_subnet_free: .*origin network of deployment ${project[main]}" \
    "s|^NEST_GRPC_ORIGIN_SUBNET=.*|NEST_GRPC_ORIGIN_SUBNET='${subnet[main]}'|;s|^NEST_GRPC_GATEWAY_ADDRESS=.*|NEST_GRPC_GATEWAY_ADDRESS='${gateway_ip[main]}'|"
negative "Main's edge alias" "^FAIL edge_alias: ${alias[main]} on $edge is also the gateway alias of ${project[main]}" \
    "s|^NEST_GRPC_EDGE_ALIAS=.*|NEST_GRPC_EDGE_ALIAS='${alias[main]}'|"
negative "Main's public name" "^FAIL public_host: " \
    "s|^NEST_GRPC_PUBLIC_HOST=.*|NEST_GRPC_PUBLIC_HOST='${host[main]}'|"
# Main's sidecar exists but never starts here (no Tailnet), so this is the warning a
# stopped deployment gets; a running one fails (tests/test_multi_environment.py).
sed "s|^NEST_TAILSCALE_HOSTNAME=.*|NEST_TAILSCALE_HOSTNAME='nest-controller-main'|" \
    "$fixture/release.env.good" > "$fixture/release.env"
preflight release > "$fixture/negative.txt" || true
grep -q "^WARN tailnet_hostname: Tailscale hostname nest-controller-main is also used by ${project[main]} (stopped)" \
    "$fixture/negative.txt" || { cat "$fixture/negative.txt"; fail "Main's Tailnet hostname not flagged"; }
echo "PASS: preflight flags Main's Tailnet hostname (stopped sidecar: WARN)"
negative "Main's HPC workdir" "^FAIL hpc_workdir: .*LAB = ${project[main]} LAB" \
    "s|nest-release|nest-main|"
cp "$fixture/release.env.good" "$fixture/release.env"

docker run --rm --entrypoint /bin/sh -v "$PWD/deploy/public-grpc:/opt/nest-grpc:ro" \
    -e NEST_GRPC_EDGE_PROXY="$edge_proxy/32" -e NEST_GRPC_EDGE_ALIAS=grpc-gateway \
    envoyproxy/envoy:v1.36.2 /opt/nest-grpc/gateway-entrypoint.sh > "$fixture/alias.txt" 2>&1 \
    && fail "the gateway started with a Compose service name as its alias"
grep -q "NEST_GRPC_EDGE_ALIAS grpc-gateway is a Compose service name" "$fixture/alias.txt" \
    || { cat "$fixture/alias.txt"; fail "no alias refusal"; }
echo "PASS: the gateway refuses a Compose service name as its edge alias"

echo "PASS: Main and Release isolated on one Dokploy-like host (Traefik ${traefik_tags[*]})"
