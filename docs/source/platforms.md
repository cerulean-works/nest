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
- Slurm accounts are not part of a Platform's identity. They are registered per
  Platform in a separate registry: see [Map Slurm accounts to Projects](slurm-accounts).

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

1. **Platform.** See [Project eligibility and default Platform](#project-eligibility-and-default-platform)
   below for the full order. In short: a named Platform must be eligible, then the
   Project's default, then the server's default, then the only eligible Platform.
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

## Project eligibility and default Platform

A Project's **eligible Platforms** are exactly the active Platforms the operator
granted to it (`nest-db platform grant`). Nothing else makes a Platform eligible: no
Project, Organization or Platform role, no Slurm account and no client input.
[`authz.PlatformCatalog.select_platform`] is the one policy every interface uses to
decide which Platform a Project's request runs on; it returns the Platform and the
`origin` of the choice. With a Project, the first rule that applies wins:

| Step | Rule | `origin` |
| --- | --- | --- |
| 1 | The Platform the caller named (slug or ID), which must be eligible | `explicit` |
| 2 | The Project's default Platform | `project_default` |
| 3 | The operator's server default (`set-default`), if eligible | `server_default` |
| 4 | The Project's only eligible Platform | `only_eligible` |

These outcomes are deterministic and never substitute another Platform:

- **Unauthorized selection.** A named Platform that does not exist, is disabled, or
  is not granted to the Project is refused as not found (`404`), with
  `platform.resolve` and reason `platform_not_authorized` in the security audit
  log. The server never falls through to a default.
- **Unavailable default.** If the Project's default Platform is disabled, a request
  that names no Platform is refused with `default_unavailable` instead of running
  on another cluster. Naming an eligible Platform still works.
- **Missing default.** With no Project default, steps 3 and 4 apply. With no
  eligible Platform the answer is `none_authorized`; with several and no usable
  default it is `ambiguous` and the caller must name one.

Without a Project (`GET /api/platforms` with no `project_id`), the caller's
Platforms across all their Projects are considered and step 2 does not apply.
Requests that run on a Platform, such as job submissions, must always name their
Project.

### Choose a Project's default Platform

A Project's Owners and Admins may choose its default among its eligible
Platforms, from a browser session. Members and Viewers, CLI sessions and
non-members are refused. The schema keeps the default one of the Project's grants
for every writer (`project_default_platforms` has a foreign key to the grant), and
revoking the grant clears the default in the same transaction. Choosing a default
and a concurrent revocation are serialized: the revocation wins, and the choice is
refused. Every change is recorded as `project.default_platform.set` or
`project.default_platform.cleared`.

| Route | Body | Answer |
| --- | --- | --- |
| `GET /api/projects/<id>/platforms` | | The Project's eligible Platforms and default |
| `POST /api/projects/<id>/platforms/default` | `{"platform": "<slug or ID>"}` or `{"platform": null}` | The same, after the change |

The answer lists `platforms` (each with `is_project_default`),
`project_default_platform_id`, and what a request naming no Platform resolves to:
`default_platform_id` and `default_origin`, or `unresolved_reason`. Any member,
and a CLI session with `projects:read` or `jobs:run`, may read it. A non-member gets
`404` for a Project that exists, and so does a Platform that is not eligible.

From the command line and Python:

```bash
nest platforms default --project-id 6f0c...e1
# nano4 (project_default)
```

```python
from nest.platforms import project_platforms

view = project_platforms("6f0c...e1")
view.default_platform_id, view.default_origin
```

### Contract for the Controller and other interfaces

The Controller's execution path (C01 Part 2) and every later interface, including
a Web UI, must resolve the Platform on the server with
[`authz.PlatformCatalog.select_platform`] (or [`authz.AccountResolver.resolve`],
which calls it) from the **authenticated** principal and Project, passing the
caller's requested Platform only as a reference to check. They must not read
grants or defaults themselves, accept a Platform, login node, Slurm account or HPC
identity from the client as authority, or retry on another Platform when the
chosen one is refused. An operation on a Platform already chosen, such as reading
or cancelling a job the Controller submitted to it, is checked with
[`authz.PlatformCatalog.authorize_platform_action`] after the Project check. See
[Platform authorization](security#platform-authorization) for every entry point. A Web UI may hide controls it knows will be refused; that
is never the check.

The [`authz.HPCIdentity`] that account selection needs is built by the server
from its own records only. Today that is the Controller's own identity in Mode A.
Nest has no verified per-User HPC identity binding yet, so no Mode B identity,
default account or entitlement can be supplied to the policy from a client or
from Project membership; Mode B never falls back to Controller credentials.
Verified bindings are future Mode B work.

## Limitations

- The Controller still submits each job through its default Provider Profile's
  backend. Choosing the Platform per request with
  [`authz.PlatformCatalog.select_platform`], and running each Platform's
  connection through [`authz.PlatformCatalog`], is follow-up work (C01 Part 2).
- Health is recorded when a caller fails over. There is no background probe.
- A Platform's Slurm accounts and their Project links are in the
  [Slurm Account Registry](slurm-accounts).
