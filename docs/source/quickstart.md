# Run your first remote function

This guide takes you from a fresh environment to a function running on the team's
authenticated (M1) Controller. Each step says whether **you** do it or whether it
is an **operator** step that you ask the person running the Controller for.

To try Nest on one machine without a cluster, a sign-in or a Project, use the
[local development flow](#local-development-flow-no-session) at the end instead.

## Before you start

| You need | From |
| --- | --- |
| Python 3.14 and Nest in a uv project | [Install Nest](installation) |
| Access to the Controller's Tailnet, or an accepted share of the Controller node | The operator |
| The Controller's gRPC address, such as `nest.cerulean.works:50051`, and whether it uses TLS | The operator |
| Nest's sign-in address (`NEST_AUTH_URL`), such as `https://nest.example.com` | The operator |
| A Google account | You |

Examples below use placeholders (`nest.example.com`, `<project-id>`). Use the
values your operator gives you, and never paste a token or credential into a
script, a shell history or an issue.

## 1. Connect to the Controller's network

The shared Controller is reachable only through Tailscale. Connect a user-owned
device to the authorized Tailnet, or accept the Controller node share the operator
sent you (see [Share a Controller over Tailscale](tailscale-shared)). Tagged
devices cannot use shares.

## 2. Point the SDK at the Controller

```bash
export NEST_CONTROLLER_ADDRESS="nest.cerulean.works:50051"
export NEST_CONTROLLER_TLS=false
```

The team deployment uses plaintext gRPC inside the encrypted, access-controlled
Tailnet, so `NEST_CONTROLLER_TLS=false` is set explicitly; for a non-loopback
address the SDK refuses to guess. For a Controller with its own TLS certificate,
set `NEST_CONTROLLER_TLS=true` and, for a private CA,
`NEST_CONTROLLER_TLS_CA_FILE=/path/to/ca.pem`. See
[Client settings](configuration#client).

## 3. Sign in

```bash
export NEST_AUTH_URL="https://nest.example.com"
uv run nest login
```

```text
To sign in to Nest, open this link in a browser:

    https://nest.example.com/auth/cli/approve?code=BCDF-GHJK

and check that it shows the code BCDF-GHJK.
Nest will never ask for your NCHC password or one-time code there.
Waiting for approval...
Signed in to Nest.
```

Open the link on any device, sign in with Google if asked, check that the page
shows the same code, and choose **Approve**. Then confirm that Nest accepts the
session:

```bash
uv run nest auth status --check
```

```text
Signed in: https://nest.example.com
  user:          5a9254bc-...
  scope:         jobs:run projects:read
  ...
  accepted by Nest: yes
```

The session is stored in `~/.config/nest/credentials.json` (mode `0600`) and
refreshed automatically for up to 7 days. With `NEST_AUTH_URL` set, `.remote()`
sends it to the Controller with every call. It is a Nest session only: it is never
sent to an HPC login node and is not an NCHC password or one-time code. See
[Sign in from the command line](cli-login).

## 4. Join or create a Project

Every remote call runs in a **Project**, named by its ID (a UUID). Your role in
the Project decides what you may do:

| Role | Submit and cancel own Runs | Invite people, change roles |
| --- | --- | --- |
| Owner, Admin | yes | yes |
| Member | yes | no |
| Viewer | no | no |

**Join an existing Project.** Ask its Owner or an Admin for an email invitation
or an Invite Link (see [Invite people to a Project](invitations)). Open the link,
sign in with Google, review the Project and role, and press *Accept*.

**Or create one.** Creating a Project needs a browser session; Nest has no page
or CLI command for it yet. Sign in at `https://nest.example.com/auth/google/login`,
then, in that tab's developer console on the Nest origin, run:

```js
await (await fetch("/api/projects", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ name: "My lab" }),
})).json();
```

The answer contains the new Project's `id`, and you are its Owner.

**Find the ID** of the Projects you belong to with your CLI session:

```python
# list_projects.py
import json
import os
import urllib.request

from nest.login import ensure_session

session = ensure_session()
request = urllib.request.Request(
    os.environ["NEST_AUTH_URL"] + "/api/projects",
    headers={"Authorization": f"Bearer {session.access_token}"},
)
with urllib.request.urlopen(request) as response:
    print(json.dumps(json.load(response), indent=2))
```

```bash
uv run python list_projects.py
```

Then select the Project for your calls:

```bash
export NEST_PROJECT_ID="<project-id>"
```

## 5. Ask the operator to approve the Project

A Project role never grants HPC access by itself. Before the first Run, the
**operator** must, for your Project ID:

1. approve it for the Controller's Provider (`NEST_PROVIDER_ALLOWED_PROJECTS`);
2. grant it an HPC Platform (`nest-db platform grant`); and
3. register and link a Slurm account it may charge (`nest-db account link`).

These are operator-only actions, done on the Controller host with database
credentials; see [Onboard a Project](project-onboarding). Send the operator your
Project ID and the Slurm account your group should be charged to. Never send them
your Nest session file.

Check that a Platform is granted:

```bash
uv run nest platforms default --project-id "$NEST_PROJECT_ID"
```

```text
nano4 (only_eligible)
```

`No HPC Platform is granted to this Project.` means step 2 is still pending.

## 6. Run a function

Create `main.py`:

```python
import nest


@nest.remote(gpu=1)
def compute(x):
    """Returns twice the input value."""
    return x * 2


print(compute.local(21))  # 42
print(compute.remote(21))  # 42
```

Run it:

```bash
uv run python main.py
```

- `.local()` (and calling `compute(21)` directly) runs the function in your own
  Python process.
- `.remote()` sends the function to the Controller, which checks your session,
  Project role and the operator's approvals, submits a Slurm job on your Project's
  Platform and Slurm account, and waits for the result.

`gpu` requests GPUs per node and `cpu` CPUs per task; `0` (the default) omits the
Slurm option. `.remote()` is synchronous: it returns the result or raises an
error, and has a 900-second deadline (`NEST_RPC_TIMEOUT`).

If the call is refused, see
[Access and onboarding errors](remote-errors#access-and-onboarding-errors).

## Next steps

- [Use project dependencies](project-dependencies) when your function imports
  packages from your uv project.
- [Handle remote errors](remote-errors) in your application.
- [Remote functions and resources](api/remote) for the full API.

## Local development flow (no session)

For development on one machine, run a Controller without a database, Slurm or
sign-in, and call it over loopback:

```bash
# terminal 1
export NEST_EXECUTOR=local
export NEST_CONTROLLER_BIND=127.0.0.1:50051
uv run python -m nest.controller
```

```bash
# terminal 2: unset the shared Controller's settings first
unset NEST_CONTROLLER_ADDRESS NEST_CONTROLLER_TLS NEST_AUTH_URL NEST_PROJECT_ID
uv run python main.py
```

The SDK's default address is `127.0.0.1:50051`. This Controller uses
`NEST_CONTROLLER_AUTH=none`: it has no sign-in, no Projects and no authorization,
and runs every call it receives as a subprocess of the Controller, with the
Controller's own Python environment. It is safe only on loopback or another
network where everyone who can connect is trusted with your account. Never expose
it to a shared network. See [Run a local Controller](local-controller).
