# Run Main and Release side by side on one Dokploy host

This page explains how to run two independent Nest deployments on the same Dokploy
host and Traefik. Both use the same reviewed `compose.dokploy.yaml`. Main is
deployed from the `main` branch and Release from one approved, immutable `v*` tag.
A Main deploy must never replace, route to, migrate, overwrite or use the state of
the Release deployment, and the reverse holds too (#118).

|                | Main / development                                     | Release / Early Access                                 |
| -------------- | ------------------------------------------------------ | ------------------------------------------------------ |
| Source         | `main` branch                                          | one approved immutable `v*` tag (first target `v0.1.0a5`) |
| Web and sign-in | `https://main.nest.cerulean.works`                    | `https://nest.cerulean.works`                          |
| gRPC           | `grpc.main.nest.cerulean.works:443`                    | `grpc.nest.cerulean.works:443`                         |
| Audience       | authorized developers and operators                    | approved hosted users                                  |
| SDK selection  | explicit endpoint and origin with verified TLS ([below](#connect-the-sdk-to-main)) | the canonical zero-setting path, only after H09 (#90) |
| Template       | [`deploy/environments/main.env.example`](https://github.com/cerulean-works/nest/blob/main/deploy/environments/main.env.example) | [`deploy/environments/release.env.example`](https://github.com/cerulean-works/nest/blob/main/deploy/environments/release.env.example) |

Web and sign-in go through the existing Cloudflare Tunnel HTTP ingress to Dokploy's
Traefik, and from there to each Controller's port 8080. gRPC uses DNS-only records
and Traefik's TCP `HostSNI` TLS passthrough with PROXY v2 to each deployment's own
Envoy, which forwards to its Controller over private h2c. There are no public gRPC
host ports and no HTTP Domain routes for gRPC. See
[Run the public gRPC gateway pilot](public-grpc-gateway).

Everything on this page is operator work. The repository provides templates,
read-only checks and a disposable test. Nobody deploys, changes DNS, the Tunnel or
OAuth, issues a tag or moves data for you.

## What keeps the two apart

Dokploy runs each Compose application under its own project name, its `appName`.
That name scopes every container, network and volume that the file defines. The
external edge network (`dokploy-network`) and the Traefik on it are the only things
the two applications share. Every collision on this page comes from that shared
network or from shared outside resources (DNS, certificates, the Tailnet, the HPC
cluster).

| Resource | Isolated by | What must differ | Checked by |
| --- | --- | --- | --- |
| Containers, `tailnet`, `grpc-origin`, `acme` networks | Compose project (appName) | one Dokploy application each; never the default project `nest` twice | `preflight` `project_identity` |
| `grpc-origin` subnet and gateway address | settings | `NEST_GRPC_ORIGIN_SUBNET`, `NEST_GRPC_GATEWAY_ADDRESS`, `NEST_GRPC_TRUSTED_PROXIES`, `NEST_GRPC_ALLOWED_PEERS` | `preflight` `origin_subnet_free` (names the other deployment) |
| `controller-grpc-origin` (gateway upstream) | it exists only on the project's own `grpc-origin` (#109) | nothing; keep it | `gateway-runtime` `upstream_alias`, `upstream_dns` |
| Gateway name on the edge network | **`NEST_GRPC_EDGE_ALIAS`** (new, default `nest-grpc-gateway`) | one unique alias per deployment | `preflight` and `gateway-runtime` `edge_alias`; the entrypoint refuses a Compose service name |
| Traefik route | its own file, rendered with **`render.sh --instance`** | router, service and serversTransport names, SNI name, target alias | `preflight` `route_names`, `route_sni`, `route_target` |
| Public name and certificate | `NEST_GRPC_PUBLIC_HOST`; `grpc-certificates` and `grpc-acme-state` volumes | name; ACME account; Cloudflare token where practical | `preflight` `public_host` |
| PostgreSQL | `postgres-data` volume, `postgres` on the project's `tailnet` only | password (`NEST_DATABASE_PASSWORD`) | `preflight` `database_host` (no other container on the edge answers to `postgres`) |
| Tailnet node | `tailscale-state` volume | `NEST_TAILSCALE_HOSTNAME`; its own enrollment key | `preflight` `tailnet_hostname` |
| Web origin, OAuth callback, cookies | Dokploy Domain per application; host-only `__Host-` cookies | `NEST_GOOGLE_REDIRECT_URL`, `NEST_PUBLIC_URL`, Google client secret where practical | manual (#96) |
| HPC workdir (runtime, environments, jobs) | settings | `NEST_SLURM_WORKDIR` or each `NEST_PLATFORM_<ID>_WORKDIR` | `preflight` `hpc_workdir` |
| HPC capacity | **not isolated by separate databases** | an operator-approved split, see [HPC capacity](#hpc-capacity) | `preflight` `hpc_account` (warning) |

Why the edge alias matters: before #118 every gateway joined `dokploy-network` as
`nest-grpc-gateway`, and every route file named the same router
(`nest-grpc`), service (`nest-grpc-gateway`) and transport (`nest-grpc-proxy-v2`).
With a second deployment, `nest-grpc-gateway` resolves to both gateways, so Traefik
can hand one name's connection to the other deployment's Envoy. That Envoy refuses
it, because it serves only its own name. On top of that, when two files define the
same router, service or transport name differently, Traefik's file provider keeps
only one definition, so the other deployment's name is not routed at all.
`tests/smoke_two_environments.sh` reproduces both failures with real Traefik and
Envoy: with two default route files, one name was refused outright (Traefik's own
certificate) and the other failed about a third of its handshakes. The Compose service name `grpc-gateway` resolves on the edge network
too, to every deployment's gateway, so a route must never use it.

The single-deployment behavior does not change. Without `NEST_GRPC_EDGE_ALIAS`
the alias is still `nest-grpc-gateway`. `render.sh NAME VERSION` without options
prints exactly the reviewed files, byte for byte, so the existing staging route
stays valid.

## Decisions the maintainer must make

These are not settled by this page, and nothing here assumes an answer. Record
each decision in #118 before you take the step that depends on it.

1. **Which application keeps today's Tailnet node** (`nest-controller`, the name
   `HOSTED_CONTROLLER_ALIASES` binds hosted sessions to). The recommendation is that
   Main keeps it for now, because it is the same Dokploy application and the same
   `tailscale-state` volume. Release enrolls a new `nest-controller-release` node.
   Moving the name to Release later means re-enrolling both nodes, and that is #96's
   call, with the SDK alias list.
2. **Release's starting data:** see [options](#starting-data-for-release).
3. **The HPC capacity split,** or separate Slurm accounts: see
   [HPC capacity](#hpc-capacity).
4. **Which Dokploy trigger the Release application uses** (a fixed tag, or a tag
   pattern). Verify what your Dokploy version actually does before you rely on it:
   see [Create the Release application](#create-the-release-application).
5. **When Main stops serving the existing staging name,** and whether the existing
   staging route file is removed afterwards: see
   [Migrate the existing staging application](#migrate-the-existing-staging-application-to-main).

## Before you start

On the Dokploy host, from each application's checkout, with its real appName:

```bash
docker compose ls                       # appNames; the stopped legacy app is listed too
docker exec dokploy-traefik traefik version | awk '/^Version/{print $2}'
ls -l /etc/dokploy/traefik/dynamic/     # existing route files; change none of them yet
python3 scripts/nest-deploy-doctor.py preflight --project <appName> --env-file <its .env>
```

`preflight` compares this deployment with every other Nest deployment on the
host, stopped ones included, such as the legacy tag-triggered application. A
conflict with a running deployment fails. A conflict with a stopped one warns,
because starting it again would collide. Do not delete the legacy application or
its volumes, and do not assume they hold nothing.

Take a database backup of the existing application before any change
([Store Controller records in PostgreSQL](database)), and keep it off the host.

## Create the Release application

1. Create a **new** Dokploy Compose application. Use the same repository and
   `compose.dokploy.yaml`, and the source is the approved tag. Do not reuse the
   stopped legacy application or its volumes.
2. **Verify the trigger.** Confirm in Dokploy what deploys this application, and
   record your answers in #118. Find out whether a push to `main` can trigger it
   (it must not). Find out whether a new `v*` tag deploys automatically or only
   the configured tag does, and whether a manual Deploy uses the configured tag.
   With automatic deploys, pushing a tag becomes a production rollout, which is
   the maintainer's separate decision (#116). Prefer a fixed tag with manual
   deploys until that is settled.
3. Fill in its Environment panel from
   [`release.env.example`](https://github.com/cerulean-works/nest/blob/main/deploy/environments/release.env.example)
   and the settings in [Deploy on Dokploy](dokploy). Use new secrets throughout: a
   new database password, a new one-time Tailscale key, the Release DNS-01 token,
   and Google sign-in settings for the Release origin. Never paste the Main
   application's `.env`.
4. Run `preflight` (above). Then deploy **without** the `public-grpc` profiles
   first, and add them once `preflight` passes with the real Tailnet bridge subnet
   in `NEST_GRPC_ALLOWED_PEERS`.
5. Approve the new Tailnet node (tag `tag:nest-controller`), then set
   `NEST_TAILSCALE_AUTHKEY` to `''`.

## Route each gRPC name

Each deployment gets its own route file, with its own names and target:

```bash
# On the Dokploy host. VERSION is the installed Traefik's version.
deploy/public-grpc/traefik/render.sh grpc.nest.cerulean.works "$VERSION" --instance release \
  > /tmp/nest-grpc-release.yml
python3 scripts/nest-deploy-doctor.py preflight --project <release appName> --env-file <its .env> \
  --traefik-dynamic-dir /etc/dokploy/traefik/dynamic      # route_target WARN: not installed yet
sudo install -m 0644 /tmp/nest-grpc-release.yml /etc/dokploy/traefik/dynamic/nest-grpc-release.yml
python3 scripts/nest-deploy-doctor.py preflight --project <release appName> --env-file <its .env>
python3 scripts/nest-deploy-doctor.py gateway-runtime --project <release appName>
```

- `--instance release` names the router `nest-grpc-release`, the service
  `nest-grpc-gateway-release` and the transport `nest-grpc-proxy-v2-release`. The
  route dials `nest-grpc-gateway-release:8443`, which must be the deployment's
  `NEST_GRPC_EDGE_ALIAS`. Use `--edge-alias` for any other alias.
- Both Traefik families are supported, as before: v2.x to v3.4 take PROXY v2 on the
  loadBalancer, and v3.5 and later on a serversTransport. `render.sh` picks the form.
- Install new files under new names. **Never overwrite or edit the existing staging
  route file** (`nest-grpc.yml`). Traefik reloads a new file by itself. Removing that
  file is the rollback.
- `preflight` must show `route_names`, `route_sni`, `route_target` and `edge_alias`
  as PASS. Then run the credential-free external checks for that name from a machine
  outside the Tailnet ([Validate](public-grpc-gateway)).
- Certificates: each deployment's `grpc-certificates` service requests only its
  own name, with its own ACME account in its own volume. Both start on Let's
  Encrypt **staging**. Switching Release to the production directory is part of H09
  (#90) and needs its approval. Main stays on staging unless a production
  certificate for its name is approved separately.

## Web and sign-in

- Add one Dokploy Domain for each application, on the `controller` service, port
  8080: `main.nest.cerulean.works` for Main and `nest.cerulean.works` for Release.
  Dokploy names each router after the application, so the routers do not collide.
- The Cloudflare Tunnel's public hostnames must point at Dokploy's Traefik (with the
  `Host` header kept), **never** at `controller:8080`. On the shared edge network,
  `controller` resolves to both Controllers.
- Each origin has its own `NEST_GOOGLE_REDIRECT_URL` (registered in Google) and
  `NEST_PUBLIC_URL`. Session cookies are `__Host-` cookies, host-only, so a browser
  session for one origin is never sent to the other, and each Controller accepts
  only the sessions in its own database.
- The canonical origin move, the legacy `auth.nest.cerulean.works` behavior and the
  Console's connection profiles belong to #96. This page does not change them. Keep
  `NEST_CONSOLE_HOSTED_PROFILE=off` and `HOSTED_PROFILE_DEFAULT=False` until H09.

## Migrate the existing staging application to Main

The existing application keeps its appName, volumes, database, Tailnet node and
staging route until the maintainer decides otherwise (decision 5).

1. **Phase 1, no change to Main.** Create Release as above with alias
   `nest-grpc-gateway-release` and origin subnet `10.253.251.0/28`. Main keeps the
   default alias `nest-grpc-gateway`, its `10.253.250.0/28` subnet and its existing
   staging route. The two aliases already differ, and `preflight` checks both.
2. **Phase 2, Main's new names (when approved).** Set Main's
   `NEST_GRPC_PUBLIC_HOST=grpc.main.nest.cerulean.works` and
   `NEST_GRPC_EDGE_ALIAS=nest-grpc-gateway-main`, and set `NEST_GOOGLE_REDIRECT_URL`
   and `NEST_PUBLIC_URL` to the Main origin. Render and install
   `nest-grpc-main.yml` with `--instance main`. Then redeploy Main. Its
   certificate service requests the new name, and the gateway starts serving it.
   From then on the gateway no longer serves the old staging name.
3. Verify Main (`preflight`, `gateway-runtime`, external checks with the staging
   CA, and sign-in on the Main origin). Remove the old staging route file only
   after an explicit decision, and keep a copy.
4. **Rollback of phase 2:** restore Main's previous Environment values and
   redeploy, then remove `nest-grpc-main.yml`. The staging route file was never
   changed. Release is not touched at any point.

## Starting data for Release

Main and Release never share a writable database. Pick one option and record the
choice (decision 2). The Release application must not start serving users before
then.

| Option | What it means | Trade-offs |
| --- | --- | --- |
| A. Fresh | A new, empty database, migrated by `nest-db upgrade`. The bootstrap admin signs in, then Projects, memberships, Platform grants and Slurm account links are recreated for the approved cohort. | No inherited state or test data. Users and Projects are recreated by hand, and existing CLI sessions are not valid on Release. |
| B. Copy | `pg_dump` from Main at a recorded time, then `pg_restore` into Release's new volume before Release first starts. Then revoke every session in the copy (`nest-db sessions revoke --user-id` for each User, see [Sign in with Google](authentication)), and review Projects, invitations and grants. | Keeps Projects and users, but also copies Main's test data, and the two copies diverge from then on. The dump holds personal data and must be handled as a secret. |
| C. Move | As B, but Main is then reset to a new database. | Main loses its history. It needs a Main backup and an explicit decision, and is rarely worth it. |

Whichever option you pick, the schema must match the Release tag's minimum: the
`migrate` profile runs `nest-db upgrade`, and `nest-db verify` checks the result.
The rollback for B or C is the backup taken first. Restore it only into the
application it came from.

## HPC capacity

Separate databases give each deployment its own Execution Registry and admission
ledger. If both submit to **the same Slurm account on the same cluster**, each one
admits up to its own limits, so the cluster sees the sum. Two deployments with
`NEST_LIMIT_ACTIVE_PER_PLATFORM=4` can hold 8 jobs. The policy:

1. **Separate workdirs, always.** Each deployment deploys its own runtime version
   into `<workdir>/project`. It also keeps its own `environments/` cache and
   `jobs/` inputs, outputs and worker state. Use distinct paths such as
   `<cluster path>/nest-main` and `<cluster path>/nest-release` on every Platform.
   `preflight` fails when another deployment on the host uses the same workdir on
   the same login hosts. It does not read a `NEST_PLATFORMS_ENV_FILE`, so compare
   those by hand.
2. **Account and quota.** Prefer separate Slurm accounts, or QoS limits set by the
   cluster administrator (`GrpTRES`, `GrpJobs`), which Slurm itself enforces across
   both deployments. If one account is shared, the operator approves a split, and
   each deployment's `NEST_LIMIT_ACTIVE_PER_PLATFORM`, `..._PER_PROJECT`,
   `..._CPU_PER_PROJECT` and `..._GPU_PER_PROJECT` are set so that Main's plus
   Release's stay within the approved allocation. `preflight` warns about a shared
   account and names the deployments. It never changes a limit.
3. **Attribution.** Jobs from both deployments are named `nest-worker`. The working
   directory tells them apart:
   `sacct --format=JobID,JobName,Account,WorkDir%60,AllocTRES%40`. Each deployment's
   audit log records its own submissions.
4. Main is for authorized developers. Keep its cohort and limits small. Do not
   submit live jobs to check this setup: the checks above are read-only.

## Connect the SDK to Main

Main is never chosen by default, and Release's hosted session is never sent to it.
Developers name Main explicitly, over verified TLS:

```bash
export NEST_CONTROLLER_ADDRESS=grpc.main.nest.cerulean.works:443
export NEST_CONTROLLER_TLS=true
export NEST_CONTROLLER_TLS_CA_FILE=/path/to/lets-encrypt-staging-roots.pem  # while Main has a staging certificate
export NEST_AUTH_URL=https://main.nest.cerulean.works
uv run nest login        # a Main session, stored for that origin
```

- Sessions are stored per sign-in origin, so a Main session and a Release session
  live side by side.
- If `NEST_AUTH_URL` is the hosted origin, the SDK refuses to send that session to
  Main unless `NEST_HOSTED_SESSION_ADDRESS` names Main again. It never sends it in
  plaintext. Without `NEST_AUTH_URL`, calls to Main carry no session at all.
- The hosted profile (`NEST_PROFILE=hosted`, and `HOSTED_PROFILE_DEFAULT` after
  H09) only ever names the Release gateway. `tests/test_multi_environment.py`
  checks these rules.

## Monitoring

The monitoring for both deployments comes from #114 (PR #115). Until it can tell
the deployments apart, run its collectors for one deployment only, Release once
it serves users. Then two deployments can never overwrite each other's
`nest_<collector>.prom` files in one textfile directory. Monitoring both needs:

- one textfile directory, state directory and systemd instance per deployment;
- a bounded `environment` label (`main` or `release`) on every series and in every
  alert;
- a probe per public name, with its own trust root.

That is coordinated on PR #115. This page does not change the monitoring stack.

## Verify

For each application, after every deploy, restart or route change:

```bash
python3 scripts/nest-deploy-doctor.py preflight --project <appName> --env-file <its .env>
python3 scripts/nest-deploy-doctor.py gateway-runtime --project <appName>
# from outside the Tailnet, no credentials:
uv run python scripts/verify-public-grpc.py <its gRPC name>:443 [--ca-file <staging root>]
```

Also check that the other application was not restarted: its containers'
`docker inspect --format '{{.State.StartedAt}}'` values and its route file are
unchanged.

## Roll back one environment

- **Release:** remove `nest-grpc-release.yml` (gRPC stops for that name only), stop
  or redeploy the Release application at its previous tag, and restore its own
  backup if needed. Main's containers, route and volumes are not touched.
- **Main:** redeploy the previous `main` commit, or restore its previous
  Environment values. Its route file, if changed, goes back to the kept copy.
- Never run `docker compose down --volumes` on either application, never
  `docker network prune`, and never delete the other application's route file. The
  doctor reports problems, and its fixes change nothing on the host.

## Evidence

| Claim | Evidence | Kind |
| --- | --- | --- |
| Old fixed alias resolves to both gateways; doctor fails both; with duplicate route names Traefik routes only one name | `tests/smoke_two_environments.sh` part 1 | synthetic, real Traefik and Envoy containers |
| Per-deployment aliases and `--instance` routes: right gateway, Controller and certificate for each name, PROXY v2 attribution, unknown name refused, no cross-origin path, no host ports; Traefik pre-v3.5 and v3.5+ | part 2 | synthetic, real containers |
| Main recreated, rolled back and unrouted while Release serves every handshake; Release containers, route and database untouched; Main's data survives | part 3 | synthetic, real containers |
| Negative preflights: subnet, alias, name, Tailnet hostname, workdir; service-name alias refused | part 4; `tests/test_multi_environment.py` | synthetic |
| Single-deployment output unchanged | `tests/test_multi_environment.py` | synthetic |
| Dokploy trigger semantics, DNS, Tunnel routes, OAuth sign-in on both origins, certificates, data migration, Tailnet enrollment, HPC smoke | **PENDING**: operator only | live |

## Not verified here

- Real Dokploy: application creation, trigger behavior and whether it rewrites the
  file (the smoke test runs Compose directly, as Dokploy does).
- Cloudflare Tunnel and DNS records, Google OAuth on the new origins, Let's
  Encrypt issuance for the new names, and Tailnet enrollment of a second node.
- Anything on the HPC cluster. The workdir and account checks compare settings
  only.
