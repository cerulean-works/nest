# nest

Run Python functions locally or through a gRPC controller, with an optional Slurm backend.

## Setup

```sh
uv sync
uv run python examples/hello.py
uv run python examples/serialization.py
```

Start a controller, then run the remote example in another terminal:

```sh
uv run python -m nest.controller
```

```sh
uv run python examples/remote.py
```

## Configuration

- `NEST_CONTROLLER_ADDRESS`: controller address (default: `127.0.0.1:50051`).
- `NEST_EXECUTOR`: `local` (default) or `slurm`.
- `NEST_SLURM_HOST`: SSH host for the Slurm backend.
- `NEST_SLURM_ACCOUNT`: Slurm account.
- `NEST_SLURM_WORKDIR`: remote working directory.

The Apptainer scripts use `$HOME/nest-smoke` for images, the project, and job files. Set up that directory on the remote host before running them.

When upgrading from the previous package name, update imports, environment variables, and remote paths; run `uv sync` in each deployed checkout. Restart both controller and clients together because the gRPC service is now `nest.ExecutionService`. Regenerate pending serialized job payloads with the new package before submitting them.

## Regenerate Protobuf code

```sh
uv run python -m grpc_tools.protoc -I src --python_out=src --grpc_python_out=src src/nest/proto/execution.proto
```
