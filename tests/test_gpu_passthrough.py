"""GPU passthrough regressions for `scripts/worker.sbatch` and the worker's GPU check.

The worker script runs unchanged under `FakeHpc`, which emulates Slurm's GPU
allocation variables and Apptainer's `--nv` with a fake NVIDIA driver library.
These tests prove launch decisions and diagnostics; they make no hardware claim.
"""

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import cloudpickle
import pytest
from fake_hpc import FakeHpc
from test_controller import controller_at_free_port

import nest
from nest.backends.slurm import new_execution_id
from nest.errors import RemoteExecutionError, SlurmJobError
from nest.gpu import GpuUnavailableError, check_gpu_access, requested_gpus

WORKER_SCRIPT = "project/scripts/worker.sbatch"
# Module-level helpers run remotely, where this test module cannot be imported.
cloudpickle.register_pickle_by_value(sys.modules[__name__])

UUIDS = "GPU-1f2e3d4c-0000-1111-2222-333344445555,GPU-9a8b7c6d-6666-7777-8888-999900001111"


@pytest.fixture
def hpc(tmp_path):
    return FakeHpc(tmp_path)


@pytest.fixture
def slurm_controller(monkeypatch, hpc):
    with controller_at_free_port(**hpc.controller_settings()) as (_, stub, address):
        monkeypatch.setenv("NEST_CONTROLLER_ADDRESS", address)
        yield stub


def _worker_runs(hpc):
    """The Apptainer launches of `worker.sbatch`, which always pass the GPU request."""
    return [call for call in hpc.calls("apptainer") if "NEST_GPU_REQUESTED" in call["env"]]


def _gpu_view():
    """Runs remotely: what the function can see of the GPUs."""
    import ctypes
    import os

    view = {
        "visible": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "order": os.environ.get("CUDA_DEVICE_ORDER"),
    }
    try:
        cuda = ctypes.CDLL("libcuda.so.1")
    except OSError:
        view["driver"] = False
        return view
    count = ctypes.c_int()
    assert cuda.cuInit(0) == 0
    assert cuda.cuDeviceGetCount(ctypes.byref(count)) == 0
    view.update(driver=True, count=count.value)
    return view


def _run_worker(hpc, func, *args, flags=(), legacy=False):
    """Submits `worker.sbatch` directly, as the Controller does, and returns its outcome."""
    execution_id = new_execution_id()
    job_dir = hpc.base / "jobs" / execution_id
    job_dir.mkdir(parents=True)
    (job_dir / "input.pkl").write_bytes(cloudpickle.dumps((func, (), {})))
    script_args = [execution_id, "", str(hpc.base), "", "INFO", *args]
    if legacy:
        script_args = script_args[:5]
    env = {**os.environ, **hpc.controller_settings()}
    result = subprocess.run(
        ["sbatch", "--parsable", *flags, WORKER_SCRIPT, *script_args],
        cwd=hpc.base,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    job_id = int(result.stdout.split(";")[0])
    output = job_dir / "output.pkl"
    value = cloudpickle.loads(output.read_bytes()) if output.is_file() else None
    return hpc.job_state(job_id), hpc.job_output(job_id), value


# Launch decisions through the Controller


def test_gpu_request_enables_passthrough_with_the_slurm_mapping(slurm_controller, hpc):
    @nest.remote(gpu=1)
    def view():
        return _gpu_view()

    assert view.remote() == {"visible": "0", "order": None, "driver": True, "count": 1}
    [run] = _worker_runs(hpc)
    assert run["nv"] is True
    assert run["clean"] is True
    assert run["env"]["CUDA_VISIBLE_DEVICES"] == "0"
    assert run["env"]["NEST_GPU_REQUESTED"] == "1"
    [job] = [c for c in hpc.calls("sbatch") if c["script"] == WORKER_SCRIPT]
    assert "--gpus-per-node=1" in job["flags"]
    output = hpc.job_output(job["job_id"])
    assert "GPU mode: enabled requested=1 allocated=1 CUDA_VISIBLE_DEVICES=0" in output
    assert "GPUs ready count=1 visible_devices=0 cuda_driver=13.2" in output


def test_multi_gpu_request_sees_exactly_its_allocation(slurm_controller, hpc):
    @nest.remote(gpu=2)
    def view():
        return _gpu_view()

    assert view.remote() == {"visible": "0,1", "order": None, "driver": True, "count": 2}
    [run] = _worker_runs(hpc)
    assert (run["nv"], run["env"]["CUDA_VISIBLE_DEVICES"]) == (True, "0,1")


def test_cpu_only_request_is_not_given_the_script_default_gpu(slurm_controller, hpc):
    @nest.remote(gpu=0)
    def view():
        return _gpu_view()

    # The script's #SBATCH default still allocates a GPU, but none is exposed.
    assert view.remote() == {"visible": "", "order": None, "driver": False}
    [job] = [c for c in hpc.calls("sbatch") if c["script"] == WORKER_SCRIPT]
    assert not any(flag.startswith("--gpus") for flag in job["flags"])
    [run] = _worker_runs(hpc)
    assert run["nv"] is False
    assert run["env"]["CUDA_VISIBLE_DEVICES"] == ""
    assert run["env"]["NEST_GPU_REQUESTED"] == "0"
    output = hpc.job_output(job["job_id"])
    assert "CPU-only request: the 1 GPU(s) this job holds are not exposed" in output
    assert "GPU mode: disabled (gpu=0)" in output


def test_remote_cuda_error_returns_traceback_from_a_completed_job(slurm_controller, hpc):
    @nest.remote(gpu=1)
    def train():
        raise RuntimeError("CUDA error: out of memory")

    with pytest.raises(RemoteExecutionError) as caught:
        train.remote()
    assert caught.value.remote_type == "builtins.RuntimeError"
    assert caught.value.remote_message == "CUDA error: out of memory"
    assert "in train" in caught.value.remote_traceback
    # An application failure is still a scheduler success: Slurm reports COMPLETED.
    [job] = [c for c in hpc.calls("sbatch") if c["script"] == WORKER_SCRIPT]
    assert hpc.job_state(job["job_id"]) == "COMPLETED"


def test_missing_driver_is_a_remote_error_before_user_code(slurm_controller, hpc, tmp_path):
    hpc.nvidia_driver(present=False)
    marker = tmp_path / "ran"

    @nest.remote(gpu=1)
    def touch():
        marker.write_text("ran")

    with pytest.raises(RemoteExecutionError) as caught:
        touch.remote()
    assert caught.value.remote_type == "nest.gpu.GpuUnavailableError"
    assert "libcuda.so.1 is not available in the worker container" in caught.value.remote_message
    assert "nvidia-smi" in caught.value.remote_message
    assert not marker.exists()


# Visibility validation in worker.sbatch


def test_site_uuid_mapping_and_device_order_are_preserved(hpc):
    hpc.gpu_environment(
        SLURM_GPUS_ON_NODE="2", CUDA_VISIBLE_DEVICES=UUIDS, CUDA_DEVICE_ORDER="PCI_BUS_ID"
    )
    state, _, value = _run_worker(hpc, _gpu_view, "2", flags=["--gpus-per-node=2"])
    assert state == "COMPLETED"
    assert value == {"visible": UUIDS, "order": "PCI_BUS_ID", "driver": True, "count": 2}
    [run] = _worker_runs(hpc)
    assert run["env"]["CUDA_VISIBLE_DEVICES"] == UUIDS


def test_mig_uuid_mapping_is_accepted(hpc):
    mig = "MIG-4b1f6a2c-1234-5678-9abc-def012345678"
    hpc.gpu_environment(CUDA_VISIBLE_DEVICES=mig)
    state, _, value = _run_worker(hpc, _gpu_view, "1", flags=["--gpus-per-node=1"])
    assert state == "COMPLETED"
    assert value["visible"] == mig


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"CUDA_VISIBLE_DEVICES": None}, "Slurm did not set CUDA_VISIBLE_DEVICES"),
        ({"CUDA_VISIBLE_DEVICES": ""}, "CUDA_VISIBLE_DEVICES is empty"),
        ({"CUDA_VISIBLE_DEVICES": "NoDevFiles"}, "unrecognized device 'NoDevFiles'"),
        ({"CUDA_VISIBLE_DEVICES": "0;rm -rf /"}, "unrecognized device"),
        ({"CUDA_VISIBLE_DEVICES": "0,0"}, "lists device '0' more than once"),
        ({"CUDA_VISIBLE_DEVICES": "0,1"}, "lists 2 device(s), but Slurm allocated 1"),
        ({"SLURM_GPUS_ON_NODE": None}, "SLURM_GPUS_ON_NODE=<unset>"),
        ({"SLURM_GPUS_ON_NODE": "two"}, "SLURM_GPUS_ON_NODE=two"),
        ({"SLURM_GPUS_ON_NODE": "0"}, "Slurm allocated 0 GPU(s)"),
        ({"SLURM_JOB_ID": ""}, "SLURM_JOB_ID is unset"),
    ],
)
def test_unsafe_visibility_fails_closed_without_launching(hpc, tmp_path, environment, message):
    hpc.gpu_environment(**environment)
    marker = tmp_path / "ran"

    def touch():
        marker.write_text("ran")

    state, output, value = _run_worker(hpc, touch, "1", flags=["--gpus-per-node=1"])
    assert state == "FAILED"
    [error] = [line for line in output.splitlines() if line.startswith("GPU allocation error:")]
    assert message in error
    assert "No GPU was exposed and the user function did not run." in output
    assert value is None
    assert hpc.calls("apptainer") == []
    assert not marker.exists()


def test_invalid_gpu_request_argument_is_rejected(hpc):
    state, output, _ = _run_worker(hpc, _gpu_view, "-1")
    assert state == "FAILED"
    assert "Invalid GPU request: -1" in output
    assert hpc.calls("apptainer") == []


def test_older_controller_without_a_gpu_request_runs_cpu_only(hpc):
    state, output, value = _run_worker(hpc, _gpu_view, flags=["--gpus-per-node=1"], legacy=True)
    assert state == "COMPLETED"
    assert value == {"visible": "", "order": None, "driver": False}
    [run] = _worker_runs(hpc)
    assert run["nv"] is False
    assert "GPU request: not passed by the Controller" in output


def test_larger_allocation_is_exposed_as_assigned_not_trimmed(hpc):
    hpc.gpu_environment(SLURM_GPUS_ON_NODE="2", CUDA_VISIBLE_DEVICES="2,5")
    state, output, value = _run_worker(hpc, _gpu_view, "1", flags=["--gpus-per-node=1"])
    assert state == "COMPLETED"
    assert value["visible"] == "2,5"
    assert "Slurm allocated 2 GPU(s) for gpu=1" in output


# Worker GPU check


@pytest.mark.parametrize(
    ("driver", "message"),
    [
        ({"init_error": 999}, "CUDA_ERROR_UNKNOWN (999): the driver reported an unknown error"),
        ({"init_error": 100}, "CUDA_ERROR_NO_DEVICE (100): no CUDA device is visible"),
        ({"device_count": 2}, "The CUDA driver sees 2 device(s), but Slurm assigned 1"),
    ],
)
def test_driver_failures_are_reported_before_user_code(hpc, tmp_path, driver, message):
    hpc.nvidia_driver(**driver)
    marker = tmp_path / "ran"

    def touch():
        marker.write_text("ran")

    state, _, value = _run_worker(hpc, touch, "1", flags=["--gpus-per-node=1"])
    assert state == "COMPLETED"
    assert value["__nest_remote_error_v1__"] is True
    assert value["type"] == "nest.gpu.GpuUnavailableError"
    assert message in value["message"]
    assert "GpuUnavailableError" in value["traceback"]
    assert not marker.exists()


class _FakeCuda:
    def __init__(self, init=0, count=1):
        self.init, self.count = init, count

    def cuInit(self, flags):
        return self.init

    def cuDriverGetVersion(self, version):
        ctypes.cast(version, ctypes.POINTER(ctypes.c_int)).contents.value = 12040
        return 0

    def cuDeviceGetCount(self, count):
        ctypes.cast(count, ctypes.POINTER(ctypes.c_int)).contents.value = self.count
        return 0

    def cuGetErrorName(self, code, name):
        return 1


def test_check_gpu_access_skips_cpu_only_requests():
    def never(name):
        raise AssertionError("loaded the driver for a CPU-only request")

    assert check_gpu_access({}, never) is None
    assert check_gpu_access({"NEST_GPU_REQUESTED": "0"}, never) is None


def test_check_gpu_access_reports_the_confirmed_gpus():
    access = check_gpu_access(
        {"NEST_GPU_REQUESTED": "2", "CUDA_VISIBLE_DEVICES": UUIDS}, lambda _: _FakeCuda(count=2)
    )
    assert access is not None
    assert (access.visible_devices, access.device_count) == (UUIDS, 2)
    assert access.driver_version == "12.4"


@pytest.mark.parametrize(
    ("environ", "cuda", "message"),
    [
        ({"NEST_GPU_REQUESTED": "1"}, _FakeCuda(), "received no CUDA_VISIBLE_DEVICES"),
        (
            {"NEST_GPU_REQUESTED": "1", "CUDA_VISIBLE_DEVICES": ""},
            _FakeCuda(),
            "received no CUDA_VISIBLE_DEVICES",
        ),
        ({"NEST_GPU_REQUESTED": "x"}, _FakeCuda(), "Invalid NEST_GPU_REQUESTED"),
        (
            {"NEST_GPU_REQUESTED": "1", "CUDA_VISIBLE_DEVICES": "0"},
            _FakeCuda(init=803),
            "CUDA error 803: the user-mode driver and the kernel module versions differ",
        ),
        (
            {"NEST_GPU_REQUESTED": "1", "CUDA_VISIBLE_DEVICES": "0,1"},
            _FakeCuda(count=1),
            "sees 1 device(s), but Slurm assigned 2",
        ),
    ],
)
def test_check_gpu_access_failures(environ, cuda, message):
    with pytest.raises(GpuUnavailableError) as caught:
        check_gpu_access(environ, lambda _: cuda)
    assert message in str(caught.value)


def test_check_gpu_access_reports_a_missing_driver_library():
    def missing(name):
        raise OSError(f"{name}: cannot open shared object file")

    with pytest.raises(GpuUnavailableError) as caught:
        check_gpu_access({"NEST_GPU_REQUESTED": "1", "CUDA_VISIBLE_DEVICES": "0"}, missing)
    assert "libcuda.so.1: cannot open shared object file" in str(caught.value)


def test_requested_gpus_treats_unset_as_cpu_only():
    assert requested_gpus({}) == 0
    assert requested_gpus({"NEST_GPU_REQUESTED": ""}) == 0
    assert requested_gpus({"NEST_GPU_REQUESTED": "4"}) == 4


def test_fake_driver_is_on_the_path_only_with_nv(hpc):
    # Guards the emulation itself: without --nv there is no driver to find.
    state, _, value = _run_worker(hpc, _gpu_view, "0")
    assert state == "COMPLETED"
    assert value["driver"] is False
    assert Path(hpc.nvidia_libs, "libcuda.so.1").is_file()


def test_slurm_job_error_for_unsafe_visibility_through_the_controller(slurm_controller, hpc):
    hpc.gpu_environment(CUDA_VISIBLE_DEVICES=None)

    @nest.remote(gpu=1)
    def view():
        return _gpu_view()

    with pytest.raises(SlurmJobError) as caught:
        view.remote()
    assert "failed: FAILED" in str(caught.value)
    assert _worker_runs(hpc) == []
