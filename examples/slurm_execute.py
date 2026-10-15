import os
import cloudpickle

from nest.backends.slurm import SlurmBackend, SlurmConfig

def compute(x):
    return x * 2

def main():
    workdir = os.environ["NEST_SLURM_WORKDIR"]

    backend = SlurmBackend(
        SlurmConfig(
            ssh_host=os.environ["NEST_SLURM_HOST"],
            account=os.environ["NEST_SLURM_ACCOUNT"],
            partition="dev",
            time="00:05:00",
            work_root=f"{workdir}/jobs",
            worker_workdir=workdir,
        )
    )
    
    # Serialize Python function
    payload = cloudpickle.dumps(
        (compute, (21,), {})
    )
    
    # Submit to Slurm
    job_id, job_dir = backend.submit_payload(payload, gpu=1)
    print(f"Submitted job: {job_id}")
    print(f"Job directory: {job_dir}")
    
    # Wait for completion
    result_bytes = backend.wait_result(job_id, job_dir)
    
    # Deserialize result
    result = cloudpickle.loads(result_bytes)
    print(f"Remote Result: {result}")
    
if __name__ == "__main__":
    main()
