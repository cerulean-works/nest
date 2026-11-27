# Quickstart

Run a Python function on HPC with Nest: install it, log in, write `main.py` and
run it. This page is the shortest path for a user who has been given access to
a Nest Project. Operators who run a Controller start at the
[Operator guide](index#operator-guide) instead.

> [!NOTE]
> Nest is in **Early Access**, and the API may change before 1.0. These docs
> describe the release `v0.1.0a5`, which step 1 installs once it is published.
> See [Releases and `@main`](installation#releases-and-main) for the development
> build and for updates.

## Before you start

| You need | Where it comes from |
| --- | --- |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Install it once. uv downloads Python 3.14 for you if it is missing |
| A Google account | You sign in to Nest with it |
| A Project you may run in | An invitation from the Project's Owner or an Admin, and the operator's approval of the Project. No access yet? See [Get access to a Project](get-access) |
| A connection to the Controller | The next section |

## Connection settings

Set these once per terminal before the four steps. They say **where** your calls
go; the SDK never guesses an address for a shared Controller.

**Cerulean-hosted Nest, private path** (available today). Connect Tailscale to
the authorized Tailnet, or accept the Controller node share the operator sent you
([details](tailscale-shared)), then, in a new terminal or after clearing settings
left from another connection:

```bash
unset NEST_PROFILE NEST_CONTROLLER_TLS_CA_FILE NEST_HOSTED_SESSION_ADDRESS NEST_PROJECT_ID
tailscale ping nest-controller.tail8e5ba1.ts.net    # the Controller must answer
export NEST_AUTH_URL="https://auth.nest.cerulean.works"
export NEST_CONTROLLER_ADDRESS="nest-controller.tail8e5ba1.ts.net:50051"
export NEST_CONTROLLER_TLS=false    # plaintext gRPC, only inside the encrypted Tailnet
```

The `unset` line clears what another connection, such as a staging pilot or a
self-hosted Nest, may have left in the terminal. The SDK refuses conflicting
settings instead of guessing: for example a leftover CA file with
`NEST_CONTROLLER_TLS=false`, or `NEST_PROFILE=hosted` together with an explicit
address. It also clears an old `NEST_PROJECT_ID`, which would override the Project
choice in step 2; set it again afterwards only if you chose that Project
deliberately for this Controller.

Use the MagicDNS name, never a Tailscale IP address copied from someone else.
`NEST_CONTROLLER_TLS=false` is safe only because the Tailnet encrypts and
access-controls the connection; never set it for a Controller on the public
Internet. [Why these settings are explicit](configuration#why-the-hosted-controller-is-not-a-default).

**Other ways to connect:**

- *Public gateway, without Tailscale:* not generally available. It is in an
  operator-run staging pilot until the production sign-off (H09). Use it only if
  the operator enrolled you, with the exact settings they give you; see
  [Connect to Cerulean-hosted Nest](hosted-connection).
- *Your organization's own Nest:* see [Use a self-hosted Nest](#use-a-self-hosted-nest).
- *No cluster, one machine:* see [Local development](#local-development-flow-no-session).

## 1. Install

These docs describe the release `v0.1.0a5`. **First check** that `v0.1.0a5` is
listed on the [Releases page](https://github.com/cerulean-works/nest/releases), then create
a project and add that release:

```bash
uv init --python 3.14 hello-nest
cd hello-nest
uv add "nest @ git+https://github.com/cerulean-works/nest@v0.1.0a5"
```

`uv.lock` now pins the release's commit, which a tag never changes. Run every
command below from this directory with `uv run`.

**Development build (`@main`), only if `v0.1.0a5` is not listed yet, or if you
develop against the Main deployment.** Until the release is published, its tag
does not exist and the command above fails. Developers and operators install the
development build instead:

```bash
uv add "nest @ git+https://github.com/cerulean-works/nest@main"
```

`@main` moves with every merge, and an SDK built from it can be newer than, and
incompatible with, the Controller of the Release deployment. Once `v0.1.0a5` is
published, ordinary users install the release. See
[Releases and `@main`](installation#releases-and-main).

## 2. Log in

```bash
uv run nest login
```

```text
To sign in to Nest, open this link in a browser:

    https://auth.nest.cerulean.works/auth/cli/approve?code=BCDF-GHJK

and check that it shows the code BCDF-GHJK.
Nest will never ask for your NCHC password or one-time code there.
Waiting for approval...
Signed in to Nest.
```

Open the link on any device, sign in with Google, check that the page shows the
same code, and choose **Approve**. Then check which Project your calls will run
in:

```bash
uv run nest projects current
```

```text
3f0c1a2b-4d5e-4f60-8a7b-9c0d1e2f3a4b (only eligible)
```

`only eligible` means the Controller just listed exactly one Project you may run
in, so Nest uses it: you need no Project ID. `saved` is a choice you made earlier
that the Controller still lists. If the line ends in `(NEST_PROJECT_ID)` instead,
it is your own override, printed without asking the Controller: use
`uv run nest projects list` to see the Projects you may actually run in. Either
way the Controller authorizes every call again when it runs. With several
Projects or none, see [Several Projects, or none](#several-projects-or-none).

## 3. Create main.py

`uv init` created a `main.py`. Replace its contents with:

```python
import nest


@nest.remote(cpu=1)
def compute(x):
    """Returns twice the input value."""
    return x * 2


print(compute.local(21))  # runs here
print(compute.remote(21))  # runs on HPC
```

## 4. Run

```bash
uv run python main.py
```

```text
42
42
```

The first `42` was computed on your machine, the second on the cluster:

- `.local()` (or calling `compute(21)` directly) runs the function in your own
  Python process.
- `.remote()` sends it to the Controller, which checks your session, your
  Project role and the operator's approvals, runs it as a Slurm job on your
  Project's HPC Platform with one CPU (`cpu=1`), and returns the result. It waits
  for the job, up to 900 seconds by default (`NEST_RPC_TIMEOUT`).
- No GPU was requested, so the function **sees** no GPU (`CUDA_VISIBLE_DEVICES=""`,
  no NVIDIA passthrough). That is not the same as allocating none: the default
  worker script asks Slurm for one GPU (`#SBATCH --gpus-per-node=1`), so the job
  normally still **holds** one. Nest's admission reserves that GPU and counts it
  against the Project's GPU limit, and the cluster's own accounting may charge it.
  See
  [Run functions on GPUs](gpus#limitations) and
  [What is reserved](admission-limits#what-is-reserved).

The same program, with its dependency declared inline on the development build
(`@main`), is
[`examples/hosted_remote.py`](https://github.com/cerulean-works/nest/blob/main/examples/hosted_remote.py).

## Several Projects, or none

`uv run nest projects list` shows the Projects you may run in on this Controller.

- **One:** nothing to do; Nest uses it.
- **Several:** the first call in a terminal asks you and remembers the answer, or
  choose ahead of time with `uv run nest projects select`. Scripts, CI and
  notebooks never prompt: set `export NEST_PROJECT_ID="<project-id>"` there.
- **None:** you need an invitation, or the operator has not approved your Project
  yet. A call fails with `ProjectSelectionError` (`none_eligible`) and nothing
  runs. See [Get access to a Project](get-access).

Details: [Choose the Project your calls run in](project-selection).

## If a call is refused

A refused call runs nothing and names the reason, for example
`AuthenticationRequiredError` (log in again, or `NEST_AUTH_URL` is unset in that
terminal) or `RemoteAccessDeniedError` (your role or the Project's approval). See
[Access and onboarding errors](remote-errors#access-and-onboarding-errors).
`uv run nest auth status --check` shows whether your session is accepted, and
[Sign in from the command line](cli-login) explains where it is stored. Never
paste a token, session file or password into a script, a shell history or an
issue.

## Next steps

- [Use project dependencies](project-dependencies) when your function imports
  packages from your uv project.
- [Run functions on GPUs](gpus) with `@nest.remote(gpu=1)`.
- [Handle remote errors](remote-errors) in your application.
- [Manage Projects in the browser](console): members, invitations and the
  *Connect* page at `https://auth.nest.cerulean.works/console`.
- [Remote functions and resources](api/remote) for the full API.

## Use a self-hosted Nest

For a Nest your organization runs, the operator gives you its sign-in origin and
Controller address. Use them instead of the connection settings above:

```bash
unset NEST_PROFILE NEST_CONTROLLER_TLS_CA_FILE NEST_HOSTED_SESSION_ADDRESS NEST_PROJECT_ID
export NEST_AUTH_URL="https://nest.example.com"   # or: nest login --url https://nest.example.com
export NEST_CONTROLLER_ADDRESS="controller.example.com:50051"
export NEST_CONTROLLER_TLS=true                   # false only on a trusted private network
```

For a private CA, also set `NEST_CONTROLLER_TLS_CA_FILE=/path/to/ca.pem`. Then
follow steps 1 to 4. With `NEST_AUTH_URL` set, `nest login`, `nest auth status`,
`nest logout` and `.remote()` all use that Nest, and the Cerulean-hosted sign-in
is never contacted. Without it, a Controller other than the hosted one receives
no session at all. See [Sign-in origin](configuration#sign-in-origin).

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
unset NEST_PROFILE NEST_CONTROLLER_ADDRESS NEST_CONTROLLER_TLS NEST_CONTROLLER_TLS_CA_FILE \
      NEST_AUTH_URL NEST_HOSTED_SESSION_ADDRESS NEST_PROJECT_ID
uv run python main.py
```

The SDK's default address is `127.0.0.1:50051`. This Controller uses
`NEST_CONTROLLER_AUTH=none`: it has no sign-in, no Projects and no authorization,
and runs every call it receives as a subprocess of the Controller, with the
Controller's own Python environment. It is safe only on loopback or another
network where everyone who can connect is trusted with your account. Never expose
it to a shared network. See [Run a local Controller](local-controller).
