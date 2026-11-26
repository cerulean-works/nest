# Run a local Controller

A local Controller is for **development on one machine**: no Slurm, no database,
no sign-in and no Projects. It is the stateless, unauthenticated mode
(`NEST_CONTROLLER_AUTH=none`), separate from the authenticated M1 Controller that
the [quickstart](quickstart) uses.

```bash
export NEST_EXECUTOR="local"
export NEST_CONTROLLER_BIND="127.0.0.1:50051"

uv run python -m nest.controller
```

Leave `NEST_DATABASE_URL` unset. The startup log shows `auth=none database=none`.

In another terminal, clear any shared-Controller settings and call it:

```bash
unset NEST_PROFILE NEST_CONTROLLER_ADDRESS NEST_CONTROLLER_TLS NEST_CONTROLLER_TLS_CA_FILE \
      NEST_AUTH_URL NEST_HOSTED_SESSION_ADDRESS NEST_PROJECT_ID
uv run python main.py
```

The SDK's default address is `127.0.0.1:50051`; set
`NEST_CONTROLLER_ADDRESS="127.0.0.1:50051"` explicitly if you prefer. No
`nest login` or `NEST_PROJECT_ID` is needed, and the SDK sends no session when
`NEST_AUTH_URL` is unset, even if you signed in to hosted Nest: only the hosted
Controller receives the hosted session.

## What it does and does not do

The Controller runs calls in a separate subprocess using its own Python
interpreter. It does not allocate GPUs or prepare a project's uv environment, so
`project=` is ignored for installation: install the function's dependencies into
the Controller environment first.

The job status, submission, and cancellation RPCs still require a configured
Slurm backend. For HPC execution, use [Deploy a Slurm Controller](deploy).

## Security assumptions

Without a database, the Controller authenticates and authorizes nobody: anyone who
can open a connection to the listener can run arbitrary Python as the user running
the Controller. That is acceptable on `127.0.0.1`, where only your own account can
connect. The Controller logs a warning when this mode listens beyond loopback.

Do not bind it to a shared network, and do not use it as a fallback for a shared
Controller that refuses a call. A shared deployment uses session authentication,
described in [Authenticated execution](security#authenticated-execution).

To develop against authentication locally, run the Controller with a local
PostgreSQL database and Google sign-in on `http://127.0.0.1` (see
[Sign in with Google](authentication#local-development)); it then behaves like
the shared Controller.
