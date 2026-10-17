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
    
    env_dir = backend.prepare_environment("examples/uv_demo", gpu=1)
    print(f"Environment ready: {env_dir}")
    
if __name__ == "__main__":
    main()
