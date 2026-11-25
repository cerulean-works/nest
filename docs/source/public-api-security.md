# Public API security and abuse controls (H07)

This page describes the Controller-side controls that make the gRPC execution
service safe to put behind the public gateway of the
[H06 transport study](public-grpc-transport) (topology D: Traefik TCP SNI
passthrough to a dedicated Envoy, h2c to the Controller on a private network).
It changes no production DNS, route, firewall or Controller deployment, and it
enables no public pilot. The hosted Controller stays on the Tailnet.

Reaching the Controller is still not permission to run code on it. The
[security model](security) is unchanged: approved Projects run trusted,
deserialized Python as one HPC account. Untrusted execution stays blocked until
D02, D05, D06 and H05 land.

## Order of checks

Every unary RPC passes these steps in order. A call refused at any step stages no
file, opens no SSH connection and runs no Slurm command.

| Step | Where | Refused as |
| --- | --- | --- |
| 1. Connection admitted (`NEST_GRPC_MAX_CONCURRENT_RPCS`) | gRPC server | `RESOURCE_EXHAUSTED` |
| 2. Metadata and message size, after decompression (`NEST_GRPC_MAX_METADATA_BYTES`, `NEST_GRPC_MAX_MESSAGE_BYTES`) | gRPC server | `RESOURCE_EXHAUSTED` |
| 3. Origin: peer inside `NEST_GRPC_ALLOWED_PEERS`, when set | [`rpc_guard.RpcGuard`] | `PERMISSION_DENIED`, `nest-error-kind: origin_not_allowed` |
| 4. Client address rate limit (`NEST_GRPC_RATE_LIMIT_PER_PEER`) | [`rpc_guard.RpcGuard`] | `RESOURCE_EXHAUSTED`, `nest-error-kind: rate_limited`, `retry-after` |
| 5. Request message decoded (protobuf only) | [`rpc_guard.RpcGuard`] | `INVALID_ARGUMENT` (`Malformed request message`) |
| 6. Nest session authenticated | [`authz.ExecutionAccess`] | `UNAUTHENTICATED` |
| 7. Per-User rate limit (`NEST_GRPC_RATE_LIMIT_PER_USER`) | [`rpc_guard.RpcGuard.allow_user`] | `RESOURCE_EXHAUSTED`, `rate_limited` |
| 8. `run.submit` in the Project, Provider approval, Platform eligibility | [`authz.ExecutionAccess`] | `NOT_FOUND`, `PERMISSION_DENIED` |
| 9. Admission limits and reservation; session, membership, grant, account and Provider rechecked in the same transaction | [Admission limits](admission-limits) | `RESOURCE_EXHAUSTED`, `FAILED_PRECONDITION`, `PERMISSION_DENIED` |
| 10. Client still waiting (not cancelled, deadline not passed) before the environment build, staging and `sbatch` | [`controller.ExecutionService`] | `CANCELLED`, `nest-error-kind: client_gone`; the attempt is recorded `failed` and its reservation released |

Steps 1 to 5 run before the request message is decoded, and nothing before step 9
writes to the database except the audit log of a refusal. The Slurm path never
deserializes the payload in the Controller: the cloudpickle bytes are copied to
the worker, which unpickles them on the cluster.

### The Controller-side code path

With `NEST_EXECUTOR=local`, `Execute` unpickles the call inside the Controller,
next to its database and SSH credentials. With session authentication that path
is now off unless the listener is loopback (a developer's own machine) or the
operator sets `NEST_CONTROLLER_LOCAL_EXECUTION=true`. Refused calls get
`FAILED_PRECONDITION` and are never unpickled. Without authentication nothing
changes, since any caller could already run code. A public or staging pilot must
use `NEST_EXECUTOR=slurm`.

## Client address and proxy trust

The rate limit and the `client=` field of each `RPC started` log line use the TCP
peer address. `x-forwarded-for` is read only when the peer is one of
`NEST_GRPC_TRUSTED_PROXIES`, and then from the right: the first address that is
not itself a trusted proxy is the client. A single header is required, and an
unparsable entry falls back to the peer, so a client can never choose the address
it is limited by. With no trusted proxy configured, `x-forwarded-for` is ignored.

For topology D, set Envoy's address on the private network as the only trusted
proxy, and keep `use_remote_address: true` in `envoy.yaml` so Envoy sets the
header itself. Recording the address in the audit log is H08.

## Origin bypass protection

`NEST_GRPC_ALLOWED_PEERS` refuses every connection that does not come from the
listed networks, before authentication. Set it to the gateway's private network
(for example the `origin` Docker network's subnet) and keep the Tailnet path's
addresses in the list while it is in use. It complements, and does not replace,
the network-level controls: the Controller still publishes no port, and the
`origin_unreachable_*` probe checks must still pass.

## Bounds

| Bound | Default | Why |
| --- | --- | --- |
| Message size, both directions, after decompression | 4 MiB | gRPC's default, which the SDK also uses; a 5 KiB gzip request that inflates past the limit is refused unread. Raise it only with the SDK and the gateway together (H06 contract). |
| Worker result downloaded from the cluster | the message size, less 16 bytes of envelope | The login node is asked for at most one byte past the bound (`head -c`), the Controller reads no further, kills SSH, and answers `RESOURCE_EXHAUSTED` with `nest-error-kind: result_too_large`. The job completed, so its attempt is settled `failed` (`result_too_large`) and its reservation released; the result stays in the execution directory. Standard error of that download is kept to 64 KiB. |
| Request metadata | 16 KiB | A session token, Project and request ID are well under 1 KiB. |
| RPC worker threads | 4 | Unchanged from before H07. |
| RPCs running or queued | 32 | Excess calls are refused at once instead of queueing without limit; memory is bounded by this times the message size. |
| HTTP/2 PING without data | one per 20 s, 2 strikes | Explicit policy from the H06 contract: accepts the SDK's planned 30–60 s keepalive; the gateway still must not PING the Controller upstream. |
| Calls per minute per client address | 600 (burst 60) | Before authentication: unauthenticated callers cost a database lookup each. |
| Calls per minute per Nest User | 300 (burst 30) | After authentication, across all of a User's sessions. The SDK's status polling is well under it. |

Rate limits are per Controller replica and kept in memory: two replicas allow
twice the rate. At most 10,000 addresses or Users are tracked; the least recently
seen is forgotten first. Refused credentials are not charged to the User whose
token was guessed: a call is charged to a User only after its session is accepted.

## Credentials and request identity

- Session tokens travel only in `authorization` metadata, are hashed before any
  lookup, and never appear in a log line, an audit record or a status message
  (tested in `tests/test_public_api_security.py`).
- Each RPC has a request ID: the caller's `x-nest-request-id` when it is 32
  lowercase hexadecimal characters, otherwise a new one, so metadata cannot
  inject log lines. Every log line of the call carries it.
- Rate-limit and origin refusals log the scope and the client address only.
- A session that was only rotated by a refresh still counts as the same sign-in
  when the attempt is recorded; a revoked family does not.

## Deadlines and cancellation of `Execute`

`Execute` is still one blocking unary RPC. These limits from H06 remain and are
**not** fixed here:

- The Controller checks that the client is still waiting before each side effect
  (the environment build, staging the payload, `sbatch`). Once `sbatch` has run,
  a cancelled or expired call does not cancel the Slurm job: the worker thread
  keeps waiting until `NEST_RESULT_TIMEOUT`, and the job keeps its
  [reservation](admission-limits) until Slurm reports it finished.
- Abandoned calls still occupy worker threads. `NEST_GRPC_MAX_CONCURRENT_RPCS`
  bounds the queue behind them, and per-User admission limits bound how many one
  User can start, but the real fix is detached execution.
- **C03/C04 (detached execution) remain required** before any pilot behind
  Cloudflare's proxy or any proxy with a read timeout under 900 s, and before any
  pilot open beyond operator-approved trusted Projects.

## Configuration

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_GRPC_MAX_WORKERS` | `4` | RPC worker threads, 1 to 256. |
| `NEST_GRPC_MAX_CONCURRENT_RPCS` | `32` | RPCs running or queued at once; at least `NEST_GRPC_MAX_WORKERS`. More get `RESOURCE_EXHAUSTED`. |
| `NEST_GRPC_MAX_MESSAGE_BYTES` | `4194304` | Largest request or response message after decompression, 1024 to 67108864. |
| `NEST_GRPC_MAX_METADATA_BYTES` | `16384` | Largest request metadata, 1024 to 65536. |
| `NEST_GRPC_RATE_LIMIT_PER_PEER` | `600` | Calls per minute per client address; `0` turns it off. |
| `NEST_GRPC_RATE_LIMIT_PER_USER` | `300` | Calls per minute per authenticated User; `0` turns it off. |
| `NEST_GRPC_ALLOWED_PEERS` | Unset (all) | Comma-separated IP addresses or CIDR networks; connections from anywhere else are refused before authentication. |
| `NEST_GRPC_TRUSTED_PROXIES` | Unset (none) | Gateways whose `x-forwarded-for` names the client address. |
| `NEST_CONTROLLER_LOCAL_EXECUTION` | `true` on a loopback listener or without authentication, else `false` | Whether an authenticated Controller with `NEST_EXECUTOR=local` runs calls in its own process. Refused with `NEST_EXECUTOR=slurm`. |

## Gateway fixtures

The reproducible gateway is [`poc/public-grpc/`](https://github.com/cerulean-works/nest/tree/main/poc/public-grpc):
`envoy.yaml` (topology D) and `compose.yaml`, whose origin is on an
`internal: true` network with no published port. For a staging run against the
real Controller image, set on the Controller:

```bash
NEST_EXECUTOR=slurm
NEST_CONTROLLER_AUTH=session
NEST_CONTROLLER_ALLOW_PLAINTEXT=true            # h2c on the internal network only
NEST_GRPC_ALLOWED_PEERS=<origin network CIDR>   # plus the Tailnet range while it serves
NEST_GRPC_TRUSTED_PROXIES=<Envoy's address on that network>
NEST_LIMIT_BATCH_SCRIPTS=refuse                 # batch scripts cannot be quota-bounded
```

Safe defaults apply without any of these settings, and the existing Tailnet
deployment needs no change: its callers are within the default rate limits, and
`NEST_GRPC_ALLOWED_PEERS` is unset.

## Rollout and rollback

Rollout is a Controller upgrade: `nest-db upgrade` (applies
`0018_admission_reservations`; see [Admission limits](admission-limits)), then
redeploy. Nothing public changes until the H06 gateway is deployed, which stays
an operator decision outside this change.

Rollback, fastest first:

1. Loosen a limit: raise `NEST_GRPC_RATE_LIMIT_*` or set it to `0`, unset
   `NEST_GRPC_ALLOWED_PEERS`, and redeploy. No data changes.
2. Roll the Controller back. Nest has no down-migrations, and an older
   Controller refuses to start on a schema with a migration it lacks, so restore
   the backup taken before `nest-db upgrade` and deploy the previous image (see
   [Upgrade the schema](database)). Reservations recorded since the upgrade are
   lost with the restore; nothing on the cluster changes.
3. Remove a staging gateway as described in the
   [H06 rollback](public-grpc-transport#rollback). The Tailnet path stays.

## Evidence

**In CI** (`tests/test_public_api_security.py`, `tests/test_admission_limits.py`,
`tests/test_pilot_integration.py`, against real PostgreSQL and the fake login
node): oversized and compressed-oversized requests, oversized responses,
malformed messages, the origin allowlist, proxy header trust, per-address and
per-User rate limits, the concurrency bound, missing, malformed, invalid,
expired and revoked credentials, a wrong or missing Project, a missing scope,
legitimate session reuse and rotation, policy and admission-database outages,
the local-execution block, and that no refusal stages, deserializes or submits
anything. Status codes, details and trailers pass through the guard unchanged.

**Pending live checks (not claimed as passed).** These need staging or production
infrastructure and stay with H08 (staging/pilot) and H09 (release):

- [ ] The H06 matrix (`probe.py`) through the real gateway with the real
  Controller image, including `origin_unreachable_*` from outside, with
  `NEST_GRPC_ALLOWED_PEERS` set.
- [ ] Rate limits and `RESOURCE_EXHAUSTED` behavior measured through Envoy from an
  outside network, and `x-forwarded-for` from Envoy's `use_remote_address`.
- [ ] Load and saturation against a staging Controller (`ghz` or similar) with
  the default bounds, recording latency and memory.
- [ ] Carried forward from G03 (#79) and G02a (#84): concurrent revocation under
  load across two Controller replicas, a database outage during live traffic,
  replica consistency of approvals and reservations on the deployed database, the
  privileged write/CSRF negative matrix against the deployed sign-in origin, and
  verified PostgreSQL TLS (`NEST_DATABASE_SSLMODE=verify-full`) on the deployed
  database. CI covers each of these against a local PostgreSQL only.
- [ ] Gateway logs grepped for a real staging session token after a run (must be 0).
