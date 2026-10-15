import os
import sys

from nest.backends.slurm import SlurmBackend, SlurmConfig

def main():
    backend = SlurmBackend(
        SlurmConfig(
            ssh_host=os.environ["NEST_SLURM_HOST"],
            account=os.environ["NEST_SLURM_ACCOUNT"],
            partition="dev",
        )
    )
    
    job_id = int(sys.argv[1])
    print(f"Job {job_id}: {backend.status(job_id)}")
    
if __name__ == "__main__":
    main()
