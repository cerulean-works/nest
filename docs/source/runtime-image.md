# Build and roll out the HPC runtime image

This page is for hosts who operate a Nest Controller. It covers the Apptainer
image at `images/uv-python314.sif` in the [HPC work directory](deploy#hpc-work-directory):
what it contains, how to build and verify it, and how to replace it safely.

## What the image contains

Every Slurm job runs in this image. `scripts/environment-smoke.sbatch` uses it to
run `uv sync --frozen --no-dev --no-install-project` for a user's `uv.lock`, and
`scripts/worker.sbatch` uses it to run the deployed Nest runtime with `uv run`.

The image is the `hpc-runtime` target of the repository's `Dockerfile`, next to
the Controller image and built from the same base and uv:

| Component | Source | Why |
| --- | --- | --- |
| Python 3.14 | `python:3.14-slim-bookworm` | Interpreter for Nest and user environments |
| uv 0.13.1 | `ghcr.io/astral-sh/uv:0.13.1` | Builds and runs the frozen environments |
| Git | Debian `git` | uv clones Git dependencies with the `git` executable |
| CA certificates | Debian `ca-certificates` | Git verifies HTTPS servers |
| OpenSSH client | Debian `openssh-client` | `ssh://` Git URLs |

An image without Git can install PyPI-only lockfiles but fails on a cold cache as
soon as `uv.lock` pins a Git source, such as
`six @ git+https://github.com/benjaminp/six@<commit>`. uv then reports
`Git executable not found`. The build script now detects this case before
running uv; see [Diagnostics](#diagnostics).

## Build the image

On a workstation or CI host with Docker, from a clean checkout of the commit you
intend to deploy:

```bash
bash scripts/build-runtime-image.sh build/uv-python314.sif
```

The script builds `docker build --target hpc-runtime`, prints and checks the
Python, uv, Git and SSH versions inside the image, saves a Docker archive, and
writes `build/uv-python314.manifest` with the commit, image ID, base image
digest, versions, and archive checksum. Set `NEST_RUNTIME_PLATFORM` if the
compute nodes are not `linux/amd64`.

If Apptainer is installed on the build host, the script also writes the SIF and
its SHA-256. Otherwise, copy the archive to the login node and convert it there.
Converting an archive needs neither root nor `--fakeroot`:

```bash
scp build/uv-python314.docker.tar <login-node>:<workdir>/images/
ssh <login-node>
cd <workdir>/images
apptainer build uv-python314.sif.new docker-archive://uv-python314.docker.tar
sha256sum uv-python314.sif.new
```

Keep the manifest and the SIF's SHA-256 with your deployment notes. The SHA-256 is
the image half of every environment's runtime ID, so it identifies which image
built which cached environment.

## Verify the new image before rollout

Build the SIF under a temporary name, such as `uv-python314.sif.new`, and verify it
before it replaces the deployed image. On the login node:

```bash
apptainer exec --cleanenv images/uv-python314.sif.new git --version
apptainer exec --cleanenv images/uv-python314.sif.new uv --version
apptainer exec --cleanenv images/uv-python314.sif.new python --version
```

Then check it on a compute node, where jobs actually run. Adjust the partition
and account lines in the script for your cluster:

```bash
sbatch project/scripts/apptainer-smoke.sbatch "$PWD" "$PWD/images/uv-python314.sif.new"
```

The job prints the image's SHA-256 and checks Python, uv, Git, the SSH client and
the CA bundle. To test a Git dependency end to end, stage a project that pins one,
such as `tests/fixtures/git-dependency`, and call it with `project=` after the
rollout below. Its build log, `env-<job>.out` in the work directory, should show
`+ six==1.17.0 (from git+https://github.com/benjaminp/six@...)`.

## Roll out without disrupting jobs

The Controller and the batch scripts identify a cached environment by the SHA-256
of `images/uv-python314.sif` and the host architecture. Replacing the image
therefore never reuses an environment built by the old image: each project's
next call rebuilds its environment under a new runtime directory, and the old
runtime directories are left in place.

1. Drain Nest jobs. Wait until the Nest account has no queued or running
   `nest-worker` or `nest-env-test` jobs (`squeue --me`), or announce a short
   window. A worker that was submitted against the old runtime but starts after
   the swap computes the new runtime ID, finds no ready environment, and fails
   with `Environment is not ready`; its caller can retry.
2. Keep the current image for rollback, then swap the new one in with a rename on
   the same file system. Never copy over the deployed file in place; a rename
   leaves running containers on the old file.

   ```bash
   cd <workdir>/images
   cp -p uv-python314.sif uv-python314.sif.previous
   mv uv-python314.sif.new uv-python314.sif
   sha256sum uv-python314.sif
   ```

3. Run a call with `project=` from a trusted client. The first call for each
   project is a cache miss and takes longer while its environment rebuilds.
4. To roll back, rename `uv-python314.sif.previous` back to `uv-python314.sif`.
   The old runtime directories are still ready, so calls hit their caches again.

Old runtime directories under `environments/*/runtimes/` are not removed
automatically. Delete them only when no job uses them.

## Git dependencies: supported scope

The frozen build installs **public** Git dependencies over HTTPS that compute
nodes can reach, exactly as pinned in `uv.lock`. The user's project, the host, and
the cluster need no changes, root access, or runtime package installation.

Each build runs Git with no operator credentials:

- `HOME` is an empty per-build directory, removed after the build, so `~/.netrc`,
  `~/.gitconfig`, `~/.git-credentials` and uv's credential store are not read.
  uv keeps its usual cache and Python installations under the real home directory.
- Git ignores system and global configuration and never prompts
  (`GIT_TERMINAL_PROMPT=0`).
- SSH reads no `ssh_config` and offers no keys, agent, host-based or Kerberos
  authentication. `StrictHostKeyChecking=yes` and `BatchMode=yes` stay set.
- `apptainer exec --cleanenv` already drops the job's environment, so variables
  such as `GIT_SSL_NO_VERIFY` cannot disable TLS verification.

The Controller's SSH key stays on the Controller. Managed SSH profiles always set
`ForwardAgent=no`; with a file-based SSH configuration, keep `ForwardAgent` unset
or `no` for the login node. Even a forwarded agent would not reach a build, as the
build unsets `SSH_AUTH_SOCK` and uses `IdentityAgent=none`.

These limits are deliberate. Two related needs are separate concerns and are not
bypassed:

- **Private Git repositories.** They need a credential the user, not the
  operator, controls, for example a read-only deploy token scoped to one
  repository. Nest has no mechanism yet to deliver such a credential to a build.
  A private dependency fails with `could not read Username` or
  `Permission denied (publickey)`, and the build log says that builds use no
  operator credentials.
- **Restricted compute-node networks.** Some clusters block outbound access from
  compute nodes or require a proxy. `--cleanenv` drops proxy variables from the
  job, so such a cluster needs an approved network path, an image-defined proxy,
  or a mirror, agreed with the cluster administrators. Builds then fail with a
  connection or name resolution error.

## Diagnostics

The build log `env-<job>.out` is in the HPC work directory. The Controller reports
`Environment build <job> failed: FAILED` to the caller, and the runtime is never
marked ready after a failed build. A partial `.venv` is removed.

| Message | Cause | Fix |
| --- | --- | --- |
| `uv.lock pins Git dependencies, but … has no git executable` | The deployed SIF predates the `hpc-runtime` target | Build and roll out the image as above |
| `uv sync failed with exit status N` followed by a Git hint | uv could not fetch a Git dependency: no route, DNS, TLS, or a private repository | Check the error above the hint, then the [supported scope](#git-dependencies-supported-scope) |
| `uv sync failed with exit status N` with no Git hint | Resolution or download from a package index failed | Read uv's error above it |

uv and Git output is filtered before it reaches the log: credentials embedded in
URLs (`https://user:secret@host`) and GitHub tokens are replaced with `***`.

## Validation record (2026-11-24)

Recorded against `main` at `39635e4` for issue #62.

| Check | Result | Where |
| --- | --- | --- |
| Original failure reproduced | `uv lock` of the Git fixture in `python:3.14-slim-bookworm` with uv 0.13.1 and no Git failed with `Git executable not found` | Local Docker |
| Image without Git is diagnosed | `EXPECT=no-git bash tests/hpc_runtime_flow.sh` in that image exited 3 with the Git diagnostic; no runtime was marked ready | Local Docker |
| Build and worker scripts with real uv and Git | `tests/hpc_runtime_flow.sh` passed with uv 0.13.1, Git 2.43.0 and CPython 3.14.8, using the `apptainer exec` emulator: cold build from empty caches installed `iniconfig==2.1.0` and `six==1.17.0` from commit `ebd9b3af…`; `worker.sbatch` imported both in the user environment; a no-project call returned its result; a second build hit the cache; a changed image rebuilt under a new runtime ID and left the old one ready; an unreachable Git source failed with the hint, no credentials in the log, and no ready runtime | Local host, not the image |
| Fake-cluster regressions | `tests/test_runtime_e2e.py`: credential-isolated Git environment, missing-Git diagnostic, PyPI-only builds without Git, sanitized failure output, image change invalidation | `pytest`, every pull request |
| `hpc-runtime` image build and in-image flow | `tests/smoke_hpc_runtime.sh` | CI (`Controller container` workflow); the local sandbox could not reach Debian mirrors |

The local base image digest was
`python@sha256:b3239c3decf32ac32957604130bb546bcd5a4c8952070ed02ebfa2dd5cc1114b`
(amd64). No SIF was built or deployed for this record.

Not verified: the contents of the SIF currently deployed on nano4, a real
Apptainer conversion and `apptainer exec`, a Slurm build on a compute node, and
compute-node network access to github.com on nano4 or other platforms. Before
relying on Git dependencies on a platform, run the verification and rollout steps
above there and record the SIF's SHA-256 and the result.
