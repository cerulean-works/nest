#!/usr/bin/env python3
"""Writes the build manifest of an HPC runtime image from its OCI archive.

Usage:
    python3 scripts/runtime_image_manifest.py OCI_ARCHIVE --output MANIFEST.json
        [--summary SUMMARY.md] [--image LOCAL_TAG] [--commit SHA] [--ref REF]
        [--repository URL] [--run-url URL] [--base REF@DIGEST ...]
        [--platform linux/amd64] [--require-attestations]

OCI_ARCHIVE is the `docker buildx build --output type=oci` tarball of the
Dockerfile's `hpc-runtime` target. The manifest records the OCI digest that is
published (the top-level index or manifest in the archive), the per-platform
manifest and config digests, the source commit, the pinned base images, the
tool and Debian package versions inside the image (with --image, a local Docker
tag of the same image), and the Controller and batch-script revision the image
was tested with. See docs/source/runtime-image.md.

Exits non-zero, with a message naming the problem, when the archive has no image
for the requested platform, its revision label does not match --commit, or
(with --require-attestations) it carries no SBOM or provenance attestation.
Needs only the Python standard library (3.11 or later, so the CI runner's system
Python works), plus Docker for --image.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
# Files whose content defines what the image must provide; recorded so an
# operator can tell which Controller and batch scripts the image was tested with.
COMPATIBILITY_FILES = (
    "Dockerfile",
    "scripts/worker.sbatch",
    "scripts/environment-smoke.sbatch",
    "scripts/apptainer-smoke.sbatch",
)
TOOLS_SCRIPT = r"""
set -eu
# Assign before printing so a missing tool fails the check under `set -e`.
value=$(python --version 2>&1); echo "python=$value"
value=$(uv --version); echo "uv=$value"
value=$(git --version); echo "git=$value"
value=$(ssh -V 2>&1); echo "ssh=$value"
test -s /etc/ssl/certs/ca-certificates.crt
echo "ca_bundle=/etc/ssl/certs/ca-certificates.crt"
value=$(uname -m); echo "machine=$value"
for pkg in ca-certificates git openssh-client; do
    value=$(dpkg-query --show --showformat='${Version}' "$pkg"); echo "deb:$pkg=$value"
done
"""


class ManifestError(Exception):
    pass


class OciArchive:
    def __init__(self, path: Path):
        self.path = path

    def read(self, name: str) -> bytes:
        try:
            with tarfile.open(self.path) as tar:
                member = tar.extractfile(name)
                if member is not None:
                    return member.read()
        # Separate clauses, not a tuple: the script also runs on the CI runner's
        # older system Python, and the formatter drops the tuple's parentheses.
        except KeyError:
            pass
        except tarfile.TarError:
            pass
        except OSError:
            pass
        raise ManifestError(f"{self.path}: missing {name}; is this an OCI archive?")

    def blob(self, digest: str) -> dict:
        algorithm, _, hexdigest = digest.partition(":")
        data = self.read(f"blobs/{algorithm}/{hexdigest}")
        if algorithm == "sha256" and hashlib.sha256(data).hexdigest() != hexdigest:
            raise ManifestError(f"{self.path}: blob {digest} does not match its digest")
        return json.loads(data)


def platform_name(platform: dict) -> str:
    name = f"{platform.get('os', '?')}/{platform.get('architecture', '?')}"
    if platform.get("variant"):
        name += f"/{platform['variant']}"
    return name


def describe_archive(archive: OciArchive, platform: str) -> dict:
    index = json.loads(archive.read("index.json"))
    top = index.get("manifests") or []
    if len(top) != 1:
        raise ManifestError(
            f"{archive.path}: expected one image in index.json, found {len(top)}; "
            "build a single image with `--output type=oci`"
        )
    digest = top[0]["digest"]
    root = archive.blob(digest)

    images, attestations = [], []
    if root.get("mediaType") in INDEX_TYPES or "manifests" in root:
        for entry in root["manifests"]:
            annotations = entry.get("annotations") or {}
            if annotations.get("vnd.docker.reference.type") == "attestation-manifest":
                attestations.append(entry)
            else:
                images.append(entry)
    else:
        images.append({"digest": digest, "mediaType": root.get("mediaType")})

    described, selected = [], None
    for entry in images:
        manifest = archive.blob(entry["digest"])
        config = archive.blob(manifest["config"]["digest"])
        name = platform_name(entry.get("platform") or config)
        item = {
            "platform": name,
            "digest": entry["digest"],
            "config": manifest["config"]["digest"],
            "layers": len(manifest.get("layers", [])),
        }
        described.append(item)
        if name == platform:
            selected = (item, config)
    if selected is None:
        found = ", ".join(item["platform"] for item in described) or "none"
        raise ManifestError(
            f"{archive.path}: no {platform} image (found: {found}); the image must match "
            f"the compute nodes, so build with `--platform {platform}`"
        )

    predicate_types = set()
    for entry in attestations:
        manifest = archive.blob(entry["digest"])
        for layer in manifest.get("layers", []):
            kind = (layer.get("annotations") or {}).get("in-toto.io/predicate-type")
            if kind:
                predicate_types.add(kind)

    item, config = selected
    return {
        "digest": digest,
        "media_type": root.get("mediaType"),
        "platform": platform,
        "platform_manifest": item["digest"],
        "config": item["config"],
        "images": described,
        "labels": (config.get("config") or {}).get("Labels") or {},
        "attestations": {
            "sbom": any("spdx" in kind or "cyclonedx" in kind for kind in predicate_types),
            "provenance": any("slsa.dev/provenance" in kind for kind in predicate_types),
            "predicate_types": sorted(predicate_types),
        },
    }


def image_tools(image: str, platform: str) -> tuple[dict, dict]:
    result = subprocess.run(
        ["docker", "run", "--rm", "--platform", platform, image, "sh", "-c", TOOLS_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ManifestError(
            f"{image}: tool check failed (exit {result.returncode}); the runtime image needs "
            f"Python, uv, Git, the OpenSSH client and the CA bundle:\n{result.stderr.strip()}"
        )
    tools, packages = {}, {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key.startswith("deb:"):
            packages[key[4:]] = value
        elif key:
            tools[key] = value
    return tools, packages


def file_sha256(name: str) -> str | None:
    path = ROOT / name
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def nest_version() -> str | None:
    path = ROOT / "pyproject.toml"
    if not path.is_file():
        return None
    with path.open("rb") as file:
        return tomllib.load(file)["project"]["version"]


def build_manifest(args: argparse.Namespace) -> dict:
    archive = OciArchive(args.archive)
    image = describe_archive(archive, args.platform)
    revision = image["labels"].get("org.opencontainers.image.revision")
    if args.commit and revision != args.commit:
        raise ManifestError(
            f"{args.archive}: label org.opencontainers.image.revision is {revision!r}, "
            f"expected {args.commit}; pass `--label org.opencontainers.image.revision=<commit>`"
        )
    if args.require_attestations:
        missing = [kind for kind in ("sbom", "provenance") if not image["attestations"][kind]]
        if missing:
            raise ManifestError(
                f"{args.archive}: no {' or '.join(missing)} attestation; build with "
                "`--sbom=true --provenance=mode=max` and a builder that supports attestations"
            )

    bases = {}
    for value in args.base:
        name, sep, digest = value.partition("@")
        if not sep or not digest.startswith("sha256:"):
            raise ManifestError(f"--base {value!r}: expected NAME@sha256:<digest>")
        bases[name] = digest

    manifest = {
        "schema": 1,
        "name": "nest-hpc-runtime",
        "image": args.image_name,
        "digest": image["digest"],
        "media_type": image["media_type"],
        "platform": image["platform"],
        "platform_manifest": image["platform_manifest"],
        "config": image["config"],
        "images": image["images"],
        "attestations": image["attestations"],
        "source": {
            "repository": args.repository,
            "commit": args.commit or revision,
            "ref": args.ref,
            "run": args.run_url,
        },
        "base_images": bases,
        "labels": image["labels"],
        "compatibility": {
            "nest_version": nest_version(),
            "controller_commit": args.commit or revision,
            "files_sha256": {name: file_sha256(name) for name in COMPATIBILITY_FILES},
        },
    }
    if args.image:
        tools, packages = image_tools(args.image, args.platform)
        expected = args.platform.split("/")[1]
        machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(expected, expected)
        if tools.get("machine") != machine:
            raise ManifestError(
                f"{args.image}: runs as {tools.get('machine')}, expected {machine} for "
                f"{args.platform}"
            )
        manifest["tools"] = tools
        manifest["debian_packages"] = packages
    return manifest


def summary(manifest: dict) -> str:
    source = manifest["source"]
    rows = [
        ("OCI digest", f"`{manifest['digest']}`"),
        ("Platform", f"{manifest['platform']} (`{manifest['platform_manifest']}`)"),
        ("Source commit", f"`{source['commit']}`"),
        ("Nest version", manifest["compatibility"]["nest_version"]),
        ("SBOM attestation", "yes" if manifest["attestations"]["sbom"] else "**no**"),
        ("Provenance attestation", "yes" if manifest["attestations"]["provenance"] else "**no**"),
    ]
    rows += [(f"Base `{name}`", f"`{digest}`") for name, digest in manifest["base_images"].items()]
    rows += [(f"Tool `{name}`", value) for name, value in manifest.get("tools", {}).items()]
    rows += [
        (f"Debian `{name}`", value) for name, value in manifest.get("debian_packages", {}).items()
    ]
    lines = ["### HPC runtime image", "", "| Field | Value |", "| --- | --- |"]
    lines += [f"| {key} | {value} |" for key, value in rows]
    lines += [
        "",
        (
            "The OCI digest identifies the registry image. The SIF built from it on the "
            "cluster has its own SHA-256; record both."
        ),
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--image", help="local Docker tag of the same image, for tool versions")
    parser.add_argument("--image-name", default="ghcr.io/cerulean-works/nest-hpc-runtime")
    parser.add_argument("--commit")
    parser.add_argument("--ref")
    parser.add_argument("--repository", default="https://github.com/cerulean-works/nest")
    parser.add_argument("--run-url")
    parser.add_argument("--base", action="append", default=[], metavar="NAME@DIGEST")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--require-attestations", action="store_true")
    args = parser.parse_args(argv)

    try:
        manifest = build_manifest(args)
    except ManifestError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    if args.summary:
        args.summary.write_text(summary(manifest))
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
