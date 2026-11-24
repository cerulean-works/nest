#!/bin/bash
#SBATCH --job-name=nest-apptainer
#SBATCH --partition=8gpus
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=00:02:00
#SBATCH --output=apptainer-%j.out

set -euo pipefail

BASE="${1:?Missing Nest work directory}"
# Optional candidate image, so a new SIF can be checked before it replaces the
# deployed one. Defaults to the deployed image.
IMAGE="${2:-$BASE/images/uv-python314.sif}"

if [[ "$BASE" != /* ]]; then
    echo "Nest work directory must be an absolute path: $BASE"
    exit 1
fi

if [[ ! -r "$IMAGE" ]]; then
    echo "Image not found: $IMAGE"
    exit 1
fi

echo "=== Image ==="
echo "Path: $IMAGE"
echo "SHA-256: $(sha256sum "$IMAGE" | cut -d' ' -f1)"

echo "=== Host ==="
hostname
python3 --version

echo "=== Apptainer ==="
apptainer exec --cleanenv "$IMAGE" python --version
apptainer exec --cleanenv "$IMAGE" uv --version
# Git, the CA bundle and the SSH client are required for uv Git dependencies.
apptainer exec --cleanenv "$IMAGE" git --version
apptainer exec --cleanenv "$IMAGE" ssh -V
apptainer exec --cleanenv "$IMAGE" test -s /etc/ssl/certs/ca-certificates.crt

echo "=== Container Python ==="
apptainer exec --cleanenv "$IMAGE" \
    python -c 'import sys; print(sys.executable)'

echo "=== Done ==="