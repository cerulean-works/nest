import os
import hashlib
import cloudpickle

from nestappt.backends.slurm import SlurmBackend, SlurmConfig

def compute(x):
    return x * 2

def main():
    backend = SlurmBackend(
        SlurmConfig(
            ssh_host=os.environ["NESTAPPT_SLURM_HOST"],
            account=os.environ["NESTAPPT_SLURM_ACCOUNT"],
            work_root=(
                os.environ["NESTAPPT_SLURM_WORKDIR"] + "/jobs"
            ),
        )
    )
    
    payload = cloudpickle.dumps(
        (compute, (21,), {})
    )
    
    job_dir = backend.stage_payload(payload)
    print("Remote directory: ", job_dir)
    print("Payload SHA256: ", hashlib.sha256(payload).hexdigest())
    
if __name__ == "__main__":
    main()
