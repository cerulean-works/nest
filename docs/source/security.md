# Security and limitations

Nest currently uses Python object serialization for remote function execution.

Deserializing untrusted Python payloads may execute arbitrary code.

The current Controller does not provide application-level authentication, multi-tenant isolation, or per-user HPC identity management.

Only trusted users should be granted access to the Controller.

For the current team deployment, restrict network access using Tailscale Grants / ACLs.

Do not expose the Controller directly to the public Internet.

The [shared Tailscale deployment](tailscale-shared) exposes only TCP 50051 to
accepted, policy-authorized recipients. All callers still submit work as the
Controller's single HPC SSH user. Node Sharing and Grants restrict network
reachability; they do not authenticate callers inside Nest or isolate jobs.

## Deployment credentials

Keep SSH keys, `known_hosts`, and the Tailscale Auth Key in the ignored `ssh/` and
`secrets/` directories or Dokploy File Mounts, never in Git, `.env`, or Compose
YAML. Keep `StrictHostKeyChecking yes` with a verified `known_hosts`; never use
`accept-new`, `no`, or `/dev/null` to work around a host-key error. The test suite
fails if a tracked file contains a private key or token, or weakens host-key
verification. See [Deployment validation and roadmap](deployment-architecture).

Mode B (managed SSH, see [configuration](configuration)) can take the key from
`NEST_SSH_PRIVATE_KEY` for platforms without file mounts. Set it from the
platform's secret store or the deploying shell, never in `.env` or Git. The
Controller writes it to a private temporary file, never logs it, and masks it
in its own log output. It remains visible to anyone who can inspect the
container's environment, so prefer `NEST_SSH_IDENTITY_FILE` where a secret
file mount is available. Mode B always uses `StrictHostKeyChecking=yes` and
ignores every SSH configuration file.

## Known limitations

- Experimental API with potential breaking changes.
- No multi-tenant isolation.
- No per-user HPC accounts.
- No automatic project source or data synchronization.
- No application-level authentication. `NEST_AUTH_TOKEN_FILE` is reserved and
  rejected at startup until it is implemented. TLS is available with
  `NEST_TLS_CERT_FILE` and `NEST_TLS_KEY_FILE`.
- Controller and backend deployment are managed by the host.

## Execution behavior

The client connects through an insecure gRPC channel. The current deployment
relies on the trusted network and host configuration to restrict access.

Project dependency installation applies to the Slurm backend; a local Controller
uses its own environment.

Client RPC and backend polling deadlines do not cancel submitted Slurm jobs.
See [Environment variables and defaults](configuration) for current values and
[Handle remote errors](remote-errors) for failure handling.
