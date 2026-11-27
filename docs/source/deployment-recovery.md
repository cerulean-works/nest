# Preflight and recover a Dokploy deployment

This page covers two ways a `compose.dokploy.yaml` deploy can fail before the
Controller starts, how to check for them first, and how to recover safely. Both
were seen on the hosted Dokploy deployment on 2026-11-26. The tool for all of it is
`scripts/nest-deploy-doctor.py`. It needs only Python 3.9+ and the Docker CLI on
the Dokploy host, and it does not import Nest.

```bash
# On the Dokploy host, from the deployment's checkout:
cd /etc/dokploy/compose/<appName>/code
python3 scripts/nest-deploy-doctor.py preflight      --project <appName> --env-file .env
python3 scripts/nest-deploy-doctor.py migrate-status --project <appName>
```

`<appName>` is the Compose project name Dokploy uses (`docker compose ls` lists
it). The doctor only reads the network settings from `.env`, reads them literally,
and never prints any other value. Only `recover-migrate --apply` changes anything,
and all it can do is remove one container that never started.

## What happened on 2026-11-26

Dokploy, Docker Compose v5.8.0, Docker Engine 29.10.0 (linux/amd64):

1. The image built, but the deploy failed creating `grpc-origin`: its default
   subnet `172.31.250.0/28` overlaps an existing `172.31.0.0/16` Docker network.
2. The operator checked the host's networks and routes and set
   `NEST_GRPC_ORIGIN_SUBNET=10.253.250.0/28` and `NEST_GRPC_GATEWAY_ADDRESS=10.253.250.2`.
   The public pilot stayed off.
3. The retry created the network and recreated the Controller, then waited forever
   on a `migrate` container whose name had a 12-hex-digit prefix. It was in state
   Created, with PID 0 and empty logs. PostgreSQL and Tailscale were healthy.
4. `docker start -a` on that container printed "schema 18 already current" and
   exited 0. The waiting deploy then started the Controller, which became healthy.
5. Private SDK sign-in, `nest projects list` and `current` (only one eligible),
   and a remote function with no `NEST_PROJECT_ID` returning 42 all passed. That
   tests the private path only; it is not public ingress or CUDA acceptance.

## Verified cause and open hypotheses

Reproduced with `tests/repro_migrate_stall.sh`, a busybox-only copy of
`compose.dokploy.yaml`'s start order, on Docker Engine 29.9.3 with Compose v5.7.1.
Compose v5.8.0 and Dokploy itself were not available to test.

| Claim | Status | Evidence |
| --- | --- | --- |
| A subnet that overlaps an existing network makes the deploy fail at network creation, and the running containers stay as they were | **Verified** | Step 1 of the script: `invalid pool request: Pool overlaps with other one on this address space` |
| The default `172.31.250.0/28` is inside Docker's default address pools (`172.17.0.0/16` to `172.31.0.0/16`, then `192.168.0.0/16`), so a host with many networks can already hold it | **Verified** (Docker defaults) | `docker network create` without a subnet hands out those pools |
| Compose recreates a container by first creating its replacement as `<first 12 hex of the old ID>_<name>`, then removing the old one, then renaming. Interrupting it in between leaves the replacement in Created (PID 0, never started) | **Verified** | Step 2: killing `docker compose up` during `migrate`'s recreate leaves exactly that container |
| The next `docker compose up -d` adopts that container, removes the old one, and waits on it forever without starting it | **Verified** | Step 3: the retry is still waiting after 30 s. The log ends at `Container <prefix>_…-migrate-1 Waiting` |
| `--wait --wait-timeout N` bounds that wait, but the deploy still fails | **Verified** | `controller … not started: condition "service_completed_successfully" of service "migrate" not met` |
| Removing the never-started container makes a waiting deploy fail at once, and the next deploy runs `migrate` and starts the Controller with every volume kept | **Verified** | Step 4 |
| What interrupted the first recreate on Dokploy | **Not confirmed** | Compose starts `migrate`'s recreate while it is still creating networks. A network failure that lands after the replacement was created, a Dokploy-side timeout or cancellation, or a Compose v5.8.0 behavior change are all consistent with step 3. In 35 runs with Compose v5.7.1, a failed network create alone did not leave the container. No upstream bug is claimed. |

The step 2 interruption is a race, so the script retries it, and step 3 has failed
to reproduce in some runs. When it passes, every step behaved as described in the
table.

## Before a deploy: preflight

Run `preflight` before any deploy that creates or changes `grpc-origin`, such as
the first deploy of this release, a new `NEST_GRPC_ORIGIN_SUBNET`, or enabling
`public-grpc`. It exits 1 if any check fails:

It reads the values the way `docker compose --env-file .env` resolves them. A
variable exported in the invoking shell wins over `.env`, even when it is empty.
An empty or unset value takes the Compose file's default, as `${NAME:-default}`
does. A `.env` value that Compose would interpolate (a `$` outside single quotes)
is refused rather than guessed. Run it from a clean shell: Dokploy deploys from
`.env` alone, and the doctor warns (`WARN settings`) when the shell overrides it.

| Check | Fails when |
| --- | --- |
| `settings` | A `.env` value it checks uses `$` interpolation (FAIL), or the shell environment overrides `.env` (WARN) |
| `origin_subnet` | `NEST_GRPC_ORIGIN_SUBNET` is not an IPv4 network (host bits set count too), or is not a /16 to /29 |
| `gateway_address` | `NEST_GRPC_GATEWAY_ADDRESS` is outside the subnet, or is its network, broadcast or first host (Docker gives the first host to the bridge) |
| `origin_subnet_free` | Another Docker network overlaps the subnet; it is named with its subnet. This project's own `grpc-origin` with the same subnet is fine. If the subnet differs, you get a warning: Compose will recreate the network and restart the Controller. |
| `origin_routes` | A host route overlaps the subnet (from `ip -json -4 route show table all`), for example a VPN or cloud route. Reported as SKIP, never PASS, when `ip` is missing. |
| `public_pilot`, `trusted_proxies`, `allowed_peers`, `edge_proxy`, `traefik_version` | With `public-grpc` in `COMPOSE_PROFILES` only. `NEST_PUBLIC_PILOT` is off. `NEST_GRPC_TRUSTED_PROXIES` is not exactly the gateway's /32. `NEST_GRPC_ALLOWED_PEERS` misses the gateway or the actual `<appName>_tailnet` bridge subnet (Tailnet users would be refused), or is wider than the origin subnet. `NEST_GRPC_EDGE_PROXY` is not the running Traefik's /32 on the edge network. The last check names the [Traefik route file](public-grpc-gateway#deploy-to-staging) that matches the installed Traefik version. |
| `project_identity` | Containers of this Compose project come from more than one checkout: two deployments share one project name, and with it every network, volume and database. A WARN for the default project `nest`. |
| `edge_alias`, `public_host` | With `public-grpc` only. `NEST_GRPC_EDGE_ALIAS` is malformed or a Compose service name, or another container on the edge network answers to it; or another deployment's gateway serves the same `NEST_GRPC_PUBLIC_HOST`. A conflict with a running deployment fails and one with a stopped deployment warns ([two deployments on one host](multi-environment)). |
| `database_host` | The short database host name in `NEST_DATABASE_URL` (such as `postgres`) also names another project's container on the edge network, which the Controller joins. The URL is never printed. |
| `tailnet_hostname` | Another deployment's Tailscale sidecar uses the same `NEST_TAILSCALE_HOSTNAME`. |
| `hpc_workdir`, `hpc_account` | Another deployment on this host uses the same HPC workdir on the same login hosts (FAIL), or the same Slurm account (WARN: their admission limits add up). Values are never printed; a `NEST_PLATFORMS_ENV_FILE` is not read. |
| `route_names`, `route_sni`, `route_target` | Read from `--traefik-dynamic-dir` (default `/etc/dokploy/traefik/dynamic`; SKIP if unreadable). A TCP router, service or transport name is defined in two files (Traefik keeps one definition and the other name is not routed), one SNI name has two routers, or this deployment's route does not dial exactly `NEST_GRPC_EDGE_ALIAS`:8443 (WARN while no route is installed). |

If `origin_subnet_free` or `origin_routes` fails, pick a private /28 that
`docker network inspect $(docker network ls -q) --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}'`
and `ip route show table all` do not list. Set both variables in Dokploy's
Environment panel, with the gateway on the subnet's second host:

```ini
NEST_GRPC_ORIGIN_SUBNET='10.253.250.0/28'
NEST_GRPC_GATEWAY_ADDRESS='10.253.250.2'
```

Run `preflight` again. Never delete a network you did not create to free a subnet.

`preflight` checks settings before a deploy. After a deploy with `public-grpc`,
`nest-deploy-doctor.py gateway-runtime --project <appName>` checks the running
gateway's upstream resolution and the address the Controller actually sees (see
[The upstream name and the private path](public-grpc-gateway#the-upstream-name-and-the-private-path)).

### Why private deployments still allocate `grpc-origin`

The Controller always joins `grpc-origin`, so every `compose.dokploy.yaml` deploy
creates it, including deploys with the public pilot off. Compose has no way to join
a network only when a profile is on. Dokploy needs the one self-contained file, so
an override file is not an option either. The fixed subnet is also what lets the
Controller trust exactly the gateway's address (`NEST_GRPC_TRUSTED_PROXIES`). A
Docker-assigned subnet would not give the gateway a known address. This release
keeps the default for compatibility and adds the preflight above. Moving the
default outside Docker's pools, or giving private-only deployments a Compose file
without `grpc-origin`, would change every existing deployment's network, so it is
left for review (see the pull request).

## During or after a deploy: the migration

The Controller starts only after `migrate` (`nest-db upgrade`) exits 0. Nothing on
this page bypasses that. Compose YAML cannot put a timeout on its own `depends_on`
wait, and Dokploy runs Compose itself, so the doctor is the bounded check:

```bash
# Start it as you press Deploy, or name the deploy's start time:
python3 scripts/nest-deploy-doctor.py wait-migrate --project <appName> --timeout 300
python3 scripts/nest-deploy-doctor.py wait-migrate --project <appName> --since 2026-11-26T09:00:00Z
```

It counts only a migration that finished after `--since`, which defaults to when
the command starts. The previous deploy's finished `migrate` container is still
there at the start of a deploy, and it never counts as this deploy's result. The
command exits 0 when this deploy's migration finished and 4 when it failed. It
exits 3 when a container has been `stuck-created` (below) for `--stuck-after`
seconds (default 60), and 5 on timeout. It starts and stops nothing. On a plain Docker host, without Dokploy,
`docker compose -p <appName> -f compose.dokploy.yaml up -d --wait --wait-timeout 300`
gives the same bound, but the deploy itself fails rather than waits.

`migrate-status` explains what it sees. `--logs 40` adds the last lines of a
failed or running container, capped at 200; `nest-db` never logs credentials.

| State | Meaning | What to do |
| --- | --- | --- |
| `completed` | `nest-db upgrade` exited 0 | Nothing |
| `failed` | It exited non-zero; the Controller stays down (fails closed) | Read `--logs 40`, fix the cause (database reachability, password, TLS, a schema from a newer Nest; see [Troubleshooting](database#troubleshooting)), then deploy again. Never reset the database or start the Controller by hand. |
| `running` | The upgrade is still running | Wait; a long upgrade on a large database is normal. Check PostgreSQL if it does not finish. |
| `waiting-dependency` | Created but not started yet, because this project's PostgreSQL is not healthy. Compose creates `migrate` first and starts it only once PostgreSQL's health check passes, which can take minutes on a first start or a slow volume. | Wait. If it lasts, check the `postgres` container's health and logs. Never remove the `migrate` container. |
| `pending` | Created less than `--min-age` seconds ago (default 300, longer than PostgreSQL's health window) and not started yet | Wait |
| `stuck-created` | Created at least `--min-age` seconds ago and never started (PID 0, no start time, no logs), although PostgreSQL is healthy (or the project has none). `replacement prefix` when the name carries Compose's temporary prefix. | Recover as below |

`migrate-status` exits 0 for `completed`, 4 for `failed`, 3 for `stuck-created`,
and 5 while the migration is still in progress (`running`, `waiting-dependency`,
`pending`).

### Recover a stuck `migrate`

```bash
python3 scripts/nest-deploy-doctor.py recover-migrate --project <appName>          # prints the command
python3 scripts/nest-deploy-doctor.py recover-migrate --project <appName> --apply  # runs it
```

It acts only on `stuck-created` containers: they carry this project's label and
the `migrate` service label, have been in Created without ever starting for at
least `--min-age` seconds, and PostgreSQL, when the project has one, is healthy.
It never touches a container that is `waiting-dependency` or `pending`. It waits
`--settle` seconds (default 10), so that Compose can start a container whose
PostgreSQL has only just become healthy. Then it reads PostgreSQL's health and
each container again right before acting, and runs plain `docker rm <id>` without `-f` or `-v`, so it
cannot remove a running container or any volume. If a Dokploy deploy is still
waiting on that container, the deploy fails at once instead of hanging. Then
press **Deploy** again: Compose creates a fresh `migrate`, runs
`nest-db upgrade`, and starts the Controller only after it succeeds. Finally:

```bash
python3 scripts/nest-deploy-doctor.py migrate-status --project <appName>   # completed
docker compose ls                                                          # the project is running
docker volume ls --filter label=com.docker.compose.project=<appName>      # postgres-data, tailscale-state kept
```

Then run [Verify](dokploy#verify), including a private SDK call over the Tailnet.

The manual recovery used on 2026-11-26, `docker start -a <that container>`, also
works. It runs the migration with the configuration that container was created
with, and it only helps while a deploy is still waiting. Prefer `recover-migrate`
and a fresh deploy: the migration then always runs with the current configuration.

### Never do these

- `docker compose down --volumes`, deleting `postgres-data` or `tailscale-state`, or
  `nest-db` commands that reset data. A failed deploy never needs them.
- Starting the Controller without a successful migration, or removing the `migrate`
  profile to get past a failure.
- `docker rm -f`, or deleting networks or containers that belong to another
  application.
- Pasting `.env`, `docker inspect` output of the Controller, or logs with tokens
  into an issue. The doctor's output is safe to share: it contains only container
  names, states, network names and subnets.

## Tests

| Test | Kind | Covers |
| --- | --- | --- |
| `tests/test_deploy_doctor.py` | CI (unit) | Shell-over-`.env` precedence, empty overrides and defaults, refused interpolation, and shell-chosen profiles; `log-leak-check` with a synthetic token (found, clean, unreadable log, missing service, values only on stdin and never printed); every Compose command in the operator docs names `compose.dokploy.yaml` and an existing service; each `migrate` state, including the incident's container pair; a container waiting for a `starting` or `unhealthy` PostgreSQL, or created recently, is never stuck and never removed; a previous deploy's result never satisfies `wait-migrate`; bounded logs; project scoping; dry run; `--apply` removes only a never-started container, with plain `docker rm`, and skips one that started in the meantime; the `wait-migrate` bounds; every preflight check with passing and failing settings; literal `.env` reading with no secret printed; the defaults match `compose.dokploy.yaml`; Traefik file selection |
| `tests/test_deploy_doctor.py::RealDockerTests` | CI when a Docker daemon is present | A real never-started container with Compose labels is detected and removed; another container of the project is kept |
| `tests/repro_migrate_stall.sh` | Manual (Docker) | The failed network create, the interrupted recreate, the stall, the bounded wait and the recovery, with named volumes kept |
| `tests/smoke_compose_env.sh` | CI | Repeated `migrate` runs from an environment-only `.env`, restart and recreation keep data and the Tailscale identity |
| `tests/smoke_compose_postgres_upgrade.sh` | CI | `nest-db upgrade` across schema versions |
