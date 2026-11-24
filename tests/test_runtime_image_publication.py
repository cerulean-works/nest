"""Checks for the HPC runtime image publication path (E03a).

Covers `scripts/runtime_image_manifest.py` against synthetic OCI archives,
`scripts/publish-runtime-tag.sh` against a fake `skopeo`, and the trust
boundaries of `.github/workflows/hpc-runtime-image.yml`: pull requests build and
test with a read-only token, and only push events on `main` or release tags get
package-write credentials. No registry or cluster is contacted.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCRIPT = ROOT / "scripts" / "runtime_image_manifest.py"
TAG_SCRIPT = ROOT / "scripts" / "publish-runtime-tag.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "hpc-runtime-image.yml"
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def load_manifest_module():
    spec = importlib.util.spec_from_file_location("runtime_image_manifest", MANIFEST_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def section(pattern: str, text: str) -> str:
    match = re.search(pattern, text)
    assert match is not None, f"{pattern!r} not found"
    return match.group(1)


def blob(blobs: dict, content: dict, media_type: str, **extra) -> dict:
    data = json.dumps(content).encode()
    digest = f"sha256:{hashlib.sha256(data).hexdigest()}"
    blobs[digest] = data
    return {"mediaType": media_type, "digest": digest, "size": len(data), **extra}


def write_archive(
    path: Path,
    *,
    architecture="amd64",
    revision=COMMIT,
    attestations=("https://spdx.dev/Document", "https://slsa.dev/provenance/v1"),
    corrupt=False,
) -> str:
    blobs = {}
    config = blob(
        blobs,
        {
            "architecture": architecture,
            "os": "linux",
            "config": {"Labels": {"org.opencontainers.image.revision": revision}},
        },
        "application/vnd.oci.image.config.v1+json",
    )
    image = blob(
        blobs,
        {"schemaVersion": 2, "config": config, "layers": [config]},
        "application/vnd.oci.image.manifest.v1+json",
        platform={"architecture": architecture, "os": "linux"},
    )
    manifests = [image]
    if attestations:
        layers = [
            {**config, "annotations": {"in-toto.io/predicate-type": kind}} for kind in attestations
        ]
        manifests.append(
            blob(
                blobs,
                {"schemaVersion": 2, "config": config, "layers": layers},
                "application/vnd.oci.image.manifest.v1+json",
                platform={"architecture": "unknown", "os": "unknown"},
                annotations={
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": image["digest"],
                },
            )
        )
    index = blob(
        blobs,
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": manifests,
        },
        "application/vnd.oci.image.index.v1+json",
    )
    if corrupt:
        blobs[image["digest"]] = b'{"tampered": true}'
    files = {"oci-layout": b'{"imageLayoutVersion": "1.0.0"}'}
    files["index.json"] = json.dumps({"schemaVersion": 2, "manifests": [index]}).encode()
    for digest, data in blobs.items():
        files[f"blobs/sha256/{digest.removeprefix('sha256:')}"] = data
    with tarfile.open(path, "w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return index["digest"]


class RuntimeImageManifestTests(unittest.TestCase):
    def setUp(self):
        self.module = load_manifest_module()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.archive = self.tmp / "image.oci.tar"
        self.output = self.tmp / "manifest.json"

    def run_main(self, *args: str) -> int:
        with (
            open(os.devnull, "w") as devnull,
            redirect_stdout(devnull),
            redirect_stderr(devnull),
        ):
            return self.module.main([str(self.archive), "--output", str(self.output), *args])

    def test_records_published_digest_platform_source_and_bases(self):
        digest = write_archive(self.archive)
        base = "python:3.14-slim-bookworm@sha256:" + "a" * 64
        summary = self.tmp / "summary.md"
        code = self.run_main(
            "--commit", COMMIT, "--base", base, "--require-attestations", "--summary", str(summary)
        )
        self.assertEqual(code, 0)
        manifest = json.loads(self.output.read_text())
        self.assertEqual(manifest["digest"], digest)
        self.assertEqual(manifest["platform"], "linux/amd64")
        self.assertNotEqual(manifest["platform_manifest"], digest)
        self.assertEqual(manifest["source"]["commit"], COMMIT)
        self.assertEqual(manifest["compatibility"]["controller_commit"], COMMIT)
        self.assertEqual(
            manifest["base_images"], {"python:3.14-slim-bookworm": "sha256:" + "a" * 64}
        )
        self.assertTrue(manifest["attestations"]["sbom"])
        self.assertTrue(manifest["attestations"]["provenance"])
        self.assertIn(digest, summary.read_text())
        self.assertIn("SIF", summary.read_text())

    def test_rejects_a_revision_that_is_not_the_commit(self):
        write_archive(self.archive, revision="f" * 40)
        self.assertEqual(self.run_main("--commit", COMMIT), 1)
        self.assertFalse(self.output.exists())

    def test_rejects_an_archive_without_the_compute_node_platform(self):
        write_archive(self.archive, architecture="arm64")
        self.assertEqual(self.run_main(), 1)

    def test_requires_sbom_and_provenance_when_asked(self):
        write_archive(self.archive, attestations=("https://slsa.dev/provenance/v1",))
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.run_main("--require-attestations"), 1)

    def test_rejects_a_blob_that_does_not_match_its_digest(self):
        write_archive(self.archive, corrupt=True)
        self.assertEqual(self.run_main(), 1)

    def test_rejects_a_base_without_a_digest(self):
        write_archive(self.archive)
        self.assertEqual(self.run_main("--base", "python:3.14-slim-bookworm"), 1)


FAKE_SKOPEO = r"""#!/bin/bash
# Fake skopeo: a registry is a directory of files named after references.
set -euo pipefail
key() { printf '%s' "${1#docker://}" | tr '/:@' '___'; }
case "$1" in
    inspect)
        ref=${@: -1}
        file="$FAKE_REGISTRY/$(key "$ref")"
        if [[ -f "$file" ]]; then cat "$file"; else echo "manifest unknown" >&2; exit 1; fi
        ;;
    copy)
        src=${@: -2:1}
        dest=${@: -1}
        echo "copy $src $dest" >> "$FAKE_REGISTRY/log"
        cp "$FAKE_REGISTRY/$(key "$src")" "$FAKE_REGISTRY/$(key "$dest")"
        ;;
esac
"""


@unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "requires bash")
class PublishRuntimeTagTests(unittest.TestCase):
    repository = "ghcr.io/example/nest-hpc-runtime"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.registry = self.tmp / "registry"
        self.registry.mkdir()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        skopeo = bin_dir / "skopeo"
        skopeo.write_text(FAKE_SKOPEO)
        skopeo.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_REGISTRY": str(self.registry),
        }
        self.digest = self.put(f"{self.repository}@reviewed", b'{"image": "reviewed"}')

    def put(self, ref: str, content: bytes) -> str:
        (self.registry / re.sub(r"[/:@]", "_", ref)).write_bytes(content)
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    def tag(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(TAG_SCRIPT), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def copies(self) -> list[str]:
        log = self.registry / "log"
        return log.read_text().splitlines() if log.exists() else []

    def source(self) -> str:
        return f"docker://{self.repository}@reviewed"

    def test_pushes_a_missing_tag_and_reads_it_back(self):
        result = self.tag(self.source(), self.repository, "sha-abc", self.digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.copies()), 1)
        self.assertIn(f"-> {self.digest}", result.stdout)

    def test_existing_tag_with_the_same_digest_is_left_alone(self):
        self.put(f"{self.repository}:sha-abc", b'{"image": "reviewed"}')
        result = self.tag(self.source(), self.repository, "sha-abc", self.digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.copies(), [])

    def test_never_overwrites_an_immutable_tag(self):
        self.put(f"{self.repository}:sha-abc", b'{"image": "other"}')
        result = self.tag(self.source(), self.repository, "sha-abc", self.digest)
        self.assertEqual(result.returncode, 1)
        self.assertIn("never overwritten", result.stderr)
        self.assertEqual(self.copies(), [])

    def test_moves_a_convenience_tag(self):
        self.put(f"{self.repository}:main", b'{"image": "older"}')
        result = self.tag("--move", self.source(), self.repository, "main", self.digest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.copies()), 1)

    def test_refuses_a_source_that_is_not_the_reviewed_digest(self):
        result = self.tag(self.source(), self.repository, "sha-abc", "sha256:" + "0" * 64)
        self.assertEqual(result.returncode, 1)
        self.assertIn("expected the reviewed digest", result.stderr)
        self.assertEqual(self.copies(), [])

    def test_rejects_a_malformed_digest(self):
        result = self.tag(self.source(), self.repository, "sha-abc", "latest")
        self.assertEqual(result.returncode, 64)


def workflow_jobs(text: str) -> dict[str, str]:
    """Splits a workflow into its top-level jobs, keyed by job ID."""
    body = text.split("\njobs:\n", 1)[1]
    jobs = re.split(r"^  ([A-Za-z0-9_-]+):\n", body, flags=re.MULTILINE)
    return dict(zip(jobs[1::2], jobs[2::2], strict=True))


@unittest.skipUnless(WORKFLOW.is_file(), "requires the repository's workflows")
class RuntimeImageWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.jobs = workflow_jobs(self.text)

    def test_no_workflow_runs_pull_request_code_with_privileges(self):
        for path in WORKFLOWS.glob("*.y*ml"):
            with self.subTest(workflow=path.name):
                self.assertNotIn("pull_request_target", path.read_text())
                self.assertNotIn("workflow_run", path.read_text())

    def test_default_permissions_are_empty(self):
        self.assertRegex(self.text, r"(?m)^permissions: \{\}$")

    def test_build_job_has_a_read_only_token_and_no_secrets(self):
        build = self.jobs["build"]
        permissions = section(r"permissions:\n((?:      .*\n)+)", build)
        self.assertEqual(permissions.split(), ["contents:", "read"])
        self.assertNotIn("secrets.", build)
        self.assertIn("persist-credentials: false", build)

    def test_only_trusted_push_jobs_can_write_packages(self):
        writers = {name for name, job in self.jobs.items() if "packages: write" in job}
        self.assertEqual(writers, {"publish", "promote"})
        for name in writers:
            condition = section(r"if: >-\n((?:      .*\n)+)", self.jobs[name])
            with self.subTest(job=name):
                self.assertIn("github.event_name == 'push'", condition)
                self.assertIn("github.repository == 'cerulean-works/nest'", condition)
        self.assertIn("github.ref == 'refs/heads/main'", self.jobs["publish"])
        self.assertIn("startsWith(github.ref, 'refs/tags/v')", self.jobs["promote"])

    def test_release_tags_promote_a_main_digest_instead_of_rebuilding(self):
        promote = self.jobs["promote"]
        self.assertNotIn("buildx build", promote)
        self.assertIn("merge-base --is-ancestor", promote)
        self.assertIn("gh attestation verify", promote)
        self.assertIn("startsWith(github.ref, 'refs/tags/')", self.jobs["build"])

    def test_publishes_the_tested_archive_and_attests_it(self):
        publish = self.jobs["publish"]
        self.assertIn("needs: build", publish)
        self.assertIn("oci-archive:$RUNNER_TEMP/runtime/hpc-runtime.oci.tar", publish)
        self.assertIn('"sha-$GITHUB_SHA"', publish)
        self.assertIn("actions/attest-build-provenance@", publish)
        self.assertNotIn("buildx build", publish)
        self.assertIn("NEST_HPC_RUNTIME_PREBUILT", self.jobs["build"])

    def test_actions_are_pinned_to_commits(self):
        for action in re.findall(r"uses: (\S+)", self.text):
            with self.subTest(action=action):
                self.assertRegex(action, r"@[0-9a-f]{40}$")

    def test_registry_token_is_passed_on_stdin(self):
        self.assertNotRegex(self.text, r"--password[ =]")
        self.assertIn("--password-stdin", self.text)


if __name__ == "__main__":
    unittest.main()
