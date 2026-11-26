# Run the public gRPC gateway pilot (H08)

This page deploys, validates and rolls back the public gRPC gateway for the
**bounded trusted pilot**. Clients reach the hosted Controller at
`grpc.nest.cerulean.works:443` over verified TLS, without a VPN or Tailnet.

> [!WARNING]
> This is a pilot gateway, not a production release. **H09** (production rollout
> and security sign-off) must approve the public endpoint before any production DNS
> record points at it and before the SDK's hosted default is switched on (see
> [Connect to Cerulean-hosted Nest](hosted-connection)). Reaching the gateway grants
> nothing: every call still needs a Nest session, Project membership, Provider
> approval and a Platform grant. Execution is still trusted Python run as one HPC
> account (see [Security](security)). Untrusted users and untrusted code stay out of
> scope until D02, D05, D06 and H05. **C03/C04** (detached execution and lifecycle
> reconciliation) remain prerequisites for broader or long-running public
> workloads, and for any Cloudflare-proxied edge.

The design follows topology D of the [H06 transport study](public-grpc-transport)
and the controls of [H07](public-api-security) and [F02a](admission-limits).

## Topology

```text
SDK (NEST_PROFILE=hosted, or NEST_CONTROLLER_ADDRESS=grpc.nest.cerulean.works:443)
  │ TLS 1.2+/1.3, SNI grpc.nest.cerulean.works, ALPN h2, :443
  ▼
Cloudflare DNS only (grey cloud): resolves the name; carries no traffic
  ▼
Dokploy host :443 ── Traefik TCP router HostSNI(grpc.nest.cerulean.works), TLS passthrough,
  │                  PROXY protocol v2 (every other name: the unchanged HTTPS routers)
  ▼  dokploy-network
Envoy grpc-gateway :8443 (TLS termination; no published port)
  │  h2c on grpc-origin, an `internal` network with a fixed subnet (172.31.250.0/28)
  ▼
Controller :50051 (NEST_PUBLIC_PILOT=true; publishes no port)
  └── still on the Tailnet through Tailscale Serve: existing users and rollback
```

## What each layer enforces

| Layer | Enforces | Configured in |
| --- | --- | --- |
| Cloudflare | DNS only. No proxy, so no 100 s proxy timeout and no WAF; Cloudflare Access does not apply to gRPC. | The zone (operator) |
| Traefik | One SNI name passed through to Envoy without decrypting it, with the client address in a PROXY v2 header. | `deploy/public-grpc/traefik/nest-grpc.yml` |
| Envoy | Connections only from Traefik's address (RBAC on the TCP peer, so no other container can claim a client address); only the SNI name, TLS 1.2+, ALPN h2; 1024 connections overall, 512 on the listener, 100 new connections per second (burst), 200 requests (refill 50 per second) answered `RESOURCE_EXHAUSTED`, 16 streams per connection, 16 KiB of headers, 10 s to send them; only `/nest.ExecutionService/*` (health and reflection are 404, seen as `UNIMPLEMENTED`); no retries; no upstream PINGs; idle and deadline bound 1200 s; access log without headers or bodies; certificate reloaded on renewal without a restart. | `deploy/public-grpc/envoy.yaml.template`, rendered and validated by `gateway-entrypoint.sh` |
| Controller | Only connections from the gateway and the Tailnet (`NEST_GRPC_ALLOWED_PEERS`); client address from Envoy's `x-forwarded-for` only (`NEST_GRPC_TRUSTED_PROXIES`), recorded as `client_address` in audit records; per-address and per-User rate limits; a Nest session; `run.submit`, Provider approval and Platform grant; registry-backed admission with its limits and reservations; batch scripts refused; message size, metadata and concurrency bounds. `NEST_PUBLIC_PILOT=true` refuses to start unless all of these are set. | Controller environment |

## Files

| File | Purpose |
| --- | --- |
| `deploy/public-grpc/envoy.yaml.template` | The gateway's Envoy configuration, with placeholders only for deployment values |
| `deploy/public-grpc/gateway-entrypoint.sh` | Validates those values (refusing a missing, wide or default-route edge address and malformed names), renders the configuration and runs Envoy. `--render DIR` only renders, for review and `envoy --mode validate` |
| `deploy/public-grpc/renew-certificate.sh` | Obtains and renews the certificate with lego (DNS-01 through Cloudflare) and installs it by rename |
| `deploy/public-grpc/traefik/nest-grpc.yml` | The Traefik TCP passthrough router, for `/etc/dokploy/traefik/dynamic/` |
| `compose.dokploy.yaml` profiles `public-grpc` and `public-grpc-acme` | The `grpc-gateway` (Envoy `v1.36.2`) and `grpc-certificates` (lego) services, the internal `grpc-origin` network, and the external edge network |
| `scripts/verify-public-grpc.py` | The live validation checks below |

## Deploy to staging

Run everything first against a **staging name**, such as `grpc.staging.<domain>`,
and the ACME **staging** directory. Production DNS, the production name and
production certificates change only after the staging checklist passes, with the
operator's approval. These steps are operator actions; nothing in this repository
performs them.

1. **Upgrade the Controller** to this release (`nest-db upgrade`, then redeploy).
   This release needs no schema change.

2. **Controller settings** (Dokploy Environment panel). The Tailnet path stays,
   so add its network to the allowed peers:

   ```bash
   NEST_PUBLIC_PILOT=true
   NEST_EXECUTOR=slurm
   NEST_CONTROLLER_AUTH=session
   NEST_EXECUTION_REGISTRY=required
   NEST_LIMIT_BATCH_SCRIPTS=refuse
   NEST_CONTROLLER_ALLOW_PLAINTEXT=true                  # h2c on grpc-origin and the Tailnet bridge only
   NEST_GRPC_ALLOWED_PEERS=172.31.250.0/28,<tailnet bridge subnet>
   NEST_GRPC_TRUSTED_PROXIES=172.31.250.2/32             # the gateway's fixed address
   NEST_ENVIRONMENT_TIMEOUT=540                          # with NEST_RESULT_TIMEOUT=600: under 1200 s
   NEST_PROVIDER_APPROVALS=database                      # or an explicit list, never '*'
   ```

   Find the Tailnet bridge subnet with
   `docker network inspect <project>_tailnet --format '{{(index .IPAM.Config 0).Subnet}}'`.
   The Controller refuses to start, listing every unmet requirement, if any of these
   is missing (see `NEST_PUBLIC_PILOT` in
   [Environment variables](configuration#controller)). Keep the pilot's admission
   limits small (see [Admission limits](admission-limits)) and approve only the
   invited, trusted Projects for the Provider.

3. **The edge proxy's address.** Envoy accepts connections, and PROXY headers, only
   from Traefik:

   ```bash
   docker inspect dokploy-traefik \
     --format '{{(index .NetworkSettings.Networks "dokploy-network").IPAddress}}'
   ```

   Set `NEST_GRPC_EDGE_PROXY=<that address>/32`. If Dokploy recreates Traefik with
   another address, the gateway refuses every connection (it fails closed) until
   this setting is updated.

4. **Certificate.** Either let the `public-grpc-acme` profile obtain it, or place
   your own `fullchain.pem` and `privkey.pem` (readable by group 101) in the
   `grpc-certificates` volume. For the ACME profile set:

   ```bash
   NEST_GRPC_PUBLIC_HOST=grpc.staging.<domain>
   NEST_GRPC_ACME_EMAIL=<ops contact>
   NEST_GRPC_ACME_ACCEPT_TOS=true                        # after reading the CA's terms
   NEST_GRPC_CLOUDFLARE_DNS_API_TOKEN=<token>            # Zone:DNS:Edit on that zone only
   # NEST_GRPC_ACME_SERVER unset: Let's Encrypt staging
   ```

   lego adds and removes only the `_acme-challenge` TXT record. The token reaches
   only the `grpc-certificates` container, as a secret file. Renewal runs twice a
   day and renews 30 days before expiry; Envoy reloads the moved-in pair without a
   restart. Alert when `verify-public-grpc.py` reports `days_left` under 14.

5. **Start the gateway**: `COMPOSE_PROFILES=public-grpc,public-grpc-acme` (plus any
   profile already in use), and redeploy. Check `docker compose logs grpc-gateway`
   for `rendered for grpc.staging.<domain>`.

6. **Route it.** Copy `deploy/public-grpc/traefik/nest-grpc.yml` to
   `/etc/dokploy/traefik/dynamic/nest-grpc.yml` with the staging name in its rule.
   Traefik reloads it by itself; no other router changes. Confirm the
   `proxyProtocol` key against the deployed Traefik version: some v3 releases move
   it to a TCP `serversTransport`. A mismatch fails closed: Envoy refuses
   connections without a PROXY header.

7. **DNS.** Create the staging record as **DNS only** (grey cloud), pointing at the
   Dokploy host. Do not proxy it and do not change any production record.

## Validate

From a network outside the Tailnet, as a pilot user who ran `nest login`:

```bash
uv run python scripts/verify-public-grpc.py grpc.staging.<domain>:443 \
  --ca-file letsencrypt-staging-roots.pem \
  --origin <dokploy-host-public-ip>:50051 --origin <dokploy-host-public-ip>:8443
```

Each check prints one JSON line and the script exits 1 if any fails. Then, with the
SDK pointed at the staging name (`NEST_CONTROLLER_ADDRESS=grpc.staging.<domain>:443`,
`NEST_CONTROLLER_TLS=true`, `NEST_CONTROLLER_TLS_CA_FILE` for the staging roots,
`NEST_AUTH_URL=https://auth.nest.cerulean.works`, and
`NEST_HOSTED_SESSION_ADDRESS=grpc.staging.<domain>:443` to entrust the staging
gateway with the hosted session), add `--signed-in` and, only in
the approved pilot, `--execute`, which submits one real Slurm job. Both first run
the `sdk_target` check: the SDK must name the same address over TLS with the same
roots as `--ca-file` (or system roots). If it names another Controller, such as
your Tailnet one, the run fails before any session is read or any job is
submitted; the script never redirects a session to the address you give it.
Finally:

```bash
docker compose logs grpc-gateway | grep -cF "<a staging session token>"   # must print 0
```

### Evidence

**CI evidence** (`tests/test_public_gateway.py`, `tests/test_project_selection.py`,
`tests/test_hosted_profile.py`, `tests/check_shared_compose.py`). The Envoy tests run
the pinned Envoy binary with the rendered configuration, in front of the real
Controller, PostgreSQL and the fake login node, behind a local stand-in for
Traefik's PROXY passthrough. They cover:

- `envoy --mode validate` accepts the production configuration;
- TLS 1.2+, SNI and ALPN `h2`; another name is refused; HTTP/1.1-only gets no `h2`;
- an unauthenticated call is `UNAUTHENTICATED` with the Controller's message, and
  the health path is refused;
- the hosted SDK selects the only eligible Project and runs through the gateway;
  the audit record names the PROXY-protocol client, not an address the client set
  in `x-forwarded-for`;
- revocation is enforced on the next call, and per-User rate limits answer
  `rate_limited` through the gateway;
- a timeout reaches the SDK and the submission is not repeated;
- a direct connection to Envoy from anything but the edge proxy is refused;
- the operator script passes end to end, and no token appears in Envoy's log.

The compose check covers no published ports, the internal origin network, the
gateway's fixed address, and that only the ACME client gets the Cloudflare
token. The Controller tests cover every `NEST_PUBLIC_PILOT` requirement.

**Pending live acceptance (not claimed as passed).** These need the staging host,
a real name, a public CA and an outside network:

- [ ] Staging record (DNS only) and the Traefik passthrough on the deployed Traefik
  version, including its `proxyProtocol` key.
- [ ] ACME staging issuance, then renewal and reload without a restart.
- [ ] `verify-public-grpc.py` passes from a residential network and from a cloud
  VM, including `origin_unreachable_*` for the Controller and gateway ports.
- [ ] `--signed-in` and `--execute` as an invited pilot user; a non-member, a
  revoked member and a wrong Project are refused and submit nothing.
- [ ] Rate limits and `RESOURCE_EXHAUSTED` measured through the real edge; load
  and saturation (`ghz` or similar) with latency and memory recorded.
- [ ] Cancellation: an interrupted call is answered `CANCELLED` before `sbatch`
  (H07), and after `sbatch` its job and reservation are reconciled (C04 gap).
- [ ] The Tailnet path still serves existing users throughout.
- [ ] No token in the gateway's logs after the run.
- [ ] Operator approval of the public ingress and the NCHC workload policy for the
  pilot cohort.

## Monitor

- Envoy's access log: path, gRPC status, response flags, sizes and duration.
  Watch for `RESOURCE_EXHAUSTED` (gateway or Controller limits), `UH`/`UF` flags
  (Controller unreachable) and certificate reload messages.
- The Controller's `RPC rate-limited` and `RPC refused` warnings, and its audit log
  (`client_address`, actor, Project, reason) in [the security audit log](audit-log).
- `days_left` from the validation script, daily.

## Roll back

Fastest first. The Tailnet path stays in place throughout, so only clients that
switched to the public address change back (unset `NEST_PROFILE`, or restore their
Tailnet `NEST_CONTROLLER_ADDRESS`).

1. **Stop public traffic:** delete `/etc/dokploy/traefik/dynamic/nest-grpc.yml`.
   Traefik drops the route at once; every other router is untouched.
2. **Remove the gateway:** take `public-grpc` and `public-grpc-acme` out of
   `COMPOSE_PROFILES` and redeploy. The `grpc-certificates` and `grpc-acme-state`
   volumes keep the certificate for a later retry; delete them to discard it.
3. **DNS:** delete the staging record; revoke the Cloudflare token.
4. **Controller:** unset `NEST_PUBLIC_PILOT` (or leave it: the settings it
   requires are safe for the Tailnet path), and remove the grpc-origin subnet from
   `NEST_GRPC_ALLOWED_PEERS`. No data changes.

## Acceptance checklist (H08)

- [x] Envoy gateway in topology D with verified TLS, HTTP/2, SNI, origin
  protection, trusted proxy handling, rate limits and admission controls (CI).
- [x] Controller pilot profile requiring session authentication, registry-backed
  admission and `NEST_LIMIT_BATCH_SCRIPTS=refuse` (CI).
- [x] Client address of each gRPC call recorded in the audit log (CI).
- [x] Tailnet access preserved; deployment, validation and rollback documented.
- [ ] Every pending live check above, then operator approval. H09 follows.
