# Nest

Nest is a Python SDK for executing Python functions on remote Slurm clusters.
Decorate a function once, run it locally while developing, and send it to a
Controller for execution on HPC.

Nest is in Early Access (`0.1.0a1`). The API is experimental and may change
before version 1.0.0.

## Get started

1. [Install Nest](installation) with Python 3.12 or later.
2. [Run your first remote function](quickstart) using the team's Controller.
3. [Use project dependencies](project-dependencies) when your function needs
   packages from a uv project.
4. [Handle remote errors](remote-errors) in your application.

## Host a Controller

Hosts manage the Controller, SSH connection, Slurm scripts, and Apptainer runtime.
See [Deploy a Slurm Controller](deploy), or [Run a local Controller](local-controller)
to develop without an HPC cluster.

## Reference

- [Remote functions and resources](api/remote): decorator, wrapper, and resource counts.
- [Execution service and RPCs](api/controller): Controller handlers and messages.
- [Slurm backend](api/slurm): job and environment operations.
- [Environment variables and defaults](configuration): addresses, paths, and deadlines.
- [Security and limitations](security): the current trust model and feature limits.

For contributing and documentation commands, see [Develop Nest and its docs](developing).
