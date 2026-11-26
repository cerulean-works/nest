# Slurm backend

`SlurmBackend` is the shared Slurm execution core: it prepares environments,
stages payloads, submits jobs, and reads status, logs, results and accounting,
and cancels jobs. It runs every remote command through a transport that owns the
SSH connection. The Controller uses it for remote execution with its own
noninteractive SSH, but hosts can also use it directly for job submission, status
queries, cancellation, and environment preparation.

See [Deploy a Slurm Controller](../deploy) for the required remote directory
layout. The examples in
[`examples/`](https://github.com/cerulean-works/nest/tree/main/examples) include
`slurm_submit.py`, `slurm_status.py`, and `slurm_cancel.py`.

## Configuration

[[autodoc]] backends.slurm.SlurmConfig

## Job and environment operations

Each SSH command is classified: a timeout raises [`errors.SSHTimeoutError`],
an SSH failure (exit status 255) raises [`errors.SSHError`], and any other
nonzero exit raises [`errors.RemoteCommandError`]. `submit` never retries. An
ambiguous submission raises [`errors.SubmissionUnknownError`].

Waiting for a result or environment has a polling deadline. If the job never
started, the timeout raises [`errors.QueueTimeoutError`]; if it started, it raises
[`errors.ExecutionTimeoutError`]. A timeout does not cancel the Slurm job;
inspect its status before submitting another execution.

While a job is pending, Nest reads Slurm's pending reason and the job's
partition, QoS, and account from `squeue`, and logs each change. Reasons that
need attention, such as a QoS or association limit (`MaxGRESPerAccount`) or a
request the partition cannot run (`PartitionConfig`), are logged as warnings
with the requested resources from `scontrol show job` and a suggested fix. The
last reason is kept in the timeout message and in `QueueTimeoutError.pending_reason`.
A job pending for a reason it cannot outwait for `blocked_timeout` seconds raises
[`errors.JobBlockedError`] before the deadline; see
[Pending jobs](../remote-errors#pending-jobs).

[[autodoc]] backends.slurm.SlurmBackend

[[autodoc]] backends.slurm.SlurmJobInfo

[[autodoc]] backends.slurm.SlurmAccounting

[[autodoc]] backends.slurm.classify_pending_reason

[[autodoc]] backends.slurm.new_execution_id

## Transports and session capabilities

A transport runs the core's commands and advertises, through
`TransportCapabilities`, whose HPC identity runs them
(`execution_authority`), whether a person must answer prompts
(`interactive_required`), and what it can do without the user's live session
(`can_query_offline`, `can_cancel_offline`, `can_submit_offline`).

| Transport | Authority | Interactive | Offline status/logs/result/accounting | Offline cancel | Status |
| --- | --- | --- | --- | --- | --- |
| `ControllerSSHTransport` | `controller_owned` | No | Yes | Yes | Current (trusted Mode A; the default) |
| `LocalOpenSSHTransport` | `user_owned` | Yes | No | No | Client-side building block for planned Mode B |

`ControllerSSHTransport` is what `SlurmBackend(config)` always used: the same
`ssh` command line for `NEST_SLURM_SSH_MODE=A` and the managed-key
`NEST_SLURM_SSH_MODE=B` (which is still Controller-owned SSH, not the planned
user-owned Mode B).

`LocalOpenSSHTransport` runs the user's own OpenSSH on the client. OpenSSH asks
for the HPC password and MFA on the user's terminal, never through Nest, and
reuses the authenticated connection through a private control socket. It reads
no SSH configuration file, checks host keys strictly against operator-distributed
pins, and disables agent and port forwarding. It authenticates only with the
user's password and keyboard-interactive (MFA) answers: public keys (including
default `~/.ssh/id_*` files), the SSH agent, GSSAPI and host-based authentication
are disabled, and `SSH_AUTH_SOCK` is not passed to `ssh`, so it cannot log in
with an identity the running process happens to have. Askpass helpers are
disabled too (`SSH_ASKPASS_REQUIRE=never`). Authentication happens only in an
explicit login step that requires the user's terminal and starts a new
connection in a private directory used by no other transport. Remote commands
run only through that connection, in batch mode: if it has gone away, they fail
instead of authenticating again. The
Controller does not use it: the opaque Controller relay and user-owned
submission are separate, later work.

When a user-owned session is not active (the client exited, or a headless run
has no terminal for the prompts), the core raises
[`errors.SessionUnavailableError`] before running any command. Nest never falls
back to Controller-owned SSH, and a `SlurmBackend` refuses to pair a user-owned
transport with a configuration that carries Controller SSH credentials.

[[autodoc]] backends.transport.TransportCapabilities

[[autodoc]] backends.transport.ExecutionAuthority

[[autodoc]] backends.transport.SlurmTransport

[[autodoc]] backends.transport.ControllerSSHTransport

[[autodoc]] backends.transport.LocalOpenSSHTransport
