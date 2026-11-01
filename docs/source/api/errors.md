# RemoteExecutionError

The client raises `RemoteExecutionError` when a worker returns a serialized Python
exception record. It is a subclass of `RuntimeError` and exposes `remote_type`,
`remote_message`, and `remote_traceback`.

This differs from a gRPC transport or Controller failure, which can raise
`ConnectionError` or `grpc.RpcError`. See [Handle remote errors](../remote-errors).

[[autodoc]] errors.RemoteExecutionError
