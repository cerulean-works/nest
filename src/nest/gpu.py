"""
GPU checks the worker runs before a GPU-requesting function.

`scripts/worker.sbatch` enables Apptainer NVIDIA passthrough (`--nv`) only when the
caller requested GPUs and Slurm's allocation of the node is consistent. It passes the
request as `NEST_GPU_REQUESTED` and Slurm's device mapping as `CUDA_VISIBLE_DEVICES`.
[`gpu.check_gpu_access`] then confirms, through the CUDA driver API, that the driver
works and sees exactly the devices Slurm assigned, so a broken node fails with a
diagnostic before user code runs instead of with an opaque framework error.
"""

import ctypes
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

CUDA_LIBRARY = "libcuda.so.1"
"""The NVIDIA driver library that Apptainer `--nv` binds into the container."""

# Driver API codes with an actionable explanation; others report the driver's name.
_CUDA_HINTS = {
    3: "the driver could not be initialized",
    46: "the GPUs are busy or in an exclusive compute mode used by another process",
    100: "no CUDA device is visible to the job",
    101: "CUDA_VISIBLE_DEVICES names a device the driver does not know",
    304: "the operating system refused the call; check device permissions and cgroups",
    803: "the user-mode driver and the kernel module versions differ; the node needs a reboot "
    "or driver reinstall",
    804: "the forward-compatibility driver does not support this GPU",
    999: "the driver reported an unknown error; check `nvidia-smi` and the kernel log",
}


class GpuUnavailableError(RuntimeError):
    """A GPU-requesting function cannot use the GPUs Slurm allocated. It did not run."""


@dataclass(frozen=True)
class GpuAccess:
    """
    GPUs confirmed usable by [`gpu.check_gpu_access`].

    Attributes:
        visible_devices (`str`): `CUDA_VISIBLE_DEVICES` exactly as Slurm assigned it.
        device_count (`int`): Devices the CUDA driver reports.
        driver_version (`str`): Highest CUDA version the driver supports, such as `13.2`.
    """

    visible_devices: str
    device_count: int
    driver_version: str


def requested_gpus(environ: Mapping[str, str] = os.environ) -> int:
    """
    Returns the GPU count the worker script passed as `NEST_GPU_REQUESTED`.

    Unset, as from an older worker script, means no GPU was requested.

    Raises:
        [`gpu.GpuUnavailableError`]: If the value is not a nonnegative integer.
    """
    value = environ.get("NEST_GPU_REQUESTED", "0") or "0"
    if not value.isdecimal():
        raise GpuUnavailableError(f"Invalid NEST_GPU_REQUESTED value {value!r}")
    return int(value)


def _cuda_error(library, code: int) -> str:
    name = ctypes.c_char_p()
    try:
        if library.cuGetErrorName(code, ctypes.byref(name)) == 0 and name.value:
            label = f"{name.value.decode(errors='replace')} ({code})"
        else:
            label = f"CUDA error {code}"
    except AttributeError:
        label = f"CUDA error {code}"
    hint = _CUDA_HINTS.get(code)
    return f"{label}: {hint}" if hint else label


def check_gpu_access(
    environ: Mapping[str, str] = os.environ,
    load: Callable[[str], Any] = ctypes.CDLL,
) -> GpuAccess | None:
    """
    Confirms that a GPU-requesting worker can use exactly the GPUs Slurm assigned.

    Does nothing for a CPU-only request. Never changes `CUDA_VISIBLE_DEVICES`.

    Args:
        environ (`Mapping[str, str]`, *optional*): Environment to read. Defaults to
            the process environment.
        load (`Callable[[str], Any]`, *optional*): Shared-library loader. Defaults
            to `ctypes.CDLL`.

    Returns:
        [`gpu.GpuAccess`] or `None`: The confirmed GPUs, or `None` when no GPU was
        requested.

    Raises:
        [`gpu.GpuUnavailableError`]: If the device mapping is missing, the NVIDIA driver
            library is absent or fails to initialize, or the driver sees a different
            number of devices than Slurm assigned.
    """
    requested = requested_gpus(environ)
    if requested == 0:
        return None

    visible = environ.get("CUDA_VISIBLE_DEVICES")
    if not visible:
        raise GpuUnavailableError(
            f"gpu={requested} was requested, but the worker received no CUDA_VISIBLE_DEVICES "
            "mapping from Slurm, so no GPU was exposed. Ask the operator to check that the "
            "deployed scripts/worker.sbatch and the Controller are the same release."
        )
    expected = len(visible.split(","))

    try:
        library = load(CUDA_LIBRARY)
    except OSError as exc:
        raise GpuUnavailableError(
            f"gpu={requested} was requested, but the NVIDIA driver library {CUDA_LIBRARY} is "
            f"not available in the worker container ({exc}). The compute node may have no "
            "NVIDIA driver, or Apptainer --nv could not bind it; ask the operator to run "
            "`nvidia-smi` on the node."
        ) from exc

    code = library.cuInit(0)
    if code != 0:
        raise GpuUnavailableError(
            f"CUDA driver initialization failed with CUDA_VISIBLE_DEVICES={visible}: "
            f"{_cuda_error(library, code)}"
        )

    version = ctypes.c_int()
    code = library.cuDriverGetVersion(ctypes.byref(version))
    if code != 0:
        raise GpuUnavailableError(
            f"Could not read the CUDA driver version: {_cuda_error(library, code)}"
        )
    driver_version = f"{version.value // 1000}.{version.value % 1000 // 10}"

    count = ctypes.c_int()
    code = library.cuDeviceGetCount(ctypes.byref(count))
    if code != 0:
        raise GpuUnavailableError(f"Could not count CUDA devices: {_cuda_error(library, code)}")

    if count.value != expected:
        raise GpuUnavailableError(
            f"The CUDA driver sees {count.value} device(s), but Slurm assigned {expected} "
            f"(CUDA_VISIBLE_DEVICES={visible}). The function did not run, so it cannot use "
            "a device outside its allocation; ask the operator to check the node's GPU "
            "configuration."
        )

    return GpuAccess(visible, count.value, driver_version)
