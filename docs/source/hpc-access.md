# Authorize execution authority and HPC account bindings

A registry-backed Controller (`NEST_EXECUTION_REGISTRY=required`) decides on the
server, for every submission, **whose HPC identity runs it**, before it selects a
login node, opens any SSH connection or stages anything. This page covers that
decision (D01) and the per-User HPC account bindings it relies on (D02).

> **Status.** The policy and binding records are enforced and audited. The
> user-owned SSH session itself (the relay and local OpenSSH, D03 to D05) does not
> exist yet. A `user_owned` submission that passes every check is therefore still
> refused with `user_owned_transport_unavailable`: no Mode B job runs, and none
> ever runs with the Controller's credentials.

## Execution authority

| Authority | Design name | Whose HPC identity | Status |
| --- | --- | --- | --- |
| `controller_owned` | Mode A, Controller SSH | The Controller's operator-configured UNIX user (`SSH_USER`). Trusted deployments only. | Current |
| `user_owned` | Mode B, User SSH Proxy | The submitting User's own HPC account, through a session the User owns | Policy and bindings only; no transport yet |

Execution authority is a separate dimension from the **transport modes** `A` and
`B` of a login node and of `NEST_SLURM_SSH_MODE` (see
[Register HPC Platforms and login nodes](platforms)). Their meaning is unchanged:
transport `B` is still the Controller's own managed-key SSH and is **not**
reinterpreted as a User's session. A registry-backed Controller still refuses to
submit over legacy transport `B` (`hpc_binding_unavailable` or
`mode_b_transport_unavailable`); routing it is D09's versioned migration.

### Three layers, all must permit

An authority is permitted only when all three layers permit it:

1. **Deployment:** `NEST_EXECUTION_AUTHORITIES` on the Controller, default
   `controller_owned`. Only the Execution Registry resolves authority, so the
   legacy path accepts only the default.
2. **Platform policy**, operator managed and versioned.
3. **Project policy** on that Platform, operator managed and versioned. It does
   not replace the Platform grant, which every submission still needs.

A Platform or Project without a policy is at version `0`, which permits only
`controller_owned`. Existing deployments therefore keep running Mode A unchanged,
and `user_owned` needs an explicit operator opt-in on both the Platform and the
Project. Only an operator changes these policies, with `nest-db`: no API changes
them, and the database refuses a change that names no operator, including one by a
Project Owner or a `platform_admin`.

### How a submission is decided

A submission may ask for an authority with the gRPC request metadata
`x-nest-execution-authority: controller_owned | user_owned`. **No Nest client
sends it yet**: the SDK and `nest` CLI send no authority, so their submissions
always take the unrequested path below. The header is only a request:

- An authority that is asked for must be permitted; otherwise the submission is
  refused with `authority_not_permitted` (`PERMISSION_DENIED`), and **no other
  authority is tried**.
- When none is asked for, `user_owned` is chosen whenever it is permitted, and
  `controller_owned` otherwise. A Project that may run under its Users' own
  identities never silently runs under the Controller's.
- No permitted authority at all: `no_authority_permitted`.
- `user_owned` needs a human Nest User (browser or CLI session) with a
  **verified** binding on the Platform: otherwise `hpc_binding_missing`,
  `hpc_binding_unverified`, or `hpc_binding_not_allowed` for a non-human caller.
  Service principals (B09) never have a user-owned binding.
- A verified `user_owned` submission is refused with
  `user_owned_transport_unavailable` until the user-owned transport ships.

Project membership and the Platform grant are checked first, so an outsider learns
nothing about policies or bindings. Every refusal is recorded in the security audit
log as `execution.authority` and `execution.admit` with its reason, and nothing
reaches the cluster.

**There is no Mode B → Mode A fallback.** A `user_owned` submission that fails for
any reason (policy, binding, session, transport) is refused, even when the
Controller has working Mode A credentials configured.

### What every attempt pins

Every attempt the registry records from migration `0019` on pins, and never
changes:

| Column | Meaning |
| --- | --- |
| `execution_authority` | `controller_owned` or `user_owned` |
| `platform_authority_policy_version`, `project_authority_policy_version` | The policy versions in force when it was admitted (`0` for no policy) |
| `hpc_binding_id`, `hpc_binding_version`, `unix_user`, `unix_uid` | For `user_owned`, the verified binding version it runs as; for `controller_owned`, no binding and the Controller's UNIX user |

The transaction that records the attempt re-checks the policies under the
Platform's authority lock and share-locks the binding, so a policy change or a
revocation either commits first and refuses the attempt (`authority_policy_changed`,
`hpc_binding_revoked`), or waits until the attempt is recorded. The database
enforces the same rules for every writer: an attempt without an authority, with
stale policy versions, with an authority the policies do not permit, or a
`user_owned` attempt whose binding is not its owner's current verified binding on
the attempt's Platform, is refused.

Later policy changes, binding replacements and revocations affect **future**
submissions only. Historical attempts keep their authority, policy versions and
original HPC principal. Attempts recorded before `0019` keep `NULL` for the
authority columns: they are not reinterpreted.

## HPC account bindings

A binding maps one Nest User to one UNIX account on one Platform. It is keyed by
the immutable `platform_id`, never by a login node or host name, so adding,
removing, disabling or failing over login nodes changes no binding.

A binding stores only non-secret facts: the UNIX user name, optionally the UNIX
user ID, its version and its verification and revocation state. It never stores a
password, OTP, MFA secret, SSH key or SSH session, and the tables have no column
that could hold one. The evidence reference is a short identifier without spaces,
such as a ticket or attestation ID.

| Status | Meaning |
| --- | --- |
| `claimed` | The User typed a UNIX user name (onboarding). **Not evidence** of ownership; never authorizes a job. |
| `verified` | An operator recorded an independent attestation (`nchc_attestation` or `operator_attestation`) with an evidence reference. |
| `replaced` | A later version replaced it; it names the replacement. Final. |
| `revoked` | Revoked, with a reason. Final. |

Rules, enforced by the database:

- At most one current (`claimed` or `verified`) binding per User and Platform.
- A UNIX account is verified for at most one Nest User per Platform: a shared UNIX
  account does not isolate its Users. The same name on another Platform is another
  account.
- Only an operator can verify a binding, or change or revoke a verified one. A
  User can only record and withdraw their own claim.
- A recorded verification is never edited. Replacing a binding creates the next
  version and marks the old one `replaced`. No binding row is ever deleted.
- A typed username, a successful SSH relay and a client-reported `whoami` are not
  verification.

Every claim, verification, replacement and revocation is recorded in the security
audit log (`hpc_binding.*`), with the operator and reason.

> **Warning: enabling `user_owned` stops a Project's submissions today.** Once
> the deployment, the Platform and a Project all permit `user_owned`, every
> submission from that Project that asks for no authority (every SDK and CLI
> submission) resolves to `user_owned` and is refused (`hpc_binding_missing`,
> `hpc_binding_unverified` or `user_owned_transport_unavailable`), even if
> `controller_owned` is permitted too. It never runs as the Controller, and no
> shipped client can opt back into `controller_owned`. Until the user-owned
> transport ships, enable `user_owned` only to stage and test policies and
> bindings, on a Project that should not run Mode A jobs.

## Operate with `nest-db`

```bash
# Permit user_owned on the Platform and one Project. See the warning above: with
# NEST_EXECUTION_AUTHORITIES also permitting it, that Project's SDK and CLI
# submissions are refused until the user-owned transport ships.
nest-db hpc-access set-platform-policy nano4 --authorities controller_owned,user_owned \
    --operator alice --reason "NCHC approved user-owned SSH, ticket 42"
nest-db hpc-access set-project-policy nano4 --project-id PROJECT_ID \
    --authorities controller_owned,user_owned --operator alice --reason "pilot cohort"
nest-db hpc-access show nano4 --project-id PROJECT_ID

# Record an independent attestation as a User's verified binding.
nest-db hpc-access attest nano4 --user-id USER_ID --unix-user u1234567 --unix-uid 40123 \
    --method nchc_attestation --evidence-ref NCHC-ATTEST-881 \
    --operator alice --reason "NCHC account confirmation"
nest-db hpc-access bindings nano4 --user-id USER_ID

# Revoke it: future submissions are refused, past attempts keep it.
nest-db hpc-access revoke BINDING_ID --revoked-reason left_project \
    --operator alice --reason "offboarded"
```

`--authorities none` sets a policy that permits nothing. Policies are append-only:
each change is the next version.

## Separate identities

| Identity | Where | Not to be confused with |
| --- | --- | --- |
| Nest User | `users` | A UNIX user, a Slurm account or a service principal |
| HPC UNIX account | `hpc_account_bindings` (`user_id`, `platform_id`, `unix_user`) | A Slurm account: a binding grants no Slurm account and no charge permission |
| Slurm account | `slurm_accounts` (see [Map Slurm accounts to Projects](slurm-accounts)) | A UNIX identity |
| Controller identity | The Platform Profile's `SSH_USER` | Any User's binding; it is never used for `user_owned` |
| Service principal | B09 | A human User; it has no user-owned binding |

## Rollback

Migration `0019_hpc_access_policy` is additive. It adds three tables and three
nullable attempt columns and backfills nothing. As with every Nest migration
there is no down-migration: to roll back, restore the backup taken before
`nest-db upgrade` and deploy the previous release (see
[Database migrations](database#migrations)). A previous release's Controller
left running against an upgraded database fails closed: the database refuses
its attempts, because they pin no authority, and nothing is submitted. Upgrade
and restart together.
