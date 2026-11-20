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

[[autodoc]] backends.slurm.classify_pending_reason

[[autodoc]] backends.slurm.new_execution_id
