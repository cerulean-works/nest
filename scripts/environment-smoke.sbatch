#!/bin/bash
#SBATCH --job-name=nest-env-test
#SBATCH --account=ACD115198
#SBATCH --partition=dev
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=2G
#SBATCH --time=00:10:00
#SBATCH --output=env-%j.out

set -euo pipefail

ENV_ID="${1:?Missing environment ID}"
BASE="${2:?Missing Nest work directory}"

if [[ ! "$ENV_ID" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Invalid environment ID"
    exit 1
fi

if [[ "$BASE" != /* ]]; then
    echo "Nest work directory must be an absolute path: $BASE"
    exit 1
fi

if [[ ! -d "$BASE" ]]; then
    echo "Nest work directory does not exist: $BASE"
    exit 1
fi

IMAGE="$BASE/images/uv-python312.sif"
ENV_DIR="$BASE/environments/$ENV_ID"

# Identify container runtime
IMAGE_HASH=$(sha256sum "$IMAGE" | cut -d' ' -f1)
ARCH=$(uname -m)

RUNTIME_ID=$(
    printf '%s:%s' "$IMAGE_HASH" "$ARCH" |
    sha256sum | cut -d' ' -f1
)

RUNTIME_DIR="$ENV_DIR/runtimes/$RUNTIME_ID"
READY="$RUNTIME_DIR/.ready"

mkdir -p "$RUNTIME_DIR"

# Prevent concurrent environment builds
exec 9>"$RUNTIME_DIR/.build.lock"
flock -x 9

if [[ -f "$READY" && -d "$RUNTIME_DIR/.venv/lib" ]]; then
    echo "Environment cache hit"
else
    echo "Environment cache miss"

    rm -f "$READY"
    rm -rf "$RUNTIME_DIR/.venv"

    apptainer exec \
        --cleanenv \
        --bind "$BASE:/workspace" \
        "$IMAGE" \
        sh -c '
            set -eu

            cd "/workspace/environments/$1"

            export UV_PROJECT_ENVIRONMENT="/workspace/environments/$1/runtimes/$2/.venv"
            export UV_LINK_MODE=copy

            uv sync --frozen --no-dev --no-install-project
        ' sh "$ENV_ID" "$RUNTIME_ID"

    # Only mark ready after successful installation
    touch "$READY"
    echo "Environment ready"
fi

# Release lock
flock -u 9

# Verify environment
apptainer exec \
    --cleanenv \
    --bind "$BASE:/workspace" \
    "$IMAGE" \
    sh -c '
        PYTHON="/workspace/environments/$1/runtimes/$2/.venv/bin/python"

        "$PYTHON" -c "
import sys
assert sys.prefix != sys.base_prefix
print(\"Environment Python:\", sys.version.split()[0])
"
    ' sh "$ENV_ID" "$RUNTIME_ID"
