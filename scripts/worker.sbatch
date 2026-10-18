#!/bin/bash
#SBATCH --job-name=nest-worker
#SBATCH --partition=dev
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=00:05:00
#SBATCH --output=worker-%j.out

set -euo pipefail

BASE="$HOME/nest-smoke"

# Receive unique execution ID
JOB_ID="${1:?Missing execution ID}"

echo "Received execution ID: [$JOB_ID]"
echo "Length: ${#JOB_ID}"
printf 'Shell-escaped value: %q\n' "$JOB_ID"

# Validate UUID format
if [[ ! "$JOB_ID" =~ ^[0-9a-f]{32}$ ]]; then
    echo "Invalid execution ID"
    exit 1
fi

# Receive optional environment ID
ENV_ID="${2:-}"

if [[ -n "$ENV_ID" ]]; then
    if [[ ! "$ENV_ID" =~ ^[0-9a-f]{64}$ ]]; then
        echo "Invalid environment ID"
        exit 1
    fi

    echo "Environment ID: [$ENV_ID]"
else
    echo "Environment ID: none"
fi

# Resolve user Python environment
IMAGE="$BASE/images/uv-python312.sif"

if [[ -n "$ENV_ID" ]]; then

    # Calculate runtime identity
    IMAGE_HASH=$(sha256sum "$IMAGE" | cut -d' ' -f1)
    ARCH=$(uname -m)

    RUNTIME_ID=$(
        printf '%s:%s' "$IMAGE_HASH" "$ARCH" |
        sha256sum |
        cut -d' ' -f1
    )

    # Resolve environment directory
    RUNTIME_DIR="$BASE/environments/$ENV_ID/runtimes/$RUNTIME_ID"

    READY="$RUNTIME_DIR/.ready"
    USER_PYTHON="$RUNTIME_DIR/.venv/bin/python"

    # Verify environment is ready
    if [[ ! -f "$READY" ]]; then
        echo "Environment is not ready: $RUNTIME_DIR"
        exit 1
    fi

    # Python may be a symlink into the Apptainer image
    if [[ ! -L "$USER_PYTHON" && ! -f "$USER_PYTHON" ]]; then
        echo "Environment Python not found: $USER_PYTHON"
        exit 1
    fi

    echo "Runtime ID: [$RUNTIME_ID]"
    echo "User Python: [$USER_PYTHON]"
    echo "Environment verified"
fi

apptainer exec \
    --cleanenv \
    --bind "$BASE:/workspace" \
    "$BASE/images/uv-python312.sif" \
    sh -c '
        cd /workspace/project

        uv run --frozen --no-dev python -m nest.worker \
            "/workspace/jobs/$1/input.pkl" \
            "/workspace/jobs/$1/output.pkl"
    ' sh "$JOB_ID"

test -s "$BASE/jobs/$JOB_ID/output.pkl"

echo "Worker finished successfully"