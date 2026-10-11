import shlex
import subprocess
from dataclasses import dataclass

@dataclass(frozen=True)
class SlurmConfig:
    ssh_host: str
    account: str
    partition: str = "dev"
    
class SlurmBackend:
    def __init__(self, config: SlurmConfig):
        self.config = config
        
    def _run_remote(self, command: str) -> str:
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
        )
        
        if result.returncode != 0:
            raise RuntimeError(
                f"Remote command failed:\n"
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        
        return result.stdout.strip()
    
    def submit(self, workdir: str, script: str = "hello.sbatch", gpu: int = 1) -> int:
        gpu_option = f"--gpus-per-node={gpu}" if gpu > 0 else ""
        
        command = (
            f"cd {shlex.quote(workdir)} && "
            f"sbatch --parsable "
            f"--account={shlex.quote(self.config.account)} "
            f"--partition={shlex.quote(self.config.partition)} "
            f"{gpu_option} "
            f"{shlex.quote(script)}"
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
