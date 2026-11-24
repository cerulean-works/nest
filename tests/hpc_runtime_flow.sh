#!/bin/bash
# Runs the real environment build and worker scripts inside the HPC runtime image.
#
# tests/smoke_hpc_runtime.sh runs this in a fresh container of the image, so the uv
# and Git caches start empty. A small `apptainer exec` emulator stands in for
# Apptainer: the container already is the image, `--bind SRC:/workspace` becomes a
# symlink, and `--cleanenv` keeps only PATH, HOME and the names listed in
# RUNTIME_KEEP_ENV. Needs network access to PyPI and github.com. This is container
# validation only; it does not exercise Slurm or a real Apptainer.
#
# Usage: bash tests/hpc_runtime_flow.sh [REPOSITORY]
#   EXPECT=ready (default): Git dependency installs, imports in the user runtime,
#     caches hit, image changes miss, failures are sanitized.
#   EXPECT=no-git: the image lacks Git; the build must fail with a diagnostic and
#     leave the runtime unready.

set -euo pipefail

repo=$(cd "${1:-$(dirname "$0")/..}" && pwd)
expect="${EXPECT:-ready}"
fixture="$repo/tests/fixtures/git-dependency"

work=$(mktemp -d)
base="$work/nest"
export HOME="$work/home"
mkdir -p "$HOME" "$work/bin" "$base"/{images,environments,jobs,project}

if [[ -e /workspace ]]; then
    echo "/workspace already exists; run this in a disposable container" >&2
    exit 2
fi
trap 'rm -f /workspace; rm -rf "$work"' EXIT

cat > "$work/bin/apptainer" <<'SHIM'
#!/bin/bash
set -euo pipefail
[[ "$1" == exec ]] || { echo "emulator supports only exec" >&2; exit 2; }
shift
clean=0
extra=()
while [[ "$1" == --* ]]; do
    case "$1" in
        --cleanenv) clean=1; shift ;;
        --env) extra+=("$2"); shift 2 ;;
        --bind)
            source=${2%%:*} target=${2#*:}
            [[ -e "$target" ]] || ln -s "$source" "$target"
            [[ "$(readlink -f "$target")" == "$(readlink -f "$source")" ]]
            shift 2 ;;
        *) echo "unsupported flag $1" >&2; exit 2 ;;
    esac
done
shift  # The image: this container.
if (( clean )); then
    keep=("PATH=$PATH" "HOME=$HOME")
    for name in ${RUNTIME_KEEP_ENV:-}; do
        [[ -n "${!name:-}" ]] && keep+=("$name=${!name}")
    done
    exec env -i "${keep[@]}" "${extra[@]}" "$@"
fi
exec env "${extra[@]}" "$@"
SHIM
chmod +x "$work/bin/apptainer"
export PATH="$work/bin:$PATH"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

build() {
    # Usage: build ENV_ID LOG; returns the script's exit status.
    local status=0
    bash "$repo/scripts/environment-smoke.sbatch" "$1" "$base" > "$2" 2>&1 || status=$?
    cat "$2"
    return "$status"
}

runtime_id() {
    printf '%s:%s' "$(sha256sum "$base/images/uv-python314.sif" | cut -d' ' -f1)" "$(uname -m)" |
        sha256sum | cut -d' ' -f1
}

stage() {
    # Usage: stage NAME LOCKFILE; prints the environment ID.
    local id
    id=$(printf '%s' "$1" | sha256sum | cut -d' ' -f1)
    mkdir -p "$base/environments/$id"
    cp "$fixture/pyproject.toml" "$base/environments/$id/pyproject.toml"
    cp "$2" "$base/environments/$id/uv.lock"
    echo "$id"
}

echo "=== Runtime ==="
python --version
uv --version
git --version || echo "git: not installed"

printf 'image-a' > "$base/images/uv-python314.sif"
env_id=$(stage fixture "$fixture/uv.lock")

if [[ "$expect" == no-git ]]; then
    echo "=== Build without Git fails with a diagnostic ==="
    status=0
    build "$env_id" "$work/no-git.log" || status=$?
    (( status == 3 )) || fail "expected exit status 3, got $status"
    grep -q "pins Git dependencies, but .* has no git executable" "$work/no-git.log" ||
        fail "missing Git diagnostic"
    grep -q "hpc-runtime target" "$work/no-git.log" || fail "missing rebuild hint"
    [[ ! -e "$base/environments/$env_id/runtimes/$(runtime_id)/.ready" ]] ||
        fail "failed build marked the runtime ready"
    echo "PASS: missing Git is diagnosed and the runtime stays unready"
    exit 0
fi

# Hostile operator configuration in the real home directory. If the build read
# it, Git would rewrite github.com to an unreachable host and the build would fail.
cat > "$HOME/.gitconfig" <<'EOF'
[url "https://nest-operator-config.invalid/"]
    insteadOf = https://github.com/
EOF
printf 'machine github.com login operator password operator-secret\n' > "$HOME/.netrc"

echo "=== Cold build: PyPI and Git dependencies from empty caches ==="
[[ ! -e "$HOME/.cache/uv" ]] || fail "uv cache is not empty"
build "$env_id" "$work/cold.log" || fail "cold build failed"
grep -q "Environment cache miss" "$work/cold.log" || fail "expected a cache miss"
grep -q "Environment ready" "$work/cold.log" || fail "expected a ready runtime"
first_runtime=$(runtime_id)
runtime="$base/environments/$env_id/runtimes/$first_runtime"
[[ -f "$runtime/.ready" ]] || fail "runtime not marked ready"
[[ ! -e "$runtime/.build-home" ]] || fail "per-build home was not removed"
"$runtime/.venv/bin/python" -c 'import iniconfig, six; assert six.__version__ == "1.17.0"' ||
    fail "dependencies are not importable"
[[ -d "$HOME/.cache/uv" ]] || fail "uv did not use its usual cache directory"

echo "=== Nest runtime: user environment and no-project calls ==="
cp -r "$repo/pyproject.toml" "$repo/uv.lock" "$repo/README.md" "$repo/LICENSE" "$repo/src" \
    "$repo/scripts" "$base/project/"

with_env_job=$(printf 'a%.0s' {1..32})
no_env_job=$(printf 'b%.0s' {1..32})
mkdir "$base/jobs/$with_env_job" "$base/jobs/$no_env_job"
(
    cd "$base/project"
    uv run --frozen --no-dev python - "$base/jobs" "$with_env_job" "$no_env_job" <<'PY'
import sys

import cloudpickle

jobs, with_env, no_env = sys.argv[1:]


def probe():
    import sys

    import iniconfig
    import six

    return six.__version__, iniconfig.__name__, sys.prefix


def double(x):
    return x * 2


for job, call in ((with_env, (probe, (), {})), (no_env, (double, (21,), {}))):
    with open(f"{jobs}/{job}/input.pkl", "wb") as f:
        cloudpickle.dump(call, f)
PY
)

bash "$repo/scripts/worker.sbatch" "$with_env_job" "$env_id" "$base" "" INFO ||
    fail "worker with the user environment failed"
bash "$repo/scripts/worker.sbatch" "$no_env_job" "" "$base" "" INFO ||
    fail "worker without a project failed"
(
    cd "$base/project"
    uv run --frozen --no-dev python - "$base/jobs" "$with_env_job" "$no_env_job" "$env_id" <<'PY'
import sys

import cloudpickle

jobs, with_env, no_env, env_id = sys.argv[1:]
with open(f"{jobs}/{with_env}/output.pkl", "rb") as f:
    version, module, prefix = cloudpickle.load(f)
assert (version, module) == ("1.17.0", "iniconfig"), (version, module)
assert f"/environments/{env_id}/runtimes/" in prefix, prefix
with open(f"{jobs}/{no_env}/output.pkl", "rb") as f:
    assert cloudpickle.load(f) == 42
print("User runtime imported six", version, "from", prefix)
PY
) || fail "remote results are wrong"

echo "=== Cache hit ==="
build "$env_id" "$work/hit.log" || fail "cached build failed"
grep -q "Environment cache hit" "$work/hit.log" || fail "expected a cache hit"

echo "=== A changed image invalidates the runtime cache ==="
printf 'image-b' > "$base/images/uv-python314.sif"
[[ "$(runtime_id)" != "$first_runtime" ]] || fail "runtime ID did not change"
build "$env_id" "$work/changed.log" || fail "rebuild after the image change failed"
grep -q "Environment cache miss" "$work/changed.log" || fail "expected a cache miss"
[[ -f "$base/environments/$env_id/runtimes/$(runtime_id)/.ready" ]] || fail "new runtime not ready"
[[ -f "$runtime/.ready" ]] || fail "the old runtime was disturbed"

echo "=== An unreachable Git source fails without leaking credentials ==="
sed -e 's#https://github.com/benjaminp/six#https://nest-user:not-a-real-secret@nest-unreachable.invalid/six#g' \
    "$fixture/uv.lock" > "$work/unreachable.lock"
bad_id=$(stage unreachable "$work/unreachable.lock")
status=0
build "$bad_id" "$work/unreachable.log" || status=$?
(( status != 0 )) || fail "the unreachable build succeeded"
! grep -q "not-a-real-secret\|operator-secret" "$work/unreachable.log" || fail "a credential leaked"
grep -q "uv sync failed with exit status" "$work/unreachable.log" || fail "missing failure line"
grep -q "Compute nodes must reach each Git host" "$work/unreachable.log" || fail "missing hint"
[[ ! -e "$base/environments/$bad_id/runtimes/$(runtime_id)/.ready" ]] ||
    fail "failed build marked the runtime ready"
[[ ! -e "$base/environments/$bad_id/runtimes/$(runtime_id)/.venv" ]] ||
    fail "failed build left a partial environment"

echo "PASS: HPC runtime image builds and runs Git-based uv environments"
