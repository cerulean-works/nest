# Register HPC Platforms and login nodes

Nest keeps a server-side catalog of the HPC Platforms it can run on and the login
nodes it may use to reach them. Operators manage the catalog; users only discover
the Platforms their Projects were granted, and the Controller, never the client,
chooses which login node to connect to.

The catalog needs the PostgreSQL database (see
[Store Controller records in PostgreSQL](database)) and is added by migration
`0012_hpc_platform_catalog`. Run `nest-db upgrade` first.

## What a Platform is

An **HPC Platform** is one Slurm job and account namespace, such as `nano4` or
`f1`:

- It has a generated, immutable **Platform ID** (a UUID) and a unique, immutable
  **slug** that users see. Platforms are disabled, never deleted, so neither is
  ever reused.
- Its **operator**, such as NCHC, is optional metadata. Two Platforms run by the
  same operator are separate Platforms with separate IDs, separate login nodes and
  separate grants. There is no Provider record above them.
- A Slurm job ID belongs to one Platform: job records store the Platform ID
  (`jobs.platform_id`) they were submitted to, so job `4242` on `nano4` and job
  `4242` on `f1` never collide.
- Slurm accounts are not part of a Platform's identity. The account registry is a
  separate record (D08).

Each Platform has one or more **login nodes**. Every login node of a Platform
reaches the same namespace, so failing over from one node to another keeps the
Platform ID. A login node has:

| Field | Meaning |
| --- | --- |
| `host`, `port` | The approved endpoint. No port uses SSH's default in Mode A and 22 in Mode B. |
| `transport_modes` | Approved modes: `A` (trusted operator SSH) and/or `B` (managed SSH). |
| `priority` | Lower is tried first among nodes of the same health. |
| `status` | `active`, or `disabled` to withdraw approval without removing the node. |
| `host_key_pin_ref` | Reference to the operator-vetted SSH host key, such as `SHA256:...`. Required for Mode B. A reference only, never key material. |
| `health` | `unknown`, `healthy`, `degraded` or `unhealthy`, recorded by the Controller. |

## Manage the catalog

Operators manage Platforms with `nest-db platform`, authenticated by the database
credentials in `NEST_DATABASE_*`. Every change takes `--operator` and `--reason`.
They are recorded in the [security audit log](audit-log) with the PostgreSQL
role. `PLATFORM` is a slug or a Platform ID.

```bash
nest-db platform create --slug nano4 --name "Nano 4" --operated-by NCHC \
  --operator ops-alice --reason "onboard nano4"
nest-db platform create --slug f1 --name "Forerunner 1" --operated-by NCHC \
  --operator ops-alice --reason "onboard f1"

nest-db platform add-node nano4 --host ln01.nano4.example --modes B --priority 10 \
  --host-key-pin SHA256:Xb3...vetted --operator ops-alice --reason "primary login node"
nest-db platform add-node nano4 --host ln02.nano4.example --modes B --priority 20 \
  --host-key-pin SHA256:Q9k...vetted --operator ops-alice --reason "failover login node"

nest-db platform grant nano4 --project-id 6f0c...e1 --operator ops-alice --reason "lab allocation"
nest-db platform set-default nano4 --operator ops-alice --reason "primary cluster"
nest-db platform show nano4
```

Other actions: `list`, `update` (display name and operator metadata), `disable`
and `enable`, `set-default --none`, `update-node NODE_ID` (modes, priority, port,
host-key pin, status), `remove-node NODE_ID` and `revoke`.

The schema enforces these rules for every writer, `psql` included:

- A change to a Platform, login node or grant that names no operator is refused
  (`*_operator_managed`). Only a login node's health columns may change without
  one.
- Platform IDs and slugs never change, and a login node never moves to another
  Platform.
- A Mode B login node must have a host-key pin reference.
- There is at most one default Platform, and it is never disabled.

### Bootstrap from the environment

At every start, the Controller mirrors each Platform Profile from
`NEST_PLATFORMS` (or the deprecated `NEST_PROVIDERS` and `NEST_SLURM_*` settings,
see [HPC Platforms](configuration#hpc-platforms)) as a Platform with
`source = env`. The Platform's slug is the profile ID and it keeps its Platform ID
across restarts. Its display name and operator metadata follow
`NEST_PLATFORM_<ID>_NAME` and `NEST_PLATFORM_<ID>_OPERATOR`; a profile without an
operator label keeps the recorded one. Its login nodes follow the profile's `LOGIN_NODES` in order,
approved for the profile's SSH mode. A Mode B node's host-key pin reference is
`env:NEST_SSH_KNOWN_HOSTS`, the operator-vetted `NEST_SSH_KNOWN_HOSTS` or
`NEST_SSH_KNOWN_HOSTS_DATA`. When a profile is removed from the environment, its
Platform is disabled. Jobs the Controller submits record that Platform's ID.

These changes are audited with the operator `nest-controller:environment`.
Environment Platforms are changed by editing the environment, not by
`nest-db platform`. Grants and the default Platform are never set from the
environment: grant Projects to an environment Platform with `nest-db platform
grant` like any other. If an operator-managed Platform already has a profile's
slug, the Controller refuses to start and writes nothing (exit status 3), so no job
is ever recorded without a Platform. Rename the profile, or remove it from the
environment.

## Discover Platforms

A User may use a Platform only when the operator has granted it to a live Project
the User is an active member of, in any role. Discovery reads grants, memberships
and statuses on every request, so a revoked grant, a removed member, a disabled
User or Platform, or a deleted Project takes effect at once. Platform and Project
roles grant no Platform.

From the command line, signed in with `nest login`:

```bash
nest platforms list
#   f1               Forerunner 1 (NCHC)  id=0b9e...
# * nano4            Nano 4 (NCHC)  id=7555...
nest platforms list --project-id 6f0c...e1 --json
nest platforms show nano4
```

`*` marks the Platform the server uses when none is named. From Python, call
[`platforms.list_platforms`] and [`platforms.get_platform`].

Over HTTP, on the auth listener, with a browser session or a CLI session that has
the `projects:read` or `jobs:run` scope:

| Route | Answer |
| --- | --- |
| `GET /api/platforms[?project_id=UUID]` | `{"platforms": [...], "default_platform_id": ...}` |
| `GET /api/platforms/<slug or ID>[?project_id=UUID]` | One Platform |

Each Platform is returned as `id`, `slug`, `display_name`, `operator_name`,
`scheduler`, `is_default`, `transport_modes` and `login_node_count`. Hosts, ports
and host keys are never returned. A Platform that does not exist, is disabled, or
is not granted to the caller is answered `404 {"error": "not_found"}` in every
case. There are no write routes: the catalog cannot be changed through the API.

## Default Platform and login-node selection

[`authz.PlatformCatalog`] resolves the Platform and login node for a request on
the server:

1. **Platform.** A named Platform must be authorized for the caller. Without a
   name, the server's default is used if the caller is authorized for it,
   otherwise the caller's only authorized Platform. With several authorized
   Platforms and no usable default, the caller must name one.
2. **Login node.** Of the Platform's active nodes approved for the transport mode,
   healthy nodes come first, then unknown, degraded and unhealthy ones, each by
   priority. A caller can at most ask for one of the approved node IDs. A host,
   port, host key or SSH option from the client is never accepted.
3. **Failover.** When a node cannot be reached, it is recorded as `unhealthy` and
   the next approved node of the same Platform is returned. The Platform ID does
   not change, so job records and Slurm job IDs stay in the same namespace.

A Mode B endpoint always has an explicit port and host-key pin reference. It is
built from the registry alone and never depends on an SSH configuration file or
Tailscale. Refused requests (an unauthorized Platform, an unapproved node) are
recorded in the security audit log as `platform.resolve` or
`platform.endpoint.select` with outcome `denied`.

## Limitations

- The Controller still submits each job through its default Provider Profile's
  backend. Choosing the Platform per request, and running each Platform's
  connection through [`authz.PlatformCatalog`], is follow-up work (C01).
- Health is recorded when a caller fails over. There is no background probe.
- A Platform's Slurm accounts and per-User account links are not modeled here
  (D08).
