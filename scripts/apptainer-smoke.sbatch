#!/bin/bash
#SBATCH --job-name=nestappt-apptainer
#SBATCH --partition=dev
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=00:02:00
#SBATCH --output=apptainer-%j.out

set -euo pipefail

IMAGE="$HOME/nestappt-smoke/images/uv-python312.sif"

echo "=== Host ==="
hostname
python3 --version

echo "=== Apptainer ==="
apptainer exec --cleanenv "$IMAGE" python --version
apptainer exec --cleanenv "$IMAGE" uv --version

echo "=== Container Python ==="
apptainer exec --cleanenv "$IMAGE" \
    python -c 'import sys; print(sys.executable)'

echo "=== Done ==="