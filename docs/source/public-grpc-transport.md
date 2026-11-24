# Public gRPC transport feasibility (H06)

This page records the H06 feasibility study for a public, TLS-protected gRPC
endpoint on port 443. It is a staging study. It changes no production DNS
records, routes, firewall rules or Controller settings, and it enables no public
pilot. The hosted Controller stays private on the Tailnet
([Share a Controller over Tailscale](tailscale-shared)), and the existing HTTPS
sign-in at `auth.nest.cerulean.works` is unchanged.

Being able to reach the Controller is not permission to run code on it. The
[security model](security) still applies: approved Projects run trusted,
deserialized Python as one HPC account. A public endpoint changes who can *try*
to connect, not who is allowed to submit. Every call still needs a Nest session
and a Project grant. H07, F02a, H08 and H09 remain separate tickets.

The reproducible PoC is in [`poc/public-grpc/`](https://github.com/cerulean-works/nest/tree/main/poc/public-grpc).

## Summary

- **Recommendation: a dedicated Envoy gRPC gateway (topology D).** It runs on the
  Dokploy host next to the Controller. The shared Traefik passes its SNI name
  through on :443 by TLS passthrough. Cloudflare serves DNS only (grey cloud).
  Envoy holds the TLS certificate and reaches the Controller over h2c on a
  private Docker network. The Controller publishes no port. Only the
  `nest.ExecutionService` paths are routed.
- **Measured.** Envoy, Traefik and Traefik passthrough to Envoy all passed every
  local check. That covers TLS, SNI, ALPN `h2`, metadata and trailers, status
  codes, bidi and server streaming, deadline and cancellation propagation, the
  4 MiB limit, and silent unary calls up to 650 s. Stock gateway defaults do not
  work:
  - Envoy's default route timeout cuts every call at 15 s.
  - Traefik's stock 60 s read timeout resets bidi streams.
  - Upstream HTTP/2 PINGs from the gateway make the grpcio origin close silent
    calls with `too_many_pings`.

  The [results](#measured-results) give each run's numbers.
- **Today's blocking `Execute` can run behind topologies C or D, but not behind
  Cloudflare's proxy.** The SDK waits up to 900 s for an `Execute` reply, and no
  byte flows in the meantime. On Free, Pro and Business plans Cloudflare's proxy
  read timeout is about 100–125 s and cannot be changed. The blocking RPC also
  ignores a client that goes away: the Controller thread and the Slurm job keep
  running.
  **Decision:** detached execution (C03/C04) must come before a
  Cloudflare-proxied or otherwise middlebox-heavy pilot. It is **not** required
  for a trusted-only staging pilot through the recommended gateway, which has the
  [limits listed below](#execute-against-proxy-deadlines).
- **Pending live checks.** Nothing in this study touched a real public host,
  domain, certificate authority or Cloudflare zone. The live checks are listed
  [below](#pending-live-checks) as operator commands, and none of them is claimed
  as passed.

## Topologies compared

The provider facts come from the vendors' documentation as of 2026-11-24 and are
cited in [Sources](#sources). The fetch tool could not open
developers.cloudflare.com, doc.traefik.io or tailscale.com from this environment,
so those facts come from search excerpts of those pages. Each one is marked for
the operator to confirm on the page before rollout.

| | A. Cloudflare proxied (orange cloud) | B. Cloudflare Tunnel | C. Dokploy Traefik terminates TLS | **D. Dedicated Envoy (recommended)** | E. Tailscale Funnel |
| --- | --- | --- | --- | --- | --- |
| gRPC to a public hostname | Yes, on all plans, with the zone's **Network → gRPC** toggle (confirm) | **No.** Tunnel supports gRPC only through private-network routing with WARP clients; public hostnames are "not currently supported" (confirm) | Yes, router to an `h2c://` server (measured) | Yes (measured) | Possible with `--tcp` passthrough to our own TLS; HTTP/2 in `--https` mode not confirmed |
| Plan or cost | Free $0, Pro about $20/mo, Business about $200/mo per zone (third-party listings, confirm); gRPC itself costs nothing extra | Free | No extra cost; already deployed | No extra cost; one more container | Included, but [not for production](#e-tailscale-funnel) |
| Origin TLS | The origin must serve TLS with ALPN `h2` on **443**; the current h2c Controller cannot be the origin directly | cloudflared's `http2Origin` needs an HTTPS origin | Terminates at Traefik; h2c on the Docker network | Terminates at Envoy; h2c on a private network | Ours (passthrough) |
| Certificate renewal | Edge certificate automatic. The origin needs one too: a Cloudflare Origin CA certificate (7 days to 15 years) or Let's Encrypt | Automatic | Automatic Let's Encrypt HTTP-01 by Dokploy | Envoy has no ACME client: lego or certbot with DNS-01 writes files, and Envoy reloads them through SDS. A cron job or timer to run and monitor | Automatic |
| Stopping clients bypassing the gateway | Allowlist Cloudflare's IP ranges on 443, and/or use Authenticated Origin Pulls (mTLS); per-plan availability not confirmed | Inherent (outbound only) | The Controller is only on the internal network; Traefik is the only listener | The Controller is only on the internal network; Envoy is the only route to it (measured: direct origin connections refused) | Inherent |
| Long silent unary (`Execute`) | **About 100–125 s** proxy read timeout; only Enterprise can raise it (to 6000 s) | n/a | Works (stock: 200 s; tuned: 1300 s); streams need the **entry-point-wide** `readTimeout: 0` | Works with **per-route** tuning, capped by the client's own `grpc-timeout` | Unpublished limits |
| Edge protection | DDoS protection and WAF (headers only); **Cloudflare Access ignores gRPC** | n/a | None beyond Traefik | Path allowlist, connection limits, optional rate limit | None |
| Operational complexity | Medium: zone settings, a TLS listener at the origin, an IP allowlist | n/a | Low to set up, but it changes timeouts for every app on :443 | Medium: one config file, certificate automation, one SNI passthrough route | Low, but no SLA |

### Why D over C

Both C and D passed the same matrix ([Measured results](#measured-results)).
They differ in where the timeout changes land.

Stock Traefik already carries today's silent unary `Execute`: 200 s passed
with the stock entry point. Its 60 s `readTimeout` resets any stream that is
still sending at 60 s, though, which rules out C03/C04's streaming or long
client-streaming RPCs. For those, topology C has to change Traefik's `websecure`
entry point:

- `readTimeout` goes from `60s` to `0`.
- `idleTimeout` goes up as well.

On Dokploy's shared Traefik, these settings apply to the sign-in site and to every
other application on :443. Removing the read timeout also removes Traefik's main
defense against slow clients on all of them.

Topology D keeps those settings scoped to the Nest gRPC routes. Envoy also honors
the client's `grpc-timeout` (`grpc_timeout_header_max`), allowlists RPC paths,
and keeps the gRPC listener out of the Traefik setup that serves sign-in.

The shared Traefik only has to pass one SNI name through, using a TCP router with
`tls.passthrough`. The [passthrough measurements](#measured-results) show that
stock Traefik timeouts then do not affect the stream.

If D's certificate automation is not wanted, C is the documented alternative.
Use a separate entry point (its own port, or its own IP on :443) for gRPC, with
the timeouts above, instead of changing `websecure`.

### Why not A yet

Cloudflare proxied mode adds DDoS protection and hides the origin. Its proxy
read timeout, however, ends any response that sends no bytes for about 100–125 s.
On Free, Pro and Business plans that timeout cannot be changed.

HTTP/2 PING keepalives do not help. They apply to a single hop, so they never
reset the timer between Cloudflare and the origin.

A also needs its own TLS listener at the origin on 443, plus a Cloudflare IP
allowlist or Authenticated Origin Pulls. Cloudflare Access does not protect gRPC
traffic, so Nest's own session authentication remains the only access control.

Revisit A once execution is detached (C03/C04). Then every RPC is short or
streams messages more often than the read timeout.

### E. Tailscale Funnel

Funnel serves only on ports 443, 8443 and 10000. It relays traffic through
Tailscale's servers under bandwidth limits that are not published and cannot be
changed, and it has no SLA. It is fine for a demonstration, but it is not a
public pilot transport.

## Recommended topology (D)

```text
client (SDK, NEST_CONTROLLER_TLS=true)
   │  TLS 1.2+/1.3, SNI grpc.<staging domain>, ALPN h2, :443
   ▼
Dokploy host :443 ── Traefik TCP router HostSNI(grpc.<staging domain>), tls.passthrough
   │                 (every other name: unchanged HTTPS routers, including auth.*)
   ▼
Envoy :8443  (TLS termination, path allowlist, gRPC timeouts, no header logging)
   │  h2c on the internal Docker network `origin` (no published port)
   ▼
Controller :50051  (NEST_CONTROLLER_AUTH=session; unchanged)
   └── also stays on the Tailnet through Tailscale Serve, so existing users are unaffected
```

The files are in `poc/public-grpc/`:

| File | Purpose |
| --- | --- |
| `envoy.yaml` | Gateway configuration (topology D). |
| `traefik/static.yml`, `traefik/dynamic.yml` | Topology C, for comparison and as the alternative. |
| `compose.yaml` | Staging stack. The origin is on an `internal: true` network, the gateway publishes `127.0.0.1:8443` unless `NEST_POC_PUBLISH` says otherwise, and the profiles are `envoy` and `traefik`. |
| `origin.py` | Synthetic origin: the real `ExecutionService` handler with synthetic token authentication and no Slurm backend, plus the `nest.poc.TransportProbe` echo service. It never unpickles, executes or submits anything. |
| `probe.py` | The test matrix. It prints JSON, never prints the token or a payload, and exits 1 on any failure. |
| `make_certs.py` | A throwaway 7-day staging CA and certificate for `grpc.staging.nest.invalid`. |

`tests/test_public_grpc_poc.py` checks the harness itself in CI:

- The matrix passes against a TLS origin.
- The token never appears in the output.
- Wrong tokens are refused.
- A reachable origin fails the bypass check.
- The synthetic access check grants nothing beyond authentication.

### Prerequisites for a live staging run

- A staging hostname, for example `grpc.staging.<domain>`, set to **DNS only** in
  Cloudflare. Use a new staging record and leave production records unchanged.
- A staging host where :443 can be used. This is either a disposable VM, or the
  Dokploy host with one added Traefik TCP passthrough router.
- A certificate for that name from Let's Encrypt **staging** (DNS-01 through lego
  with a Cloudflare API token scoped to `Zone:DNS:Edit` for the staging zone
  only), stored outside Git.
- A random synthetic token in `poc/public-grpc/.secrets/token` (ignored by Git).
  Never use a real Nest session.
- The `nest-controller:local` image (`docker build .`).

## Test matrix

Each check runs through the gateway with `probe.py`:

| Check | What passes | Issue criterion |
| --- | --- | --- |
| `tls_certificate_sni_alpn_h2` | The chain verifies for the SNI name, ALPN selects `h2`, and TLS is 1.2 or later. Records the issuer and days left | TLS, SNI, ALPN h2 |
| `tls_wrong_name_refused` | A client verifying another name refuses the certificate | SNI |
| `tls_http1_only_not_h2` | A client offering only `http/1.1` is not given `h2` | ALPN |
| `unauthenticated_refused` | A call without a token gets `UNAUTHENTICATED` | Auth compatibility |
| `controller_unauthenticated` | The real Controller's `UNAUTHENTICATED` and its `nest login` message arrive intact | Existing API compatibility |
| `controller_authenticated_probe` | With a bearer token, the real `Execute` handler rejects the empty environment (the `verify-grpc.py` contract) | Existing API compatibility; no execution |
| `unary_metadata_trailers` | A 64 KiB payload round-trips. Initial metadata, a text trailer and a `-bin` trailer arrive. `authorization`, `x-nest-project-id` and `x-nest-request-id` reach the origin (reported by name only) | Unary, metadata, trailers |
| `status_codes` | Eight codes arrive with their exact details and the `nest-error-kind` trailer | Status codes |
| `bidirectional_stream` | 20 messages, each sent only after the previous reply arrived, so the gateway does not buffer | Bidi streaming |
| `server_stream` | One-second ticks arrive spread out, not buffered | Streaming |
| `deadline_propagation` | A 1.5 s client deadline ends the call, and the origin records `cancelled` | Deadlines |
| `client_cancellation` | `future.cancel()` reaches the origin | Cancellation |
| `message_limits` | 1 MiB and 4 MiB−1 KiB pass both ways; 5 MiB gets `RESOURCE_EXHAUSTED` both ways | Message limits |
| `silent_unary_<N>s` | A unary call that sends nothing for N seconds still returns | Long-lived RPC, Execute |
| `long_bidi_<N>s`, `long_stream_<N>s` | Streams that exchange one message every 30 s stay up for N seconds | Long-lived RPC |
| `origin_unreachable_<addr>` | The origin refuses TCP from the client's network or, failing that, refuses a direct gRPC call | Origin bypass |

## Measured results

### Environment and method

- **Date:** 2026-11-24.
- **Host:** Linux, Docker 29.9.3, Envoy `v1.36.2`, Traefik `v3.6.2`, grpcio 1.84.0
  on Python 3.14.
- **Setup:** Local loopback, with the staging CA from `make_certs.py`.
- **Topology:** The gateways ran with `--network host`. The origin listened on
  `127.0.0.1:50061` and the gateways on the host's non-loopback address, so the
  bypass check ran against that address.
- **Not tested:** The `internal: true` network of `compose.yaml` has not been
  run. Docker Hub rate limits blocked building the Controller image in this
  environment, so that run is [pending](#pending-live-checks).
- **Latency:** Loopback latency says nothing about Internet latency, so none is
  reported.

### Short matrix

| Gateway | Configuration | Result |
| --- | --- | --- |
| Envoy (D) | `envoy.yaml` | **14/14 passed** |
| Traefik (C) | `traefik/*.yml` | **14/14 passed** |
| Traefik passthrough → Envoy | Stock Traefik entry point, TCP `HostSNI` passthrough router | **13/13 passed** (bypass check not repeated) |

Details shared by all three:

- **TLS:** TLS 1.3 with ALPN `h2`.
- **Status codes:** All eight codes, their details and the `nest-error-kind`
  trailer were preserved.
- **Deadline:** The client got `DEADLINE_EXCEEDED` after 1.50 s, and the origin
  saw the cancellation.
- **Message limits:** 5 MiB got `RESOURCE_EXHAUSTED` in both directions.
- **Wrong SNI:** Both gateways refused it. Envoy answered with a name mismatch,
  and Traefik served its default self-signed certificate for the unknown name.

### Long-lived calls

| Gateway | Silent unary | Bidi, a message every 30 s | Server stream, a tick every 30 s |
| --- | --- | --- | --- |
| Envoy, `envoy.yaml` (final) | 30 s ✅; 650 s ✅ (with a 30 s client keepalive); 1300 s ❌ `UNAVAILABLE: stream timeout` at **1200.0 s** | 150 s ✅ | 1290 s ✅ (44 ticks, longest gap 30.0 s) |
| Envoy with 30 s upstream PINGs (first draft of `envoy.yaml`) | 650 s ❌ and 1300 s ❌ `UNAVAILABLE: Too many pings` at **257 s** and **158 s** | — | — |
| Envoy, stock route timeout | 20 s ❌ and 400 s ❌ `UNAVAILABLE: upstream request timeout` at **15.0 s** | 150 s ✅ | — |
| Traefik, `traefik/static.yml` | 650 s ✅; 1300 s ✅ | 150 s ✅ | 1290 s ✅ (44 ticks) |
| Traefik, stock v3 entry point | 90 s ✅; 200 s ✅ | 150 s ❌ `INTERNAL: RST_STREAM` at **60.0 s** | — |
| Traefik passthrough (stock entry point) → Envoy | 200 s ✅; 650 s ✅ | 150 s ✅ | — |

What these show:

- **Envoy's stock route timeout (15 s) cuts every `Execute`.** Use `timeout: 0s`
  with `max_stream_duration.grpc_timeout_header_max`. The stock config did not
  cut the bidi stream, because the route timeout starts only once the client
  finishes its request.
- **Upstream HTTP/2 PING keepalive breaks grpcio origins.** grpcio servers,
  including the Controller with its current options, allow a PING without data
  only every 5 minutes. They count more frequent PINGs as strikes and close the
  connection with `GOAWAY too_many_pings`. That killed silent calls after 158 s
  and 257 s. The final `envoy.yaml` sends no PINGs to the origin. Any future
  gateway, sidecar or load balancer in front of the Controller has the same
  constraint (see the [contract](#proposed-transport-contract-for-h07-f02a-h08)).
  Envoy's 30 s downstream PINGs to the client and the client's own 30 s
  keepalive caused no errors.
- **Envoy's `stream_idle_timeout` is the cap for silent calls.** The 1300 s
  silent call ended at exactly 1200.0 s, as configured. This is above the SDK's
  900 s deadline, by design.
- **Traefik v3's stock `readTimeout` (60 s) cuts streams that send in both
  directions, but not silent unary calls.** The bidi stream was reset at 60.0 s.
  The silent unary calls (90 s and 200 s) passed, because the client had already
  sent its whole request. Stock `idleTimeout` (180 s) did not close a connection
  with an active call. Nest has no streaming RPC today, so stock Traefik would
  carry today's `Execute`. A streaming result RPC from C03/C04 would need
  `readTimeout: 0` on the entry point it uses.
- **SNI passthrough** through a stock Traefik entry point to Envoy kept every
  check, including a 650 s silent call. The shared Traefik therefore needs no
  timeout change for topology D.
- **Tokens and payloads were never logged.** After all runs, the synthetic token
  appeared 0 times in the logs of all five gateway containers, in the origin log,
  and in the probe output. Envoy's log lines carry only path, gRPC status, sizes
  and duration.

## Execute against proxy deadlines

Today `Execute` is one blocking unary RPC:

- The SDK sets a deadline from `NEST_RPC_TIMEOUT` (900 s by default).
- The Controller waits up to `NEST_RESULT_TIMEOUT` (600 s) for the Slurm result.
- An environment build can take up to `NEST_ENVIRONMENT_TIMEOUT` (1800 s). That
  is already longer than the SDK's default deadline.
- No byte flows on the stream until the result is ready.

A gateway in between therefore has to meet all of these conditions:

1. **Allow more than 900 s of silence on one stream.** The recommended Envoy
   configuration allows 1200 s (`stream_idle_timeout`) and is bounded by the
   client's `grpc-timeout`, capped at 3600 s. Cloudflare's proxy on non-Enterprise
   plans allows about 100–125 s, so this condition rules it out.
2. **Survive idle timeouts in NAT and firewalls on the client's network.** gRPC
   keepalive PINGs (`grpc.keepalive_time_ms`) refresh the client-to-gateway hop.
   Envoy also PINGs the client every 30 s. The SDK sets no client keepalive
   today, so H07/E08 should add one (see the
   [contract](#proposed-transport-contract-for-h07-f02a-h08)).
   Nothing may PING the Controller more often than every 5 minutes without data.
   Its grpcio ping policy closes the connection, as measured above.
3. **Accept that a cut call is not cancelled.** `ExecutionService.Execute` never
   checks `context.is_active()` or registers a callback. When the gateway or the
   client drops the call:
   - The Controller's worker thread keeps waiting for the Slurm result.
   - The Slurm job keeps running.
   - The SDK reports `DEADLINE_EXCEEDED` or `UNAVAILABLE`.

   The Controller has four RPC worker threads. Four abandoned `Execute` calls make
   it unresponsive until their jobs finish or `NEST_RESULT_TIMEOUT` expires.
   `deadline_propagation` and `client_cancellation` show that a gateway does pass
   cancellation through; the Controller just does not act on it.

### Decision

Detached execution (C03/C04: submit returns an Execution ID, and the result is
fetched or streamed separately) **must come before**:

- any pilot behind Cloudflare's proxy, or behind any other proxy whose read
  timeout is under 900 s;
- any pilot open to callers beyond the operator-approved trusted Projects.

Without detached execution, ordinary network failures turn into orphaned jobs and
exhausted Controller threads.

C03/C04 is **not** required for a trusted-only staging pilot through topology D,
within these limits:

- calls shorter than 900 s;
- payloads and results under 4 MiB;
- operators who accept that a dropped call leaves its Slurm job running.

### Explicitly unsupported

The following are unsupported until C03/C04 lands:

- An `Execute` longer than the SDK deadline, or than 1200 s of gateway idle time.
- An `Execute` through Cloudflare proxied mode longer than about 100 s.
- Payloads or results over 4 MiB. Both the SDK and the Controller use gRPC's
  default receive limit, so this is the same as today.
- Resuming a call after a connection is lost.
- gRPC-Web or browser clients.
- Server reflection and health checks over the public endpoint. These paths
  return 404 by design.

## Security gaps and mitigations

| Gap | Impact | Mitigation in this design | Owner |
| --- | --- | --- | --- |
| Anyone on the Internet can reach the gRPC handler | Unauthenticated callers reach `_authenticate` and cost one database lookup each | Path allowlist and connection limits at Envoy. Add a rate limit (`local_ratelimit`) and `global_downstream_max_connections` before a pilot. Brute force against random 256-bit session tokens is not practical, but the database load is real | H07 |
| Gateway or client PINGs reach the Controller's grpcio ping policy | Silent `Execute` calls end with `UNAVAILABLE: Too many pings` | No upstream PINGs in `envoy.yaml`. A Controller-side ping policy must be set explicitly before any keepalive reaches it directly | H07, E08 |
| Four Controller threads, and `Execute` ignores cancellation | A few slow or abandoned calls deny service to everyone | Envoy `max_concurrent_streams` and a per-connection limit. The real fix is detached execution | C03/C04 |
| Bearer token in `authorization` metadata | A gateway that logs headers would leak sessions | Neither PoC gateway logs headers (Envoy's format has no `%REQ(authorization)%`; Traefik's access log drops headers). After each run, grep the gateway logs for the token: it must not appear | H07 |
| Execution is still trusted Python as one HPC account | A public endpoint widens who can try | Unchanged: session plus Project grant, approved Projects only ([Security](security)). Transport reachability grants nothing | F02a, H08 |
| Origin bypass | A Controller port opened to the public would skip the gateway | The Controller is only on an `internal: true` network with no published port, and the probe's `origin_unreachable` check must pass from outside. Keep `NEST_CONTROLLER_ALLOW_PLAINTEXT` only for the Tailnet and internal-network paths | H07 |
| Certificate renewal (Envoy) | An expired certificate means a total outage | Let's Encrypt is moving to 64-day (2027-02-10) and 45-day (2028-02-16) certificates. Renew at about two-thirds of the lifetime or by ARI, alert at 14 days left (`tls_certificate_sni_alpn_h2` reports `days_left`), and use DNS-01 so that port 80 is not needed | H07 |
| Cloudflare Access does not cover gRPC | It cannot be used as a second factor in mode A | Nest sessions are the only access control. Do not plan on Access | — |
| `x-forwarded-for` and client IP | The audit log records the gateway's address | Envoy `use_remote_address: true` sets XFF. Recording XFF in the audit log is a separate change | H08 |

## Rollback

The PoC changes nothing in production. A staging run is removed by reversing
what it added:

1. `docker compose -f poc/public-grpc/compose.yaml --profile envoy --profile traefik down`.
   The stack has no volumes.
2. If a Traefik passthrough router was added for staging, delete its file from
   `/etc/dokploy/traefik/dynamic/`. Traefik reloads dynamic files on its own, and
   the other routers are untouched.
3. Delete the staging DNS record and revoke the scoped Cloudflare API token.
4. Delete `poc/public-grpc/.secrets/`. The synthetic token is not a Nest session,
   so nothing else needs revoking.

For the eventual H07 rollout, the same steps apply. The Tailnet path (Tailscale
Serve on :50051) stays in place throughout, so rolling back means removing the
public route, and no client has to change except those that switched to the
public address.

## Proposed transport contract for H07, F02a, H08

This is a proposal for review and is not implemented here. H07 owns the public
gateway, F02a the access policy, H08 the audit fields and E08 the SDK default.

- **Address:** `grpc.<domain>:443`, using TLS 1.2+ (1.3 preferred) and ALPN `h2`
  only. Certificates come from a public WebPKI CA. Clients verify with system
  roots, so `NEST_CONTROLLER_TLS=true` needs no CA file. Plaintext is never
  offered on a public address.
- **Services:** Only `/nest.ExecutionService/*`. Any other path returns 404 (or
  `UNIMPLEMENTED`), including reflection and `grpc.health.v1`. Health checks stay
  internal.
- **Authentication:** `authorization: Bearer <Nest session>`, `x-nest-project-id`
  and `x-nest-request-id` reach the Controller unchanged. The gateway does not
  authenticate, log, or rewrite them. Status codes, details and the
  `nest-error-kind` trailer pass through unchanged.
- **Deadlines:**
  - The client always sends `grpc-timeout`.
  - The gateway honors it up to 3600 s (`grpc_timeout_header_max`) and has no
    route timeout of its own.
  - The stream idle timeout is at least the SDK's `NEST_RPC_TIMEOUT` plus a
    margin. Today that is 1200 s.
  - Once C03/C04 lands, unary RPCs should finish within 60 s, and long work
    should stream or be polled. A Cloudflare-proxied edge then becomes possible.
- **Keepalive:**
  - The gateway PINGs clients every 30 s.
  - The SDK should set `grpc.keepalive_time_ms` to 30–60 s with
    `keepalive_permit_without_calls=0`. Envoy accepts that (measured).
  - The gateway must **not** PING the Controller more often than the Controller's
    grpcio ping policy allows: one PING without data per 5 minutes by default.
    The alternative is for the Controller to set
    `grpc.http2.min_recv_ping_interval_without_data_ms` and
    `grpc.http2.max_ping_strikes` explicitly. Otherwise silent calls die with
    `too_many_pings` (measured).
  - The same constraint applies if the Controller is ever exposed without Envoy:
    SDK keepalives would then reach grpcio directly.
- **Message size:** 4 MiB in each direction (gRPC default) until a ticket raises
  it. Any increase must change the SDK, the Controller and the gateway together.
- **Origin:** The Controller listens only on a private network the gateway
  shares, for example h2c with `NEST_CONTROLLER_ALLOW_PLAINTEXT=true` on an
  `internal` Docker network, or TLS to the origin. It has no published host port.
  The Tailnet path remains.
- **Observability:** The gateway logs method, gRPC status, sizes and duration,
  but never headers or bodies. H08 decides whether the Controller records
  `x-forwarded-for`.
- **SDK default (E08):** The hosted SDK default changes from the Tailnet address
  to the public one only after H07 passes this matrix live and the gates for
  H08 and H09 are met.

## Pending live checks

These checks need infrastructure or accounts that this study did not use. They
are **not** claimed as passed. Each one runs only against a staging name and the
synthetic origin.

```bash
cd poc/public-grpc
python make_certs.py .secrets/certs          # or place the Let's Encrypt staging cert here
python -c 'import secrets; print(secrets.token_urlsafe(32))' > .secrets/token
sudo chown 10001 .secrets/token             # the origin runs as UID 10001
docker compose --profile envoy up -d --build

# 1. Compose topology with the internal network (from the host, then from another machine):
NEST_POC_TOKEN_FILE=.secrets/token uv run python probe.py 127.0.0.1:8443 \
  --server-name grpc.staging.nest.invalid --ca-file .secrets/certs/ca.crt \
  --origin "$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' nest-h06-poc-origin-1 | awk '{print $1}'):50061"

# 2. Real staging name and public certificate, from an outside network:
NEST_POC_TOKEN_FILE=.secrets/token uv run python probe.py grpc.staging.<domain>:443 \
  --silent 30 --silent 650 --long-chat 150 --long-stream 1300 --keepalive 30 \
  --origin <staging-host-public-ip>:50061 --origin <staging-host-public-ip>:50051

# 3. Only if evaluating topology A: a separate staging hostname, proxied, with gRPC
#    enabled in the zone. Expect silent_unary_650s to fail at about 100–125 s, which
#    confirms the decision above. Record the exact time and status.
NEST_POC_TOKEN_FILE=.secrets/token uv run python probe.py grpc-cf.staging.<domain>:443 \
  --silent 60 --silent 130 --silent 650 --long-chat 300

# 4. Token never logged:
docker compose logs envoy | grep -cF "$(cat .secrets/token)"   # must print 0
```

The checklist for each run:

- [ ] Compose `internal` network: the origin is not reachable from the host or
  the LAN.
- [ ] The public certificate chain, renewal (`days_left`) and SNI behave as
  expected for the staging name.
- [ ] The matrix passes from a residential network and from a cloud VM, so the
  client-side NAT behavior is known.
- [ ] Topology A's limit is measured (exact cut time, and the status the SDK
  sees).
- [ ] Traefik passthrough is checked on the real Dokploy Traefik version. The
  version Dokploy installs could not be confirmed from Dokploy's documentation.
- [ ] Authenticated Origin Pulls plan availability and Cloudflare's current
  limits are confirmed on the primary pages, if A is reconsidered.

## Sources

Retrieved 2026-11-24. "Confirm" in the comparison marks facts read from search
excerpts of these pages, not the page itself.

- Cloudflare gRPC: <https://developers.cloudflare.com/network/grpc-connections/>
  and <https://developers.cloudflare.com/support/network/understanding-cloudflare-grpc-support/>
- Cloudflare limits and timeouts:
  <https://developers.cloudflare.com/fundamentals/reference/connection-limits/>
  and <https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/>
- Cloudflare upload limits:
  <https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/>
- Authenticated Origin Pulls:
  <https://developers.cloudflare.com/ssl/origin-configuration/authenticated-origin-pull/>
- Origin CA: <https://developers.cloudflare.com/ssl/origin-configuration/origin-ca/>
- Tunnel and gRPC:
  <https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/use-cases/grpc/>
  and origin parameters at
  <https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/cloudflared-parameters/origin-parameters/>
- Traefik gRPC and entry points:
  <https://doc.traefik.io/traefik/v3.5/user-guides/grpc/> and
  <https://doc.traefik.io/traefik/v3.5/reference/install-configuration/entrypoints/>
- Dokploy Traefik and certificates:
  <https://docs.dokploy.com/docs/core/traefik/overview> and
  <https://docs.dokploy.com/docs/core/domain/certificates>
- Envoy timeouts: <https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/timeouts>
- Tailscale Funnel: <https://tailscale.com/kb/1223/funnel>
- Let's Encrypt lifetimes: <https://letsencrypt.org/2025/12/02/from-90-to-45.html>
  and <https://letsencrypt.org/docs/cert-lifetimes/>
