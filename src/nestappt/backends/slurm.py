import shlex
import subprocess
from dataclasses import dataclass

@dataclass(frozen=True)
class SlurmConfig:
    ssh_host: str
    account: str
    partition: str = "dev"
    time: str | None = None
    
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
    
    def submit(
        self, 
        workdir: str,
        script: str = "hello.sbatch", 
        gpu: int = 1,
        *,
        account: str | None = None,
        partition: str | None = None,
        time: str | None = None,
    ) -> int:
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
