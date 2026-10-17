import os
from pathlib import Path

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
    
    project = Path("examples/uv_demo")
    
    env1 = backend.prepare_environment(str(project), gpu=1)
    env2 = backend.prepare_environment_bytes(
        pyproject_toml=(project / "pyproject.toml").read_bytes(),
        uv_lock=(project / "uv.lock").read_bytes(),
        gpu=1,
    )
    
    assert env1 == env2
    print("Environment: ", env2)
    print("Pass: Both APIs use the same environment")
    
if __name__ == "__main__":
    main()
