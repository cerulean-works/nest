import re
import uuid
import time
import shlex
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class SlurmConfig:
    ssh_host: str
    account: str
    partition: str = "dev"
    time: str | None = None
    work_root: str | None = None
    worker_workdir: str | None = None
    
class SlurmBackend:
    def __init__(self, config: SlurmConfig):
        self.config = config
        
    def _run_remote(self, command: str) -> str:
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    self.config.ssh_host,
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=60
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                "Remote command timed out after 60 seconds. "
                "The Slurm operation status is unknown."
            ) from exc
        
        if result.returncode != 0:
            raise RuntimeError(
                f"Remote command failed:\n"
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        
        return result.stdout.strip()
    
    def submit(
        self, 
        workdir: str,
        script: str = "hello.sbatch", 
        gpu: int = 1,
        *,
        account: str | None = None,
        partition: str | None = None,
        time: str | None = None,
        script_args: tuple[str, ...] = (),
    ) -> int:
        if isinstance(script_args, str):
            raise TypeError("script_args must be a tuple of strings")
            
        if gpu < 0:
            raise ValueError("GPU count cannot be negative")
        
        args = [
            "sbatch",
            "--parsable",
            f"--account={account or self.config.account}",
            f"--partition={partition or self.config.partition}",
        ]
        
        if gpu > 0:
            args.append(f"--gpus-per-node={gpu}")
            
        time_limit = time or self.config.time
        
        if time_limit:
            args.append(f"--time={time_limit}")
        
        args.append(script)
        args.extend(script_args)
        
        command = (
            f"cd {shlex.quote(workdir)} && "
            f"{shlex.join(args)}"
        )
        
        output = self._run_remote(command)
        job_id = output.split(";")[0].strip()
        
        if not job_id.isdecimal():
            raise RuntimeError(f"Unexpected sbatch output: {output}")
        
        return int(job_id)
    
    def status(self, job_id: int) -> str:
        job_id = int(job_id)
        
        # Check active jobs
        try:            
            output = self._run_remote(
                f"squeue --noheader --jobs={job_id} --format=%T"
            )
        except RuntimeError as exc:
            if "Invalid job id specified" not in str(exc):
                raise
            output = ""
        
        if output:
            return output.splitlines()[0].strip()
        
        # Check job accouting histroy
        output = self._run_remote(
            f"sacct -X -n -P --jobs={job_id} --format=State"
        )
        
        if output:
            return output.splitlines()[0].strip()
        
        return "UNKNOWN"

    def cancel(self, job_id: int) -> None:
        job_id = int(job_id)
        self._run_remote(f"scancel {job_id}")

    def stage_payload(self, payload: bytes) -> str:
        if not self.config.work_root:
            raise ValueError("Slurm work_root is not configured")
        
        work_root = self.config.work_root
        
        if not work_root.startswith("/"):
            raise ValueError("Slurm work_root must be an absolute path")
        
        job_dir = f"{work_root.rstrip('/')}/{uuid.uuid4().hex}"
        input_path = f"{job_dir}/input.pkl"
        
        command = (
            f"mkdir -p {shlex.quote(job_dir)} && "
            f"cat > {shlex.quote(input_path)}"
        )
        
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.config.ssh_host,
                command,
            ],
            input=payload,
            capture_output=True,
            timeout=60,
        )
        
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.decode(errors="replace").strip()
            )
        
        return job_dir

    def submit_payload(self, payload: bytes, gpu: int = 1) -> tuple[int, str]:
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")

        # Upload payload
        job_dir = self.stage_payload(payload)
        
        # Extract execution UUID
        execution_id = job_dir.rsplit("/", 1)[-1]
        
        # Submit Slurm job
        job_id = self.submit(
            workdir=self.config.worker_workdir,
            script="project/scripts/worker.sbatch",
            gpu=gpu,
            script_args=(execution_id,),
        )
        
        return job_id, job_dir

    def wait_result(
        self,
        job_id: int,
        job_dir: str,
        timeout: int = 600,
        poll_interval: int = 5,
    ) -> bytes:
        deadline = time.monotonic() + timeout
        
        failed_states = (
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "OUT_OF_MEMORY",
            "NODE_FAIL",
            "BOOT_FAIL",
            "PREEMPTED",
            "DEADLINE",
            "REVOKED",
        )
        
        while True:
            state = self.status(job_id)
            
            if state == "COMPLETED":
                break
                
            if state.startswith(failed_states):
                raise RuntimeError(
                    f"Slurm job {job_id} failed: {state}"
                )
                
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Slurm job {job_id}. "
                    "The job may still be running."
                )
            
            time.sleep(poll_interval)
            
        # Download binary result through SSH stdout
        output_path = f"{job_dir}/output.pkl"
        
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.config.ssh_host,
                f"cat {shlex.quote(output_path)}",
            ],
            capture_output=True,
            timeout=60,
        )
        
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.decode(errors="replace").strip()
            )
            
        if not result.stdout:
            raise RuntimeError("Worker returned an empty result file")
        
        return result.stdout

    def stage_environment_files(self, project_dir: str) -> str:
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")
        
        project = Path(project_dir)
        names = ("pyproject.toml", "uv.lock")
        
        files = {
            name: (project / name).read_bytes() 
            for name in names
        }
        
        # Calculate environment hash
        hasher = hashlib.sha256()
        
        for name, content in files.items():
            hasher.update(name.encode())
            hasher.update(b"\0")
            hasher.update(len(content).to_bytes(8, "big"))
            hasher.update(content)
        
        env_id = hasher.hexdigest()
        
        env_dir = (
            f"{self.config.worker_workdir.rstrip('/')}"
            f"/environments/{env_id}"
        )
        
        paths = {
            name: f"{env_dir}/{name}" 
            for name in names
        }
        expected = [
            hashlib.sha256(files[name]).hexdigest()
            for name in names
        ]
        
        # Check remote cache
        def remote_hashes() -> list[str]:
            quoted = [
                shlex.quote(paths[name])
                for name in names
            ]
            
            command = (
                f"if [ -f {quoted[0]} ] && [ -f {quoted[1]} ]; then "
                f"sha256sum {' '.join(quoted)}; "
                f"fi"
            )
            
            output = self._run_remote(command)
            
            return [
                line.split()[0]
                for line in output.splitlines()
            ]
        
        if remote_hashes() == expected:
            print(f"Environment cache hit: {env_id}")
            return env_dir

        print(f"Environment cache miss: {env_id}")
        
        self._run_remote(
            f"mkdir -p {shlex.quote(env_dir)}"
        )
        
        # upload files atomically
        for name, content in files.items():
            remote_path = paths[name]
            temp_path = f"{remote_path}.tmp-{uuid.uuid4().hex}"
            
            command = (
                f"cat > {shlex.quote(temp_path)} && "
                f"mv -f -- {shlex.quote(temp_path)} "
                f"{shlex.quote(remote_path)}"
            )
            
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    self.config.ssh_host,
                    command,
                ],
                input=content,
                capture_output=True,
                timeout=60,
            )
            
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.decode(errors="replace")
                )
            
        actual = remote_hashes()
        if actual != expected:
            print("Environment direcotry: ", env_dir)
            print(self._run_remote(f"ls -al {shlex.quote(env_dir)}"))
            print(f"Expected hashes: {expected}")
            print(f"Actual hashes: {actual}")
            
            raise RuntimeError(
                "Environment file verification failed"
            )
            
        return env_dir

    def build_environment(self, env_dir: str, gpu: int = 1) -> int:
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")
        
        env_id = Path(env_dir).name
        
        if not re.fullmatch(r"[0-9a-f]{64}", env_id):
            raise ValueError("Invalid environment ID")
        
        expected_dir = (
            Path(self.config.worker_workdir)
            / "environments"
            / env_id
        )
        
        if Path(env_dir) != expected_dir:
            raise ValueError("Invalid environment directory")
        
        return self.submit(
            workdir=self.config.worker_workdir,
            script="project/scripts/environment-smoke.sbatch",
            gpu=gpu,
            script_args=(env_id,),
        )
        
    def wait_environment(self, job_id: int, timeout: int = 1800, poll_interval: int = 5) -> None:
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("Timeout and poll interval must be positive")
        
        deadline = time.monotonic() + timeout
        
        failed_states = (
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "OUT_OF_MEMORY",
            "NODE_FAIL",
            "BOOT_FAIL",
            "PREEMPTED",
            "DEADLINE",
            "REVOKED",
        )
        
        while True:
            state = self.status(job_id)
            
            if state == "COMPLETED":
                return

            if state.startswith(failed_states):
                raise RuntimeError(
                    f"Environment build {job_id} failed: {state}"
                )
                
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for environment build {job_id}. "
                    f"Last state: {state}"
                )
            
            time.sleep(poll_interval)

    def runtime_id(self) -> str:
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")
        
        image = (
            f"{self.config.worker_workdir.rstrip('/')}"
            "/images/uv-python312.sif"
        )
        
        quoted = shlex.quote(image)
        
        command = (
            f"test -r {quoted} && "
            f"image_hash=$(sha256sum {quoted} | cut -d' ' -f1) && "
            "arch=$(uname -m) && "
            """printf '%s:%s' "$image_hash" "$arch" | """
            "sha256sum | cut -d' ' -f1"
        )
        
        return self._run_remote(command)

    def environment_ready(self, env_dir: str) -> bool:
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")

        env_id = Path(env_dir).name
        
        if not re.fullmatch(r"[0-9a-f]{64}", env_id):
            raise ValueError("Invalid environment ID")

        expected_dir = (
            Path(self.config.worker_workdir)
            / "environments"
            / env_id
        )
        
        if Path(env_dir) != expected_dir:
            raise ValueError("Invalid environment directory")
        
        runtime_id = self.runtime_id()
        runtime_dir = f"{env_dir}/runtimes/{runtime_id}"
        ready = f"{runtime_dir}/.ready"
        python = f"{runtime_dir}/.venv/bin/python"
        
        command = (
            f"if test -f {shlex.quote(ready)} && "
            f"( test -L {shlex.quote(python)} || "
            f"test -f {shlex.quote(python)} ); then "
            "printf ready; "
            "else printf missing; fi"
        )
        
        return self._run_remote(command) == "ready"
        
    def prepare_environment(self, project_dir: str, gpu: int = 1) -> str:
        env_dir = self.stage_environment_files(project_dir)
        
        if self.environment_ready(env_dir):
            print("Runtime cache hit, skipping Slurm job")
            return env_dir
        
        print("Runtime cache miss, building environment")
        job_id = self.build_environment(env_dir, gpu=gpu)
        print(f"Submitted environment job: {job_id}")
        self.wait_environment(job_id)
        
        if not self.environment_ready(env_dir):
            raise RuntimeError("Environment build completed but cache is not ready")

        return env_dir
