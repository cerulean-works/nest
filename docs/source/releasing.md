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

## Current state (checked on 2026-11-27)

| Item | Value |
| --- | --- |
| Source package version on `main` | `0.1.0a5`, set by the release-prep pull request for the planned tag `v0.1.0a5` |
| Planned next tag | `v0.1.0a5`. It is published only once it appears on the [Releases page](https://github.com/cerulean-works/nest/releases); creating it is the maintainer's decision (checklist step 6), and it is **blocked** until its [release-specific gates](#release-prep-checklist) have evidence |
| Newest published release tag | `v0.1.0a4`, a pre-release published 2026-11-12, at commit `492055e` (pull request #11) |
| Source package version in every tag `v0.1.0a1` to `v0.1.0a4` | `0.1.0a1`: the tags were created without a version bump |
| `main` relative to `v0.1.0a4` | Every change from pull request #12 on is unreleased, including the `nest` command, sign-in and sessions, Projects and Project selection, PostgreSQL, the Console, the Execution Registry and the public gateway pilot |
| Documentation | `main` is published at [docs.cerulean.works/nest/main/guide/](https://docs.cerulean.works/nest/main/guide/). A `v*` tag publishes its own version of the docs |

**Consequence for users:** the [Quickstart](quickstart) and
[Install Nest](installation) install the release `v0.1.0a5`, once it is listed on
the Releases page. Until then the only install is the development build from
`main`, labeled as such. The existing tags predate session authentication: an SDK
installed from `v0.1.0a4` has no `nest` command and sends no session, so an
authenticated Controller refuses its calls with `UNAUTHENTICATED`.

## How `main` and tagged docs differ

doc-builder builds a tag's docs from the tagged commit's own `docs/source`. The
version (`main` or `v0.1.0a5`) changes only the URL base and, when the build is
given one (`--source_ref`), the Git ref that API source links point at. Links
written into a page, such as `blob/main/examples/...`, are not rewritten.
doc-builder has no conditional content, so a tag's docs
and the `main` docs at the same commit have the same text. The release-prep pull
request therefore writes the release install so that it is true both before and
after the tag:

- The [Quickstart](quickstart) and [Install Nest](installation) first say to
  check that the tag is listed on the
  [Releases page](https://github.com/cerulean-works/nest/releases), then give the
  pinned `@v0.1.0a5` command as the ordinary install. A reader of the tag's immutable
  docs therefore gets the released SDK, never a later, possibly incompatible
  `main`.
- The development build (`@main`) follows in a separately labeled fallback: only
  while the tag is not published, or for the Main deployment. Choosing the Main
  deployment's endpoint stays an explicit setting.
- Values known only after the merge (the final `main` commit and the HPC
  runtime image digest) are not written into `docs/source`. They go into the
  GitHub Release text, drafted in
  [`docs/release-notes/`](https://github.com/cerulean-works/nest/tree/main/docs/release-notes).

`tests/test_docs_release.py` checks that the package version, `uv.lock`, the
pinned commands and the "published" condition agree. To compare the two
renderings, build both versions locally: their rendered page text is the same,
and only the `/nest/<version>/` link base differs:

```bash
.venv/bin/doc-builder build nest docs/source --version main --build_dir build/main --html
.venv/bin/doc-builder build nest docs/source --version v0.1.0a5 --build_dir build/tag --html
```

After the tag, a follow-up pull request to `main` updates the *Current state*
table above (the newest published tag). The tag's own docs keep the conditional
wording, which stays true.

## What a `v*` tag triggers

| Workflow or system | On a tag push |
| --- | --- |
| `Docs` (`.github/workflows/docs.yml`) | Builds and publishes a separate docs version for the tag |
| `HPC runtime image` (`.github/workflows/hpc-runtime-image.yml`) | Adds the tag to the digest `main` already published for that commit; fails if the commit is not on `main` or its `sha-` image is missing (see [Published images](runtime-image#published-images)) |
| Dokploy (configured outside this repository) | The Release application, if the operator configured it to follow tags. Under #118 it is a separate Compose application from the Main deployment, which follows `main`. Verify the actual trigger behavior; treat the tag as a production rollout decision |
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

**Release-specific gates.** The maintainer can set additional gates for a
release. They are listed in that release's draft notes in `docs/release-notes/`,
and each one needs evidence before step 6. While any of them lacks evidence, the
release is **blocked**, not ready, whatever CI says.

For `v0.1.0a5` (maintainer decision, 2026-11-28, #116 and #96), the tag waits for
all of these:

- the canonical Console and sign-in origin `https://nest.cerulean.works` (#96) is
  merged, and the candidate to tag contains it, including its docs;
- the exact reviewed candidate is verified on a separately approved pre-tag
  staging or canary deployment. No tag is pushed to bootstrap this;
- production TLS and the gateway at `grpc.nest.cerulean.works:443` are verified
  against system roots;
- a clean client with **no `NEST_*` settings and no Tailnet** passes on the
  production endpoint: `nest login` at the new origin, automatic selection of the
  sole eligible Project, `42` locally and remotely, the matching audit records,
  and no unauthorized work;
- H09 is explicitly approved (#90), including the reviewed switch of
  `nest.config.HOSTED_PROFILE_DEFAULT`;
- the release docs are re-checked after #96, so that the tagged Quickstart and
  Install pages describe the zero-env hosted path as the canonical path;
- Main and Release run as independent deployments on the shared host (#118),
  reviewed and verified by the operator: distinct edge gateway aliases and
  Traefik router, service and transport names, separate routes and certificates
  (`main.nest.cerulean.works` and `grpc.main.nest.cerulean.works` versus
  `nest.cerulean.works` and `grpc.nest.cerulean.works`), credentials, PostgreSQL
  databases and volumes, Tailnet identity, and HPC workdirs and runtime state; an
  operator-approved aggregate capacity policy if both use the same HPC account;
  and a redeploy of one that leaves the other's routes and state untouched.

**The tag.**

6. **Pushes the tag** on the commit checked in step 3, and publishes the GitHub
   Release as a pre-release.

**After the tag.**

7. **Verifies the published release:** the tag's docs version exists and its
   Install page shows the pinned command for this tag; a fresh
   `uv add "nest @ git+https://github.com/cerulean-works/nest@<tag>"` installs
   the package version that matches the tag, and `uv run nest --help` works; the runtime image tag points
   at the reviewed digest; and the operator confirms the deployed checkout SHA, a
   healthy Controller and a migration success.
8. **Fixes forward, never by moving the tag.** If the tagged docs or release are
   wrong, publish a corrected follow-up tag through the same steps; published tags
   and image tags are not rewritten.

Pushing a tag does not switch on the hosted profile default
(`nest.config.HOSTED_PROFILE_DEFAULT`), change DNS or open the public gateway:
those are separate, reviewed H09 actions in the [rollout runbook](hosted-rollout).
When a release depends on them, as `v0.1.0a5` does, they are completed and
verified **before** the tag, as release-specific gates.
