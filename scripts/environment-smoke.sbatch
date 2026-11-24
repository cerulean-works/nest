#!/bin/bash
#SBATCH --job-name=nest-env-test
#SBATCH --account=ACD115198
#SBATCH --partition=h200_256gpu
#SBATCH --nodes=1
#SBATCH --gpus-per-node=1
#SBATCH --cpus-per-task=12
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

IMAGE="$BASE/images/uv-python314.sif"
ENV_DIR="$BASE/environments/$ENV_ID"

# Masks credentials that uv, Git or SSH may echo back from a URL, such as
# https://user:token@host/... or a GitHub token, before they reach the job log.
redact() {
    sed -E \
        -e 's#([A-Za-z][A-Za-z0-9+.-]*://)[^/@[:space:]]+@#\1***@#g' \
        -e 's#(gh[opsur]_|github_pat_)[A-Za-z0-9_]+#\1***#g'
}

# uv clones Git dependencies with the image's git executable.
uses_git() {
    grep -Eq '^source = \{ git = ' "$ENV_DIR/uv.lock"
}

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

    if uses_git && ! apptainer exec --cleanenv "$IMAGE" git --version >/dev/null 2>&1; then
        echo "error: uv.lock pins Git dependencies, but $IMAGE has no git executable." >&2
        echo "hint: rebuild the image from the Dockerfile's hpc-runtime target; see the" >&2
        echo "hint: Nest documentation, 'Build and roll out the HPC runtime image'." >&2
        echo "Environment build failed; the runtime was not marked ready." >&2
        exit 3
    fi

    # The build sees no operator credentials: HOME is an empty per-build directory
    # (no ~/.netrc, ~/.gitconfig, ~/.git-credentials or uv credential store), Git
    # ignores user and system configuration and never prompts, and SSH reads no
    # ssh_config, offers no keys, agent or Kerberos ticket, and keeps strict
    # host-key checking. TLS verification stays on. uv keeps its usual cache and
    # Python installations under the real home directory.
    BUILD_HOME="$RUNTIME_DIR/.build-home"
    rm -rf "$BUILD_HOME"
    mkdir -m 700 "$BUILD_HOME"

    status=0
    apptainer exec \
        --cleanenv \
        --bind "$BASE:/workspace" \
        "$IMAGE" \
        sh -c '
            set -eu

            cd "/workspace/environments/$1"

            export UV_PROJECT_ENVIRONMENT="/workspace/environments/$1/runtimes/$2/.venv"
            export UV_LINK_MODE=copy
            export UV_CACHE_DIR="${UV_CACHE_DIR:-$HOME/.cache/uv}"
            export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$HOME/.local/share/uv/python}"

            export HOME="/workspace/environments/$1/runtimes/$2/.build-home"
            export GIT_CONFIG_NOSYSTEM=1
            export GIT_CONFIG_GLOBAL=/dev/null
            export GIT_TERMINAL_PROMPT=0
            export GIT_SSH_COMMAND="ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes -o IdentityAgent=none -o PubkeyAuthentication=no -o HostbasedAuthentication=no -o GSSAPIAuthentication=no"
            unset SSH_AUTH_SOCK GIT_ASKPASS SSH_ASKPASS

            uv sync --frozen --no-dev --no-install-project
        ' sh "$ENV_ID" "$RUNTIME_ID" 2>&1 | redact || status=$?

    rm -rf "$BUILD_HOME"

    if (( status != 0 )); then
        rm -rf "$RUNTIME_DIR/.venv"
        echo "error: uv sync failed with exit status $status." >&2

        if uses_git; then
            echo "hint: uv.lock pins Git dependencies. Compute nodes must reach each Git host" >&2
            echo "hint: (HTTPS, or SSH for ssh:// URLs); some clusters block outbound access" >&2
            echo "hint: from compute nodes. Builds use no operator credentials, so private" >&2
            echo "hint: repositories need separately approved access and are not supported yet." >&2
        fi

        echo "Environment build failed; the runtime was not marked ready." >&2
        exit "$status"
    fi

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
