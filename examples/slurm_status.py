import os
import sys

from nest.backends.slurm import SlurmBackend, SlurmConfig


def main():
    """
    Prints the Slurm state of the job ID supplied on the command line.

    Reads the SSH host and account from `NEST_SLURM_HOST` and
    `NEST_SLURM_ACCOUNT`.
    """
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
