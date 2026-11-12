# Share a Controller over Tailscale

Use `compose.tailscale-shared.yaml` **alone** for a trusted team whose members
use separate Tailnets. The existing `compose.tailscale.yaml` remains the
outbound-only deployment. Both use the same SSH alias, credential mounts,
official `tailscale/tailscale:v1.104.4` image, and named state volume.

```text
Recipient's user-owned Tailscale client
  -> shared Controller node, TCP 50051 (encrypted by Tailscale)
  -> userspace tailscaled / Serve raw TCP forwarder
  -> controller:50051 on the private Docker bridge

Controller / OpenSSH -> tailscale:1055 SOCKS5 -> original Tailnet -> HPC TCP 22
```

No SDK, SlurmBackend, Worker, or Login Node changes are needed. All callers use
the **same configured HPC SSH identity**. Nest accepts trusted serialized Python
and has no application authentication or per-user job authorization. A network
grant is not an application identity or tenant boundary. Only share with people
authorized to use that SSH identity and Controller. See [Security](security).

## Forwarding and compatibility

The checked-in `docker/tailscale-serve/serve.json` contains:

```json
{
  "TCP": {"50051": {"TCPForward": "controller:50051"}},
  "AllowFunnel": {}
}
```

This uses [Serve's raw TCP forwarding](https://tailscale.com/docs/reference/tailscale-cli/serve),
preserving gRPC HTTP/2 bytes. It does not use an HTTP reverse proxy, TLS
termination, HTTP identity headers, PROXY protocol, or Funnel. Tailscale encrypts
the client-to-sidecar connection; the bridge hop to Nest is plaintext.

The pinned version's [TCP handler](https://github.com/tailscale/tailscale/blob/v1.104.4/ipn/ipnlocal/serve.go)
dials the configured backend with `SystemDial`, so `controller:50051` resolves
through Docker DNS. The Controller must bind `0.0.0.0:50051` inside its own
container. It cannot bind localhost for this topology. Both containers join
the project bridge with explicit `controller` and `tailscale` aliases.

The sidecar retains `TS_USERSPACE=true`, SOCKS5 `:1055`, no accepted routes,
and all capabilities dropped. No TUN, `NET_ADMIN`, or host networking is needed.
Neither container publishes a host port, and both disable Traefik discovery.
Do not add a Dokploy Domain or public port for either service.

[`TS_SERVE_CONFIG`](https://tailscale.com/docs/features/containers/docker/docker-params)
loads the read-only directory-mounted configuration at startup. The pinned
[containerboot implementation](https://github.com/tailscale/tailscale/blob/v1.104.4/cmd/containerboot/serve.go)
also watches the directory for changes and permits this TCP-only configuration
without an HTTPS certificate domain. Mount the directory, not only an individual
file, so atomic replacements remain visible. Changes made with `tailscale serve`
are not the source of truth; edit the managed JSON and restart the sidecar.
An authenticated sidecar health check does not prove Serve or its backend works.

## Sharing eligibility and quarantine

[Node Sharing](https://tailscale.com/docs/features/sharing) grants access to a
specific recipient user, not their entire Tailnet. Recipients must connect from
user-owned devices; tagged recipient clients cannot use shares. Recipient policy
can also block access. Use the shared node's full MagicDNS name or the IP shown
to the recipient, rather than a short name or an assumed identical IP.

Keep `tag:nest-controller` on the source sidecar. The Sharing page's wording
about tags is ambiguous; [upstream issue #17399](https://github.com/tailscale/tailscale/issues/17399)
reports successful sharing of a tagged source. Do not interpret this as support
for sharing **to** a tag. Confirm that this tagged node can be shared and reached
in your actual accounts before rollout; this has not been tested here. If your
control plane rejects it, stop rollout. An alternative is a dedicated user-owned
source node, using its exact Tailnet IP for both source and destination policy
selectors. That requires a separately reviewed enrollment/policy change; simply
removing the tag can inherit the owner's broader permissions.

Quarantine prevents this shared-out node from initiating connections into the
**recipient's** Tailnet. It does not prohibit Controller-to-HPC connections in
the **original** Tailnet; those still require the owner's policy below. Do not
mutually share recipient nodes or share the HPC node. Tags are not exported for
use in the recipient's policy. Sharing does not grant recipients general access
to the owner's other devices.

## Merge a minimal policy fragment

Adapt and merge these entries into the existing **owner Tailnet** policy. This
is not a replacement policy. Replace the example IPs with the actual Controller,
HPC Login Node, and another private node; replace Alice with an accepted share
recipient's actual Tailscale login identity. No Login Node tag is required.

```json
{
  "tagOwners": {"tag:nest-controller": ["autogroup:admin"]},
  "hosts": {
    "nest-controller-node": "100.100.100.10",
    "nest-hpc-login": "100.100.100.20",
    "nest-other-device": "100.100.100.30"
  },
  "grants": [
    {
      "src": ["autogroup:shared"],
      "dst": ["tag:nest-controller"],
      "ip": ["tcp:50051"]
    },
    {
      "src": ["tag:nest-controller"],
      "dst": ["nest-hpc-login"],
      "ip": ["tcp:22"]
    }
  ],
  "tests": [
    {
      "src": "tag:nest-controller",
      "proto": "tcp",
      "accept": ["nest-hpc-login:22"],
      "deny": ["nest-hpc-login:80", "nest-other-device:22", "nest-other-device:50051"]
    },
    {
      "src": "alice@example.com",
      "proto": "tcp",
      "accept": ["nest-controller-node:50051"],
      "deny": ["nest-controller-node:22", "nest-controller-node:1055", "nest-controller-node:9002", "nest-hpc-login:22", "nest-other-device:22"]
    }
  ]
}
```

For an explicit allowlist, **replace** `autogroup:shared` with the accepted
recipient identities, such as `["alice@example.com", "bob@example.com"]`.
Use identities shown by Tailscale, not merely the addresses used to send
invitation emails. The share itself and policy must both allow the connection.
Keep tag ownership with administrators; callers need no tag ownership.

[Grants](https://tailscale.com/docs/reference/syntax/grants) and legacy ACLs are
additive. Audit **all** existing rules matching this node or shared recipients,
including broad `*`, user/group, tag, IP/CIDR, and `autogroup:shared` allowances.
Remove or narrow conflicting broad allows while preserving unrelated access.
A narrow rule cannot cancel them. If no access-control section exists, account
for the default allow policy before claiming isolation. Missing grants provide
the deny behavior; there is no explicit deny grant. The `deny` entries above
are [test assertions](https://tailscale.com/docs/reference/syntax/policy-file#tests),
not firewall rules. Run the merged policy's tests in the admin console. Policy
tests do not prove share acceptance, quarantine, or live reachability.

Expected behavior after auditing the complete policy:

| Source | Destination | Permitted new connections |
| --- | --- | --- |
| Accepted, allowed recipients | Shared Controller | TCP 50051 only |
| Controller tag | Original Tailnet HPC IP | TCP 22 only |
| Controller tag | Other private Tailnet nodes | None |
| Recipients | Other private nodes | None through this share |

This constrains Tailnet traffic, not Docker bridge access or ordinary Internet
egress. Trust the Docker/Dokploy host and other containers on these networks.

## Dokploy v0.30.10 configuration

Select repository deployment, **Compose Type: Docker Compose**, and
**Compose Path: `./compose.tailscale-shared.yaml`** at the repository root.
Use Compose v2.20 or newer. Do not combine this file with the other Compose
files. It is self-contained because v0.30.10
[parses and rewrites YAML before deployment](https://github.com/Dokploy/dokploy/blob/v0.30.10/packages/server/src/utils/docker/domain.ts).
Inheriting the development listener and clearing `ports` with `!reset` would
depend on a YAML tag surviving that preprocessing.

Keep the existing `NEST_EXECUTOR=slurm`, account, and absolute HPC work directory.
Set these Dokploy Environment values (only paths, never key contents):

```ini
NEST_SSH_DIR=../files/nest-ssh
NEST_TAILSCALE_AUTHKEY_FILE=../files/tailscale/authkey
NEST_TAILSCALE_SERVE_DIR=../files/tailscale/serve
NEST_TAILSCALE_HOSTNAME=nest-controller
NEST_SLURM_HOST=hpc
```

In **Advanced → Mounts**, create File Mounts with these **File Paths** and
securely entered contents. For Compose these files back the YAML mounts; do not
add duplicate per-file container mounts over the directory/secret targets.

| File Path under Dokploy `files/` | Contents | Container use |
| --- | --- | --- |
| `nest-ssh/config` | Adapt `docker/ssh_config.tailscale.example` | Directory at `/run/nest-ssh` |
| `nest-ssh/id_ed25519` | Dedicated existing HPC private key | Same SSH directory |
| `nest-ssh/known_hosts` | Independently verified HPC host keys | Same SSH directory |
| `tailscale/authkey` | Non-ephemeral, tagged Auth Key | Compose Secret in sidecar only |
| `tailscale/serve/serve.json` | Reviewed JSON above | `/etc/tailscale/serve/serve.json` |

If the UI requires Mount Path, use the corresponding container file path;
verify the rendered Compose still has only the directory mounts and Auth Key
secret above. Dokploy v0.30.10's
[mount service](https://github.com/Dokploy/dokploy/blob/v0.30.10/packages/server/src/services/mount.ts)
stores Compose File Mounts in `<app>/files`, outside `<app>/code`. With the
Compose file at the repository root, `../files` resolves to that sibling
directory. A nested Compose path or custom project directory needs rechecking.
The [Dokploy Compose guide](https://docs.dokploy.com/docs/core/docker-compose)
documents this persistence pattern. Do not mount credentials from the checkout.

The host directories must be `0700`, private key and Auth Key `0600`, and SSH
config/known_hosts no more permissive than `0600`. Ensure the deployment daemon
can read them. The File Mount implementation does not explicitly apply these
modes; inspect metadata and correct it on the server after creation/updates.
The existing entrypoint copies SSH files to tmpfs with ownership `nest` and
directories `0700` / files `0600`, without modifying the source. Use the exact
filename `known_hosts`: rename the old `/etc/nest/.ssh/known-hosts` during secure
migration, and retain its verified contents. Preserve the private key's trailing
newline. File secrets use host permissions; Compose cannot reliably enforce
`uid`/`gid`/`mode` for file-backed secrets.

Enable [Isolated Deployments](https://docs.dokploy.com/docs/core/docker-compose/utilities).
Inspect the converted Compose before rollout: no `ports`, Domains, public
Traefik routes, privileged mode, TUN, or added capabilities. Both services must
share the project bridge; Docker DNS aliases must still resolve. Dokploy also
attaches an app network and may connect its Traefik container; isolation is not
a boundary against that trusted infrastructure or host administrators. Keep
unrelated workloads off the networks: SOCKS5 has no client authentication.
Keep the app/project name and volume naming/isolation settings unchanged so
`tailscale-state` reuses the existing identity. Record the actual volume name
before migration. Changing these settings can select a new empty volume.

## Start, migrate state, and verify

For a non-Dokploy deployment with prepared credentials:

```bash
docker compose -f compose.tailscale-shared.yaml config --quiet
docker compose -f compose.tailscale-shared.yaml up -d --build --wait
```

In Dokploy, use its deployment action and service terminals; do not run a second
`up` outside Dokploy. Fresh enrollment uses the tagged non-ephemeral Auth Key
and sets `--shields-up=false --ssh=false --accept-routes=false`.

**Existing state migration:** after auditing the policy, run this once in the
sidecar terminal, or from the deployed checkout with the correct project name:

```bash
docker compose -f compose.tailscale-shared.yaml -p YOUR_APP exec tailscale \
  tailscale set --shields-up=false --ssh=false --accept-routes=false
```

In this pinned image, `TS_AUTH_ONCE=true` reuses authenticated state, and
[`tailscaleSet`](https://github.com/tailscale/tailscale/blob/v1.104.4/cmd/containerboot/tailscaled.go)
does **not** reapply `TS_EXTRA_ARGS`. Merely changing the Compose environment
will leave the old shields-up preference active. The explicit `set` persists
these preferences without logging out or consuming a new Auth Key. Keep the
existing tag, and verify it. This does not enable Tailscale SSH.

Run the deployment check, which performs only `hostname && id -un` over SSH:

```bash
bash scripts/verify-tailnet.sh --shared -p YOUR_APP
```

It verifies authenticated tagged state, shields-up/SSH/route preferences, the
active Serve mapping and absence of Funnel, then the existing SOCKS5 + strict
host-key SSH path. Keep `StrictHostKeyChecking yes`, `BatchMode yes`, and the
verified `UserKnownHostsFile`; never use `accept-new` or `/dev/null` to fix errors.
No Slurm job is submitted.

Share **only the Controller node** with each authorized member in the Tailscale
admin console. Confirm the accepting account's identity and the source tag.
From an accepted recipient's user-owned device with Nest installed, run:

```bash
uv run --locked python scripts/verify-grpc.py nest-tag.tail8e5ba1.ts.net:50051
```

This sends an explicitly empty environment to the existing `Execute` RPC and
expects Nest's exact `INVALID_ARGUMENT` response. Validation occurs before
payload deserialization, executor selection, SSH, or Slurm; the probe never
submits work. A channel-ready check alone only proves a gRPC transport.

To check the backend independently inside Docker, pass that script via stdin:

```bash
docker compose -f compose.tailscale-shared.yaml -p YOUR_APP exec -T --user nest controller \
  python - controller:50051 < scripts/verify-grpc.py
```

For the required denial checks, use these controlled, non-job probes:

1. Run the same RPC probe from an unshared client, and from an accepted client
   excluded by an explicit email allowlist. Both must fail to reach the handler.
   Keep an authorized probe passing at the same time to rule out an outage.
2. From a shared client, check TCP 22, 1055, and 9002 on the shared node with
   `nc -vz -w 5 NODE_IP PORT`; all must fail. It must not reach other private nodes.
3. In the Controller terminal, run
   `nc -v -z -w 5 -X 5 -x tailscale:1055 OTHER_PRIVATE_IP 22` and repeat for a
   known listening service on an unauthorized node. Both must fail while the
   HPC SSH probe succeeds. Use known online targets so an offline host cannot
   masquerade as a policy denial. Do not use ordinary container ping for this.
4. In a disposable SSH directory/config copy, change `UserKnownHostsFile` to an
   empty file while retaining strict checking. The same SSH command must fail
   with host-key verification before authentication. Do not edit trusted files.

## Persistence and rollback

After successful enrollment, the bootstrap Auth Key can be revoked in Tailscale;
revoking it does not revoke the enrolled node. Preserve the state volume and
the secret's host file (it may retain the revoked value). Compose still requires
the file to exist. `TS_AUTH_ONCE=true` lets the existing node restart without
using that key. Lost state, logout, revoked/expired node identity, or a new
replica needs valid enrollment credentials again. See
[Auth keys](https://tailscale.com/kb/1085/auth-keys). Treat state and backups as
device credentials; never clone the volume to simultaneous instances.

Record the sidecar's `tailscale status --json` Self ID/IP, actual state-volume
name, SSH file metadata, and `tailscale serve status --json`. Recreate both
containers with the same project/volume and File Mounts through Dokploy (or
`up -d --force-recreate --wait` outside Dokploy). Repeat the checks above and
compare identity/IP/config. Repeat after revoking the bootstrap key. Serve is
reloaded from its persistent File Mount, and SSH is copied afresh from its
persistent directory. Do not use `down --volumes`.

To return to outbound-only mode with the same state, revoke the shares, then
run `tailscale serve reset` and `tailscale set --shields-up=true --ssh=false`
in the sidecar before selecting the old Compose file. Removing
`TS_SERVE_CONFIG` alone does not erase stored Serve config, and the old
`TS_AUTH_ONCE` deployment will not reset shields automatically.

## Verification record (2026-11-03)

The Docker build, Compose smoke test, and an offline Controller-to-Login-Node
SSH test have since been run; see
[Deployment validation and roadmap](deployment-architecture#validation-record-2026-11-12).

Based on PR #7 HEAD `a14c95f7459b7fa5df406bfda8fc4052efcb3246`. No credentials,
personal policy, HPC node, or Dokploy deployment were changed. No jobs were
submitted. There is no Docker executable/daemon or enrolled Tailnet in this
workspace; standalone Compose v2.39.4 was used only to render configuration.

| Requirement | Evidence here | Still required on deployment |
| --- | --- | --- |
| Build/start, services healthy | All three Compose files render; shared isolation assertions pass | Docker build, `up --wait`, runtime health |
| Tagged sidecar enrollment | Fixed-version startup source reviewed | Actual tag, device approval, authenticated state |
| SOCKS5 SSH to HPC | Existing offline OpenSSH/SOCKS5 regression passes | Real `hostname && id -un` |
| Strict host-key checking | OpenSSH template regression passes | Empty/wrong host-key negative check |
| Cross-Tailnet sharing | Eligibility and recipient restrictions documented | Tagged source share + real recipient TCP/RPC |
| No-job RPC | Probe tested against real local gRPC server in local and Slurm modes; no backend/unpickle/executor calls | Same probe over real Serve/share |
| Unauthorized client denied | No host ports or public proxy in rendered YAML | Unshared/excluded clients and other node ports denied |
| Other private nodes denied | Mergeable grant and negative policy-test examples | Full-policy audit, policy tests, live SOCKS5 denial |
| Recreate preserves setup | Persistent mounts and identical named state volume verified in rendered Compose | Identity/Serve/SSH comparison after recreate and key revocation |
| Existing regressions | All 11 unit/integration tests pass, including original 9 | Existing Docker Compose smoke test; not runnable here |

Offline checks (no HPC or Tailnet credentials):

```bash
python -m unittest discover -s tests -v
python tests/check_shared_compose.py
bash -n scripts/verify-tailnet.sh tests/smoke_compose.sh
```

CI validates the shared Compose invariants, builds the existing image, runs the
tests, and retains the original Compose smoke check. A configured CI workflow
is not evidence that a new run or live sharing test has passed.
