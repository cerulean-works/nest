# Public SSH egress feasibility and relay contract (D03)

This page is the D03 no-job feasibility package (roadmap wave W0). It covers two
things:

1. **Egress feasibility.** Can the Controller open a direct public-Internet TCP
   connection to an HPC Platform's login node (normally port 22), and has the
   Platform operator approved that? This is checked without logging in and
   without submitting a Slurm job.
2. **The relay contract.** The boundaries a future restricted, authenticated
   gRPC/TLS relay must keep when it carries a user's opaque SSH bytes between
   local OpenSSH and that login node.

The egress in question is **outbound** from the Controller to the login node. It
is not Mode A's Tailnet route to the HPC system, and it is not the public
*inbound* SDK ingress of H06–H09. Being able to reach the Controller's public
gRPC endpoint says nothing about whether the Controller may reach a login node.

Nothing here is a production relay. No Controller RPC, setting or route is
added or turned on, and no real host is contacted by tests or CI. The full D03
relay is dispatched only after D01 (server-enforced mode selection) and D02
(per-user HPC account binding) have landed, B07 and D07 are in place, and the
operator feasibility evidence below has been reviewed.

The code is in [`poc/ssh-egress/`](https://github.com/cerulean-works/nest/tree/main/poc/ssh-egress).
Its fixture tests are in `tests/test_ssh_egress_poc.py`.

## Naming: this is not today's `NEST_SLURM_SSH_MODE=B`

In the current code, `B` (in `NEST_SLURM_SSH_MODE` and in a login node's
`transport_modes`) means **Controller-managed key-based SSH**: the Controller
holds a key and runs Slurm commands itself. The design doc's "Mode B", the
*user-owned* SSH path through a relay, is a different thing. This page uses the
design's independent dimensions and keeps the existing names unchanged:

| Dimension | Value for the D03 relay |
| --- | --- |
| `execution_authority` | `user_owned`: the user's own NCHC session runs every command |
| `network_transport` | `direct_public_tcp`: Controller egress to the public login node, never the Tailnet |
| `principal_type` | `human` (CI uses Controller-owned authority, D09/D10) |

Existing `B` deployments are not reinterpreted. A versioned migration (D09)
introduces the new fields.

## Evidence classes

Every claim about D03 is one of three kinds, and they are reported separately.

| Class | Meaning | What this package delivers |
| --- | --- | --- |
| **Implementation** | Code and documents exist and are reviewed | The probe, the reference relay model, the draft `relay.proto`, this page |
| **Synthetic** | Checks passed against local, disposable fixtures | The fixture tests below; the probe with `--fixture` (all output marked `"evidence": "synthetic"`) |
| **Live** | An authorized operator ran a check against the real Platform from the Controller host | **None.** The steps are listed under [Operator smoke steps](#operator-smoke-steps-no-job) and nothing there is claimed as passed |

Live evidence, once collected, is attached to the D03 Issue with the approval
record below. A green CI run is synthetic evidence only.

## What must be approved before any live check

Record these for each Platform (for Nano4: with NCHC) before anyone runs the
live smoke. Keep the completed record with the D03 Issue, not in this
repository.

| Item | What to record | Why |
| --- | --- | --- |
| Operator approval | Who at the Platform operator approved, when, the reference (ticket or email ID) and its scope: reachability smoke only, or an application-level relay | An approved TCP check is not approval to relay users' SSH sessions. Each needs its own answer |
| Relay permission | Whether the operator permits a third party to carry users' SSH sessions from one source address, and any terms (user cap, notice, logging) | Many sessions will appear from one Controller address. The operator must accept that |
| Source IP policy | The Controller's public egress address(es) (IPv4 and IPv6), whether they are static, and whether the operator allowlists them | The login node may rate-limit or block by source. A shared NAT or changing cloud address must be known in advance |
| Expected endpoint | Login-node hostnames, port (normally 22), and the public addresses they are expected to resolve to | It becomes the server-side allowlist and the probe's `--approved-address` |
| Host-key trust source | The SHA256 fingerprints and how they were obtained independently: the operator's published page, signed email or a support ticket. Not from the network path being tested | OpenSSH's strict checking against this pin is the only thing that proves it reached the real node (see [Host-key trust](#host-key-trust)) |
| Authentication | The methods the login node offers (password, keyboard-interactive, MFA) | D04 must not assume public-key login |
| Abuse contacts | Who to contact if the operator sees unusual traffic, and how Nest pauses the relay | Needed before any user traffic |

Without the operator approval and the source IP item, do not run even the
reachability smoke against a real host.

## Operator smoke steps (no job)

These are for an authorized operator, run **on the Controller host** (so the
check uses the Controller's real egress path and source address), after the
approval above. They read only public information. They never log in, never
send a username or password, never run a command on the login node and never
submit a Slurm job. They change no firewall, route, DNS or Controller setting.

Before you start, confirm Mode A's Tailnet is not the route. If the Controller
uses Tailscale, check that the login node's addresses are not routed through
`tailscale0`:

```bash
ip route get <login-node-public-ip>      # must not say "dev tailscale0"
```

**1. Rehearse against the local fixture** (synthetic; needs only Python):

```bash
cd poc/ssh-egress
python3 fake_sshd.py --port 2222 &
python3 egress_probe.py --host 127.0.0.1 --port 2222 --allowed-port 2222 --fixture
kill %1
```

**2. Reachability and SSH identification** (live):

```bash
python3 egress_probe.py --host ln01.nano4.example \
  --approved-address <approved-ip> [--approved-address <approved-ipv6>]
```

The probe resolves the name once and fails unless every address is public, not
on the Tailnet (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), and in the approved set.
It connects to one validated address on port 22, reads the server's `SSH-2.0-...`
identification line and closes. It sends **zero bytes**: no client banner, so no
key exchange and no authentication. Its `local_address` is the host's interface
address. Your public egress address still has to be confirmed against the source
IP item, from your provider's console, not from a third-party "what is my IP"
service.

**3. Host-key observation, compared with the independent pin** (live):

```bash
ssh-keyscan -p 22 -t ed25519,ecdsa,rsa ln01.nano4.example > keyscan.txt
python3 egress_probe.py --host ln01.nano4.example --approved-address <approved-ip> \
  --pin SHA256:<fingerprint-from-the-operator> --keyscan-file keyscan.txt
```

`ssh-keyscan` performs only key exchange and never authenticates. The probe
passes `host_key` only if an observed key matches a `--pin` you got from the
operator. A key observed from the same path is never trusted on its own, and the
probe fails if no `--pin` is given.

**4. Attach the output.** Save the JSON lines from steps 2 and 3 with the
approval record. The summary line always has `slurm_jobs_submitted: 0` and
`logins_attempted: 0`, and every record has `"evidence": "live"`. Then delete
`keyscan.txt`.

Not part of this package, and not to be done without separate approval: an
interactive `ssh` login (even to check `whoami`), any Slurm command, a relay
carrying real user sessions, or opening Controller firewall rules.

## Host-key trust

- The trust anchor is the operator-published SHA256 fingerprint, obtained out of
  band, stored as the login node's `host_key_pin_ref` (D07) and given to the
  user's OpenSSH (D04) through a separate, authenticated channel.
- The relay never supplies, learns or vouches for a host key. A key the relay or
  the probe observes is an observation over the same path that might be attacked.
- OpenSSH runs with strict host-key checking against an application-owned known
  hosts file. Never use `StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`
  or trust on first use. Unknown or mismatched keys fail closed.
- Rotation (announcing a new key ahead of time and overlapping old and new pins)
  is an [open decision](#unresolved-decisions).

## Relay contract

The draft service is `poc/ssh-egress/relay.proto` (`nest.relay.v1alpha.SshRelay`,
not compiled and not served). `poc/ssh-egress/relay_contract.py` is a synthetic
reference model of how the server makes its decisions and moves bytes. The
fixture tests run it against `fake_sshd.py`. A production relay would be new code
in the Controller that is held to the same tests.

### Shape

One bidirectional streaming call per OpenSSH connection:
`rpc Open(stream ClientFrame) returns (stream ServerFrame)`. The local helper
(OpenSSH's `ProxyCommand`, D04) sends an `OpenRelay` first, then `data` frames,
and `half_close` when OpenSSH closes its write side. The server replies with
`opened`, then `data`, and `half_close` on the login node's FIN. Errors end the
call with a gRPC status and a stable `nest-relay-reason` trailer. The helper's
stdout carries SSH bytes only, and diagnostics go to stderr.

### Boundaries

| Boundary | Contract | Synthetic test |
| --- | --- | --- |
| No generic forwarding | `OpenRelay` has `platform_id`, `project_id` and an optional `attempt_id`, and nothing else. There is no host, port, address or URL field, and unknown fields are rejected | `test_client_cannot_name_a_destination`, `DraftProtoTests` |
| Server-side allowlist | The login node comes from the Platform's active, approved login nodes (D07), which must have a host-key pin. Port 22 only unless the operator approved another | `test_destination_policy` |
| Authorization first | The Nest session (call metadata over TLS, never a frame or process argument) is authenticated. D01 mode policy and B07 RBAC for the actor, Project and Platform are checked **before** any DNS lookup or socket | `test_denial_happens_before_dns_and_never_falls_back` |
| No fallback | A denial, expired session or unreachable node ends the call. Nothing retries through Mode A, the Controller's credential or another Platform | same |
| DNS and IP validation | Resolve once. Every answer must be globally routable unicast, not loopback, private, link-local, metadata, multicast or Tailnet, and must be in the operator-approved set when one is recorded. A mixed answer is rejected whole | `AddressPolicyTests`, `test_destination_policy` |
| Rebinding defense | Connect to the validated address literal. Never hand the name to `connect()` and never resolve again within a stream | `test_rebinding_defense_connects_to_the_validated_literal` |
| Egress restriction | Network policy limits the relay's egress to the approved addresses on port 22, as defense in depth below the application check, and keeps it off the Tailnet. Mode B works with Tailscale disabled | operator-run (live) |
| Opaque bytes | No parsing, decryption or logging of the bytes. The Controller never has a password, OTP, private key or session key | `test_audit_never_contains_payload` |
| Bounded concurrency | Global, per-user and per-(user, Platform) stream caps, plus a per-user open rate. Slots are released on every exit path | `test_concurrency_limit_and_release`, `test_open_rate_limit` |
| Bounded size and time | Frame size cap, total-bytes cap, connect timeout, idle timeout and maximum duration | `test_size_limits`, `test_idle_and_total_timeouts` |
| Backpressure | Each direction has at most one frame in flight. A slow side stops reads on the other, so memory per stream is bounded at about twice the frame size | `test_backpressure_bounds_in_flight_data` |
| EOF and half-close | Client `half_close` becomes TCP FIN, and the login node's FIN becomes server `half_close`. The call ends when both directions have closed | `test_opaque_bytes_round_trip_with_half_close` |
| Cancel and failure | Client cancellation closes the socket, releases the slot and records `cancelled`. Connect failures and resets end the call with `connect_failed` or `upstream_error` | `test_cancellation_closes_socket_and_releases`, `test_connect_failure`, `test_upstream_reset_is_reported` |
| Audit | `relay.connected` and `relay.closed` events, plus every denial. Each records the actor, Project, Platform, attempt, node, address, port, outcome, byte counts and duration, with authority `user_owned` and transport `direct_public_tcp`. Never payload | `test_audit_never_contains_payload` |

The reference limits in `RelayLimits` (64 streams in total, 4 per user, 2 per user
and Platform, 10 opens a minute, 32 KiB frames, 1 GiB, 10 s connect, 15 min idle,
8 h total) are placeholders for the tests. They are not proposed production
values.

### What a working relay does not prove

A TCP connection, an SSH banner or a successful relayed login proves **nothing**
about who the user is on the HPC system. The relay carries ciphertext and cannot
see which UNIX account authenticated. HPC identity comes from D02's
independently verified binding and the operator's own accounting. Nest never
infers it from reachability, the source address or a username the user typed.

## Unresolved decisions

These must be settled (and recorded in the design doc) before full D03 dispatch:

- **Operator acceptance.** Whether NCHC permits an application-level relay at all,
  under what terms, and whether it wants per-user attribution beyond the shared
  source address. The fallback, if the answer is no, is an approved NCHC-native
  adapter (H01), not a workaround.
- **Egress address.** A static egress address for the Controller, the IPv4/IPv6
  policy, and what happens when the provider changes it.
- **Login-node choice and failover.** Pick the first healthy node, or fail over
  within a stream's connect budget. Relay health checks must not become
  unapproved probing.
- **Gateway timeouts.** Relay streams last hours. H06 measured the gateway
  defaults that cut long streams (Envoy's route timeout, Traefik's read timeout,
  HTTP/2 PING policy). The route for `SshRelay` needs its own timeouts and
  keepalives.
- **Limits.** Production values for concurrency, rate, bytes and durations, and
  whether they live in F02a admission policy.
- **Frame protocol details.** The maximum frame size, whether to coalesce small
  writes, and how `nest-relay-reason` maps onto gRPC status codes for the D04
  helper.
- **Host-key distribution and rotation.** How pins reach the client (the
  authenticated Platform API or a signed bundle) and how rotation overlaps old
  and new pins.
- **Session revocation.** Whether revoking a Nest session or Project grant ends
  open streams immediately, or only blocks new ones.
- **Audit retention.** How long relay records are kept, and whether the source
  port is recorded so that the operator's logs can be matched to Nest's.

## Prerequisites for full D03

- D01 server-enforced mode selection and D02 per-user, per-Platform binding have
  merged. B07 audit and D07 login-node registry are in place.
- The approval record and live smoke output above have been reviewed by the
  maintainers.
- The decisions above are recorded.

Until then, keep the Controller unchanged: it has no relay RPC, and the PoC
stays in `poc/`.
