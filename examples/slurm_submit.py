import os

from nestappt.backends.slurm import SlurmBackend, SlurmConfig

def main():
    config = SlurmConfig(
        ssh_host=os.environ["NESTAPPT_SLURM_HOST"],
        account=os.environ["NESTAPPT_SLURM_ACCOUNT"],
        partition="dev",
    )
    
    backend = SlurmBackend(config)
    
    job_id = backend.submit(
        workdir=os.environ["NESTAPPT_SLURM_WORKDIR"],
        script="hello.sbatch",
        gpu=1,
    )
    
    print(f"Submitted Slurm job: {job_id}")
    
if __name__ == "__main__":
    main()
