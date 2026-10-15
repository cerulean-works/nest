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