import os

from nest.backends.slurm import SlurmBackend, SlurmConfig

def main():
    workdir = os.environ["NEST_SLURM_WORKDIR"]
    
    backend = SlurmBackend(
        SlurmConfig(
            ssh_host=os.environ["NEST_SLURM_HOST"],
            account=os.environ["NEST_SLURM_ACCOUNT"],
            partition="dev",
            worker_workdir=workdir,
        )
    )
    
    # Stage environment files
    env_dir = backend.stage_environment_files("examples/uv_demo")
    print(f"Environment: {env_dir}", flush=True)
    
    # Submit environment build
    job_id = backend.build_environment(env_dir, gpu=1)
    print(f"Submitted job: {job_id}", flush=True)
    
    # Wait for completion
    backend.wait_environment(job_id)
    print("Environment ready!")
    
if __name__ == "__main__":
    main()
