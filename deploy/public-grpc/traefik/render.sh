#!/bin/sh
# Renders the Traefik passthrough route for the Nest gRPC gateway (H08) for one name
# and one Traefik version, to standard output. Nothing is installed or changed.
#
#   render.sh grpc.staging.example.org v3.3.7 > nest-grpc.yml
#   render.sh grpc.staging.example.org "$(docker exec dokploy-traefik traefik version | awk '/^Version/{print $2}')"
#
# Traefik v2.x to v3.4 take `proxyProtocol` on the TCP loadBalancer (nest-grpc.yml);
# v3.5 and later on a TCP serversTransport (nest-grpc.servers-transport.yml). Both
# forms were checked against real Traefik images by tests/check_traefik_passthrough.sh.
set -eu
fail() {
    echo "render.sh: $*" >&2
    exit 2
}
[ $# -eq 2 ] || fail "usage: render.sh NAME TRAEFIK_VERSION"
host=$1
version=${2#v}
here=$(cd "$(dirname "$0")" && pwd)
echo "$host" | grep -Eqx '[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+' \
    || fail "NAME must be a DNS name, got '$host'"
major=${version%%.*}
rest=${version#*.}
minor=${rest%%.*}
echo "$major" | grep -Eqx '[0-9]+' && echo "$minor" | grep -Eqx '[0-9]+' \
    || fail "TRAEFIK_VERSION must look like v3.3.7, got '$2'"
if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 5 ]; }; then
    source=$here/nest-grpc.servers-transport.yml
elif [ "$major" -ge 2 ]; then
    source=$here/nest-grpc.yml
else
    fail "Traefik $2 is not supported (v2.x or later)"
fi
sed "s|HostSNI(\`grpc.nest.cerulean.works\`)|HostSNI(\`$host\`)|" "$source"
