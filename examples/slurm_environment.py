import os

from nest.backends.slurm import SlurmBackend, SlurmConfig

def main():
    """
    Stages the demo project dependency files and prints the remote directory.

    Reads `NEST_SLURM_HOST`, `NEST_SLURM_ACCOUNT`, and `NEST_SLURM_WORKDIR`.
    Stages `examples/uv_demo` without submitting an environment build.
    """
    backend = SlurmBackend(
        SlurmConfig(
            ssh_host=os.environ["NEST_SLURM_HOST"],
            account=os.environ["NEST_SLURM_ACCOUNT"],
            worker_workdir=os.environ["NEST_SLURM_WORKDIR"],
        )
    )
    
    env_dir = backend.stage_environment_files("examples/uv_demo")
    print("Environment direcotry: ", env_dir)
    
if __name__ == "__main__":
    main()
