import uuid
import time
import shlex
import subprocess
from dataclasses import dataclass

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
            script="worker.sbatch",
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
