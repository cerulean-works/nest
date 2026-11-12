import hashlib
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .._logging import current_request_id, get_logger, operation

logger = get_logger(__name__)


@dataclass(frozen=True)
class SlurmConfig:
    """
    Stores SSH connection details, Slurm defaults, and remote working paths.

    Attributes:
        ssh_host (`str`): SSH host name or configured SSH alias.
        account (`str`): Default Slurm account.
        partition (`str`, *optional*, defaults to `"dev"`): Default Slurm partition.
        time (`str`, *optional*): Default `sbatch --time` value. Omits the flag if unset.
        work_root (`str`, *optional*): Absolute directory for staged execution payloads.
        worker_workdir (`str`, *optional*): Remote Nest directory containing the
            deployed `project`, `images`, and `environments` directories.
    """

    ssh_host: str
    account: str
    partition: str = "dev"
    time: str | None = None
    work_root: str | None = None
    worker_workdir: str | None = None


class SlurmBackend:
    """Stages files and manages Slurm jobs through noninteractive SSH commands."""

    def __init__(self, config: SlurmConfig):
        """
        Stores the backend configuration without connecting to the cluster.

        Args:
            config ([`backends.slurm.SlurmConfig`]): SSH host, Slurm defaults, and paths.
        """
        self.config = config

    def _run_remote(self, command: str) -> str:
        """
        Runs a text command over SSH with batch mode enabled.

        Args:
            command (`str`): Shell command to execute on the configured host.

        Returns:
            `str`: Standard output with leading and trailing whitespace removed.

        Raises:
            `TimeoutError`: If SSH exceeds 60 seconds. The remote operation's status
                remains unknown.
            `RuntimeError`: If SSH exits with a nonzero status.
        """
        started = time.monotonic()
        logger.debug("SSH command started host=%s", self.config.ssh_host)
        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    self.config.ssh_host,
                    command,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                "Remote command timed out after 60 seconds. The Slurm operation status is unknown."
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"Remote command failed:\n{result.stderr.strip() or result.stdout.strip()}"
            )

        logger.debug(
            "SSH command completed host=%s elapsed_ms=%.1f",
            self.config.ssh_host,
            (time.monotonic() - started) * 1000,
        )
        return result.stdout.strip()

    @operation(logger, "Slurm submit")
    def submit(
        self,
        workdir: str,
        script: str = "hello.sbatch",
        gpu: int = 1,
        *,
        account: str | None = None,
        partition: str | None = None,
        time: str | None = None,
        script_args: tuple[str, ...] = (),
    ) -> int:
        """
        Submits a batch script from a remote working directory.

        Args:
            workdir (`str`): Remote directory in which to run `sbatch`.
            script (`str`, *optional*, defaults to `"hello.sbatch"`): Script path,
                resolved from `workdir` when relative.
            gpu (`int`, *optional*, defaults to `1`): GPUs per node. Omits the GPU
                flag when `0`; script directives can still request GPUs.
            account (`str`, *optional*): Account override. Uses the configured account
                when unset or empty.
            partition (`str`, *optional*): Partition override. Uses the configured
                partition when unset or empty.
            time (`str`, *optional*): Time limit override. Uses the configured limit
                when unset or empty. Omits the flag if neither has a value.
            script_args (`tuple[str, ...]`, *optional*, defaults to `()`): Arguments
                appended after the script path.

        Returns:
            `int`: Job ID parsed from `sbatch --parsable` output.

        Raises:
            `TypeError`: If `script_args` is a string.
            `ValueError`: If the GPU count is negative.
            `RuntimeError`: If submission fails or its output has no decimal job ID.
            `TimeoutError`: If the SSH command exceeds 60 seconds.
        """
        if isinstance(script_args, str):
            raise TypeError("script_args must be a tuple of strings")

        if gpu < 0:
            raise ValueError("GPU count cannot be negative")

        args = [
            "sbatch",
            "--parsable",
            f"--account={account or self.config.account}",
            f"--partition={partition or self.config.partition}",
        ]

        if gpu > 0:
            args.append(f"--gpus-per-node={gpu}")

        time_limit = time or self.config.time

        if time_limit:
            args.append(f"--time={time_limit}")

        args.append(script)
        args.extend(script_args)

        command = f"cd {shlex.quote(workdir)} && {shlex.join(args)}"

        output = self._run_remote(command)
        job_id = output.split(";")[0].strip()

        if not job_id.isdecimal():
            raise RuntimeError(f"Unexpected sbatch output: {output}")

        logger.info(
            "Slurm job submitted job_id=%s script=%s gpu=%s partition=%s",
            job_id,
            script,
            gpu,
            partition or self.config.partition,
        )
        return int(job_id)

    def status(self, job_id: int) -> str:
        """
        Looks up a job's state in the active queue, then in accounting history.

        Args:
            job_id (`int`): Slurm job ID.

        Returns:
            `str`: First state reported by `squeue` or `sacct`, or `"UNKNOWN"` when
                neither returns a state.

        Raises:
            `RuntimeError`: If a remote query fails, except an invalid job ID from `squeue`.
            `TimeoutError`: If an SSH command exceeds 60 seconds.
        """
        job_id = int(job_id)

        # Check active jobs
        try:
            output = self._run_remote(f"squeue --noheader --jobs={job_id} --format=%T")
        except RuntimeError as exc:
            if "Invalid job id specified" not in str(exc):
                raise
            output = ""

        if output:
            return output.splitlines()[0].strip()

        # Check job accouting histroy
        output = self._run_remote(f"sacct -X -n -P --jobs={job_id} --format=State")

        if output:
            return output.splitlines()[0].strip()

        return "UNKNOWN"

    @operation(logger, "Slurm cancel")
    def cancel(self, job_id: int) -> None:
        """
        Requests job cancellation with `scancel` without waiting for a state change.

        Args:
            job_id (`int`): Slurm job ID.

        Raises:
            `RuntimeError`: If the remote cancellation command fails.
            `TimeoutError`: If the SSH command exceeds 60 seconds.
        """
        job_id = int(job_id)
        logger.info("Cancelling Slurm job job_id=%s", job_id)
        self._run_remote(f"scancel {job_id}")

    @operation(logger, "Slurm stage_payload")
    def stage_payload(self, payload: bytes) -> str:
        """
        Uploads a payload as `input.pkl` in a new remote execution directory.

        Args:
            payload (`bytes`): Serialized function and arguments.

        Returns:
            `str`: Remote directory under `work_root`, named with a random UUID.

        Raises:
            `ValueError`: If `work_root` is missing or is not an absolute path.
            `RuntimeError`: If the upload fails.
            `subprocess.TimeoutExpired`: If the upload exceeds 60 seconds.
        """
        if not self.config.work_root:
            raise ValueError("Slurm work_root is not configured")

        work_root = self.config.work_root

        if not work_root.startswith("/"):
            raise ValueError("Slurm work_root must be an absolute path")

        job_dir = f"{work_root.rstrip('/')}/{uuid.uuid4().hex}"
        input_path = f"{job_dir}/input.pkl"
        logger.info(
            "Staging payload host=%s job_dir=%s payload_bytes=%s",
            self.config.ssh_host,
            job_dir,
            len(payload),
        )

        command = f"mkdir -p {shlex.quote(job_dir)} && cat > {shlex.quote(input_path)}"

        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.config.ssh_host,
                command,
            ],
            check=False,
            input=payload,
            capture_output=True,
            timeout=60,
        )

        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())

        return job_dir

    def submit_payload(
        self,
        payload: bytes,
        gpu: int = 1,
        *,
        environment_id: str | None = None,
    ) -> tuple[int, str]:
        """
        Stages a serialized call and submits the deployed worker batch script.

        Args:
            payload (`bytes`): Serialized function and arguments.
            gpu (`int`, *optional*, defaults to `1`): GPU count passed to
                [`backends.slurm.SlurmBackend.submit`].
            environment_id (`str`, *optional*): Prepared environment's 64-character
                lowercase hexadecimal ID. Omitting it selects the Nest runtime.

        Returns:
            `tuple[int, str]`: Submitted job ID and remote execution directory.

        Raises:
            `ValueError`: If required working paths are missing or invalid, the
                environment ID is invalid, or the GPU count is negative.
            `RuntimeError`: If payload staging or submission fails.
            `TimeoutError`: If the submission SSH command exceeds 60 seconds.
            `subprocess.TimeoutExpired`: If payload upload exceeds 60 seconds.
        """
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")

        if environment_id is not None and not re.fullmatch(r"[0-9a-f]{64}", environment_id):
            raise ValueError("Invalid environment ID")

        # Upload payload
        job_dir = self.stage_payload(payload)

        # Extract execution UUID
        execution_id = job_dir.rsplit("/", 1)[-1]

        # Prepare script arguments
        script_args = (
            execution_id,
            environment_id or "",
            self.config.worker_workdir,
            current_request_id(),
            os.getenv("NEST_LOG_LEVEL", "INFO"),
        )

        # Submit Slurm job
        job_id = self.submit(
            workdir=self.config.worker_workdir,
            script="project/scripts/worker.sbatch",
            gpu=gpu,
            script_args=script_args,
        )

        logger.info(
            "Execution scheduled execution_id=%s job_id=%s environment_id=%s job_dir=%s",
            execution_id,
            job_id,
            environment_id or "none",
            job_dir,
        )
        return job_id, job_dir

    @operation(logger, "Slurm wait_result")
    def wait_result(
        self,
        job_id: int,
        job_dir: str,
        timeout: int = 600,
        poll_interval: int = 5,
    ) -> bytes:
        """
        Waits for a job to complete and downloads its serialized result over SSH.

        Args:
            job_id (`int`): Slurm job ID to poll.
            job_dir (`str`): Remote execution directory containing `output.pkl`.
            timeout (`int`, *optional*, defaults to `600`): Seconds allowed for polling.
                Remote queries and the final download can extend the total duration.
            poll_interval (`int`, *optional*, defaults to `5`): Seconds between queries.

        Returns:
            `bytes`: Nonempty contents of the completed job's `output.pkl`.

        Raises:
            `RuntimeError`: If the job enters a failure state, a remote command fails,
                or the result file is empty.
            `TimeoutError`: If polling reaches its deadline or a status query times out.
                The job is not cancelled and may still be running.
            `subprocess.TimeoutExpired`: If the download exceeds 60 seconds.
        """
        deadline = time.monotonic() + timeout
        previous_state = None
        logger.info("Waiting for Slurm job job_id=%s timeout_s=%s", job_id, timeout)

        failed_states = (
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "OUT_OF_MEMORY",
            "NODE_FAIL",
            "BOOT_FAIL",
            "PREEMPTED",
            "DEADLINE",
            "REVOKED",
        )

        while True:
            state = self.status(job_id)
            if state != previous_state:
                logger.info("Slurm state changed job_id=%s state=%s", job_id, state)
                previous_state = state

            if state == "COMPLETED":
                break

            if state.startswith(failed_states):
                raise RuntimeError(f"Slurm job {job_id} failed: {state}")

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Slurm job {job_id}. The job may still be running."
                )

            time.sleep(poll_interval)

        # Download binary result through SSH stdout
        output_path = f"{job_dir}/output.pkl"
        logger.info("Downloading result job_id=%s job_dir=%s", job_id, job_dir)

        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.config.ssh_host,
                f"cat {shlex.quote(output_path)}",
            ],
            check=False,
            capture_output=True,
            timeout=60,
        )

        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())

        if not result.stdout:
            raise RuntimeError("Worker returned an empty result file")

        logger.debug("Result downloaded job_id=%s result_bytes=%s", job_id, len(result.stdout))
        return result.stdout

    def stage_environment_files(self, project_dir: str) -> str:
        """
        Reads a local project's environment files and stages them on the cluster.

        Args:
            project_dir (`str`): Local directory containing `pyproject.toml` and `uv.lock`.

        Returns:
            `str`: Remote directory returned by
                [`backends.slurm.SlurmBackend.stage_environment_bytes`].

        Raises:
            `OSError`: If either local environment file cannot be read.
            `ValueError`: If environment files are empty or `worker_workdir` is missing.
            `RuntimeError`: If staging or remote hash verification fails.
            `TimeoutError`: If a remote text command exceeds 60 seconds.
            `subprocess.TimeoutExpired`: If a file upload exceeds 60 seconds.
        """
        project = Path(project_dir)

        return self.stage_environment_bytes(
            pyproject_toml=(project / "pyproject.toml").read_bytes(),
            uv_lock=(project / "uv.lock").read_bytes(),
        )

    @operation(logger, "Slurm stage_environment_bytes")
    def stage_environment_bytes(
        self,
        *,
        pyproject_toml: bytes,
        uv_lock: bytes,
    ) -> str:
        """
        Caches environment files in a remote directory identified by their contents.

        Hashes each file's name, byte length, and contents to identify the environment.
        Reuses files when their remote SHA-256 hashes match. Otherwise, uploads each
        file through a temporary path and verifies both hashes after replacement.
        This stages dependency files without building a Python environment.

        Args:
            pyproject_toml (`bytes`): Contents of the project's `pyproject.toml`.
            uv_lock (`bytes`): Contents of the project's `uv.lock`.

        Returns:
            `str`: Directory under `worker_workdir/environments` named with the
                environment's SHA-256 ID.

        Raises:
            `ValueError`: If `worker_workdir` is missing or either file is empty.
            `RuntimeError`: If a remote command, upload, or hash verification fails.
            `TimeoutError`: If a remote text command exceeds 60 seconds.
            `subprocess.TimeoutExpired`: If a file upload exceeds 60 seconds.
        """
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")

        if not pyproject_toml or not uv_lock:
            raise ValueError("Environment files cannot be empty")

        names = ("pyproject.toml", "uv.lock")
        files = {
            "pyproject.toml": pyproject_toml,
            "uv.lock": uv_lock,
        }

        # Calculate environment hash
        hasher = hashlib.sha256()

        for name, content in files.items():
            hasher.update(name.encode())
            hasher.update(b"\0")
            hasher.update(len(content).to_bytes(8, "big"))
            hasher.update(content)

        env_id = hasher.hexdigest()

        env_dir = f"{self.config.worker_workdir.rstrip('/')}/environments/{env_id}"

        paths = {name: f"{env_dir}/{name}" for name in names}
        expected = [hashlib.sha256(files[name]).hexdigest() for name in names]

        # Check remote cache
        def remote_hashes() -> list[str]:
            """
            Reads remote checksums for the two cached environment files.

            Returns:
                `list[str]`: SHA-256 checksums in `names` order, or an empty list if
                    either remote file is absent.

            Raises:
                `RuntimeError`: If the remote checksum command fails.
                `TimeoutError`: If the SSH command exceeds 60 seconds.
            """
            quoted = [shlex.quote(paths[name]) for name in names]

            command = (
                f"if [ -f {quoted[0]} ] && [ -f {quoted[1]} ]; then "
                f"sha256sum {' '.join(quoted)}; "
                f"fi"
            )

            output = self._run_remote(command)

            return [line.split()[0] for line in output.splitlines()]

        if remote_hashes() == expected:
            logger.info("Environment files cache hit environment_id=%s", env_id)
            return env_dir

        logger.info("Environment files cache miss environment_id=%s", env_id)

        self._run_remote(f"mkdir -p {shlex.quote(env_dir)}")

        # upload files atomically
        for name, content in files.items():
            remote_path = paths[name]
            temp_path = f"{remote_path}.tmp-{uuid.uuid4().hex}"

            command = (
                f"cat > {shlex.quote(temp_path)} && "
                f"mv -f -- {shlex.quote(temp_path)} "
                f"{shlex.quote(remote_path)}"
            )

            result = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    self.config.ssh_host,
                    command,
                ],
                check=False,
                input=content,
                capture_output=True,
                timeout=60,
            )

            if result.returncode != 0:
                raise RuntimeError(result.stderr.decode(errors="replace"))

        actual = remote_hashes()
        if actual != expected:
            logger.error(
                "Environment verification failed environment_id=%s env_dir=%s expected_hashes=%s actual_hashes=%s",
                env_id,
                env_dir,
                expected,
                actual,
            )

            raise RuntimeError("Environment file verification failed")

        return env_dir

    def build_environment(self, env_dir: str, gpu: int = 1) -> int:
        """
        Submits the environment build script for a staged environment directory.

        Args:
            env_dir (`str`): Directory under `worker_workdir/environments` whose name
                is a 64-character lowercase hexadecimal environment ID.
            gpu (`int`, *optional*, defaults to `1`): GPU count passed to
                [`backends.slurm.SlurmBackend.submit`].

        Returns:
            `int`: Environment build job ID. Does not wait for completion.

        Raises:
            `ValueError`: If `worker_workdir` is missing, the directory or its ID is
                invalid, or the GPU count is negative.
            `RuntimeError`: If submission fails or returns an invalid job ID.
            `TimeoutError`: If the SSH command exceeds 60 seconds.
        """
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")

        env_id = Path(env_dir).name

        if not re.fullmatch(r"[0-9a-f]{64}", env_id):
            raise ValueError("Invalid environment ID")

        expected_dir = Path(self.config.worker_workdir) / "environments" / env_id

        if Path(env_dir) != expected_dir:
            raise ValueError("Invalid environment directory")

        return self.submit(
            workdir=self.config.worker_workdir,
            script="project/scripts/environment-smoke.sbatch",
            gpu=gpu,
            script_args=(env_id, self.config.worker_workdir),
        )

    @operation(logger, "Slurm wait_environment")
    def wait_environment(self, job_id: int, timeout: int = 1800, poll_interval: int = 5) -> None:
        """
        Polls an environment build job until it completes or fails.

        Args:
            job_id (`int`): Environment build job ID.
            timeout (`int`, *optional*, defaults to `1800`): Positive polling deadline
                in seconds. Remote queries can extend the total duration.
            poll_interval (`int`, *optional*, defaults to `5`): Positive interval
                between status queries, in seconds.

        Raises:
            `ValueError`: If `timeout` or `poll_interval` is not positive.
            `RuntimeError`: If the build enters a failure state or a status query fails.
            `TimeoutError`: If polling reaches its deadline or a status query times out.
                The build job is not cancelled.
        """
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("Timeout and poll interval must be positive")

        deadline = time.monotonic() + timeout
        previous_state = None
        logger.info("Waiting for Slurm job job_id=%s timeout_s=%s", job_id, timeout)

        failed_states = (
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "OUT_OF_MEMORY",
            "NODE_FAIL",
            "BOOT_FAIL",
            "PREEMPTED",
            "DEADLINE",
            "REVOKED",
        )

        while True:
            state = self.status(job_id)
            if state != previous_state:
                logger.info("Slurm state changed job_id=%s state=%s", job_id, state)
                previous_state = state

            if state == "COMPLETED":
                return

            if state.startswith(failed_states):
                raise RuntimeError(f"Environment build {job_id} failed: {state}")

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for environment build {job_id}. Last state: {state}"
                )

            time.sleep(poll_interval)

    def runtime_id(self) -> str:
        """
        Hashes the remote container image checksum and host architecture.

        Returns:
            `str`: SHA-256 runtime ID for `images/uv-python314.sif` and `uname -m`.

        Raises:
            `ValueError`: If `worker_workdir` is missing.
            `RuntimeError`: If the remote runtime identity command fails.
            `TimeoutError`: If the SSH command exceeds 60 seconds.
        """
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")

        image = f"{self.config.worker_workdir.rstrip('/')}/images/uv-python314.sif"

        quoted = shlex.quote(image)

        command = (
            f"test -r {quoted} && "
            f"image_hash=$(sha256sum {quoted} | cut -d' ' -f1) && "
            "arch=$(uname -m) && "
            """printf '%s:%s' "$image_hash" "$arch" | """
            "sha256sum | cut -d' ' -f1"
        )

        return self._run_remote(command)

    def environment_ready(self, env_dir: str) -> bool:
        """
        Checks the readiness marker and Python path for the current runtime.

        Args:
            env_dir (`str`): Staged environment directory under
                `worker_workdir/environments`, named with its 64-character hash.

        Returns:
            `bool`: Whether `.ready` exists and `.venv/bin/python` is a file or symlink
                under the environment's current runtime directory. A symlink can
                resolve only inside the container.

        Raises:
            `ValueError`: If `worker_workdir` is missing or the directory or ID is invalid.
            `RuntimeError`: If a remote runtime or readiness command fails.
            `TimeoutError`: If an SSH command exceeds 60 seconds.
        """
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")

        env_id = Path(env_dir).name

        if not re.fullmatch(r"[0-9a-f]{64}", env_id):
            raise ValueError("Invalid environment ID")

        expected_dir = Path(self.config.worker_workdir) / "environments" / env_id

        if Path(env_dir) != expected_dir:
            raise ValueError("Invalid environment directory")

        runtime_id = self.runtime_id()
        runtime_dir = f"{env_dir}/runtimes/{runtime_id}"
        ready = f"{runtime_dir}/.ready"
        python = f"{runtime_dir}/.venv/bin/python"

        command = (
            f"if test -f {shlex.quote(ready)} && "
            f"( test -L {shlex.quote(python)} || "
            f"test -f {shlex.quote(python)} ); then "
            "printf ready; "
            "else printf missing; fi"
        )

        ready = self._run_remote(command) == "ready"
        logger.debug(
            "Runtime readiness environment_id=%s runtime_id=%s ready=%s",
            env_id,
            runtime_id,
            ready,
        )
        return ready

    def prepare_environment(self, project_dir: str, gpu: int = 1) -> str:
        """
        Stages a local project's dependency files and prepares its cached runtime.

        Args:
            project_dir (`str`): Local directory containing `pyproject.toml` and `uv.lock`.
            gpu (`int`, *optional*, defaults to `1`): GPU count for a required build job.

        Returns:
            `str`: Remote environment directory after its runtime is ready.

        Raises:
            `OSError`: If either local environment file cannot be read.
            `ValueError`: If configuration or environment files are invalid, or a
                required build receives a negative GPU count.
            `RuntimeError`: If staging, building, or readiness verification fails.
            `TimeoutError`: If a remote text command or the build wait times out.
            `subprocess.TimeoutExpired`: If an environment file upload exceeds 60 seconds.
        """
        project = Path(project_dir)

        return self.prepare_environment_bytes(
            pyproject_toml=(project / "pyproject.toml").read_bytes(),
            uv_lock=(project / "uv.lock").read_bytes(),
            gpu=gpu,
        )

    @operation(logger, "Slurm prepare_environment_bytes")
    def prepare_environment_bytes(
        self,
        *,
        pyproject_toml: bytes,
        uv_lock: bytes,
        gpu: int = 1,
    ) -> str:
        """
        Stages dependency files and reuses or builds their runtime environment.

        Skips submission when the current runtime is ready. Otherwise, submits a
        build, waits with [`backends.slurm.SlurmBackend.wait_environment`], and checks
        readiness again before returning.

        Args:
            pyproject_toml (`bytes`): Contents of the project's `pyproject.toml`.
            uv_lock (`bytes`): Contents of the project's `uv.lock`.
            gpu (`int`, *optional*, defaults to `1`): GPU count for a required build job.

        Returns:
            `str`: Remote environment directory after its runtime is ready.

        Raises:
            `ValueError`: If configuration or files are invalid, or a required build
                receives a negative GPU count.
            `RuntimeError`: If staging or building fails, or a completed build does
                not leave a ready runtime.
            `TimeoutError`: If a remote text command or the build wait times out.
            `subprocess.TimeoutExpired`: If an environment file upload exceeds 60 seconds.
        """
        env_dir = self.stage_environment_bytes(
            pyproject_toml=pyproject_toml,
            uv_lock=uv_lock,
        )

        if self.environment_ready(env_dir):
            logger.info("Runtime cache hit environment_id=%s", Path(env_dir).name)
            return env_dir

        logger.info("Runtime cache miss environment_id=%s", Path(env_dir).name)
        job_id = self.build_environment(env_dir, gpu=gpu)
        logger.info(
            "Environment build submitted job_id=%s environment_id=%s", job_id, Path(env_dir).name
        )
        self.wait_environment(job_id)

        if not self.environment_ready(env_dir):
            raise RuntimeError("Environment build completed but cache is not ready")

        return env_dir
