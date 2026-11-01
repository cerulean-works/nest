# Executor and workers

These entry points implement function execution behind the Controller. SDK
users normally call [the wrapper's `.remote()` method](remote#wrapped-functions) instead.

## Local subprocess executor

The local Controller uses the current interpreter to start a separate worker
process. `execute` returns a worker error record as a dictionary; the client is
responsible for turning it into [`errors.RemoteExecutionError`].

[[autodoc]] executor.execute

## Worker entry point

The worker reads serialized input and writes serialized output. An optional user
interpreter selects the prepared project environment.

[[autodoc]] worker.main

## User environment runner

The worker invokes this runner in isolated Python mode and supplies a
`cloudpickle` bootstrap next to the input file.

[[autodoc]] user_runner.main
