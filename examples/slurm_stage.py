import hashlib
import os

import cloudpickle

from nest.backends.slurm import SlurmBackend, SlurmConfig


def compute(x):
    """
    Returns twice the input value.

    Args:
        x (`int`): Value to double in this example.

    Returns:
        `int`: Twice `x`.
    """
    return x * 2


def main():
    """
    Uploads a serialized call and prints its directory and payload checksum.

    Reads `NEST_SLURM_HOST`, `NEST_SLURM_ACCOUNT`, and `NEST_SLURM_WORKDIR`.
    Stages the payload without submitting a worker job.
    """
    backend = SlurmBackend(
        SlurmConfig(
            ssh_host=os.environ["NEST_SLURM_HOST"],
            account=os.environ["NEST_SLURM_ACCOUNT"],
            work_root=(os.environ["NEST_SLURM_WORKDIR"] + "/jobs"),
        )
    )

    payload = cloudpickle.dumps((compute, (21,), {}))

    job_dir = backend.stage_payload(payload)
    print("Remote directory: ", job_dir)
    print("Payload SHA256: ", hashlib.sha256(payload).hexdigest())


if __name__ == "__main__":
    main()
