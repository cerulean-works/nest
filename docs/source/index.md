# Nest

Nest is a Python SDK for running Python functions on HPC clusters without writing
Slurm scripts. Decorate a function, call it with `.local()` while you develop, and
with `.remote()` to run it on the cluster.

```python
import nest


@nest.remote(cpu=1)
def compute(x):
    return x * 2


print(compute.remote(21))  # 42, computed by a Slurm job
```

Nest is in **Early Access**: install it from `main`, and expect API changes before
1.0. See [Install Nest](installation) for versions.

## Quickstart

**[Run your first remote function in four steps](quickstart):** install, log in,
create `main.py`, run. You need uv, a Google account, access to a Project and a
connection to the Controller; the Quickstart lists each one.

No access yet? [Get access to a Project](get-access). Working alone without a
cluster? [Run a local Controller](local-controller): stateless, with no sign-in.

## User guide

- Write and run functions: [project dependencies](project-dependencies),
  [GPUs](gpus), [remote errors](remote-errors).
- Accounts and Projects: [sign in from the command line](cli-login),
  [choose the Project your calls run in](project-selection),
  [roles](projects), [invitations](invitations) and the
  [Management Console](console).
- Connections: [the Cerulean-hosted profile](hosted-connection) (public gateway,
  staging pilot only until H09) and [client settings](configuration#client).

## Operator guide

For hosts who run a Controller, its database, Google sign-in, SSH connection, Slurm
scripts and HPC runtime, and approve each Project:

1. Deploy: [on Dokploy from environment variables](dokploy), or
   [with Docker Compose](docker-compose) plus [PostgreSQL](database) and
   [Google sign-in](authentication). [Preflight and recover](deployment-recovery)
   a deployment.
2. Prepare the cluster: [Deploy a Slurm Controller](deploy) and
   [Build and roll out the HPC runtime image](runtime-image).
3. Share it: [over Tailscale](tailscale-shared); the
   [public gRPC gateway pilot](public-grpc-gateway) and its
   [rollout runbook](hosted-rollout) are not yet in production.
4. [Onboard a Project](project-onboarding): Provider approval, Platform grant and
   Slurm account, through `nest-db`, the [privileged API](privileged-api) or the
   Console.

## Reference

- [Remote functions and resources](api/remote): decorator, wrapper and resource counts.
- [Execution service and RPCs](api/controller): Controller handlers, messages and metadata.
- [Environment variables and defaults](configuration): addresses, paths and deadlines.
- [Security and limitations](security): the trust model and feature limits.

## Contributing

[Develop Nest and its docs](developing) covers tests and the docs preview;
[Release status and checklist](releasing) covers versions, tags and what a
pre-release needs.
