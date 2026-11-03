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

## Known limitations

- Experimental API with potential breaking changes.
- No multi-tenant isolation.
- No per-user HPC accounts.
- No automatic project source or data synchronization.
- No application-level authentication.
- Controller and backend deployment are managed by the host.

## Execution behavior

The client connects through an insecure gRPC channel. The current deployment
relies on the trusted network and host configuration to restrict access.

The `cpu` decorator argument is sent by the client but is not forwarded by the
Slurm Controller. Project dependency installation applies to the Slurm backend;
a local Controller uses its own environment.

Client RPC and backend polling deadlines do not cancel submitted Slurm jobs.
See [Environment variables and defaults](configuration) for current values and
[Handle remote errors](remote-errors) for failure handling.
