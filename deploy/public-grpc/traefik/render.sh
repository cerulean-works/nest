#!/bin/sh
# Renders the Traefik passthrough route for the Nest gRPC gateway (H08) for one name
# and one Traefik version, to standard output. Nothing is installed or changed.
#
#   render.sh grpc.staging.example.org v3.3.7 > nest-grpc.yml
#   render.sh grpc.staging.example.org "$(docker exec dokploy-traefik traefik version | awk '/^Version/{print $2}')"
#
# Several Nest deployments on one Traefik (#118), each with its own route file:
#
#   render.sh grpc.nest.example.org v3.6.2 --instance release > nest-grpc-release.yml
#   render.sh grpc.main.nest.example.org v3.6.2 --instance main > nest-grpc-main.yml
#
# --instance ID gives the file its own router (nest-grpc-ID), service
# (nest-grpc-gateway-ID) and serversTransport (nest-grpc-proxy-v2-ID) names, because
# Traefik's file provider keeps only one definition of a name that two files define,
# so the other deployment's name would not be routed. The target becomes the gateway alias nest-grpc-gateway-ID, which must be
# that deployment's NEST_GRPC_EDGE_ALIAS. --edge-alias ALIAS names another alias,
# such as nest-grpc-gateway for a deployment that still uses the default. Without
# options the output is the single-deployment file, unchanged.
#
# Traefik v2.x to v3.4 take `proxyProtocol` on the TCP loadBalancer (nest-grpc.yml);
# v3.5 and later on a TCP serversTransport (nest-grpc.servers-transport.yml). Both
# forms were checked against real Traefik images by tests/check_traefik_passthrough.sh.
set -eu
fail() {
    echo "render.sh: $*" >&2
    exit 2
}
usage="usage: render.sh NAME TRAEFIK_VERSION [--instance ID] [--edge-alias ALIAS]"
instance=
alias=
instance_set=
alias_set=
positional=
while [ $# -gt 0 ]; do
    case $1 in
        --instance)
            [ $# -ge 2 ] || fail "$usage"
            instance=$2
            instance_set=1
            shift 2
            ;;
        --edge-alias)
            [ $# -ge 2 ] || fail "$usage"
            alias=$2
            alias_set=1
            shift 2
            ;;
        -*) fail "$usage" ;;
        *)
            positional="$positional
$1"
            shift
            ;;
    esac
done
# Exactly NAME and TRAEFIK_VERSION, one per line (neither may contain a newline).
count=$(printf '%s\n' "$positional" | sed '1d' | grep -c '' || true)
[ "$count" -eq 2 ] || fail "$usage"
host=$(printf '%s\n' "$positional" | sed -n 2p)
raw_version=$(printf '%s\n' "$positional" | sed -n 3p)
version=${raw_version#v}
here=$(cd "$(dirname "$0")" && pwd)
echo "$host" | grep -Eqx '[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+' \
    || fail "NAME must be a DNS name, got '$host'"
major=${version%%.*}
rest=${version#*.}
minor=${rest%%.*}
echo "$major" | grep -Eqx '[0-9]+' && echo "$minor" | grep -Eqx '[0-9]+' \
    || fail "TRAEFIK_VERSION must look like v3.3.7, got '$raw_version'"
if [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 5 ]; }; then
    source=$here/nest-grpc.servers-transport.yml
elif [ "$major" -ge 2 ]; then
    source=$here/nest-grpc.yml
else
    fail "Traefik $raw_version is not supported (v2.x or later)"
fi

# Lower-case DNS labels, so a name never needs quoting in YAML or in a Traefik rule.
if [ -n "$instance_set" ]; then
    echo "$instance" | grep -Eqx '[a-z0-9]([a-z0-9-]{0,22}[a-z0-9])?' \
        || fail "--instance must be 1 to 24 lower-case letters, digits or inner hyphens, got '$instance'"
    router=nest-grpc-$instance
    service=nest-grpc-gateway-$instance
    transport=nest-grpc-proxy-v2-$instance
    [ -n "$alias_set" ] || alias=nest-grpc-gateway-$instance
else
    router=nest-grpc
    service=nest-grpc-gateway
    transport=nest-grpc-proxy-v2
    [ -n "$alias_set" ] || alias=nest-grpc-gateway
fi
echo "$alias" | grep -Eqx '[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?' \
    || fail "--edge-alias must be a lower-case DNS label, got '$alias'"
# Compose service names are also DNS names on every network a service joins, so on
# a shared edge network they resolve to every deployment's container (#109, #118).
case $alias in
    grpc-gateway | controller | controller-grpc-origin | tailscale | postgres | migrate | grpc-certificates)
        fail "--edge-alias $alias is a Compose service name, which every deployment on the edge network shares; use the gateway's own NEST_GRPC_EDGE_ALIAS"
        ;;
esac

# Only the exact identifier lines of the reviewed files change; with the defaults the
# output is byte-for-byte the file itself.
rendered=$(sed \
    -e "s|HostSNI(\`grpc.nest.cerulean.works\`)|HostSNI(\`$host\`)|" \
    -e "s|^    nest-grpc:\$|    $router:|" \
    -e "s|^      service: nest-grpc-gateway\$|      service: $service|" \
    -e "s|^    nest-grpc-gateway:\$|    $service:|" \
    -e "s|^          - address: nest-grpc-gateway:8443\$|          - address: $alias:8443|" \
    -e "s|^        serversTransport: nest-grpc-proxy-v2\$|        serversTransport: $transport|" \
    -e "s|^    nest-grpc-proxy-v2:\$|    $transport:|" \
    "$source")
# Fail closed if the reviewed file changed shape and an identifier was not replaced.
printf '%s\n' "$rendered" | grep -qx "    $router:" || fail "router name not set; $source changed"
printf '%s\n' "$rendered" | grep -qx "      service: $service" || fail "service not set; $source changed"
printf '%s\n' "$rendered" | grep -qx "          - address: $alias:8443" || fail "target not set; $source changed"
printf '%s\n' "$rendered" | grep -qF "HostSNI(\`$host\`)" || fail "rule not set; $source changed"
if [ "$source" = "$here/nest-grpc.servers-transport.yml" ]; then
    printf '%s\n' "$rendered" | grep -qx "        serversTransport: $transport" || fail "transport not set; $source changed"
fi
if [ -n "$instance_set" ]; then
    printf '# Nest instance: %s; edge alias: %s (NEST_GRPC_EDGE_ALIAS of that deployment).\n' \
        "$instance" "$alias"
fi
printf '%s\n' "$rendered"
