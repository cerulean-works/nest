#!/usr/bin/env bash
# Creates throwaway TLS material for the PostgreSQL TLS tests in DIR:
#   ca.crt               CA that signed the server certificate
#   server.crt/.key      server certificate valid only for DNS:localhost (no IP SAN)
#   other-ca.crt         unrelated CA, which must not verify the server
# Test fixtures only; never use these files for a real database.
set -euo pipefail

dir=${1:?usage: make_postgres_tls_certs.sh DIR}
mkdir -p "$dir"
cd "$dir"
days=2

openssl req -x509 -newkey rsa:2048 -nodes -days "$days" -subj "/CN=Nest test CA" \
    -keyout ca.key -out ca.crt 2>/dev/null
openssl req -x509 -newkey rsa:2048 -nodes -days "$days" -subj "/CN=Unrelated test CA" \
    -keyout other-ca.key -out other-ca.crt 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" \
    -keyout server.key -out server.csr 2>/dev/null
printf 'subjectAltName=DNS:localhost\n' > server.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days "$days" \
    -extfile server.ext -out server.crt 2>/dev/null
chmod 600 server.key ca.key other-ca.key
rm -f server.csr server.ext ca.srl
