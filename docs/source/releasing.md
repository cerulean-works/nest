# Release status and checklist

This page records what is true for `main` versus a tagged Early Access release, and
what a maintainer checks before creating the next pre-release tag. Creating a tag,
changing the package version, publishing a release and deploying are explicit
maintainer and operator actions; nothing in this repository does them on its own
initiative.

## Four kinds of version

| Kind | Where it lives | What it tells you |
| --- | --- | --- |
| **Source package version** | `version` in `pyproject.toml` (and the `nest` entry of `uv.lock`) | The version string Python reports. It is changed only by a reviewed commit, so many commits share it |
| **Release tag** | A `v*` Git tag and its GitHub Release | An immutable commit that a maintainer chose to publish |
| **Commit** | The SHA pinned in a user's `uv.lock`, or the Controller's checkout | What is actually installed or running. Compare commits, not version strings |
| **Deployed revision** | The commit, schema and HPC runtime digest of a running Controller | Ask the operator; `nest-db status` shows the schema and [the runtime image page](runtime-image) shows the digest |

## Current state (checked on 2026-11-26)

| Item | Value |
| --- | --- |
| Source package version on `main` | `0.1.0a1` |
| Newest release tag | `v0.1.0a4`, a pre-release published 2026-11-12, at commit `492055e` (pull request #11) |
| Source package version in every tag `v0.1.0a1` to `v0.1.0a4` | `0.1.0a1`: the tags were created without a version bump |
| `main` relative to `v0.1.0a4` | Every change from pull request #12 on is unreleased, including the `nest` command, sign-in and sessions, Projects and Project selection, PostgreSQL, the Console, the Execution Registry and the public gateway pilot |
| Documentation | `main` is published at [docs.cerulean.works/nest/main/guide/](https://docs.cerulean.works/nest/main/guide/). A `v*` tag publishes its own version of the docs |

**Consequence for users:** install from `main`, as the [Quickstart](quickstart)
does. The existing tags predate session authentication: an SDK installed from
`v0.1.0a4` has no `nest` command and sends no session, so an authenticated
Controller refuses its calls with `UNAUTHENTICATED`. Do not document a
release-pinned install until a tag that contains the authenticated SDK exists.

## What a `v*` tag triggers

| Workflow or system | On a tag push |
| --- | --- |
| `Docs` (`.github/workflows/docs.yml`) | Builds and publishes a separate docs version for the tag |
| `HPC runtime image` (`.github/workflows/hpc-runtime-image.yml`) | Adds the tag to the digest `main` already published for that commit; fails if the commit is not on `main` or its `sha-` image is missing (see [Published images](runtime-image#published-images)) |
| Dokploy (configured outside this repository) | The operator's Compose deployment that follows tags. Treat the tag as a production rollout decision |
| `Tests` (`.github/workflows/tests.yml`) | Runs the test suite again on the tagged commit |
| `Controller container`, `Code Quality` | Not triggered by tags; they run on `main` and pull requests |

## Release-prep checklist

Before tagging, the maintainer:

1. **Chooses the tag.** The next pre-release follows `v0.1.0a4`; the exact name
   is the maintainer's decision. Confirm it does not already exist.
2. **Decides the version metadata**, in a reviewed pull request merged before the
   tag (the changes below are not made by this page):
   - `version` in `pyproject.toml`, so that the package reports the tag's
     version (PEP 440: tag `v0.1.0aN` matches version `0.1.0aN`); run `uv lock`
     so the `nest` entry in `uv.lock` matches;
   - the version badge and the Early Access line in `README.md`;
   - the example `uv.lock` entry and `uv lock --upgrade-package nest` output in
     [Install Nest](installation), which show the current version string.
3. **Checks `main` at the commit to tag:** all `main` push workflows succeeded
   (Tests, Code Quality, Controller container, HPC runtime image, Docs), and the
   HPC runtime image's `sha-<commit>` tag is published.
4. **Writes the release notes:** the changes since the last tag, the minimum
   Controller schema, the HPC runtime image digest, and the compatibility rule
   *SDK at or after the Controller's commit*. Mark Early Access, and state which
   live acceptance (for example H09) is still pending.
5. **Runs the deployment preflight** in
   [Preflight and recover a Dokploy deployment](deployment-recovery) and has the
   operator's approval for the tag-triggered rollout.
6. **Tags and publishes the GitHub Release** as a pre-release.
7. **Verifies after the tag:** the tag's docs version is published, the runtime
   image tag points at the reviewed digest, and the operator confirms the deployed
   checkout SHA and a healthy Controller.
8. **Updates the install docs** with the release-pinned command for that tag, for
   example `uv add "nest @ git+https://github.com/cerulean-works/nest@<tag>"`,
   keeping `@main` as the development and Early Access path.

A release does not switch on the hosted profile default
(`nest.config.HOSTED_PROFILE_DEFAULT`), change DNS or open the public gateway:
those are separate, reviewed H09 actions in the [rollout runbook](hosted-rollout).
