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
Controller refuses its calls with `UNAUTHENTICATED`. A release-pinned install is
added only by the release-prep pull request for an approved target tag that
contains the authenticated SDK (checklist step 2).

## What a `v*` tag triggers

| Workflow or system | On a tag push |
| --- | --- |
| `Docs` (`.github/workflows/docs.yml`) | Builds and publishes a separate docs version for the tag |
| `HPC runtime image` (`.github/workflows/hpc-runtime-image.yml`) | Adds the tag to the digest `main` already published for that commit; fails if the commit is not on `main` or its `sha-` image is missing (see [Published images](runtime-image#published-images)) |
| Dokploy (configured outside this repository) | The operator's Compose deployment that follows tags. Treat the tag as a production rollout decision |
| `Tests` (`.github/workflows/tests.yml`) | Runs the test suite again on the tagged commit |
| `Controller container`, `Code Quality` | Not triggered by tags; they run on `main` and pull requests |

## Release-prep checklist

A tag publishes the tagged commit's **own, immutable** documentation, and pushing
it also starts the operator's Dokploy deployment. Everything the tagged release
should say, including its pinned install command, must therefore be on `main`
**before** the tag: a later commit to `main` cannot change the tag's docs. The
order below follows from that.

**Before the tag (release-prep pull request).** The maintainer:

1. **Chooses the target tag.** The next pre-release follows `v0.1.0a4`; the exact
   name is the maintainer's decision. Confirm it does not exist yet. Until it is
   published, the docs call it planned and never tell users to install it.
2. **Merges a reviewed release-prep pull request** that names the approved target
   (nothing on this page makes these changes):
   - `version` in `pyproject.toml`, so that the package reports the tag's version
     (PEP 440: tag `v0.1.0aN` matches version `0.1.0aN`), and `uv lock` so the
     `nest` entry in `uv.lock` matches;
   - the Early Access paragraph in `README.md`. The README release badge reads the
     newest GitHub pre-release by itself: it shows `v0.1.0a4` until the new tag is
     published, then the new tag, with no edit;
   - [Install Nest](installation): the release-pinned command for the target,
     `uv add "nest @ git+https://github.com/cerulean-works/nest@<target-tag>"`, as
     the supported release install; the *Releases and `@main`* section; and the
     example `uv.lock` entry and upgrade output that show the version string;
   - the [Quickstart](quickstart) install step, if the tagged docs should install
     the release rather than `main`, and this page's *Current state* table;
   - `@main` stays documented as the development and Early Access path.
3. **Checks `main` at the commit to tag:** all `main` push workflows succeeded on
   that exact commit (Tests, Code Quality, Controller container, HPC runtime
   image, Docs), and the HPC runtime image's `sha-<commit>` tag is published.
4. **Writes the release notes:** the changes since the last tag, the minimum
   Controller schema, the HPC runtime image digest, and the compatibility rule
   *SDK at or after the Controller's commit*. Mark Early Access, and state which
   live acceptance (for example H09) is still pending.
5. **Runs the deployment preflight** in
   [Preflight and recover a Dokploy deployment](deployment-recovery) and records
   the operator's explicit approval of the rollout. Pushing the tag **is** the
   rollout trigger, so this approval comes before step 6, never after.

**The tag.**

6. **Pushes the tag** on the commit checked in step 3, and publishes the GitHub
   Release as a pre-release.

**After the tag.**

7. **Verifies the published release:** the tag's docs version exists and its
   Install page shows the pinned command for this tag; the runtime image tag points
   at the reviewed digest; and the operator confirms the deployed checkout SHA, a
   healthy Controller and a migration success.
8. **Fixes forward, never by moving the tag.** If the tagged docs or release are
   wrong, publish a corrected follow-up tag through the same steps; published tags
   and image tags are not rewritten.

A release does not switch on the hosted profile default
(`nest.config.HOSTED_PROFILE_DEFAULT`), change DNS or open the public gateway:
those are separate, reviewed H09 actions in the [rollout runbook](hosted-rollout).
