#!/bin/bash
# Checks that a Traefik image routes the Nest gRPC name by SNI with TLS passthrough
# and prepends a PROXY protocol v2 header, using the file render.sh picks for it.
# Disposable fixture: a private Docker network, Traefik, and a listener that records
# the first bytes it receives. No Nest, credential or public name is involved.
#
#   tests/check_traefik_passthrough.sh [TRAEFIK_TAG ...]   (default: v2.11 v3.3 v3.6.2)
#
# Prints one line per tag and exits 1 if any tag does not deliver the header.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
tags=("$@")
[ ${#tags[@]} -gt 0 ] || tags=(v2.11 v3.3 v3.6.2)
work=$(mktemp -d)
id=nest-traefik-check-$$
cleanup() {
    docker rm -f "$id-traefik" "$id-backend" >/dev/null 2>&1 || true
    docker network rm "$id" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT
docker network create "$id" >/dev/null
status=0
for tag in "${tags[@]}"; do
    version=$(docker run --rm "traefik:$tag" version | awk '/^Version/{print $2}')
    mkdir -p "$work/$tag"
    "$ROOT/deploy/public-grpc/traefik/render.sh" grpc.check.invalid "v$version" \
        | sed 's|nest-grpc-gateway:8443|'"$id"'-backend:8443|' > "$work/$tag/nest-grpc.yml"
    : > "$work/$tag/dump"
    chmod 666 "$work/$tag/dump"
    docker run -d --name "$id-backend" --network "$id" -v "$work/$tag:/out" busybox:1.37 \
        sh -c 'nc -l -p 8443 > /out/dump' >/dev/null
    docker run -d --name "$id-traefik" --network "$id" -p 127.0.0.1::443 \
        -v "$work/$tag:/dyn:ro" "traefik:$tag" \
        --entryPoints.websecure.address=:443 --providers.file.directory=/dyn >/dev/null
    port=$(docker port "$id-traefik" 443/tcp | head -n 1 | sed 's/.*://')
    sleep 3
    timeout 5 openssl s_client -connect "127.0.0.1:$port" -servername grpc.check.invalid \
        -alpn h2 </dev/null >/dev/null 2>&1 || true
    sleep 1
    signature=$(head -c 12 "$work/$tag/dump" | od -An -tx1 | tr -d ' \n')
    if [ "$signature" = 0d0a0d0a000d0a515549540a ]; then
        echo "PASS traefik $version: SNI passthrough with a PROXY v2 header"
    else
        echo "FAIL traefik $version: no PROXY v2 header (got '${signature:-nothing}')"
        docker logs "$id-traefik" 2>&1 | grep -E 'ERR|error' | head -n 3 || true
        status=1
    fi
    docker rm -f "$id-traefik" "$id-backend" >/dev/null
done
exit $status
