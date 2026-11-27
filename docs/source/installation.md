# Install Nest

Nest requires **Python 3.14 or later**. It is not published to PyPI yet: install
it from the GitHub repository. The recommended way is to add it to a
[uv](https://docs.astral.sh/uv/) project, which pins the exact commit in
`uv.lock`.

## Install with uv

These docs describe the release `v0.1.0a5`, the first pre-release that includes
sign-in. **First check** that `v0.1.0a5` is listed on the
[Releases page](https://github.com/cerulean-works/nest/releases): until it is
published there, it is only *planned*, the tag does not exist and the command
below fails; see [Releases and `@main`](#releases-and-main) for what to install
until then. In a new or existing uv project:

```bash
uv init --python 3.14 my-project   # only for a new project
cd my-project
uv add "nest @ git+https://github.com/cerulean-works/nest@v0.1.0a5"
```

`uv add` records the dependency in `pyproject.toml` and the resolved commit in
`uv.lock`:

```toml
# pyproject.toml
[tool.uv.sources]
nest = { git = "https://github.com/cerulean-works/nest", rev = "v0.1.0a5" }
```

```toml
# uv.lock
[[package]]
name = "nest"
version = "0.1.0a5"
source = { git = "https://github.com/cerulean-works/nest?rev=v0.1.0a5#<commit>" }
```

Run Nest and its `nest` command through the project environment:

```bash
uv run nest --help
uv run python main.py
```

A tag never moves, so `uv lock --upgrade-package nest` keeps a tag pin on the same
commit. To move to a later release, change `@v0.1.0a5` in `pyproject.toml`. The
release still has to be compatible with the Controller you call: keep the SDK at
or after the commit the Controller was deployed from (see
[below](#sdk-controller-and-hpc-runtime-versions)).

### Releases and `@main`

**Release `v0.1.0a5`** (above) is what an ordinary user of the Release deployment
installs, once it is listed on the
[Releases page](https://github.com/cerulean-works/nest/releases).

**Development build (`@main`).** `main` always exists and gets every fix first,
but it moves with every merge. It is for developers and operators of the Main
deployment, and the only install while `v0.1.0a5` is not published:

```bash
uv add "nest @ git+https://github.com/cerulean-works/nest@main"
```

```toml
# pyproject.toml
[tool.uv.sources]
nest = { git = "https://github.com/cerulean-works/nest", rev = "main" }
```

```toml
# uv.lock
[[package]]
name = "nest"
version = "0.1.0a5"
source = { git = "https://github.com/cerulean-works/nest?rev=main#<commit>" }
```

An SDK from `@main` can be newer than, and incompatible with, the Controller of
the Release deployment, so do not use it there once the release is published.
Choosing the Main deployment's endpoint stays an explicit setting. To pin a
specific commit, replace `@main` with `@<commit>`.

**Older tags.** `v0.1.0a1` to `v0.1.0a4` predate sign-in: an SDK installed from one
of them has no `nest` command and sends no session, so an authenticated
Controller refuses it. They also all report version `0.1.0a1`. Do not use them
with a shared Controller. See [Release status and checklist](releasing).

### Install with pip

Without uv, install into a Python 3.14 virtual environment, again once
`v0.1.0a5` is listed on the [Releases page](https://github.com/cerulean-works/nest/releases):

```bash
pip install "git+https://github.com/cerulean-works/nest.git@v0.1.0a5"
```

The development build is `pip install "git+https://github.com/cerulean-works/nest.git@main"`,
and `pip install --upgrade --force-reinstall "git+https://github.com/cerulean-works/nest.git@main"`
updates it.

## Update the SDK

With a release pin, update by changing the tag in `pyproject.toml` (see
[Install with uv](#install-with-uv)). With the development build,
`rev = "main"` names a branch, but `uv.lock` pins the commit that `main` pointed
to when the lock was last resolved. `uv sync` and `uv run` keep installing that
commit, however much `main` has moved since. Refresh the pin explicitly:

```bash
uv lock --upgrade-package nest
uv sync
```

```text
Updated nest v0.1.0a5 (2b2b5f19) -> v0.1.0a5 (cd756014)
```

The version string changes only when a maintainer bumps it, so many commits of
`main` report the same version (`0.1.0a5` in the example). Compare commits, not
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
