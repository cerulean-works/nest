# Nest

Nest is a Python SDK for executing Python functions on remote Slurm clusters.
Decorate a function once, run it locally while developing, and send it to a
Controller for execution on HPC.

Nest is in Early Access (`0.1.0a1`). The API is experimental and may change
before version 1.0.0.

The shared Controller is authenticated (milestone M1): every remote call carries
your Nest session, made with Google sign-in, and runs in a Project that the
operator has approved for an HPC Platform and a Slurm account.

## Get started

1. [Install Nest](installation) with Python 3.14 or later, using uv, and keep it
   up to date.
2. [Run your first remote function](quickstart): connect over Tailscale, point the
   SDK at the Controller, sign in with `nest login`, choose your Project with
   `NEST_PROJECT_ID`, and make a first call.
3. [Use project dependencies](project-dependencies) when your function needs
   packages from a uv project.
4. [Handle remote errors](remote-errors), including
   [access and onboarding errors](remote-errors#access-and-onboarding-errors).

Working alone without a cluster? [Run a local Controller](local-controller):
stateless, with no sign-in.

## Accounts and Projects

- [Sign in from the command line](cli-login): `nest login`, `nest auth status`,
  `nest logout`, and how sessions are stored.
- [Share Projects and manage roles](projects): Owner, Admin, Member and Viewer.
- [Invite people to a Project](invitations): email invitations and Invite Links.

## Host a Controller

Operators manage the Controller, its database, Google sign-in, SSH connection,
Slurm scripts and Apptainer runtime, and approve each Project:

1. Deploy: [on Dokploy from environment variables](dokploy) (the complete M1
   Controller), or [with Docker Compose](docker-compose) and
   [PostgreSQL](database) plus [Google sign-in](authentication).
2. Prepare the cluster: [Deploy a Slurm Controller](deploy) and
   [Build and roll out the HPC runtime image](runtime-image).
3. Share it: [Share a Controller over Tailscale](tailscale-shared).
4. [Onboard a Project](project-onboarding): Provider approval, Platform grant and
   Slurm account.

## Reference

- [Remote functions and resources](api/remote): decorator, wrapper, and resource counts.
- [Execution service and RPCs](api/controller): Controller handlers, messages and metadata.
- [Slurm backend](api/slurm): job and environment operations.
- [Environment variables and defaults](configuration): addresses, paths, and deadlines.
- [Security and limitations](security): the trust model and feature limits.

For contributing and documentation commands, see [Develop Nest and its docs](developing).
