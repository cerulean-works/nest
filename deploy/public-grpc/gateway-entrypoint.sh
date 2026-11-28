#!/bin/sh
# Renders the Nest public gRPC gateway's Envoy configuration and runs Envoy (H08).
#
#   gateway-entrypoint.sh              render into $NEST_GRPC_RUN_DIR, then exec envoy
#   gateway-entrypoint.sh --render DIR render into DIR only (tests, `envoy --mode validate`)
#
# Every value is validated before it is substituted, and the script fails closed: an
# unset edge proxy, a malformed name or address, or a missing template stops it.
#
#   NEST_GRPC_PUBLIC_HOST  SNI name served (default grpc.nest.cerulean.works)
#   NEST_GRPC_EDGE_PROXY   required: the edge proxy's address as a CIDR, such as
#                          10.0.1.7/32 (Dokploy's Traefik on dokploy-network). Only
#                          connections from it are accepted, and only it may send the
#                          PROXY protocol header that names the client.
#   NEST_GRPC_EDGE_ALIAS   this gateway's DNS alias on the edge network, which the
#                          Traefik route dials (default nest-grpc-gateway). Only
#                          validated here: a lower-case DNS label and never a Compose
#                          service name, which every deployment on a shared edge
#                          network has (#118). Uniqueness is checked by
#                          `nest-deploy-doctor.py preflight` and `gateway-runtime`.
#   NEST_GRPC_UPSTREAM     Controller host:port on the private network
#                          (controller-grpc-origin:50051, a name that exists only on
#                          grpc-origin; the generic `controller` name also resolves on
#                          the edge network Dokploy attaches, #109)
#   NEST_GRPC_ORIGIN_SUBNET the private network's IPv4 CIDR. When set, Envoy is not
#                          started unless every address the upstream name resolves to
#                          lies inside it (checked with getent; not with --render)
#   NEST_GRPC_LISTEN_PORT  TLS listener port (8443); never published on the host
#   NEST_GRPC_ADMIN_PORT   admin port on 127.0.0.1 only (9901)
#   NEST_GRPC_CERT_DIR     directory holding fullchain.pem and privkey.pem (/certs)
#   NEST_GRPC_TEMPLATE     template path (envoy.yaml.template next to this script)
#   NEST_GRPC_RUN_DIR      where the rendered files go (/tmp/nest-grpc)
#   NEST_ENVOY_BINARY      envoy executable (envoy)
set -eu
export LC_ALL=C

fail() {
    echo "nest-grpc-gateway: $*" >&2
    exit 2
}

here=$(cd "$(dirname "$0")" && pwd)
render_only=
out=${NEST_GRPC_RUN_DIR:-/tmp/nest-grpc}
if [ "${1:-}" = "--render" ]; then
    [ -n "${2:-}" ] || fail "--render needs a directory"
    render_only=1
    out=$2
fi

host=${NEST_GRPC_PUBLIC_HOST:-grpc.nest.cerulean.works}
edge=${NEST_GRPC_EDGE_PROXY:-}
upstream=${NEST_GRPC_UPSTREAM:-controller-grpc-origin:50051}
origin_subnet=${NEST_GRPC_ORIGIN_SUBNET:-}
listen=${NEST_GRPC_LISTEN_PORT:-8443}
admin=${NEST_GRPC_ADMIN_PORT:-9901}
certs=${NEST_GRPC_CERT_DIR:-/certs}
template=${NEST_GRPC_TEMPLATE:-$here/envoy.yaml.template}

# A dotted DNS name, or the reserved name localhost (RFC 6761) for local tests:
# it can never get a public certificate. Other single-label names are refused.
[ "$host" = localhost ] \
    || echo "$host" | grep -Eqx '[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+' \
    || fail "NEST_GRPC_PUBLIC_HOST must be a DNS name, got '$host'"

[ -n "$edge" ] || fail "set NEST_GRPC_EDGE_PROXY to the edge proxy's address, such as 10.0.1.7/32"
edge_address=${edge%/*}
edge_prefix=${edge##*/}
[ "$edge_address" != "$edge" ] || fail "NEST_GRPC_EDGE_PROXY must be a CIDR such as 10.0.1.7/32"
if echo "$edge_address" | grep -Eqx '([0-9]{1,3}\.){3}[0-9]{1,3}'; then
    max_prefix=32
    min_prefix=8
elif echo "$edge_address" | grep -Eqx '[0-9A-Fa-f:]+' && echo "$edge_address" | grep -q ':'; then
    max_prefix=128
    min_prefix=32
else
    fail "NEST_GRPC_EDGE_PROXY must be an IPv4 or IPv6 CIDR, got '$edge'"
fi
echo "$edge_prefix" | grep -Eqx '[0-9]{1,3}' || fail "NEST_GRPC_EDGE_PROXY prefix must be a number"
if [ "$edge_prefix" -lt "$min_prefix" ] || [ "$edge_prefix" -gt "$max_prefix" ]; then
    fail "NEST_GRPC_EDGE_PROXY prefix must be /$min_prefix to /$max_prefix (no wide or default route), got /$edge_prefix"
fi

edge_alias=${NEST_GRPC_EDGE_ALIAS:-nest-grpc-gateway}
echo "$edge_alias" | grep -Eqx '[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?' \
    || fail "NEST_GRPC_EDGE_ALIAS must be a lower-case DNS label, got '$edge_alias'"
case $edge_alias in
    grpc-gateway | controller | controller-grpc-origin | tailscale | postgres | migrate | grpc-certificates)
        fail "NEST_GRPC_EDGE_ALIAS $edge_alias is a Compose service name, which resolves to every deployment on the edge network; choose a unique alias such as nest-grpc-gateway-release"
        ;;
esac

upstream_host=${upstream%:*}
upstream_port=${upstream##*:}
echo "$upstream_host" | grep -Eqx '[A-Za-z0-9]([A-Za-z0-9._-]{0,251}[A-Za-z0-9])?' \
    || fail "NEST_GRPC_UPSTREAM must be host:port, got '$upstream'"
for port in "$upstream_port" "$listen" "$admin"; do
    if ! echo "$port" | grep -Eqx '[0-9]{1,5}' || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
        fail "ports must be 1 to 65535, got '$port'"
    fi
done
case "$certs" in
    /*) ;;
    *) fail "NEST_GRPC_CERT_DIR must be an absolute path" ;;
esac
echo "$certs" | grep -Eqx '[A-Za-z0-9/._-]+' || fail "NEST_GRPC_CERT_DIR has unsupported characters"
[ -r "$template" ] || fail "cannot read the template $template"

# IPv4 dotted quad to an integer, or fail.
ipv4_int() {
    echo "$1" | grep -Eqx '([0-9]{1,3}\.){3}[0-9]{1,3}' || return 1
    old_ifs=$IFS
    IFS=.
    # shellcheck disable=SC2086
    set -- $1
    IFS=$old_ifs
    for octet in "$@"; do
        [ "$octet" -le 255 ] || return 1
    done
    echo $(( ($1 << 24) + ($2 << 16) + ($3 << 8) + $4 ))
}

if [ -n "$origin_subnet" ]; then
    origin_address=${origin_subnet%/*}
    origin_prefix=${origin_subnet##*/}
    [ "$origin_address" != "$origin_subnet" ] \
        && echo "$origin_prefix" | grep -Eqx '[0-9]{1,2}' \
        && [ "$origin_prefix" -ge 8 ] && [ "$origin_prefix" -le 30 ] \
        && origin_base=$(ipv4_int "$origin_address") \
        || fail "NEST_GRPC_ORIGIN_SUBNET must be an IPv4 CIDR /8 to /30, got '$origin_subnet'"
    origin_mask=$(( (0xFFFFFFFF << (32 - origin_prefix)) & 0xFFFFFFFF ))
    origin_base=$(( origin_base & origin_mask ))
fi

# Fails unless the upstream name resolves, and only to grpc-origin addresses: a
# name that also resolves on another network (the edge network Dokploy attaches)
# would let Envoy reach the Controller from outside its allowlist (#109).
check_upstream_resolution() {
    command -v getent >/dev/null 2>&1 || fail "cannot check NEST_GRPC_UPSTREAM: getent is missing"
    resolved=$(getent ahosts "$upstream_host" | awk '{print $1}' | sort -u) || resolved=
    [ -n "$resolved" ] || fail "NEST_GRPC_UPSTREAM host '$upstream_host' does not resolve; is the Controller on grpc-origin with that alias?"
    for address in $resolved; do
        value=$(ipv4_int "$address") \
            || fail "NEST_GRPC_UPSTREAM host '$upstream_host' resolves to $address, which is not IPv4 on grpc-origin ($origin_subnet)"
        [ $(( value & origin_mask )) -eq "$origin_base" ] \
            || fail "NEST_GRPC_UPSTREAM host '$upstream_host' resolves to $address, outside grpc-origin ($origin_subnet); point it at the grpc-origin-only alias (docs/source/public-grpc-gateway.md)"
    done
    echo "nest-grpc-gateway: upstream $upstream_host resolves only inside $origin_subnet:" $resolved >&2
}

umask 077
mkdir -p "$out"
sds="$out/certificate-sds.yaml"
cat > "$sds.tmp" <<EOF
resources:
  - "@type": type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.Secret
    name: nest_grpc_certificate
    tls_certificate:
      certificate_chain: { filename: $certs/fullchain.pem }
      private_key: { filename: $certs/privkey.pem }
      # Moving a renewed pair into this directory reloads it without a restart.
      watched_directory: { path: $certs }
EOF
mv "$sds.tmp" "$sds"
sed -e "s|@PUBLIC_HOST@|$host|g" \
    -e "s|@EDGE_ADDRESS@|$edge_address|g" \
    -e "s|@EDGE_PREFIX@|$edge_prefix|g" \
    -e "s|@UPSTREAM_HOST@|$upstream_host|g" \
    -e "s|@UPSTREAM_PORT@|$upstream_port|g" \
    -e "s|@LISTEN_PORT@|$listen|g" \
    -e "s|@ADMIN_PORT@|$admin|g" \
    -e "s|@SDS_PATH@|$sds|g" \
    "$template" > "$out/envoy.yaml.tmp"
if grep -q '@[A-Z][A-Z_]*@' "$out/envoy.yaml.tmp"; then
    fail "the template has a value this script does not set"
fi
mv "$out/envoy.yaml.tmp" "$out/envoy.yaml"
echo "nest-grpc-gateway: rendered for $host, edge proxy $edge, edge alias $edge_alias, upstream $upstream" >&2

[ -z "$render_only" ] || exit 0
[ -z "$origin_subnet" ] || check_upstream_resolution
exec "${NEST_ENVOY_BINARY:-envoy}" -c "$out/envoy.yaml" --log-level "${NEST_ENVOY_LOG_LEVEL:-info}"
