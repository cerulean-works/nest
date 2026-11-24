"""A local stand-in for an HPC login node used by end-to-end runtime tests.

`FakeHpc` writes shim executables for `ssh`, `sbatch`, `squeue`, `sacct`,
`scancel`, `scontrol`, `apptainer`, `uv`, and `flock` into a directory that is placed first
on `PATH`. The real Controller, `SlurmBackend`, and `scripts/*.sbatch` files
then run unmodified:

- `ssh -o BatchMode=yes <host> <command>` runs `<command>` with local bash.
- `sbatch` runs the batch script synchronously and records its final state.
- `apptainer exec --bind SRC:DST` rewrites `DST` to `SRC` in the command.
- `uv run ... python` uses the test interpreter, which has Nest installed;
  `uv sync` creates a real virtual environment at `UV_PROJECT_ENVIRONMENT`. Each
  call logs its HOME, uv cache, and Git and SSH environment variables.

Every shim appends its invocation to a JSON-lines log so tests can assert the
exact commands the Controller sends to the cluster.

Failure injection, for timeout and ambiguity regressions:

- `FakeHpc.hang_ssh(match)` makes SSH commands containing `match` hang, either
  before running (nothing happens remotely) or after running (the remote command
  took effect, but the caller never hears back).
- `FakeHpc.drop_ssh(match)` runs the command, then exits with status 255 as if the
  connection dropped.
- `FakeHpc.hold_jobs(state, reason)` makes `sbatch` record `state` (for example
  `PENDING`, `RUNNING`, or `TIMEOUT`) instead of running the batch script, and
  `squeue` report `reason` (for example `Priority` or `MaxGRESPerAccount`).
"""

import json
import os
import shutil
import sys
import textwrap
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent

_COMMON = """\
import json, os, subprocess, sys, time
from pathlib import Path

STATE = Path(os.environ["FAKE_HPC_STATE"])


def log(tool, **fields):
    with open(STATE / "calls.jsonl", "a") as f:
        f.write(json.dumps({"tool": tool, **fields}) + "\\n")


def job_flag(args):
    for arg in args:
        if arg.startswith("--jobs="):
            return arg.split("=", 1)[1]
    sys.exit("missing --jobs")


def state_file(job_id):
    return STATE / f"job-{job_id}.state"


def meta(job_id):
    path = STATE / f"job-{job_id}.json"
    return json.loads(path.read_text()) if path.exists() else {}
"""

_SHIMS = {
    "ssh": """
args = sys.argv[1:]
log("ssh", argv=args)
options = []
while args and args[0] in ("-o", "-F", "-i", "-p", "-l"):
    options.append((args[0], args[1]))
    args = args[2:]
if len(args) != 2:
    sys.exit("fake ssh expects exactly one host and one command")
host, command = args
if host != os.environ["FAKE_HPC_HOST"]:
    print(f"ssh: Could not resolve hostname {host}", file=sys.stderr)
    sys.exit(255)
fault_file = STATE / "ssh-fault.json"
fault = json.loads(fault_file.read_text()) if fault_file.exists() else None
if fault and fault["match"] in command:
    log("ssh-fault", mode=fault["mode"], command=command)
    if fault["mode"] in ("after", "drop"):
        subprocess.run(["bash", "-c", command], stdin=sys.stdin)
        sys.stdout.flush()
    if fault["mode"] == "drop":
        print(f"Connection to {host} closed by remote host.", file=sys.stderr)
        sys.exit(255)
    # The caller's SSH timeout kills this process.
    time.sleep(3600)
os.execvp("bash", ["bash", "-c", command])
""",
    "sbatch": """
args = sys.argv[1:]
flags = []
while args and args[0].startswith("--"):
    flags.append(args.pop(0))
script, script_args = args[0], args[1:]
counter = STATE / "next-job-id"
job_id = int(counter.read_text()) if counter.exists() else 1000
counter.write_text(str(job_id + 1))
log("sbatch", cwd=os.getcwd(), flags=flags, script=script, args=script_args, job_id=job_id)
hold = STATE / "hold-state"
options = dict(flag[2:].split("=", 1) for flag in flags if "=" in flag)
hold_reason = STATE / "hold-reason"
(STATE / f"job-{job_id}.json").write_text(json.dumps({
    "partition": options.get("partition", ""),
    "account": options.get("account", ""),
    "reason": hold_reason.read_text() if hold_reason.exists() else "None",
    "gpus": options.get("gpus-per-node", "0"),
}))
if hold.exists():
    state_file(job_id).write_text(hold.read_text())
elif Path(script).name.startswith("pending"):
    state_file(job_id).write_text("PENDING")
else:
    with open(STATE / f"job-{job_id}.out", "w") as out:
        code = subprocess.run(
            ["bash", script, *script_args],
            env={**os.environ, "SLURM_JOB_ID": str(job_id)},
            stdout=out,
            stderr=subprocess.STDOUT,
        ).returncode
    state_file(job_id).write_text("COMPLETED" if code == 0 else f"FAILED")
print(f"{job_id};fake-cluster" if "--parsable" in flags else f"Submitted batch job {job_id}")
""",
    "squeue": """
job_id = job_flag(sys.argv[1:])
path = state_file(job_id)
state = path.read_text() if path.exists() else None
if state in ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING"):
    fmt = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--format=")), "%T")
    info = meta(job_id)
    reason = info.get("reason", "None") if state == "PENDING" else "None"
    fields = {
        "%T": state,
        "%r": reason,
        "%P": info.get("partition", ""),
        "%q": "normal",
        "%a": info.get("account", ""),
    }
    print("|".join(fields[field] for field in fmt.split("|")))
else:
    # Real Slurm forgets finished jobs after MinJobAge.
    print("slurm_load_jobs error: Invalid job id specified", file=sys.stderr)
    sys.exit(1)
""",
    "sacct": """
path = state_file(job_flag(sys.argv[1:]))
if path.exists():
    print(path.read_text())
""",
    "scancel": """
job_id = sys.argv[1]
log("scancel", job_id=job_id)
path = state_file(job_id)
if not path.exists():
    print(f"scancel: error: Invalid job id {job_id}", file=sys.stderr)
    sys.exit(1)
path.write_text("CANCELLED by 0")
""",
    "scontrol": """
args = sys.argv[1:]
log("scontrol", argv=args)
if args[:3] != ["show", "job", "--oneliner"]:
    sys.exit("fake scontrol supports only show job --oneliner")
job_id = args[3]
path = state_file(job_id)
if not path.exists():
    print("slurm_load_jobs error: Invalid job id specified", file=sys.stderr)
    sys.exit(1)
info = meta(job_id)
print(
    f"JobId={job_id} JobState={path.read_text()} Reason={info.get('reason')} "
    f"Partition={info.get('partition')} Account={info.get('account')} "
    f"ReqTRES=cpu=1,node=1,gres/gpu={info.get('gpus')}"
)
""",
    "apptainer": """
args = sys.argv[1:]
if args.pop(0) != "exec":
    sys.exit("fake apptainer supports only exec")
clean, extra_env, binds = False, {}, []
while args[0].startswith("--"):
    flag = args.pop(0)
    if flag == "--cleanenv":
        clean = True
    elif flag == "--env":
        key, value = args.pop(0).split("=", 1)
        extra_env[key] = value
    elif flag == "--bind":
        binds.append(args.pop(0).split(":", 1))
    else:
        sys.exit(f"unsupported apptainer flag {flag}")
image, command = args[0], args[1:]
if not Path(image).is_file():
    sys.exit(f"image not found: {image}")
for source, target in binds:
    command = [part.replace(target, source) for part in command]
log("apptainer", image=image, env=extra_env, clean=clean)
keep = ("PATH", "HOME", "TMPDIR") + tuple(k for k in os.environ if k.startswith("FAKE_HPC_"))
env = {k: v for k, v in os.environ.items() if k in keep} if clean else dict(os.environ)
env.update(extra_env)
os.execvpe(command[0], command, env)
""",
    "uv": """
args = sys.argv[1:]
ENV_KEYS = ("HOME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "SSH_AUTH_SOCK")
env = {k: v for k, v in os.environ.items() if k in ENV_KEYS or k.startswith("GIT_")}
log("uv", argv=args, cwd=os.getcwd(), env=env)
if "--frozen" not in args:
    sys.exit("Nest must always use a frozen lock")
if args[0] == "run":
    python = args.index("python")
    os.execv(os.environ["FAKE_HPC_PYTHON"], [os.environ["FAKE_HPC_PYTHON"], *args[python + 1:]])
if args[0] == "sync":
    for name in ("pyproject.toml", "uv.lock"):
        if not Path(name).is_file():
            sys.exit(f"missing {name}")
    target = os.environ["UV_PROJECT_ENVIRONMENT"]
    subprocess.run(
        [os.environ["FAKE_HPC_PYTHON"], "-m", "venv", "--without-pip", target], check=True
    )
    sys.exit(0)
sys.exit(f"unsupported uv command {args[0]}")
""",
    "flock": """
""",
}


class FakeHpc:
    """A fake Slurm login node rooted in a temporary directory.

    Attributes:
        host (`str`): SSH alias the shim accepts.
        base (`Path`): Nest work directory (`NEST_SLURM_WORKDIR`).
        image (`Path`): Deployed Apptainer image inside `base`.
        bin (`Path`): Directory containing the shim executables.
        state (`Path`): Job states, job output, and the call log.
    """

    host = "fake-hpc"

    def __init__(self, root: Path):
        self.base = root / "nest"
        self.bin = root / "bin"
        self.state = root / "state"
        for path in (self.base / "images", self.base / "jobs", self.bin, self.state):
            path.mkdir(parents=True)
        # The deployed Apptainer image that the backend and batch scripts expect.
        self.image = self.base / "images" / "uv-python314.sif"
        self.image.write_bytes(b"fake apptainer image")
        # The deployed project contains the worker scripts.
        (self.base / "project").mkdir()
        shutil.copytree(REPOSITORY / "scripts", self.base / "project" / "scripts")

        for name, body in _SHIMS.items():
            path = self.bin / name
            path.write_text(
                f"#!{sys.executable}\n" + _COMMON + textwrap.dedent(body), encoding="utf-8"
            )
            path.chmod(0o755)

    def controller_settings(self) -> dict[str, str]:
        """Returns Controller environment variables for this fake cluster."""
        return {
            "NEST_EXECUTOR": "slurm",
            "NEST_SLURM_HOST": self.host,
            "NEST_SLURM_ACCOUNT": "test-account",
            "NEST_SLURM_WORKDIR": str(self.base),
            "NEST_LOG_LEVEL": "INFO",
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_HPC_HOST": self.host,
            "FAKE_HPC_STATE": str(self.state),
            "FAKE_HPC_PYTHON": sys.executable,
        }

    def hang_ssh(self, match: str, *, after: bool = False) -> None:
        """Makes SSH commands containing `match` hang until the caller kills SSH.

        Args:
            match (`str`): Substring of the remote command to intercept.
            after (`bool`, *optional*, defaults to `False`): Run the command first, so
                it takes effect remotely before the connection hangs.
        """
        fault = {"match": match, "mode": "after" if after else "before"}
        (self.state / "ssh-fault.json").write_text(json.dumps(fault))

    def drop_ssh(self, match: str) -> None:
        """Runs SSH commands containing `match`, then fails with SSH's exit status 255."""
        (self.state / "ssh-fault.json").write_text(json.dumps({"match": match, "mode": "drop"}))

    def hold_jobs(self, state: str, reason: str | None = None) -> None:
        """Makes later `sbatch` calls record `state` without running their script.

        Args:
            state (`str`): Slurm state to record, such as `PENDING`.
            reason (`str`, *optional*): Pending reason `squeue` reports, such as
                `MaxGRESPerAccount`. Unset reports `None`, as Slurm does.
        """
        (self.state / "hold-state").write_text(state)
        if reason is not None:
            (self.state / "hold-reason").write_text(reason)

    def job_state(self, job_id: int) -> str | None:
        """Returns the recorded state of a job, or `None` if it was never submitted."""
        path = self.state / f"job-{job_id}.state"
        return path.read_text() if path.exists() else None

    def calls(self, tool: str) -> list[dict]:
        """Returns the logged invocations of one shim, oldest first."""
        log = self.state / "calls.jsonl"
        if not log.exists():
            return []
        records = [json.loads(line) for line in log.read_text().splitlines()]
        return [record for record in records if record["tool"] == tool]

    def job_output(self, job_id: int) -> str:
        """Returns the captured stdout and stderr of a batch job."""
        return (self.state / f"job-{job_id}.out").read_text()
