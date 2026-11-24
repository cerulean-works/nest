#!/bin/bash
# Builds the Nest HPC runtime image and converts it to an Apptainer SIF.
#
# Usage: scripts/build-runtime-image.sh [OUTPUT.sif]
#
# Runs on a workstation or CI host with Docker; Apptainer is optional. Builds the
# Dockerfile's `hpc-runtime` target, checks Python, uv, Git and SSH inside it,
# saves it as a Docker archive, converts that to a SIF when `apptainer` is
# installed, and writes `OUTPUT.manifest` recording what was built. Without
# Apptainer, copy the archive to the login node and run the printed command
# there; converting an archive needs neither root nor `--fakeroot`.
#
# Environment:
#   NEST_RUNTIME_TAG       Docker tag (default: nest-hpc-runtime:<commit>).
#   NEST_RUNTIME_PLATFORM  Target platform; must match the compute nodes
#                          (default: linux/amd64).
#
# The output never replaces the deployed `images/uv-python314.sif`; see
# docs/source/runtime-image.md for verification and rollout.

set -euo pipefail

cd "$(dirname "$0")/.."

commit=$(git rev-parse --short=12 HEAD)
tag="${NEST_RUNTIME_TAG:-nest-hpc-runtime:$commit}"
platform="${NEST_RUNTIME_PLATFORM:-linux/amd64}"
out="${1:-build/uv-python314.sif}"
archive="${out%.sif}.docker.tar"
manifest="${out%.sif}.manifest"

if [[ -n "$(git status --porcelain -- Dockerfile)" ]]; then
    echo "warning: Dockerfile has uncommitted changes; the manifest commit is not exact" >&2
fi

docker build --pull --platform "$platform" --target hpc-runtime --tag "$tag" .

versions=$(
    docker run --rm --platform "$platform" "$tag" sh -c '
        set -eu
        python --version
        uv --version
        git --version
        ssh -V 2>&1
        test -s /etc/ssl/certs/ca-certificates.crt
        uname -m
    '
)

mkdir -p "$(dirname "$out")"
docker save --output "$archive" "$tag"

base_digest=$(
    docker image inspect --format '{{join .RepoDigests " "}}' python:3.14-slim-bookworm 2>/dev/null ||
    echo unknown
)

{
    echo "commit: $(git rev-parse HEAD)"
    echo "tag: $tag"
    echo "platform: $platform"
    echo "image_id: $(docker image inspect --format '{{.Id}}' "$tag")"
    echo "base: python:3.14-slim-bookworm $base_digest"
    echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "archive_sha256: $(sha256sum "$archive" | cut -d' ' -f1)"
    echo "$versions" | sed 's/^/version: /'
} > "$manifest"

if command -v apptainer >/dev/null 2>&1; then
    apptainer build --force "$out" "docker-archive://$archive"
    echo "sif_sha256: $(sha256sum "$out" | cut -d' ' -f1)" >> "$manifest"
    echo "Built $out"
else
    echo "Apptainer is not installed here. On the login node, run:"
    echo "  apptainer build uv-python314.sif docker-archive://$(basename "$archive")"
fi

echo "Manifest: $manifest"
cat "$manifest"
