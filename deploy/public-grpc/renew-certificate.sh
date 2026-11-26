#!/bin/sh
# Obtains and renews the public gRPC gateway's certificate with lego (DNS-01 through
# Cloudflare), and installs it where Envoy reloads it without a restart (H08).
#
# Runs in the goacme/lego image (compose.dokploy.yaml, profile public-grpc). It
# needs, from the operator:
#   NEST_GRPC_PUBLIC_HOST         the name, such as grpc.nest.cerulean.works
#   NEST_GRPC_ACME_EMAIL          the ACME account contact
#   NEST_GRPC_ACME_ACCEPT_TOS     true, after reading the CA's terms of service
#   NEST_GRPC_ACME_SERVER         ACME directory; Let's Encrypt *staging* unless set
#   /run/secrets/cloudflare_dns_api_token
#                                 a Cloudflare API token limited to Zone:DNS:Edit on
#                                 the one zone (lego only adds and removes the
#                                 _acme-challenge TXT record; no other record changes)
#
# The pair is written to $NEST_GRPC_CERT_DIR (fullchain.pem, privkey.pem) by rename,
# so Envoy never reads half a file, with the key readable only by the gateway's
# group (NEST_GRPC_GATEWAY_GID, 101 for the Envoy image). It checks twice a day and
# renews 30 days before expiry.
set -eu
export LC_ALL=C

fail() {
    echo "nest-grpc-certificate: $*" >&2
    exit 2
}

host=${NEST_GRPC_PUBLIC_HOST:-grpc.nest.cerulean.works}
email=${NEST_GRPC_ACME_EMAIL:-}
server=${NEST_GRPC_ACME_SERVER:-https://acme-staging-v02.api.letsencrypt.org/directory}
certs=${NEST_GRPC_CERT_DIR:-/certs}
state=${NEST_GRPC_ACME_STATE:-/var/lib/lego}
gid=${NEST_GRPC_GATEWAY_GID:-101}
interval=${NEST_GRPC_RENEW_INTERVAL:-43200}
token_file=${NEST_GRPC_CLOUDFLARE_TOKEN_FILE:-/run/secrets/cloudflare_dns_api_token}

[ "${NEST_GRPC_ACME_ACCEPT_TOS:-}" = "true" ] \
    || fail "set NEST_GRPC_ACME_ACCEPT_TOS=true after reading the ACME CA's terms of service"
[ -n "$email" ] || fail "set NEST_GRPC_ACME_EMAIL"
[ -s "$token_file" ] || fail "the Cloudflare DNS API token secret is empty"
echo "$host" | grep -Eqx '[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+' \
    || fail "NEST_GRPC_PUBLIC_HOST must be a DNS name"
case "$server" in
    https://*) ;;
    *) fail "NEST_GRPC_ACME_SERVER must be an https:// ACME directory" ;;
esac
export CLOUDFLARE_DNS_API_TOKEN_FILE="$token_file"

install_pair() {
    issued="$state/certificates/$host"
    if [ ! -s "$issued.crt" ] || [ ! -s "$issued.key" ]; then
        fail "lego left no certificate for $host"
    fi
    umask 027
    mkdir -p "$certs"
    cp "$issued.crt" "$certs/.fullchain.pem.new"
    cp "$issued.key" "$certs/.privkey.pem.new"
    chgrp "$gid" "$certs/.privkey.pem.new" "$certs/.fullchain.pem.new"
    chmod 0640 "$certs/.privkey.pem.new"
    chmod 0644 "$certs/.fullchain.pem.new"
    # Key first: Envoy reloads on each move and validates the pair together.
    mv "$certs/.privkey.pem.new" "$certs/privkey.pem"
    mv "$certs/.fullchain.pem.new" "$certs/fullchain.pem"
    echo "nest-grpc-certificate: installed the certificate for $host" >&2
}

while :; do
    set -- --accept-tos --email "$email" --server "$server" --path "$state" \
        --dns cloudflare --domains "$host"
    if [ -s "$state/certificates/$host.crt" ]; then
        /lego "$@" renew --days 30 && install_pair \
            || echo "nest-grpc-certificate: renewal failed; retrying in $interval s" >&2
    else
        /lego "$@" run && install_pair \
            || echo "nest-grpc-certificate: issuance failed; retrying in $interval s" >&2
    fi
    sleep "$interval"
done
