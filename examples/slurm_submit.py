import os

from nest.backends.slurm import SlurmBackend, SlurmConfig


def main():
    """
    Submits the remote `hello.sbatch` script and prints its Slurm job ID.

    Reads the SSH host, account, and work directory from `NEST_SLURM_HOST`,
    `NEST_SLURM_ACCOUNT`, and `NEST_SLURM_WORKDIR`.
    """
    config = SlurmConfig(
        ssh_host=os.environ["NEST_SLURM_HOST"],
        account=os.environ["NEST_SLURM_ACCOUNT"],
        partition="dev",
    )

    backend = SlurmBackend(config)

    job_id = backend.submit(
        workdir=os.environ["NEST_SLURM_WORKDIR"],
        script="hello.sbatch",
        gpu=1,
    )

    print(f"Submitted Slurm job: {job_id}")


if __name__ == "__main__":
    main()
