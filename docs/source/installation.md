# Install Nest

Nest requires **Python 3.14 or later**. It is not published to PyPI yet: install
it from the GitHub repository. The recommended way is to add it to a
[uv](https://docs.astral.sh/uv/) project, which pins the exact commit in
`uv.lock`.

## Install with uv

In a new or existing uv project:

```bash
uv init --python 3.14 my-project   # only for a new project
cd my-project
uv add "nest @ git+https://github.com/cerulean-works/nest@main"
```

`uv add` records the dependency in `pyproject.toml` and the resolved commit in
`uv.lock`:

```toml
# pyproject.toml
[tool.uv.sources]
nest = { git = "https://github.com/cerulean-works/nest", rev = "main" }
```

```toml
# uv.lock
[[package]]
name = "nest"
version = "0.1.0a1"
source = { git = "https://github.com/cerulean-works/nest?rev=main#<commit>" }
```

Run Nest and its `nest` command through the project environment:

```bash
uv run nest --help
uv run python main.py
```

To pin a specific commit, replace `@main` with `@<commit>`.

### Releases and `@main`

Installing from `main` is the development and Early Access path, and the one the
[Quickstart](quickstart) uses. The existing release tags (`v0.1.0a1` to `v0.1.0a4`)
predate sign-in: an SDK installed from one of them has no `nest` command and sends
no session, so an authenticated Controller refuses it. The release-prep pull
request for the next tag adds its pinned command (`@<tag>`) here before the tag is
pushed, so that the tag's own docs show it. See
[Release status and checklist](releasing).

### Install with pip

Without uv, install into a Python 3.14 virtual environment:

```bash
pip install "git+https://github.com/cerulean-works/nest.git@main"
```

`pip install --upgrade --force-reinstall "git+https://github.com/cerulean-works/nest.git@main"`
updates it.

## Update the SDK

`rev = "main"` names a branch, but `uv.lock` pins the commit that `main` pointed
to when the lock was last resolved. `uv sync` and `uv run` keep installing that
commit, however much `main` has moved since. Refresh the pin explicitly:

```bash
uv lock --upgrade-package nest
uv sync
```

```text
Updated nest v0.1.0a1 (2b2b5f19) -> v0.1.0a1 (cd756014)
```

The version string changes only when a maintainer bumps it, so many commits of
`main` report the same version (`0.1.0a1` in the example). Compare commits, not
versions. Show the commit your environment runs:

```bash
grep -A2 '^name = "nest"' uv.lock
uv run python -c "import importlib.metadata as m, json; print(json.loads(m.distribution('nest').read_text('direct_url.json'))['vcs_info']['commit_id'])"
```

Update when your operator announces a Controller upgrade, and whenever a call is
refused in a way the [troubleshooting table](remote-errors#access-and-onboarding-errors)
does not explain. An SDK older than Nest's session authentication has no `nest`
command (`uv run nest --help` fails) and sends only `x-nest-request-id` with each
call, so an authenticated Controller answers `UNAUTHENTICATED` even after you sign
in with a newer `nest` installed elsewhere.

## SDK, Controller and HPC runtime versions

Three separately deployed pieces must be compatible:

| Piece | Who updates it | How to see its version |
| --- | --- | --- |
| **SDK** (`nest` package in your environment, including the `nest` command) | You, with `uv lock --upgrade-package nest` | The commit in `uv.lock`, as above |
| **Controller** (the gRPC service, sign-in listener and database schema) | The operator, by redeploying the Controller and running `nest-db upgrade` | Ask the operator; `nest-db status` shows the schema |
| **HPC runtime** (`NEST_SLURM_WORKDIR/project`, its Slurm scripts and `images/uv-python314.sif`) | The operator, on the cluster | [Build and roll out the HPC runtime image](runtime-image) |

Keep your SDK at or after the commit the Controller was deployed from. Your own
`uv.lock` only describes the environment on your machine and, with
`project=`, the dependencies installed for your function on HPC; it never
updates the Controller or the HPC runtime.

## Next steps

- [Quickstart](quickstart): log in, write `main.py` and run it.
- [Run a local Controller](local-controller) to develop without a cluster or a
  sign-in.
- Hosts running their own Controller also need the
  [deployment prerequisites](deploy).
