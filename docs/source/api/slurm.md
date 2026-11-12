# Slurm backend

`SlurmBackend` manages batch jobs and stages files through noninteractive SSH.
The Controller uses it for remote execution, but hosts can also use it directly
for job submission, status queries, cancellation, and environment preparation.

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

[[autodoc]] backends.slurm.SlurmBackend
