# Connect to Cerulean-hosted Nest

The SDK has a first-party **hosted connection profile**. It reaches Cerulean-hosted
Nest through its public gRPC gateway, `grpc.nest.cerulean.works:443`, over verified
TLS, with the session you get from `nest login` at `https://auth.nest.cerulean.works`.
It needs no VPN or Tailnet.

> [!IMPORTANT]
> The hosted profile is **not a default yet**. It stays inactive until H09
> (production rollout and security sign-off) approves the public endpoint; until
> then `.remote()` without settings uses the local Controller (`127.0.0.1:50051`), as
> before. Pilot users opt in with `NEST_PROFILE=hosted` once the operator has
> announced the [gateway pilot](public-grpc-gateway). Activation is a single,
> reviewed change of `nest.config.HOSTED_PROFILE_DEFAULT`, and turning it back off
> is the rollback.

## The flow

```bash
uv add "nest @ git+https://github.com/cerulean-works/nest@main"   # see Install Nest
export NEST_PROFILE=hosted   # not needed once H09 makes the profile the default
```

```python
import nest


@nest.remote(gpu=1)
def train(): ...


train.remote()
```

1. The first call finds no session. On a terminal it opens the browser approval of
   [command-line sign-in](cli-login) (or run `nest login` beforehand). Without a
   terminal it fails with `AuthenticationRequiredError` and asks you to run
   `nest login`; nothing runs.
2. It [chooses your Project](project-selection): your saved choice, your only
   eligible Project, or a prompt.
3. It runs the call. Later calls reuse and refresh the stored session.

## What the profile fixes

| Setting | Hosted profile |
| --- | --- |
| Controller | `grpc.nest.cerulean.works:443` |
| Transport | TLS 1.2+ verified against the system's public roots, for that name. A failed handshake, an untrusted or expired certificate, or another name is an error; the SDK **never** falls back to plaintext and never overrides the expected name. |
| Session | Only the session of `https://auth.nest.cerulean.works`. Another `NEST_AUTH_URL` is a configuration error. |
| Keepalive | HTTP/2 PINGs every 30 seconds during a call (`NEST_CONTROLLER_KEEPALIVE`), never while idle |
| Retries | None: the channel never sends a call twice |
| Deadline | `NEST_RPC_TIMEOUT` (900 s), at most 1200 s, the gateway's idle limit |

`NEST_PROFILE=hosted` cannot be combined with `NEST_CONTROLLER_ADDRESS`,
`NEST_CONTROLLER_TLS` or `NEST_CONTROLLER_TLS_CA_FILE`; the SDK refuses the mix
instead of guessing. Naming the gateway explicitly
(`NEST_CONTROLLER_ADDRESS=grpc.nest.cerulean.works:443`) applies the same rules:
`NEST_CONTROLLER_TLS=true` is required, and a custom CA file is refused.

## Other Controllers keep working

| You use | Set | The hosted session |
| --- | --- | --- |
| A self-hosted Nest | `NEST_CONTROLLER_ADDRESS`, `NEST_CONTROLLER_TLS` (or a CA file) and `NEST_AUTH_URL` for its sign-in | Never sent: only that Nest's session is |
| A local or stateless Controller | Nothing (loopback default), or `NEST_PROFILE=local` once the hosted default is on | Never sent; no sign-in starts |
| The hosted Controller over the Tailnet (rollback, existing users) | The Tailnet address as in [Environment variables](configuration#why-the-hosted-controller-is-not-a-default) | Sent, over the Tailnet only |

An explicit `NEST_CONTROLLER_ADDRESS` always overrides the hosted default, also
after H09. A session is only ever sent to the Controllers its sign-in origin
applies to: a lookalike such as `grpc.nest.cerulean.works.example.org` or another
port is a custom Controller and gets nothing.

## Remaining permissions and behavior without a terminal

- Signing in needs a browser where you are signed in to Nest with Google; the CLI
  shows a link and a code, and never asks for HPC passwords.
- Running needs an invitation to a Project, the operator's Provider approval and a
  Platform grant for it. The SDK cannot grant any of these; `nest projects list`
  shows what you may use.
- Without a terminal (CI, batch jobs, notebook kernels) nothing prompts: sign in
  beforehand with `nest login`, and set `NEST_PROJECT_ID` when you have several
  Projects.

## Acceptance checklist (E08)

- [x] First-party hosted profile with verified TLS and no plaintext fallback
  (`tests/test_hosted_profile.py`: untrusted CA, wrong name and plaintext server all
  fail before any call reaches a Controller).
- [x] Explicit endpoint overrides, self-hosted deployments and local development
  unchanged; the Tailnet path kept for rollback.
- [x] Hosted sessions sent only to the hosted Controller; custom endpoints never
  receive them, even with `NEST_AUTH_URL` left set to the hosted issuer, unless the
  address is named again in `NEST_HOSTED_SESSION_ADDRESS` over TLS.
- [x] Installation, first-call sign-in, Project selection and execution without
  any `NEST_` setting, tested with the default switched on in the test only.
- [x] The hosted default stays inactive (`HOSTED_PROFILE_DEFAULT = False`) until H09.
- [ ] Staged validation against the live pilot gateway (part of the
  [H08 checklist](public-grpc-gateway#evidence)), then H09.
