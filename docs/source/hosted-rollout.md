# Hosted gRPC rollout runbook and evidence (H09)

This page is the operator runbook for taking the public gRPC gateway from staging
to production. It also records the evidence for each gate, from
[#90](https://github.com/cerulean-works/nest/issues/90). It follows the H06
feasibility study ([#78](https://github.com/cerulean-works/nest/issues/78),
[Public gRPC transport](public-grpc-transport)), H07/F02a
([#87](https://github.com/cerulean-works/nest/pull/87)), and the H08/E07/E08
implementation ([#88](https://github.com/cerulean-works/nest/pull/88),
[Run the public gRPC gateway pilot](public-grpc-gateway)).

> [!IMPORTANT]
> **Nothing on this page activates anything.** `nest.config.HOSTED_PROFILE_DEFAULT`
> stays `False`, and production DNS, routes and firewall stay unchanged, until
> every staging gate below is **live passed** and the operator explicitly approves
> H09 and the security and workload policy. The production switch is a separate,
> small pull request after that approval. It is never merged automatically, and it
> never weakens TLS or session binding. CI results are synthetic evidence. They
> never count as live acceptance.

## Status legend

| Status | Meaning |
| --- | --- |
| **Implemented** | In this repository, reviewed |
| **CI** | Passes in CI or a reproducible local fixture: synthetic, no live system |
| **Live passed** | Run against the staging or production deployment, with the evidence recorded below |
| **PENDING** | Needs a live run that has not happened. The exact commands and expected results are given. |
| **BLOCKED** | Needs a dependency, an access or a decision first; named in the row |
| **Approved** | The operator signed it off, with name and date in the evidence record |

## Evidence matrix

| Gate | Implemented | CI / synthetic | Live | Approved |
| --- | --- | --- | --- | --- |
| A1. Deploy fails at network creation on an overlapping subnet; operator override | Yes | **CI**: `tests/repro_migrate_stall.sh` step 1, `test_deploy_doctor.py` | Seen on 2026-11-26 (override `10.253.250.0/28`); `preflight` not yet run on the host: **PENDING** | n/a |
| A2. Subnet, gateway, trusted-proxy, allowed-peer and edge consistency preflight | Yes (`nest-deploy-doctor.py preflight`) | **CI** | **PENDING** (run R1) | n/a |
| A3. Stuck `migrate` (interrupted recreate) detected, bounded, recovered without bypassing the migration | Yes (`migrate-status`, `wait-migrate`, `recover-migrate`) | **CI**: reproduced with Compose v5.7.1, Engine 29.9.3 | Incident recovered by hand on 2026-11-26; doctor not yet run on the host: **PENDING** (R1) | n/a |
| A4. Failed migration fails closed; repeated migrations; volumes, sign-in and Tailnet kept | Yes (existing) | **CI**: `smoke_compose_env.sh`, `smoke_compose_postgres_upgrade.sh`, repro step 4 | Private SDK login, Project selection and a remote call passed after the 2026-11-26 recovery (operator report) | n/a |
| A5. Cause of the 2026-11-26 interruption | n/a | Mechanism verified; trigger **not confirmed** (see [Preflight and recover](deployment-recovery#verified-cause-and-open-hypotheses)) | **BLOCKED**: needs Compose v5.8.0 and Dokploy's deploy log of the failed attempt | n/a |
| B1. Traefik SNI passthrough with PROXY v2 on the installed Traefik | Yes (`render.sh`, two route files) | **CI**: Traefik v2.11 to v3.6.2 (`check_traefik_passthrough.sh`) | **PENDING** (R2) | |
| B2. ACME DNS-01 staging issuance, renewal and reload | Yes (existing) | Partly (renewal script fails closed) | **PENDING** (R3) | |
| B3. TLS/SNI/ALPN h2, certificate verification, unauthenticated refusal, health path refused, origin bypass refused, from outside the Tailnet | Yes (existing verifier) | **CI** (real Envoy) | **PENDING** (R4, residential and cloud) | |
| B4. Signed-in listing and one execution through the gateway; client address in the audit log; no token in logs | Yes | **CI** | **PENDING** (R5) | |
| B5. Non-member, revoked member, wrong Project, unauthenticated, spoofed `x-forwarded-for`, batch script: refused, nothing submitted | Yes | **CI** | **PENDING** (R6) | |
| B6. Size, rate, concurrency and admission limits; saturation measured | Yes | **CI** (rate limits) | **PENDING** (R7) | |
| B7. Deadlines; cancellation before `sbatch` | Yes (H07) | **CI** | **PENDING** (R8) | |
| B8. Cancellation after `sbatch` reconciled | **No**: C04 | n/a | **BLOCKED** on C03/C04. The pilot accepts it only if the agreed scope allows bounded short jobs, and the operator records that decision. | |
| B9. Tailnet path unchanged throughout; rollback | Yes | **CI** | **PENDING** (R9) | |
| C1. Hosted opt-in without Tailscale from an external client; verified TLS; no plaintext or insecure fallback | Yes (E08) | **CI** (`test_hosted_profile.py`) | **PENDING** (R5 with `NEST_PROFILE=hosted` against production, or staging with the explicit staging CA) | |
| C2. Project discovery and selection: one eligible, saved, override, several, revoked, none | Yes (E07) | **CI** (`test_project_selection.py`) | Private path, one eligible: passed 2026-11-26 (operator report). Hosted: **PENDING** (R5) | |
| C3. Quickstart, Project selection and hosted docs; minimal uv example | Yes (this release) | Docs build | n/a | |
| D1. This runbook, rollback and monitoring | Yes | n/a | n/a | **PENDING** |
| D2. Approved cohort, NCHC/HPC workload policy, public ingress | n/a | n/a | n/a | **PENDING** (operator) |
| D3. `HOSTED_PROFILE_DEFAULT=True` and production DNS | **Not done, on purpose** | n/a | n/a | **BLOCKED** on every row above |

Prerequisites outside this pilot: C03/C04 for broad or long-running public use and
for a Cloudflare-proxied edge; D02, D05, D06 and H05 for untrusted users or code.

## Record each live run

Copy this block into the issue for each gate. Leave out tokens, session or user
IDs, email addresses, keys, `.env` contents and full logs:

```text
Gate:            B3 (R4)
Date (UTC):      2026-..-..
Operator:        <name>
Nest revision:   <git rev-parse --short HEAD on the Dokploy checkout>
Images:          controller <digest>, envoy v1.36.2, lego v4.25.2, traefik <version>
Client network:  residential | cloud (<provider/region>)
Command:         <exact command>
Result:          <the JSON lines, or the sanitized summary>
Status:          live passed | failed (<what>) | blocked (<what>)
```

## Runbook

Run each step on **staging** first: name `grpc.staging.<domain>` and the Let's
Encrypt **staging** directory. `<appName>` is the Dokploy application's Compose
project name. Commands on the Dokploy host run from its checkout
(`/etc/dokploy/compose/<appName>/code`).

### R0. Revision and rollback point

```bash
git -C /etc/dokploy/compose/<appName>/code rev-parse --short HEAD
docker compose ls
docker image ls --digests | grep -E 'nest-controller|envoy|lego|traefik'
```

Record them. The rollback point is the previous Dokploy deployment of the same
application. Its volumes (`postgres-data`, `tailscale-state`) are kept by every
step on this page.

### R1. Deployment preflight and migration (A1 to A4)

```bash
python3 scripts/nest-deploy-doctor.py preflight      --project <appName> --env-file .env
# Start this as you press Deploy in Dokploy (or pass --since <deploy start, RFC 3339>):
python3 scripts/nest-deploy-doctor.py wait-migrate   --project <appName> --timeout 300
python3 scripts/nest-deploy-doctor.py migrate-status --project <appName>
```

Expected: every preflight line `PASS` (a `SKIP` only with the reason checked by
hand), then `completed: <appName>-migrate-1` and exit 0. That line counts only a
migration that finished after the wait started. A timeout while `migrate-status`
shows `waiting-dependency` means PostgreSQL is not healthy; check it, and do not
recover `migrate`. On exit 3, follow
[Recover a stuck migrate](deployment-recovery#recover-a-stuck-migrate). On exit 4,
fix the cause from `--logs 40`. Then check that the private path still works, from
a Tailnet client: `nest projects current` and one small remote call.

### R2. Edge route (B1)

```bash
docker exec dokploy-traefik traefik version
version=$(docker exec dokploy-traefik traefik version | awk '/^Version/{print $2}')
sh deploy/public-grpc/traefik/render.sh grpc.staging.<domain> "v$version" \
  > /etc/dokploy/traefik/dynamic/nest-grpc.yml
docker logs --since 2m dokploy-traefik 2>&1 | grep -iE 'nest-grpc|error' | head
```

Expected: no error naming `nest-grpc`. Confirm the DNS record is **DNS only**
(grey cloud): `dig +short grpc.staging.<domain>` returns the Dokploy host's
public address, not a Cloudflare one. Check that no host port 50051 or 8443 is
open and that the HTTPS Auth Domain still answers:

```bash
docker ps --format '{{.Names}} {{.Ports}}' | grep -E ':(50051|8443)->' || echo "no published gRPC ports"
curl -sS -o /dev/null -w '%{http_code}\n' https://auth.nest.cerulean.works/console
```

### R3. Certificate (B2)

```bash
docker compose -p <appName> logs grpc-certificates | tail -n 20
docker compose -p <appName> logs grpc-gateway | grep -i -E 'secret|certificate' | tail -n 5
```

Expected: lego reports a certificate for the staging name from the staging CA
(`nest-grpc-certificate: installed the certificate for <name>`), and Envoy reports
loading it. To exercise renewal in staging without waiting 60 days, run one renewal
by hand in the `grpc-certificates` container. It uses the same state and token:

```bash
docker compose -p <appName> exec grpc-certificates sh -c \
  'CLOUDFLARE_DNS_API_TOKEN_FILE=/run/secrets/cloudflare_dns_api_token /lego --accept-tos --email "$NEST_GRPC_ACME_EMAIL" --server "${NEST_GRPC_ACME_SERVER:-https://acme-staging-v02.api.letsencrypt.org/directory}" \
   --path /var/lib/lego --dns cloudflare --domains "$NEST_GRPC_PUBLIC_HOST" renew --days 90'
docker compose -p <appName> restart grpc-certificates   # its loop installs the renewed pair
```

Envoy must load the new pair without a restart of `grpc-gateway`, and
`verify-public-grpc.py` must report the new `days_left`.

### R4. Outside the Tailnet, no credential (B3)

From a machine **without Tailscale running**, once on a residential network and
once on a cloud VM:

```bash
uv run python scripts/verify-public-grpc.py grpc.staging.<domain>:443 \
  --ca-file letsencrypt-staging-roots.pem \
  --origin <dokploy-public-ip>:50051 --origin <dokploy-public-ip>:8443
```

Expected: every line `"ok": true`, exit 0. That covers `tls` (TLS 1.2+, ALPN `h2`,
the certificate verifies against the explicit staging roots),
`tls_wrong_name_refused`, `tls_http1_only_not_h2`, `unauthenticated_refused`,
`non_nest_path_refused`, and both `origin_unreachable_*`.

### R5. Signed in, hosted SDK, one execution (B4, C1, C2)

On the same outside client. For staging, name the staging gateway and its CA
explicitly; the hosted profile itself accepts only public roots:

```bash
export NEST_CONTROLLER_ADDRESS=grpc.staging.<domain>:443
export NEST_CONTROLLER_TLS=true
export NEST_CONTROLLER_TLS_CA_FILE=letsencrypt-staging-roots.pem
export NEST_AUTH_URL=https://auth.nest.cerulean.works
export NEST_HOSTED_SESSION_ADDRESS=grpc.staging.<domain>:443
uv run nest login
uv run nest projects list
uv run nest projects current
uv run python scripts/verify-public-grpc.py grpc.staging.<domain>:443 \
  --ca-file letsencrypt-staging-roots.pem --signed-in --execute
uv run examples/hosted_remote.py
```

Expected: `sdk_target` passes before any session is read; `signed_in` lists the
pilot Project; `execute` returns the expected result; the example prints `42`
twice. Then on the host:

```bash
docker compose -p <appName> logs grpc-gateway | grep -cF "<a staging access token>"   # 0
docker compose -p <appName> exec controller nest-db audit list --limit 5              # client_address is the outside client's public address
```

Also cover the selection cases with one pilot account each: a saved choice
(`nest projects use`), an explicit `NEST_PROJECT_ID`, several eligible Projects
with no terminal (`ProjectSelectionError`, nothing runs), and a revoked membership
(refused, and the saved choice is forgotten). After production approval, repeat
R4 and R5 with `NEST_PROFILE=hosted` and none of the variables above, against
`grpc.nest.cerulean.works:443` with system roots.

### R6. Refusals (B5)

With a non-member, a revoked member, and a member who names a wrong
`NEST_PROJECT_ID`, run the same small call, then send one call without a session,
one with a forged `x-forwarded-for`, and one `SubmitJob` batch script
(`examples/grpc_slurm_jobs.py` with the R5 settings). Expected:
`RemoteAccessDeniedError` or `UNAUTHENTICATED`, and `FAILED_PRECONDITION`
(`limit_batch_scripts_unbounded`) for the batch script. Each audit record names
the real client address, never the forged one. Then check that nothing reached
Slurm:

```bash
docker compose -p <appName> exec controller nest-db audit list --limit 20
# on the login node, as the service account: no job from these attempts
squeue -u <service account> --start; sacct -u <service account> -S <start time> -n -X -o JobID,JobName,State
```

### R7. Limits and saturation (B6)

Within the approved pilot only, with the pilot limits from
`deploy/public-grpc/staging.env.example`. Use a load tool (for example `ghz`)
against `ListEligibleProjects` with a pilot session, never `Execute`. Raise the
rate until you see `RESOURCE_EXHAUSTED` from the gateway (`200` request burst, 50
per second) and from the Controller (`rate_limited`). Record the latency
percentiles, the error mix, and Envoy's and the Controller's memory
(`docker stats --no-stream`). Check admission with `NEST_LIMIT_ACTIVE_PER_USER + 1`
concurrent small calls: the extra one is refused with `limit_active_per_user`.

### R8. Deadlines and cancellation (B7, B8)

- A call with `NEST_RPC_TIMEOUT=30` to a function that sleeps 60 s ends in
  `DEADLINE_EXCEEDED`, and the submission is not repeated.
- A long silent call inside the bounded pilot (for example 900 s) completes
  through the gateway (idle bound 1200 s).
- Interrupt a call (Ctrl-C) while the result is still uploading or waiting for
  admission. Expected: `CANCELLED` before `sbatch`, with no job in `sacct`.
- Interrupt a call after `sbatch`. Record what happens to the job and its
  reservation. Without C04 the job is not reconciled. Mark B8 **BLOCKED**, unless
  the operator records that the pilot's bounded job length accepts it.

### R9. Tailnet regression and rollback (B9)

Throughout R2 to R8, a Tailnet client runs `nest projects current` and one small
call; both must keep working. Then rehearse the
[rollback](public-grpc-gateway#roll-back): delete
`/etc/dokploy/traefik/dynamic/nest-grpc.yml`, and check that R4's `tls` check now
fails to connect while the Tailnet client still works. Restore the file
afterwards.

### R10. Monitoring

Before production, set up alerts for:

- `days_left` under 14 (a daily `verify-public-grpc.py` run without credentials);
- a rising share of `RESOURCE_EXHAUSTED`, or `UH`/`UF` flags, in Envoy's access log;
- the Controller's `RPC refused` and `RPC rate-limited` warnings;
- `migrate-status` other than `completed` after a deploy.

## Production activation (after approval only)

1. The operator approves, in the issue: every gate above **live passed** (or
   **BLOCKED** with an accepted, recorded scope decision for B8), the pilot
   cohort, and the NCHC/HPC workload policy.
2. Repeat R2 and R3 with the production name and
   `NEST_GRPC_ACME_SERVER=https://acme-v02.api.letsencrypt.org/directory`. Create
   the production DNS record as DNS only. Repeat R4, R5 and R9 against
   `grpc.nest.cerulean.works:443` with system roots and `NEST_PROFILE=hosted`.
3. Open the follow-up pull request that sets `HOSTED_PROFILE_DEFAULT = True`, with
   the evidence linked. It is reviewed and merged by a person.
4. Rollback at any time: revert that pull request (clients go back to the local
   default, and the Tailnet path is untouched), then follow
   [Roll back](public-grpc-gateway#roll-back).
