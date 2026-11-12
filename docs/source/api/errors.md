# Errors

The client raises `RemoteExecutionError` when a worker returns a serialized Python
exception record. It is a subclass of `RuntimeError` and exposes `remote_type`,
`remote_message`, and `remote_traceback`.

SSH, Slurm, and deadline failures raise `InfrastructureError` subclasses instead.
The Controller reports their `kind` in the `nest-error-kind` trailing metadata and
the client raises the same class. They are also `grpc.RpcError`s. The timeout
classes are also `TimeoutError`s. See [Handle remote errors](../remote-errors) for
what each error means and when it is safe to call again.

[[autodoc]] errors.RemoteExecutionError

## Infrastructure errors

[[autodoc]] errors.InfrastructureError

[[autodoc]] errors.SSHError

[[autodoc]] errors.RemoteCommandError

[[autodoc]] errors.SlurmJobError

[[autodoc]] errors.SubmissionUnknownError

## Timeouts

[[autodoc]] errors.NestTimeoutError

[[autodoc]] errors.SSHTimeoutError

[[autodoc]] errors.QueueTimeoutError

[[autodoc]] errors.ExecutionTimeoutError

[[autodoc]] errors.RpcTimeoutError
