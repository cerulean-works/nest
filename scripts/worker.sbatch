#!/bin/bash
#SBATCH --job-name=nest-worker
#SBATCH --partition=h200_256gpu
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --time=00:05:00
#SBATCH --output=worker-%j.out

set -euo pipefail

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
REQUEST_ID="${4:-}"
LOG_LEVEL="${5:-INFO}"
# GPUs per node the caller requested; unset when an older Controller submitted the job.
GPU_REQUEST="${6-}"

echo "Nest worker job_id=${SLURM_JOB_ID:-unknown} execution_id=$JOB_ID request_id=${REQUEST_ID:-none} environment_id=${ENV_ID:-none}"

if [[ -n "$ENV_ID" ]]; then
    if [[ ! "$ENV_ID" =~ ^[0-9a-f]{64}$ ]]; then
        echo "Invalid environment ID"
        exit 1
    fi

    echo "Environment ID: [$ENV_ID]"
else
    echo "Environment ID: none"
fi

# Receive work directory from Controller
BASE="${3:?Missing Nest work directory}"

# Validate work directory
if [[ "$BASE" != /* ]]; then
    echo "Nest work directory must be an absolute path: $BASE"
    exit 1
fi

if [[ ! -d "$BASE" ]]; then
    echo "Nest work directory does not exist: $BASE"
    exit 1
fi

# Resolve user Python environment
IMAGE="$BASE/images/uv-python314.sif"

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

# Resolve Python path inside Apptainer
USER_PYTHON_CONTAINER=""

if [[ -n "$ENV_ID" ]]; then
    USER_PYTHON_CONTAINER="/workspace/environments/$ENV_ID/runtimes/$RUNTIME_ID/.venv/bin/python"

    echo "User Python (container): [$USER_PYTHON_CONTAINER]"
fi

# Decide GPU passthrough from the request and Slurm's allocation of this node.
#
# The `#SBATCH --gpus-per-node` directive above is only a default: the job can hold
# GPUs that the caller did not ask for. NVIDIA passthrough (`--nv`) is enabled only
# for an explicit request, and the container sees exactly the devices Slurm assigned
# (`CUDA_VISIBLE_DEVICES`, copied verbatim across `--cleanenv`). Slurm's cgroup
# device constraints remain the isolation boundary; environment variables are not.
gpu_error() {
    echo "GPU allocation error: $*" >&2
    echo "No GPU was exposed and the user function did not run." >&2
    exit 1
}

GPU_ARGS=()

if [[ -z "$GPU_REQUEST" ]]; then
    echo "GPU request: not passed by the Controller; running CPU-only without NVIDIA passthrough"
    GPU_REQUEST=0
elif [[ ! "$GPU_REQUEST" =~ ^(0|[1-9][0-9]{0,3})$ ]]; then
    printf 'Invalid GPU request: %q\n' "$GPU_REQUEST"
    exit 1
fi

if (( GPU_REQUEST == 0 )); then
    if [[ "${SLURM_GPUS_ON_NODE:-0}" != 0 ]]; then
        echo "CPU-only request: the ${SLURM_GPUS_ON_NODE} GPU(s) this job holds are not exposed"
    fi

    # Hide devices from CUDA even if a library inside the image could reach them.
    GPU_ARGS=(--env "CUDA_VISIBLE_DEVICES=")
    echo "GPU mode: disabled (gpu=0)"
else
    [[ -n "${SLURM_JOB_ID:-}" ]] ||
        gpu_error "gpu=$GPU_REQUEST requires a Slurm allocation, but SLURM_JOB_ID is unset"

    ALLOCATED="${SLURM_GPUS_ON_NODE-}"
    [[ "$ALLOCATED" =~ ^[0-9]+$ ]] ||
        gpu_error "Slurm did not report the GPUs allocated on this node" \
            "(SLURM_GPUS_ON_NODE=${SLURM_GPUS_ON_NODE-<unset>})"

    (( ALLOCATED >= GPU_REQUEST )) ||
        gpu_error "gpu=$GPU_REQUEST was requested, but Slurm allocated $ALLOCATED GPU(s) on this node"

    [[ -n "${CUDA_VISIBLE_DEVICES+set}" ]] ||
        gpu_error "Slurm did not set CUDA_VISIBLE_DEVICES for this allocation; Nest will not" \
            "expose GPUs without the scheduler's device mapping"

    [[ -n "$CUDA_VISIBLE_DEVICES" ]] ||
        gpu_error "CUDA_VISIBLE_DEVICES is empty although Slurm allocated $ALLOCATED GPU(s)"

    IFS=, read -r -a VISIBLE <<< "$CUDA_VISIBLE_DEVICES"
    declare -A SEEN=()

    for device in "${VISIBLE[@]}"; do
        # Ordinals, or GPU and MIG UUIDs as Slurm writes them.
        [[ "$device" =~ ^([0-9]+|(GPU|MIG)-[0-9A-Za-z/-]+)$ ]] ||
            gpu_error "CUDA_VISIBLE_DEVICES contains an unrecognized device '$device'"
        [[ -z "${SEEN[$device]+set}" ]] ||
            gpu_error "CUDA_VISIBLE_DEVICES lists device '$device' more than once"
        SEEN[$device]=1
    done

    (( ${#VISIBLE[@]} == ALLOCATED )) ||
        gpu_error "CUDA_VISIBLE_DEVICES lists ${#VISIBLE[@]} device(s), but Slurm allocated" \
            "$ALLOCATED GPU(s) on this node"

    if (( ALLOCATED > GPU_REQUEST )); then
        echo "GPU allocation: Slurm allocated $ALLOCATED GPU(s) for gpu=$GPU_REQUEST; exposing the allocation as assigned"
    fi

    GPU_ARGS=(--nv --env "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES")

    # Device numbering depends on the order the site configured, so keep it.
    if [[ -n "${CUDA_DEVICE_ORDER:-}" ]]; then
        [[ "$CUDA_DEVICE_ORDER" =~ ^(FASTEST_FIRST|PCI_BUS_ID)$ ]] ||
            gpu_error "CUDA_DEVICE_ORDER has an unsupported value '$CUDA_DEVICE_ORDER'"
        GPU_ARGS+=(--env "CUDA_DEVICE_ORDER=$CUDA_DEVICE_ORDER")
    fi

    if ! command -v nvidia-smi >/dev/null 2>&1 && [[ ! -e /dev/nvidiactl ]]; then
        echo "GPU warning: no nvidia-smi or /dev/nvidiactl on $(hostname); the NVIDIA driver may be missing" >&2
    fi

    echo "GPU mode: enabled requested=$GPU_REQUEST allocated=$ALLOCATED CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
fi

apptainer exec \
    --cleanenv \
    "${GPU_ARGS[@]}" \
    --env "NEST_GPU_REQUESTED=$GPU_REQUEST" \
    --env "NEST_REQUEST_ID=$REQUEST_ID" \
    --env "NEST_LOG_LEVEL=$LOG_LEVEL" \
    --bind "$BASE:/workspace" \
    "$IMAGE" \
    sh -c '
        cd /workspace/project

        uv run --frozen --no-dev python -m nest.worker \
            "/workspace/jobs/$1/input.pkl" \
            "/workspace/jobs/$1/output.pkl" \
            "$2"
    ' sh "$JOB_ID" "$USER_PYTHON_CONTAINER"

test -s "$BASE/jobs/$JOB_ID/output.pkl"

echo "Worker finished successfully"
