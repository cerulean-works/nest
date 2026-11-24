# Run functions on GPUs

Request GPUs with `gpu=`. The function then runs with the host's NVIDIA driver and
exactly the GPUs Slurm allocated to the job.

```python
import nest


@nest.remote(gpu=1, project=".")
def matmul():
    """Multiplies two matrices on the allocated GPU."""
    import torch

    a = torch.ones(32, 32, device="cuda")
    result = (a @ a).sum().item()
    torch.cuda.synchronize()
    return torch.cuda.device_count(), result


print(matmul.remote())  # (1, 32768.0)
```

`gpu` is the number of GPUs per node, passed to Slurm as `--gpus-per-node`. Install
a CUDA build of your framework through the [project's dependencies](project-dependencies);
the driver comes from the compute node.

## How the worker exposes GPUs

`scripts/worker.sbatch` decides per job, from the request the Controller passes and
from Slurm's allocation of the node:

| Request | Allocation | What the function sees |
| --- | --- | --- |
| `gpu=0` (the default) | None, or a GPU from the script's `#SBATCH` default | No NVIDIA passthrough and `CUDA_VISIBLE_DEVICES=""`. The worker log says the held GPU is not exposed. |
| `gpu=N` | `SLURM_GPUS_ON_NODE` ≥ N and a valid `CUDA_VISIBLE_DEVICES` | Apptainer `--nv` with `CUDA_VISIBLE_DEVICES` copied verbatim across `--cleanenv`. |
| `gpu=N` | Missing or inconsistent metadata | Nothing runs. The job fails with `GPU allocation error: ...` in its output file. |
| Not passed (Controller older than the script) | Any | Treated as `gpu=0`. |

The worker never widens or invents the mapping:

- `CUDA_VISIBLE_DEVICES` is kept exactly as Slurm wrote it, including GPU and MIG
  UUIDs, and `CUDA_DEVICE_ORDER` is kept when the site sets it. If Slurm allocated
  more GPUs than requested, all allocated GPUs are exposed as assigned.
- An unset or empty `CUDA_VISIBLE_DEVICES`, an entry that is not an ordinal or a
  `GPU-`/`MIG-` UUID (such as `NoDevFiles`), a duplicate entry, a device count
  different from `SLURM_GPUS_ON_NODE`, or a missing `SLURM_GPUS_ON_NODE` or
  `SLURM_JOB_ID` stops the job before Apptainer starts. Nest never falls back to all
  host GPUs.
- Environment variables are not an isolation boundary. Slurm's cgroup device
  constraint (`ConstrainDevices=yes`) is what keeps a job off unallocated GPUs;
  Nest does not bypass or replace it.

## GPU errors

Before the function runs, the worker loads the driver (`libcuda.so.1`), initializes
CUDA, and checks that the driver sees as many devices as Slurm assigned. A failure
raises `RemoteExecutionError` with `remote_type == "nest.gpu.GpuUnavailableError"`,
and the function does not run:

| Message | Cause |
| --- | --- |
| *libcuda.so.1 is not available in the worker container* | The node has no NVIDIA driver, or `--nv` could not bind it. |
| *CUDA driver initialization failed ... CUDA_ERROR_NO_DEVICE (100)* | No device is reachable, often a cgroup or device-permission problem. |
| *... (803): the user-mode driver and the kernel module versions differ* | The node needs a driver reinstall or reboot. |
| *The CUDA driver sees M device(s), but Slurm assigned N* | The node's GPU configuration disagrees with Slurm's. |

The worker log records the driver's CUDA version (`GPUs ready ... cuda_driver=13.2`).
A framework built for a newer CUDA than the driver supports fails inside your
function; its exception and traceback come back as usual, for example PyTorch's
*The NVIDIA driver on your system is too old*.

Errors your function raises, CUDA errors included, are its result: the worker
writes the error record and exits successfully, so Slurm reports `COMPLETED`, and
an Execution Registry attempt is recorded as `succeeded`. Only scheduler or
transport failures, including `GPU allocation error`, end the job as `FAILED`
(`SlurmJobError`). Recording application failures separately in the registry is
tracked in [#73](https://github.com/cerulean-works/nest/issues/73).

## Roll out

The change has two parts that can be deployed in either order:

1. Update the Controller. It passes the GPU request as the worker script's sixth
   argument; an older script ignores it.
2. Update `scripts/worker.sbatch` in the deployed `project/` directory on the
   cluster. Until the Controller is updated, the new script runs every job CPU-only,
   as before.

Then, on an authorized NVIDIA node, run the example above with `gpu=1`, then `gpu=2`
where the account allows it, and a `gpu=0` function that checks
`torch.cuda.is_available()` is `False`. Each job's output file shows the decision
(`GPU mode: enabled requested=1 allocated=1 CUDA_VISIBLE_DEVICES=...`).

To roll back, restore the previous `scripts/worker.sbatch`; a job launched by it
gets no `--nv`, so GPU functions fail at `torch.cuda.is_available()` as before.
If the cluster copy carries a local `--nv` edit, compare it with the release
before replacing it.

## Limitations

- With `gpu=0` the script's `#SBATCH --gpus-per-node=1` default still allocates
  a GPU, which is held but not exposed. Remove that directive on a site whose GPU
  partition accepts CPU-only jobs.
- Allocation checks cover the node the batch script runs on; multi-node GPU jobs
  are not supported.
- CI uses an emulated driver. Hardware behavior is verified only by the live checks
  above.
