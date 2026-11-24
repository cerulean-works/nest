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

### Updated Controller, old image

Deploying a new Controller, or new `scripts/*.sbatch` in the work directory, does
**not** change `images/uv-python314.sif`. A site whose SIF predates the
`hpc-runtime` target keeps an image without Git until the operator replaces the
file. Symptoms:

- the caller sees `Environment build <job> failed: FAILED`;
- `env-<job>.out` in the HPC work directory ends with
  `uv.lock pins Git dependencies, but … has no git executable` and
  `Environment build failed; the runtime was not marked ready.`;
- `sacct -j <job>` shows the build job exited with status 3, which
  `environment-smoke.sbatch` uses only for this check, before uv runs.

PyPI-only lockfiles keep working with such an image, so the failure appears only
for Projects that pin a Git source. Confirm it on the login node:

```bash
apptainer exec --cleanenv images/uv-python314.sif git --version   # fails: no git
sha256sum images/uv-python314.sif
```

Fix it with a new image, never by installing Git on the host or at job time:

1. Build the canonical image from the commit you deploy
   ([Build the image](#build-the-image)): `docker build --target hpc-runtime`
   through `scripts/build-runtime-image.sh`, converted to SIF. This is the
   reproducible path and the one to record in deployment notes. Nest does not yet
   publish this image to a registry (GHCR publication is planned, E03a); build it
   yourself.
2. Verify it under a temporary name: `git`, `uv` and `python` inside the image on
   the login node, then `apptainer-smoke.sbatch` on a compute node
   ([Verify](#verify-the-new-image-before-rollout)).
3. Drain Nest jobs, keep the old file as `uv-python314.sif.previous`, and swap the
   new one in with `mv` on the same file system
   ([Roll out](#roll-out-without-disrupting-jobs)). Back up the old SIF first;
   do not overwrite it in place.
4. No cache needs clearing. The runtime ID is derived from the SIF's SHA-256 and
   the host architecture, so the swapped image's digest selects new runtime
   directories, and each Project's next call rebuilds once. Record the new
   SHA-256.

**Site-specific workaround.** Where Docker is not available to build the
canonical image, an operator may build an image on the cluster with Apptainer
instead, for example by adding Git to the existing image. Whether this works
depends on the site: `apptainer build --fakeroot` needs user-namespace and
subordinate-ID support that not every cluster grants, and some file systems used
for home directories do not support it. On one site such an incremental repair
succeeded when built in node-local `/tmp` after the same build failed in the
shared home directory; the result was then moved into `images/` and verified as
above. Treat an image built this way as a stopgap: it is not reproducible from
the repository, its contents are not described by a manifest, and it should be
replaced by the canonical `hpc-runtime` build. Do not assume other sites allow
fakeroot, or give compute nodes outbound access to package mirrors or
github.com; check with the cluster administrators.

## Validation record (2026-11-25)

Live, hosted M1 Mode A deployment, sanitized: no account names, paths, Project
IDs or credentials are recorded here.

| Check | Result |
| --- | --- |
| Sign-in | Google browser sign-in and CLI approval (`nest login`) against `https://auth.nest.cerulean.works` |
| Authorization | Project membership, operator Platform grant and Slurm account link, and Provider approval (`NEST_PROVIDER_ALLOWED_PROJECTS`) all in place; the call was accepted |
| Transport | `NEST_CONTROLLER_ADDRESS=nest-controller.tail8e5ba1.ts.net:50051` with `NEST_CONTROLLER_TLS=false` over the authorized Tailnet |
| Image | The deployed SIF lacked Git; Git builds failed with exit 3 as described [above](#updated-controller-old-image). It was replaced by a site-specific repaired image (built in node-local `/tmp`), not by the canonical `hpc-runtime` build |
| Result | After the image swap, an authenticated `.remote()` call ran as a Slurm job and returned `[1234567 1234567 1234567]`; the Controller retrieved the result from the cluster |

Not yet verified, so the live acceptance of the Git-enabled runtime (A08) is
**not** complete:

- a cold-cache build job recorded against the new image's SHA-256, with its
  `env-<job>.out`;
- the canonical `hpc-runtime` image deployed in place of the repaired one, with
  its manifest;
- the Execution Registry entries and security audit records for the run;
- negative authorization checks on the live deployment (unapproved Project,
  missing Platform grant, missing account link, Viewer role);
- Controller restart and recovery with jobs in flight.

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
