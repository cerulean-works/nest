#!/bin/bash
# Container validation of the HPC runtime image (Dockerfile target `hpc-runtime`).
#
# Builds the image, checks Git, uv, Python, SSH and the CA bundle inside it, then
# runs tests/hpc_runtime_flow.sh in a fresh container: a frozen project with a PyPI
# dependency and a commit-pinned public GitHub dependency builds from empty caches
# through scripts/environment-smoke.sbatch and is imported by scripts/worker.sbatch.
# Also confirms that an image without Git, like the original runtime, fails with
# the build script's diagnostic. Needs Docker and access to PyPI and github.com.
# Real Slurm and Apptainer are not exercised; see docs/source/runtime-image.md.
#
# NEST_SMOKE_BUILD_FLAGS adds `docker build` flags, such as --build-context
# overrides for a mirror. NEST_HPC_RUNTIME_PREBUILT=1 tests the image already
# loaded as NEST_HPC_RUNTIME_IMAGE instead of building it, so CI checks the exact
# image it publishes (.github/workflows/hpc-runtime-image.yml).

set -euo pipefail

cd "$(dirname "$0")/.."

image="${NEST_HPC_RUNTIME_IMAGE:-nest-hpc-runtime:test}"
read -ra extra <<< "${NEST_SMOKE_BUILD_FLAGS:-}"

if [[ "${NEST_HPC_RUNTIME_PREBUILT:-}" == 1 ]]; then
    docker image inspect "$image" >/dev/null
else
    docker build "${extra[@]}" --target hpc-runtime --tag "$image" .
fi

echo "=== Tools in $image ==="
docker run --rm "$image" sh -c '
    set -eu
    git --version
    uv --version
    python --version
    ssh -V
    test -s /etc/ssl/certs/ca-certificates.crt
'

echo "=== Frozen Git dependency flow in a fresh container ==="
docker run --rm --volume "$PWD:/repo:ro" "$image" bash /repo/tests/hpc_runtime_flow.sh /repo

echo "=== An image without Git fails with a diagnostic ==="
docker build "${extra[@]}" --tag "$image-no-git" - <<'EOF'
FROM python:3.14-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
EOF
docker run --rm --volume "$PWD:/repo:ro" --env EXPECT=no-git "$image-no-git" \
    bash /repo/tests/hpc_runtime_flow.sh /repo

echo "HPC runtime image smoke test passed"
