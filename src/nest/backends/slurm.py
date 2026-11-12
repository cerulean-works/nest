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
from ..errors import (
    ExecutionTimeoutError,
    InfrastructureError,
    QueueTimeoutError,
    RemoteCommandError,
    SlurmJobError,
    SSHError,
    SSHTimeoutError,
    SubmissionUnknownError,
)

logger = get_logger(__name__)

_FAILED_STATES = (
    "FAILED",
    "CANCELLED",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "BOOT_FAIL",
    "PREEMPTED",
    "DEADLINE",
    "REVOKED",
)
"""Slurm states that end a job unsuccessfully, other than `TIMEOUT`."""

_STARTED_STATES = (
    "RUNNING",
    "COMPLETING",
    "SUSPENDED",
    "STOPPED",
    "SIGNALING",
    "STAGE_OUT",
    "RESIZING",
)
"""Slurm states showing that a job left the queue and started on a node."""

_SUBMIT_MARKER = "nest-sbatch-started"
"""Printed before `sbatch` so an SSH failure can show whether submission began."""


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
        ssh_timeout (`float`, *optional*, defaults to `60`): Seconds allowed for each
            SSH command or file transfer.
        result_timeout (`float`, *optional*, defaults to `600`): Default polling
            deadline for a worker job, in seconds.
        environment_timeout (`float`, *optional*, defaults to `1800`): Default polling
            deadline for an environment build job, in seconds.
        poll_interval (`float`, *optional*, defaults to `5`): Default seconds between
            Slurm status queries.
    """

    ssh_host: str
    account: str
    partition: str = "dev"
    time: str | None = None
    work_root: str | None = None
    worker_workdir: str | None = None
    ssh_timeout: float = 60
    result_timeout: float = 600
    environment_timeout: float = 1800
    poll_interval: float = 5


class SlurmBackend:
    """Stages files and manages Slurm jobs through noninteractive SSH commands."""

    def __init__(self, config: SlurmConfig):
        """
        Stores the backend configuration without connecting to the cluster.

        Args:
            config ([`backends.slurm.SlurmConfig`]): SSH host, Slurm defaults, and paths.
        """
        self.config = config

    def _ssh_process(
        self, command: str, input: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        """
        Runs one noninteractive SSH command without classifying its outcome.

        Args:
            command (`str`): Shell command to execute on the configured host.
            input (`bytes`, *optional*): Data written to the command's standard input.

        Returns:
            `subprocess.CompletedProcess[bytes]`: The finished SSH process.

        Raises:
            `subprocess.TimeoutExpired`: If SSH exceeds `ssh_timeout`. The process is
                killed, but the remote command may still have run.
        """
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", self.config.ssh_host, command],
            check=False,
            input=input,
            capture_output=True,
            timeout=self.config.ssh_timeout,
        )

    def _command_error(self, result: subprocess.CompletedProcess[bytes]) -> InfrastructureError:
        """
        Classifies an unsuccessful SSH process.

        Args:
            result (`subprocess.CompletedProcess[bytes]`): SSH process with a nonzero
                exit status.

        Returns:
            [`errors.InfrastructureError`]: [`errors.SSHError`] for exit status 255,
                which SSH reserves for its own failures, otherwise
                [`errors.RemoteCommandError`].
        """
        message = (result.stderr or result.stdout or b"").decode(errors="replace").strip()
        if result.returncode == 255:
            return SSHError(f"SSH to {self.config.ssh_host} failed:\n{message}")
        return RemoteCommandError(
            f"Remote command failed:\n{message}", returncode=result.returncode
        )

    def _ssh(self, command: str, input: bytes | None = None) -> bytes:
        """
        Runs an SSH command and classifies its failures.

        Args:
            command (`str`): Shell command to execute on the configured host.
            input (`bytes`, *optional*): Data written to the command's standard input.

        Returns:
            `bytes`: Raw standard output.

        Raises:
            [`errors.SSHTimeoutError`]: If SSH exceeds `ssh_timeout`. The remote
                operation's status is unknown.
            [`errors.SSHError`]: If SSH itself fails (exit status 255).
            [`errors.RemoteCommandError`]: If the remote command exits unsuccessfully.
        """
        started = time.monotonic()
        logger.debug("SSH command started host=%s", self.config.ssh_host)
        try:
            result = self._ssh_process(command, input)
        except subprocess.TimeoutExpired as exc:
            raise SSHTimeoutError(
                f"SSH command to {self.config.ssh_host} timed out after "
                f"{self.config.ssh_timeout:g} seconds. The remote operation status is unknown."
            ) from exc

        if result.returncode != 0:
            raise self._command_error(result)

        logger.debug(
            "SSH command completed host=%s elapsed_ms=%.1f",
            self.config.ssh_host,
            (time.monotonic() - started) * 1000,
        )
        return result.stdout

    def _run_remote(self, command: str) -> str:
        """
        Runs a text command over SSH with batch mode enabled.

        Args:
            command (`str`): Shell command to execute on the configured host.

        Returns:
            `str`: Standard output with leading and trailing whitespace removed.

        Raises:
            [`errors.SSHTimeoutError`]: If SSH exceeds `ssh_timeout` (60 seconds by
                default). The remote operation's status remains unknown.
            [`errors.SSHError`]: If SSH cannot reach or authenticate to the host.
            [`errors.RemoteCommandError`]: If the remote command exits unsuccessfully.
        """
        return self._ssh(command).decode(errors="replace").strip()

    @operation(logger, "Slurm submit")
    def submit(
        self,
        workdir: str,
        script: str = "hello.sbatch",
        gpu: int = 1,
        *,
        cpu: int = 0,
        account: str | None = None,
        partition: str | None = None,
        time: str | None = None,
        script_args: tuple[str, ...] = (),
    ) -> int:
        """
        Submits a batch script from a remote working directory exactly once.

        Never retries. When the outcome is ambiguous, raises
        [`errors.SubmissionUnknownError`] instead of submitting again.

        Args:
            workdir (`str`): Remote directory in which to run `sbatch`.
            script (`str`, *optional*, defaults to `"hello.sbatch"`): Script path,
                resolved from `workdir` when relative.
            gpu (`int`, *optional*, defaults to `1`): GPUs per node. Omits the GPU
                flag when `0`; script directives can still request GPUs.
            cpu (`int`, *optional*, defaults to `0`): CPUs per task, passed as
                `--cpus-per-task`. Omits the flag when `0`, so script directives apply.
            account (`str`, *optional*): Account override. Uses the configured account
                when unset or empty.
            partition (`str`, *optional*): Partition override. Uses the configured
                partition when unset or empty.
            time (`str`, *optional*): Time limit override. Uses the configured limit
                when unset or empty. Omits the flag if neither has a value.
            script_args (`tuple[str, ...]`, *optional*, defaults to `()`): Arguments
                appended after the script path.

        Returns:
            `int`: Job ID parsed from the last line of `sbatch --parsable` output.

        Raises:
            `TypeError`: If `script_args` is a string.
            `ValueError`: If the GPU or CPU count is negative.
            [`errors.SubmissionUnknownError`]: If SSH times out during submission,
                disconnects after `sbatch` started, or `sbatch` succeeds without a
                decimal job ID. A job may exist.
            [`errors.SSHError`]: If SSH fails before the remote command starts.
            [`errors.RemoteCommandError`]: If `cd` or `sbatch` exits unsuccessfully.
                Slurm did not accept the job.
        """
        if isinstance(script_args, str):
            raise TypeError("script_args must be a tuple of strings")

        if gpu < 0:
            raise ValueError("GPU count cannot be negative")

        if cpu < 0:
            raise ValueError("CPU count cannot be negative")

        args = [
            "sbatch",
            "--parsable",
            f"--account={account or self.config.account}",
            f"--partition={partition or self.config.partition}",
        ]

        if gpu > 0:
            args.append(f"--gpus-per-node={gpu}")

        if cpu > 0:
            args.append(f"--cpus-per-task={cpu}")

        time_limit = time or self.config.time

        if time_limit:
            args.append(f"--time={time_limit}")

        args.append(script)
        args.extend(script_args)

        # The marker shows whether the remote shell reached sbatch if SSH later fails.
        command = f"cd {shlex.quote(workdir)} && echo {_SUBMIT_MARKER} && {shlex.join(args)}"
        unknown = (
            f"The Slurm submission status of {script} is unknown and Nest will not resubmit "
            "it. Check the queue (for example `squeue --me`) before submitting again."
        )

        try:
            result = self._ssh_process(command)
        except subprocess.TimeoutExpired as exc:
            raise SubmissionUnknownError(
                f"sbatch over SSH timed out after {self.config.ssh_timeout:g} seconds. {unknown}"
            ) from exc

        stdout = result.stdout.decode(errors="replace")

        if result.returncode != 0:
            error = self._command_error(result)
            if isinstance(error, SSHError) and _SUBMIT_MARKER in stdout:
                raise SubmissionUnknownError(f"SSH disconnected during sbatch: {error}. {unknown}")
            raise error

        lines = [line.strip() for line in stdout.splitlines()]
        lines = [line for line in lines if line and line != _SUBMIT_MARKER]
        job_id = lines[-1].split(";")[0].strip() if lines else ""

        if not job_id.isdecimal():
            output = "\n".join(lines)
            raise SubmissionUnknownError(f"Unexpected sbatch output: {output!r}. {unknown}")

        logger.info(
            "Slurm job submitted job_id=%s script=%s gpu=%s cpu=%s partition=%s",
            job_id,
            script,
            gpu,
            cpu,
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
            [`errors.RemoteCommandError`]: If a remote query fails, except an invalid
                job ID from `squeue`.
            [`errors.SSHError`]: If SSH cannot reach the host.
            [`errors.SSHTimeoutError`]: If an SSH command exceeds `ssh_timeout`.
        """
        job_id = int(job_id)

        # Check active jobs
        try:
            output = self._run_remote(f"squeue --noheader --jobs={job_id} --format=%T")
        except RemoteCommandError as exc:
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
            [`errors.RemoteCommandError`]: If the remote cancellation command fails.
            [`errors.SSHError`]: If SSH cannot reach the host.
            [`errors.SSHTimeoutError`]: If the SSH command exceeds `ssh_timeout`.
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
            [`errors.SSHError`] or [`errors.RemoteCommandError`]: If the upload fails.
            [`errors.SSHTimeoutError`]: If the upload exceeds `ssh_timeout`.
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
        self._ssh(command, input=payload)
        return job_dir

    def submit_payload(
        self,
        payload: bytes,
        gpu: int = 1,
        *,
        cpu: int = 0,
        environment_id: str | None = None,
    ) -> tuple[int, str]:
        """
        Stages a serialized call and submits the deployed worker batch script once.

        Args:
            payload (`bytes`): Serialized function and arguments.
            gpu (`int`, *optional*, defaults to `1`): GPU count passed to
                [`backends.slurm.SlurmBackend.submit`].
            cpu (`int`, *optional*, defaults to `0`): CPUs per task passed to
                [`backends.slurm.SlurmBackend.submit`]; `0` keeps the script's value.
            environment_id (`str`, *optional*): Prepared environment's 64-character
                lowercase hexadecimal ID. Omitting it selects the Nest runtime.

        Returns:
            `tuple[int, str]`: Submitted job ID and remote execution directory.

        Raises:
            `ValueError`: If required working paths are missing or invalid, the
                environment ID is invalid, or a resource count is negative.
            [`errors.SubmissionUnknownError`]: If a worker job may have been
                submitted; the message names its execution directory.
            [`errors.InfrastructureError`]: If staging or submission fails over SSH.
        """
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")

        if environment_id is not None and not re.fullmatch(r"[0-9a-f]{64}", environment_id):
            raise ValueError("Invalid environment ID")

        if gpu < 0 or cpu < 0:
            raise ValueError("Resource counts cannot be negative")

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

        # Submit Slurm job exactly once; an ambiguous outcome is reported, not retried.
        try:
            job_id = self.submit(
                workdir=self.config.worker_workdir,
                script="project/scripts/worker.sbatch",
                gpu=gpu,
                cpu=cpu,
                script_args=script_args,
            )
        except SubmissionUnknownError as exc:
            logger.error(
                "Worker submission status unknown execution_id=%s job_dir=%s",
                execution_id,
                job_dir,
            )
            raise SubmissionUnknownError(
                f"{exc} Execution ID: {execution_id}; a submitted worker writes "
                f"{job_dir}/output.pkl."
            ) from exc

        logger.info(
            "Execution scheduled execution_id=%s job_id=%s environment_id=%s job_dir=%s",
            execution_id,
            job_id,
            environment_id or "none",
            job_dir,
        )
        return job_id, job_dir

    def _wait_job(self, job_id: int, label: str, timeout: float, poll_interval: float) -> None:
        """
        Polls a job until it completes, classifying failures and timeouts.

        Args:
            job_id (`int`): Slurm job ID to poll.
            label (`str`): Job description used in messages, such as `"Slurm job"`.
            timeout (`float`): Nonnegative polling deadline in seconds. `0` checks once.
            poll_interval (`float`): Positive interval between status queries.

        Raises:
            `ValueError`: If `timeout` is negative or `poll_interval` is not positive.
            [`errors.SlurmJobError`]: If the job enters a failure state.
            [`errors.ExecutionTimeoutError`]: If Slurm reports `TIMEOUT`, or the job
                started but did not finish before the deadline.
            [`errors.QueueTimeoutError`]: If the job never started before the deadline.
            [`errors.InfrastructureError`]: If a status query fails over SSH. Its
                `job_id` is set to the polled job.
        """
        if timeout < 0 or poll_interval <= 0:
            raise ValueError("Timeout cannot be negative and poll interval must be positive")

        deadline = time.monotonic() + timeout
        previous_state = None
        started = False
        logger.info("Waiting for Slurm job job_id=%s timeout_s=%s", job_id, timeout)

        while True:
            try:
                state = self.status(job_id)
            except InfrastructureError as exc:
                # The job exists; let callers find it before calling again.
                exc.job_id = job_id
                raise

            if state != previous_state:
                logger.info("Slurm state changed job_id=%s state=%s", job_id, state)
                previous_state = state

            if state == "COMPLETED":
                return

            if state.startswith("TIMEOUT"):
                raise ExecutionTimeoutError(
                    f"{label} {job_id} exceeded its Slurm time limit: {state}", job_id=job_id
                )

            if state.startswith(_FAILED_STATES):
                raise SlurmJobError(f"{label} {job_id} failed: {state}", job_id=job_id)

            started = started or state.startswith(_STARTED_STATES)

            if time.monotonic() >= deadline:
                if started:
                    raise ExecutionTimeoutError(
                        f"{label} {job_id} did not finish within {timeout:g} seconds. "
                        f"Last state: {state}. The job was not cancelled.",
                        job_id=job_id,
                    )
                raise QueueTimeoutError(
                    f"{label} {job_id} did not start within {timeout:g} seconds. "
                    f"Last state: {state}. The job was not cancelled.",
                    job_id=job_id,
                )

            time.sleep(poll_interval)

    @operation(logger, "Slurm wait_result")
    def wait_result(
        self,
        job_id: int,
        job_dir: str,
        timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> bytes:
        """
        Waits for a job to complete and downloads its serialized result over SSH.

        Args:
            job_id (`int`): Slurm job ID to poll.
            job_dir (`str`): Remote execution directory containing `output.pkl`.
            timeout (`float`, *optional*): Seconds allowed for polling. Defaults to
                `result_timeout` (600 seconds). Remote queries and the final download
                can extend the total duration.
            poll_interval (`float`, *optional*): Seconds between queries. Defaults to
                `poll_interval` (5 seconds).

        Returns:
            `bytes`: Nonempty contents of the completed job's `output.pkl`.

        Raises:
            [`errors.SlurmJobError`]: If the job enters a failure state.
            [`errors.QueueTimeoutError`]: If the job is still queued at the deadline.
            [`errors.ExecutionTimeoutError`]: If the job is still running at the
                deadline or Slurm ended it with `TIMEOUT`.
            [`errors.InfrastructureError`]: If a remote command fails or the result
                file is empty. Timeouts never cancel the job.
        """
        self._wait_job(
            job_id,
            "Slurm job",
            self.config.result_timeout if timeout is None else timeout,
            self.config.poll_interval if poll_interval is None else poll_interval,
        )

        # Download binary result through SSH stdout
        output_path = f"{job_dir}/output.pkl"
        logger.info("Downloading result job_id=%s job_dir=%s", job_id, job_dir)
        try:
            result = self._ssh(f"cat {shlex.quote(output_path)}")
        except InfrastructureError as exc:
            exc.job_id = job_id
            raise

        if not result:
            raise InfrastructureError("Worker returned an empty result file", job_id=job_id)

        logger.debug("Result downloaded job_id=%s result_bytes=%s", job_id, len(result))
        return result

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
            [`errors.InfrastructureError`]: If staging or remote hash verification fails.
            [`errors.SSHTimeoutError`]: If an SSH command or upload exceeds `ssh_timeout`.
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
            [`errors.InfrastructureError`]: If a remote command, upload, or hash
                verification fails.
            [`errors.SSHTimeoutError`]: If an SSH command or upload exceeds `ssh_timeout`.
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
                [`errors.InfrastructureError`]: If the remote checksum command fails.
                [`errors.SSHTimeoutError`]: If the SSH command exceeds `ssh_timeout`.
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

            self._ssh(command, input=content)

        actual = remote_hashes()
        if actual != expected:
            logger.error(
                "Environment verification failed environment_id=%s env_dir=%s expected_hashes=%s actual_hashes=%s",
                env_id,
                env_dir,
                expected,
                actual,
            )

            raise InfrastructureError("Environment file verification failed")

        return env_dir

    def build_environment(self, env_dir: str, gpu: int = 1, *, cpu: int = 0) -> int:
        """
        Submits the environment build script for a staged environment directory.

        Args:
            env_dir (`str`): Directory under `worker_workdir/environments` whose name
                is a 64-character lowercase hexadecimal environment ID.
            gpu (`int`, *optional*, defaults to `1`): GPU count passed to
                [`backends.slurm.SlurmBackend.submit`].
            cpu (`int`, *optional*, defaults to `0`): CPUs per task passed to
                [`backends.slurm.SlurmBackend.submit`]; `0` keeps the script's value.

        Returns:
            `int`: Environment build job ID. Does not wait for completion.

        Raises:
            `ValueError`: If `worker_workdir` is missing, the directory or its ID is
                invalid, or a resource count is negative.
            [`errors.SubmissionUnknownError`]: If a build job may have been submitted.
            [`errors.InfrastructureError`]: If submission fails over SSH.
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
            cpu=cpu,
            script_args=(env_id, self.config.worker_workdir),
        )

    @operation(logger, "Slurm wait_environment")
    def wait_environment(
        self, job_id: int, timeout: float | None = None, poll_interval: float | None = None
    ) -> None:
        """
        Polls an environment build job until it completes or fails.

        Args:
            job_id (`int`): Environment build job ID.
            timeout (`float`, *optional*): Positive polling deadline in seconds.
                Defaults to `environment_timeout` (1,800 seconds). Remote queries can
                extend the total duration.
            poll_interval (`float`, *optional*): Positive interval between status
                queries, in seconds. Defaults to `poll_interval` (5 seconds).

        Raises:
            `ValueError`: If `timeout` or `poll_interval` is not positive.
            [`errors.SlurmJobError`]: If the build enters a failure state.
            [`errors.QueueTimeoutError`]: If the build is still queued at the deadline.
            [`errors.ExecutionTimeoutError`]: If the build is still running at the
                deadline or Slurm ended it with `TIMEOUT`. The build is not cancelled.
            [`errors.InfrastructureError`]: If a status query fails over SSH.
        """
        timeout = self.config.environment_timeout if timeout is None else timeout
        poll_interval = self.config.poll_interval if poll_interval is None else poll_interval

        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("Timeout and poll interval must be positive")

        self._wait_job(job_id, "Environment build", timeout, poll_interval)

    def runtime_id(self) -> str:
        """
        Hashes the remote container image checksum and host architecture.

        Returns:
            `str`: SHA-256 runtime ID for `images/uv-python314.sif` and `uname -m`.

        Raises:
            `ValueError`: If `worker_workdir` is missing.
            [`errors.InfrastructureError`]: If the remote runtime identity command fails.
            [`errors.SSHTimeoutError`]: If the SSH command exceeds `ssh_timeout`.
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
            [`errors.InfrastructureError`]: If a remote runtime or readiness command fails.
            [`errors.SSHTimeoutError`]: If an SSH command exceeds `ssh_timeout`.
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

    def prepare_environment(self, project_dir: str, gpu: int = 1, *, cpu: int = 0) -> str:
        """
        Stages a local project's dependency files and prepares its cached runtime.

        Args:
            project_dir (`str`): Local directory containing `pyproject.toml` and `uv.lock`.
            gpu (`int`, *optional*, defaults to `1`): GPU count for a required build job.
            cpu (`int`, *optional*, defaults to `0`): CPUs per task for a required
                build job; `0` keeps the script's value.

        Returns:
            `str`: Remote environment directory after its runtime is ready.

        Raises:
            `OSError`: If either local environment file cannot be read.
            `ValueError`: If configuration or environment files are invalid, or a
                required build receives a negative resource count.
            [`errors.InfrastructureError`]: If staging, building, or readiness
                verification fails, including the timeout subclasses.
        """
        project = Path(project_dir)

        return self.prepare_environment_bytes(
            pyproject_toml=(project / "pyproject.toml").read_bytes(),
            uv_lock=(project / "uv.lock").read_bytes(),
            gpu=gpu,
            cpu=cpu,
        )

    @operation(logger, "Slurm prepare_environment_bytes")
    def prepare_environment_bytes(
        self,
        *,
        pyproject_toml: bytes,
        uv_lock: bytes,
        gpu: int = 1,
        cpu: int = 0,
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
            cpu (`int`, *optional*, defaults to `0`): CPUs per task for a required
                build job; `0` keeps the script's value.

        Returns:
            `str`: Remote environment directory after its runtime is ready.

        Raises:
            `ValueError`: If configuration or files are invalid, or a required build
                receives a negative resource count.
            [`errors.InfrastructureError`]: If staging or building fails, or a
                completed build does not leave a ready runtime. Timeouts raise the
                [`errors.NestTimeoutError`] subclasses.
        """
        env_dir = self.stage_environment_bytes(
            pyproject_toml=pyproject_toml,
            uv_lock=uv_lock,
        )

        if self.environment_ready(env_dir):
            logger.info("Runtime cache hit environment_id=%s", Path(env_dir).name)
            return env_dir

        logger.info("Runtime cache miss environment_id=%s", Path(env_dir).name)
        job_id = self.build_environment(env_dir, gpu=gpu, cpu=cpu)
        logger.info(
            "Environment build submitted job_id=%s environment_id=%s", job_id, Path(env_dir).name
        )
        self.wait_environment(job_id)

        if not self.environment_ready(env_dir):
            raise InfrastructureError(
                "Environment build completed but cache is not ready", job_id=job_id
            )

        return env_dir
