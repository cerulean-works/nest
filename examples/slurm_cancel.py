import os

from nestappt.backends.slurm import SlurmBackend, SlurmConfig

def main():
    backend = SlurmBackend(
        SlurmConfig(
            ssh_host=os.environ["NESTAPPT_SLURM_HOST"],
            account=os.environ["NESTAPPT_SLURM_ACCOUNT"],
            partition="dev",
        )
    )
    
    job_id = backend.submit(
        workdir=os.environ["NESTAPPT_SLURM_WORKDIR"],
        script="cancel-test.sbatch",
        gpu=1,
    )
    
    print(f"Submitted: {job_id}")
    
    try:
        print(f"Before: {backend.status(job_id)}")
    finally:
        backend.cancel(job_id)
    
    print(f"After: {backend.status(job_id)}")
    
if __name__ == "__main__":
    main()
    