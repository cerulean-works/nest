import hashlib
import re
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

from .. import outcome
from .._logging import current_request_id, get_logger, operation
from ..config import DEFAULT_PARTITION, load_log_level
from ..errors import (
    ExecutionTimeoutError,
    InfrastructureError,
    JobBlockedError,
    QueueTimeoutError,
    RemoteCommandError,
    SessionUnavailableError,
    SlurmJobError,
    SSHError,
    SSHTimeoutError,
    SubmissionUnknownError,
)
from .transport import (
    ControllerSSHTransport,
    ExecutionAuthority,
    Operation,
    SlurmTransport,
    TransportCapabilities,
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

_EXECUTION_ID = re.compile(r"[0-9a-f]{32}")
"""Execution IDs: a UUID4 as 32 lowercase hexadecimal characters."""


def new_execution_id() -> str:
    """Returns a new Execution ID: a UUID4 as 32 lowercase hexadecimal characters."""
    return uuid.uuid4().hex


WORKER_SCRIPT = "project/scripts/worker.sbatch"
"""The deployed worker batch script, relative to `worker_workdir`."""

_SUBMIT_MARKER = "nest-sbatch-started"
"""Printed before `sbatch` so an SSH failure can show whether submission began."""

_SQUEUE_FORMAT = "%T|%r|%P|%q|%a"
"""`squeue` fields: state, reason, partition, QoS, and account, separated by `|`."""

_REQ_TRES = re.compile(r"(?:^|\s)ReqTRES=(\S+)")
"""Requested trackable resources in `scontrol show job --oneliner` output."""

WORKER_DEFAULT_GPUS = 1
"""GPUs per node `scripts/worker.sbatch` requests when the Controller passes none."""
WORKER_DEFAULT_CPUS = 1
"""CPUs per task `scripts/worker.sbatch` requests when the Controller passes none."""
ENVIRONMENT_BUILD_GPUS = 1
"""GPUs per node `scripts/environment-smoke.sbatch` (the environment build) requests."""
ENVIRONMENT_BUILD_CPUS = 12
"""CPUs per task the environment build requests."""
ENVIRONMENT_BUILD_TIME = "00:10:00"
"""Time limit of the environment build."""

PENDING_QUEUED = "queued"
"""Pending reason category: normal queue congestion that clears on its own."""

PENDING_LIMIT = "limit"
"""Pending reason category: a QoS or association usage limit that clears as usage drops."""

PENDING_BLOCKED = "blocked"
"""Pending reason category: the job cannot start until its request or the cluster changes."""

_BLOCKED_REASONS = {
    "PartitionConfig": (
        "The request does not fit partition {partition}; check the GPU and CPU counts, the "
        "time limit, and the partition."
    ),
    "PartitionNodeLimit": (
        "The request needs more nodes than partition {partition} allows; request fewer "
        "resources or use another partition."
    ),
    "PartitionTimeLimit": (
        "The time limit exceeds the maximum of partition {partition}; request a shorter time "
        "limit or use another partition."
    ),
    "BadConstraints": (
        "No node in partition {partition} satisfies the requested constraints or resources; "
        "check the GPU and CPU counts and the partition."
    ),
    "InvalidAccount": (
        "Account {account} is invalid or not allowed here; check the Slurm account for "
        "partition {partition}."
    ),
    "InvalidQOS": (
        "QoS {qos} is invalid for account {account} or partition {partition}; check the "
        "account and partition."
    ),
    "AccountNotAllowed": (
        "Account {account} may not use partition {partition}; use another account or partition."
    ),
    "QOSNotAllowed": "QoS {qos} may not use partition {partition}; use another partition.",
    "DependencyNeverSatisfied": (
        "A job this one depends on failed, so it can never start; cancel it."
    ),
    "JobHeldUser": "The job is held; release it with `scontrol release {job_id}` or cancel it.",
    "JobHeldAdmin": "An administrator held the job; ask them to release it, or cancel it.",
}
"""Pending reasons that never clear on their own, with a suggested fix for each."""

_LIMIT_HINT = (
    "Slurm is enforcing a QoS or association limit ({reason}) for account {account} and "
    "QoS {qos}, for example on GPUs already in use. The job starts once usage drops below "
    "the limit. To start sooner, request fewer resources, use another account or QoS, or "
    "ask an administrator to raise the limit."
)
_PER_JOB_LIMIT_HINT = (
    "The job alone exceeds a per-job QoS or association limit ({reason}) for account "
    "{account} and QoS {qos}, so it can never start; request fewer resources or a shorter "
    "time limit."
)
_QUEUED_HINT = "The job is waiting in the queue ({reason}); this is normal congestion."


def _is_limit_reason(reason: str) -> bool:
    """Returns whether a pending reason names a QoS or association limit."""
    return reason.startswith(("QOS", "Assoc", "Max"))


def _is_per_job_limit(reason: str) -> bool:
    """Returns whether a limit reason concerns the job's own size, not shared usage."""
    return "PerJob" in reason or reason.startswith(("QOSMin", "AssocMin"))


def classify_pending_reason(reason: str | None) -> str:
    """
    Classifies a Slurm pending reason by whether waiting can clear it.

    Unknown reasons count as normal queueing, so an unfamiliar reason never stops
    Nest from waiting early.

    Args:
        reason (`str`, *optional*): Reason reported by `squeue`, such as `Priority`,
            `MaxGRESPerAccount`, or `PartitionConfig`.

    Returns:
        `str`: [`backends.slurm.PENDING_BLOCKED`] for reasons that need a change to the
            request or the cluster, including per-job limits the job alone exceeds;
            [`backends.slurm.PENDING_LIMIT`] for QoS and association usage limits; and
            [`backends.slurm.PENDING_QUEUED`] otherwise.
    """
    if not reason:
        return PENDING_QUEUED
    if reason in _BLOCKED_REASONS:
        return PENDING_BLOCKED
    if _is_limit_reason(reason):
        if _is_per_job_limit(reason):
            return PENDING_BLOCKED
        return PENDING_LIMIT
    return PENDING_QUEUED


@dataclass(frozen=True)
class SlurmJobInfo:
    """
    A job's Slurm state and, while it is queued, why it has not started.

    Attributes:
        job_id (`int`): Slurm job ID.
        state (`str`): State such as `PENDING`, `RUNNING`, or `COMPLETED`.
        reason (`str`, *optional*): Slurm's reason for the state, such as `Priority`
            or `MaxGRESPerAccount`. `None` when Slurm reports none.
        partition (`str`, *optional*): Partition or partitions the job was submitted to.
        qos (`str`, *optional*): Quality of service.
        account (`str`, *optional*): Slurm account.
        requested (`str`, *optional*): Requested resources (`ReqTRES` from
            `scontrol show job`), when Nest looked them up.
    """

    job_id: int
    state: str
    reason: str | None = None
    partition: str | None = None
    qos: str | None = None
    account: str | None = None
    requested: str | None = None

    @property
    def pending_category(self) -> str | None:
        """[`backends.slurm.classify_pending_reason`] of a `PENDING` job, else `None`."""
        if self.state != "PENDING":
            return None
        return classify_pending_reason(self.reason)

    def hint(self) -> str | None:
        """Returns a suggested action for a `PENDING` job's reason, or `None`."""
        category = self.pending_category
        if category is None or not self.reason:
            return None
        fields = {
            "reason": self.reason,
            "job_id": self.job_id,
            "partition": self.partition or "?",
            "qos": self.qos or "?",
            "account": self.account or "?",
        }
        if self.reason in _BLOCKED_REASONS:
            template = _BLOCKED_REASONS[self.reason]
        elif category == PENDING_BLOCKED:
            template = _PER_JOB_LIMIT_HINT
        elif category == PENDING_LIMIT:
            template = _LIMIT_HINT
        else:
            template = _QUEUED_HINT
        return template.format(**fields)

    def describe(self) -> str:
        """Returns `key=value` diagnostics for logs and error messages."""
        fields = {
            "job_id": self.job_id,
            "state": self.state,
            "reason": self.reason,
            "partition": self.partition,
            "qos": self.qos,
            "account": self.account,
            "requested": self.requested,
        }
        return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)


@dataclass(frozen=True)
class SlurmAccounting:
    """
    One job's allocation record from `sacct`, as Slurm reported it.

    Fields Slurm left empty are `None`; they are never filled in or assumed zero.

    Attributes:
        job_id (`int`): Slurm job ID.
        state (`str`): Slurm state, such as `COMPLETED` or `FAILED`.
        exit_code (`str`, *optional*): `<exit status>:<signal>`.
        elapsed (`str`, *optional*): Elapsed time, such as `00:01:02`.
        account (`str`, *optional*): Slurm account charged.
        user (`str`, *optional*): HPC UNIX user that ran the job.
        node_list (`str`, *optional*): Allocated nodes.
        alloc_tres (`str`, *optional*): Allocated trackable resources.
    """

    job_id: int
    state: str
    exit_code: str | None = None
    elapsed: str | None = None
    account: str | None = None
    user: str | None = None
    node_list: str | None = None
    alloc_tres: str | None = None


def _pending_detail(info: SlurmJobInfo) -> str:
    """Returns a message fragment naming a pending job's reason, context, and fix."""
    if info.state != "PENDING" or not info.reason:
        return ""
    context = " ".join(
        f"{key}={value}"
        for key, value in (
            ("partition", info.partition),
            ("qos", info.qos),
            ("account", info.account),
            ("requested", info.requested),
        )
        if value is not None
    )
    detail = f" Pending reason: {info.reason}"
    if context:
        detail += f" ({context})"
    return f"{detail}. {info.hint()}"


def _pending_history(reasons: list[str]) -> str:
    """Returns a message fragment listing the pending reasons seen, oldest first."""
    if not reasons:
        return ""
    return f" Pending reasons seen: {', '.join(reasons)}."


def _field(value: str | None) -> str | None:
    """Returns a stripped `squeue` field, or `None` when Slurm left it unset."""
    value = (value or "").strip()
    return None if value in ("", "None", "(null)", "n/a") else value


@dataclass(frozen=True)
class SlurmConfig:
    """
    Stores SSH connection details, Slurm defaults, and remote working paths.

    Attributes:
        ssh_host (`str`): SSH host name or configured SSH alias.
        account (`str`): Default Slurm account.
        partition (`str`, *optional*, defaults to `"h200_256gpu"`): Default Slurm partition.
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
        blocked_timeout (`float`, *optional*, defaults to `120`): Seconds a job may stay
            pending for a reason it cannot outwait, such as `PartitionConfig`, before
            Nest stops waiting with [`errors.JobBlockedError`]. `0` stops at the first
            such observation.
        limit_timeout (`float`, *optional*): Seconds a job may stay pending on a QoS or
            association usage limit, such as `MaxGRESPerAccount`, before Nest stops
            waiting with [`errors.JobBlockedError`]. Unset waits until the polling
            deadline, since such limits clear as other jobs finish.
        cancel_blocked (`bool`, *optional*, defaults to `False`): Whether to `scancel`
            a job when Nest stops waiting with [`errors.JobBlockedError`]. The job is
            never resubmitted.
        ssh_mode (`str`, *optional*, defaults to `"A"`): `"A"` resolves `ssh_host`
            through the local user's SSH configuration. `"B"` (managed SSH) ignores
            every SSH configuration file and requires `ssh_user`, `identity_file`,
            and `known_hosts_file`; host keys are always strictly checked.
        ssh_user (`str`, *optional*): Remote user passed with `-l`.
        ssh_port (`int`, *optional*): Remote port passed with `-p`.
        identity_file (`str`, *optional*): Private key for Mode B.
        known_hosts_file (`str`, *optional*): Verified host keys for Mode B.
        max_result_bytes (`int`, *optional*): Largest result
            [`backends.slurm.SlurmBackend.wait_result`] downloads; a larger one is
            never read past this bound and raises [`errors.ResultTooLargeError`].
            Unset downloads any size, as before.
        allowed_hosts (`tuple[str, ...]`, *optional*): Approved login nodes. When
            nonempty, `ssh_host` must be one of them.
    """

    ssh_host: str
    account: str
    partition: str = DEFAULT_PARTITION
    time: str | None = None
    work_root: str | None = None
    worker_workdir: str | None = None
    ssh_timeout: float = 60
    result_timeout: float = 600
    environment_timeout: float = 1800
    poll_interval: float = 5
    blocked_timeout: float = 120
    limit_timeout: float | None = None
    cancel_blocked: bool = False
    ssh_mode: str = "A"
    ssh_user: str | None = None
    ssh_port: int | None = None
    identity_file: str | None = None
    known_hosts_file: str | None = None
    allowed_hosts: tuple[str, ...] = ()
    max_result_bytes: int | None = None

    def __post_init__(self):
        if self.max_result_bytes is not None and self.max_result_bytes < 1:
            raise ValueError("max_result_bytes must be positive")
        if self.blocked_timeout < 0 or (self.limit_timeout is not None and self.limit_timeout < 0):
            raise ValueError("Pending timeouts cannot be negative")
        if self.ssh_mode not in ("A", "B"):
            raise ValueError(f"Unknown SSH mode {self.ssh_mode!r}")
        if self.allowed_hosts and self.ssh_host not in self.allowed_hosts:
            raise ValueError(f"SSH host {self.ssh_host!r} is not an approved login node")
        if self.ssh_host.startswith("-"):
            raise ValueError("SSH host cannot start with '-'")
        if self.ssh_mode == "B":
            missing = [
                name
                for name in ("ssh_user", "identity_file", "known_hosts_file")
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError("Managed SSH (mode B) requires " + ", ".join(missing))


def environment_id(pyproject_toml: bytes, uv_lock: bytes) -> str:
    """
    The SHA-256 ID of a Python environment, computed locally from its files.

    Hashes each file's name, byte length and contents, so the Controller knows the
    ID, and can record it, before anything is staged or built on the cluster.

    Returns:
        `str`: 64 lowercase hexadecimal characters.
    """
    hasher = hashlib.sha256()
    for name, content in (("pyproject.toml", pyproject_toml), ("uv.lock", uv_lock)):
        hasher.update(name.encode())
        hasher.update(b"\0")
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return hasher.hexdigest()


def worker_allocation(gpu: int, cpu: int) -> tuple[int, int]:
    """
    The GPUs per node and CPUs per task a worker job holds for a request: the
    request, or `scripts/worker.sbatch`'s default where it asks for `0`.
    """
    return (gpu or WORKER_DEFAULT_GPUS, cpu or WORKER_DEFAULT_CPUS)


class BuildProgress:
    """
    Follows one environment build for [`backends.slurm.SlurmBackend.prepare_environment_bytes`].

    Args:
        before_submit (`Callable[[], None]`, *optional*): Called after staging and
            the readiness check, immediately before the build's `sbatch`. An
            exception it raises propagates with no build job submitted.

    Attributes:
        started (`bool`): A build submission began, so a build job may exist.
        job_id (`int | None`): The build's Slurm job ID, once `sbatch` returned it.
        done (`bool`): The build job reached a final state Slurm reported.
    """

    def __init__(self, before_submit: Callable[[], None] | None = None) -> None:
        self.before_submit = before_submit
        self.started = False
        self.job_id: int | None = None
        self.done = False

    def submitting(self) -> None:
        if self.before_submit is not None:
            self.before_submit()
        self.started = True

    def submitted(self, job_id: int) -> None:
        self.job_id = job_id

    def finished(self) -> None:
        self.done = True

    @property
    def may_be_running(self) -> bool:
        """Whether a build job may exist that Slurm has not reported finished."""
        return self.started and not self.done


class SlurmBackend:
    """
    The shared Slurm execution core: stages files and manages Slurm jobs.

    Every remote command runs through a [`backends.transport.SlurmTransport`],
    which owns the connection. The default is the Controller's own noninteractive
    SSH ([`backends.transport.ControllerSSHTransport`], trusted Mode A), exactly as
    before. Operations a transport cannot perform without the user's live session
    raise [`errors.SessionUnavailableError`] before any command runs.
    """

    def __init__(self, config: SlurmConfig, transport: SlurmTransport | None = None):
        """
        Stores the backend configuration without connecting to the cluster.

        Args:
            config ([`backends.slurm.SlurmConfig`]): Slurm defaults, paths and, for
                the default transport, SSH settings.
            transport ([`backends.transport.SlurmTransport`], *optional*): Runs the
                remote commands. Defaults to the Controller's SSH from `config`.

        Raises:
            `ValueError`: If a user-owned transport is paired with a configuration
                carrying Controller SSH credentials, which it must never reach.
        """
        if transport is None:
            transport = ControllerSSHTransport(config)
        elif transport.capabilities.execution_authority != ExecutionAuthority.CONTROLLER_OWNED and (
            config.ssh_mode != "A" or config.identity_file or config.known_hosts_file
        ):
            raise ValueError(
                "A user-owned transport cannot use Controller SSH credentials; "
                "give it a configuration without ssh_mode B, identity_file or "
                "known_hosts_file"
            )
        self.config = config
        self.transport = transport

    @property
    def capabilities(self) -> TransportCapabilities:
        """What the transport can do: authority, interaction and offline limits."""
        return self.transport.capabilities

    def for_endpoint(self, host: str, port: int | None) -> SlurmBackend:
        """
        The same backend connected to another approved login node.

        Only the host and port change: the transport's identity, credentials,
        host-key file and every Slurm default stay the same.

        Raises:
            `ValueError`: If `host` is not one of `allowed_hosts` (or the configured
                host, when no list is set).
        """
        allowed = self.config.allowed_hosts or (self.config.ssh_host,)
        if host not in allowed:
            raise ValueError(f"SSH host {host!r} is not an approved login node")
        if (host, port) == (self.config.ssh_host, self.config.ssh_port):
            return self
        return SlurmBackend(
            replace(self.config, ssh_host=host, ssh_port=port),
            self.transport.for_endpoint(host, port),
        )

    def _require_session(self, operation: Operation, job_id: int | None = None) -> None:
        """
        Refuses an operation the transport cannot perform now, before any command.

        Raises:
            [`errors.SessionUnavailableError`]: If the user's session is not active
                and the transport cannot perform `operation` offline.
        """
        transport = self.transport
        capabilities = transport.capabilities
        if capabilities.allows_offline(operation) or transport.session_active():
            return
        how = (
            "Reconnect from an interactive terminal to answer the HPC password and MFA "
            "prompts, then try again."
            if capabilities.interactive_required
            else "Reconnect the session, then try again."
        )
        raise SessionUnavailableError(
            f"Slurm {operation} needs an active {capabilities.execution_authority} SSH "
            f"session to {transport.target}, and none is available. {how} Nest does not "
            "use the Controller's SSH instead.",
            job_id=job_id,
            interactive_required=capabilities.interactive_required,
            operation=str(operation),
        )

    def _ssh_bounded(self, command: str, limit: int) -> bytes:
        """
        Runs a command and returns its standard output, reading at most `limit`
        bytes of it into memory.

        Raises:
            [`errors.ResultTooLargeError`]: If the output exceeds `limit`.
            [`errors.SSHTimeoutError`], [`errors.SSHError`],
            [`errors.RemoteCommandError`]: As [`backends.slurm.SlurmBackend._ssh`].
        """
        return self.transport.run_bounded(command, limit)

    def _ssh(self, command: str, input: bytes | None = None) -> bytes:
        """
        Runs a remote command through the transport and classifies its failures.

        Args:
            command (`str`): Shell command to execute on the login node.
            input (`bytes`, *optional*): Data written to the command's standard input.

        Returns:
            `bytes`: Raw standard output.

        Raises:
            [`errors.SSHTimeoutError`]: If the command exceeds the transport's
                timeout. The remote operation's status is unknown.
            [`errors.SSHError`]: If SSH itself fails (exit status 255).
            [`errors.RemoteCommandError`]: If the remote command exits unsuccessfully.
        """
        return self.transport.check(command, input)

    def _run_remote(self, command: str) -> str:
        """
        Runs a text command through the transport.

        Args:
            command (`str`): Shell command to execute on the login node.

        Returns:
            `str`: Standard output with leading and trailing whitespace removed.

        Raises:
            [`errors.SSHTimeoutError`]: If the command exceeds the transport's
                timeout (60 seconds by default). Its status remains unknown.
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
        inherit_time: bool = True,
        script_args: tuple[str, ...] = (),
        single_task: bool = False,
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
            time (`str`, *optional*): Time limit override. When unset or empty, uses
                the configured limit if `inherit_time` is set. Omits the flag otherwise.
            inherit_time (`bool`, *optional*, defaults to `True`): Whether an unset
                `time` falls back to the configured limit. When `False`, an unset
                `time` omits the flag, so the script's `#SBATCH --time` applies.
            script_args (`tuple[str, ...]`, *optional*, defaults to `()`): Arguments
                appended after the script path.
            single_task (`bool`, *optional*, defaults to `False`): Pass `--nodes=1`
                and `--ntasks=1`, which override the script's own node and task
                counts, so the job holds at most one node and one task.

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

        if single_task:
            args.extend(("--nodes=1", "--ntasks=1"))

        if gpu > 0:
            args.append(f"--gpus-per-node={gpu}")

        if cpu > 0:
            args.append(f"--cpus-per-task={cpu}")

        time_limit = time or (self.config.time if inherit_time else None)

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

        self._require_session(Operation.SUBMIT)
        try:
            result = self.transport.run(command)
        except SSHTimeoutError as exc:
            raise SubmissionUnknownError(
                f"sbatch over SSH timed out after {self.transport.timeout:g} seconds. {unknown}"
            ) from exc

        stdout = result.stdout.decode(errors="replace")

        if result.returncode != 0:
            error = self.transport.error(result)
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
        return self.job_info(job_id).state

    def job_info(self, job_id: int) -> SlurmJobInfo:
        """
        Looks up a job's state and pending reason with `squeue`, then `sacct`.

        Args:
            job_id (`int`): Slurm job ID.

        Returns:
            [`backends.slurm.SlurmJobInfo`]: State, reason, partition, QoS, and account
                from `squeue` while the job is active. Only the state, from `sacct`,
                once it has left the queue, or `"UNKNOWN"` when neither knows the job.

        Raises:
            [`errors.RemoteCommandError`]: If a remote query fails, except an invalid
                job ID from `squeue`.
            [`errors.SSHError`]: If SSH cannot reach the host.
            [`errors.SSHTimeoutError`]: If an SSH command exceeds `ssh_timeout`.
        """
        job_id = int(job_id)
        self._require_session(Operation.STATUS, job_id)

        # Check active jobs
        try:
            output = self._run_remote(
                f"squeue --noheader --jobs={job_id} --format={shlex.quote(_SQUEUE_FORMAT)}"
            )
        except RemoteCommandError as exc:
            if "Invalid job id specified" not in str(exc):
                raise
            output = ""

        if output:
            fields = [*output.splitlines()[0].split("|"), "", "", "", ""]
            return SlurmJobInfo(
                job_id=job_id,
                state=fields[0].strip(),
                reason=_field(fields[1]),
                partition=_field(fields[2]),
                qos=_field(fields[3]),
                account=_field(fields[4]),
            )

        # Check job accounting history
        output = self._run_remote(f"sacct -X -n -P --jobs={job_id} --format=State")

        if output:
            return SlurmJobInfo(job_id=job_id, state=output.splitlines()[0].strip())

        return SlurmJobInfo(job_id=job_id, state="UNKNOWN")

    def _with_details(self, info: SlurmJobInfo) -> SlurmJobInfo:
        """
        Adds the requested resources from `scontrol show job`, when available.

        The lookup only enriches diagnostics, so any failure is logged and ignored.

        Args:
            info ([`backends.slurm.SlurmJobInfo`]): Job to enrich.

        Returns:
            [`backends.slurm.SlurmJobInfo`]: `info` with `requested` set when
                `scontrol` reported `ReqTRES`, otherwise `info` unchanged.
        """
        try:
            output = self._run_remote(f"scontrol show job --oneliner {int(info.job_id)}")
        except InfrastructureError as exc:
            logger.debug("scontrol lookup failed job_id=%s error=%s", info.job_id, exc)
            return info
        match = _REQ_TRES.search(output)
        return replace(info, requested=match.group(1)) if match else info

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
        self._require_session(Operation.CANCEL, job_id)
        logger.info("Cancelling Slurm job job_id=%s", job_id)
        self._run_remote(f"scancel {job_id}")

    _LOG_PREFIXES: ClassVar[dict[str, str]] = {"worker": "worker", "environment": "env"}
    """`#SBATCH --output` prefixes of the deployed worker and environment build scripts."""

    _LOG_MARKER = b"nest-log\n"
    """Printed before a log's contents, so a missing log differs from an empty one."""

    DEFAULT_LOG_BYTES = 64 * 1024
    """Bytes [`backends.slurm.SlurmBackend.read_log`] returns from a log's end by default."""

    @operation(logger, "Slurm read_log")
    def read_log(
        self, job_id: int, *, job: str = "worker", limit: int = DEFAULT_LOG_BYTES
    ) -> bytes | None:
        """
        Reads the end of a job's Slurm output file. Read-only.

        Args:
            job_id (`int`): Slurm job ID.
            job (`str`, *optional*, defaults to `"worker"`): `"worker"` for a worker
                job, `"environment"` for an environment build.
            limit (`int`, *optional*): Most bytes returned, from the end of the log.

        Returns:
            `bytes | None`: The last `limit` bytes, or `None` when the log does not
                exist (yet, or any more).

        Raises:
            `ValueError`: If `job` or `limit` is invalid or `worker_workdir` is missing.
            [`errors.SessionUnavailableError`]: If the transport cannot read now.
            [`errors.InfrastructureError`]: If the remote read fails.
        """
        prefix = self._LOG_PREFIXES.get(job)
        if prefix is None:
            raise ValueError(f"Unknown job kind {job!r}")
        if limit < 1:
            raise ValueError("limit must be positive")
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")
        job_id = int(job_id)
        self._require_session(Operation.LOGS, job_id)
        path = shlex.quote(f"{self.config.worker_workdir.rstrip('/')}/{prefix}-{job_id}.out")
        marker = self._LOG_MARKER
        command = f"test -f {path} || exit 0; printf {shlex.quote(marker.decode())}; tail -c {limit} {path}"
        try:
            output = self._ssh_bounded(command, limit + len(marker))
        except InfrastructureError as exc:
            exc.job_id = job_id
            raise
        if not output.startswith(marker):
            return None
        return output[len(marker) :]

    _ACCOUNTING_FIELDS: ClassVar[tuple[str, ...]] = (
        "JobID",
        "State",
        "ExitCode",
        "Elapsed",
        "Account",
        "User",
        "NodeList",
        "AllocTRES",
    )

    @operation(logger, "Slurm accounting")
    def accounting(self, job_id: int) -> SlurmAccounting | None:
        """
        Reads a job's allocation record from Slurm accounting (`sacct`). Read-only.

        Reports what Slurm recorded and nothing more: it does not decide whether
        the user's function succeeded, and a missing record is `None`, never zero
        usage.

        Args:
            job_id (`int`): Slurm job ID.

        Returns:
            [`backends.slurm.SlurmAccounting`] or `None`: The record, or `None`
                when accounting does not know the job.

        Raises:
            [`errors.SessionUnavailableError`]: If the transport cannot read now.
            [`errors.InfrastructureError`]: If the remote query fails.
        """
        job_id = int(job_id)
        self._require_session(Operation.ACCOUNTING, job_id)
        output = self._run_remote(
            f"sacct -X -n -P --jobs={job_id} --format={','.join(self._ACCOUNTING_FIELDS)}"
        )
        line = output.splitlines()[0] if output else ""
        if not line.strip():
            return None
        fields = [*line.split("|"), *[""] * len(self._ACCOUNTING_FIELDS)]
        return SlurmAccounting(
            job_id=job_id,
            state=fields[1].strip() or "UNKNOWN",
            exit_code=_field(fields[2]),
            elapsed=_field(fields[3]),
            account=_field(fields[4]),
            user=_field(fields[5]),
            node_list=_field(fields[6]),
            alloc_tres=_field(fields[7]),
        )

    def execution_dir(self, execution_id: str) -> str:
        """
        Returns the remote execution directory for an Execution ID. Runs no command.

        Args:
            execution_id (`str`): ID from [`backends.slurm.new_execution_id`].

        Returns:
            `str`: `<work_root>/<execution_id>`.

        Raises:
            `ValueError`: If the ID is malformed, or `work_root` is missing or relative.
        """
        if not _EXECUTION_ID.fullmatch(execution_id):
            raise ValueError("Invalid execution ID")
        if not self.config.work_root:
            raise ValueError("Slurm work_root is not configured")
        work_root = self.config.work_root
        if not work_root.startswith("/"):
            raise ValueError("Slurm work_root must be an absolute path")
        return f"{work_root.rstrip('/')}/{execution_id}"

    def attempt_dir(self, execution_id: str, attempt: int | None) -> str:
        """
        Returns an attempt's artifact directory. Runs no command.

        Args:
            execution_id (`str`): ID from [`backends.slurm.new_execution_id`].
            attempt (`int`, *optional*): Attempt number, for a worker that speaks
                [`outcome.WORKER_PROTOCOL`]. `None` selects the shared execution
                directory, as a legacy worker uses.

        Returns:
            `str`: `<work_root>/<execution_id>/attempts/<attempt>`, or the execution
                directory.

        Raises:
            `ValueError`: If the ID or attempt number is malformed, or `work_root`
                is missing or relative.
        """
        execution = self.execution_dir(execution_id)
        if attempt is None:
            return execution
        if type(attempt) is not int or not 1 <= attempt < 10**9:
            raise ValueError("Invalid attempt number")
        return f"{execution}/attempts/{attempt}"

    def worker_protocol(self) -> int:
        """
        Reads the protocol the deployed worker script declares
        (`# nest-worker-protocol: N`), before a submission. A script without the
        line predates the outcome record: protocol 1.

        Raises:
            `ValueError`: If `worker_workdir` is not configured.
            [`errors.SessionUnavailableError`], [`errors.SSHError`],
            [`errors.RemoteCommandError`], [`errors.SSHTimeoutError`]: If the
                script cannot be read; nothing is submitted then.
        """
        if not self.config.worker_workdir:
            raise ValueError("worker_workdir is not configured")
        self._require_session(Operation.SUBMIT)
        script = shlex.quote(f"{self.config.worker_workdir.rstrip('/')}/{WORKER_SCRIPT}")
        output = self._run_remote(
            f"test -r {script} && sed -n 's/^# nest-worker-protocol: \\([1-9][0-9]\\{{0,2\\}}\\)$/\\1/p' "
            f"{script} | head -n 1"
        )
        return int(output) if output.isdigit() else 1

    @operation(logger, "Slurm stage_payload")
    def stage_payload(
        self, payload: bytes, execution_id: str | None = None, *, attempt: int | None = None
    ) -> str:
        """
        Uploads a payload as `input.pkl` in the attempt's artifact directory (see
        [`backends.slurm.SlurmBackend.attempt_dir`]), and removes any outcome
        record already there, so it is never read as this attempt's.

        Args:
            payload (`bytes`): Serialized function and arguments.
            execution_id (`str`, *optional*): Execution ID naming the directory. The
                Controller passes the ID it already recorded; omitted, a new one is
                generated.
            attempt (`int`, *optional*): Attempt number, for a protocol 2 worker.

        Returns:
            `str`: The artifact directory under `work_root`.

        Raises:
            `ValueError`: If the ID is malformed, or `work_root` is missing or relative.
            [`errors.SSHError`] or [`errors.RemoteCommandError`]: If the upload fails.
            [`errors.SSHTimeoutError`]: If the upload exceeds `ssh_timeout`.
        """
        job_dir = self.attempt_dir(execution_id or new_execution_id(), attempt)
        self._require_session(Operation.SUBMIT)
        input_path = f"{job_dir}/input.pkl"
        logger.info(
            "Staging payload host=%s job_dir=%s payload_bytes=%s",
            self.transport.target,
            job_dir,
            len(payload),
        )

        outcome_path = f"{job_dir}/{outcome.OUTCOME_FILE}"
        command = (
            f"mkdir -p {shlex.quote(job_dir)} && rm -f {shlex.quote(outcome_path)} && "
            f"cat > {shlex.quote(input_path)}"
        )
        self._ssh(command, input=payload)
        return job_dir

    def submit_payload(
        self,
        payload: bytes,
        gpu: int = 1,
        *,
        cpu: int = 0,
        environment_id: str | None = None,
        execution_id: str | None = None,
        account: str | None = None,
        partition: str | None = None,
        time: str | None = None,
        bounded: bool = False,
        before_submit: Callable[[], None] | None = None,
        attempt: int | None = None,
    ) -> tuple[int, str]:
        """
        Stages a serialized call and submits the deployed worker batch script once.

        The Execution ID names the remote directory and is passed to the worker, so a
        caller that records it before calling can always find the execution, even
        when the submission outcome is unknown.

        Args:
            payload (`bytes`): Serialized function and arguments.
            gpu (`int`, *optional*, defaults to `1`): GPU count passed to
                [`backends.slurm.SlurmBackend.submit`] and to the worker script, which
                exposes the allocated GPUs to the function only when it is positive.
            cpu (`int`, *optional*, defaults to `0`): CPUs per task passed to
                [`backends.slurm.SlurmBackend.submit`]; `0` keeps the script's value.
            environment_id (`str`, *optional*): Prepared environment's 64-character
                lowercase hexadecimal ID. Omitting it selects the Nest runtime.
            execution_id (`str`, *optional*): Execution ID from
                [`backends.slurm.new_execution_id`]. Omitted, a new one is generated.
            account, partition, time (`str`, *optional*): Passed to
                [`backends.slurm.SlurmBackend.submit`]; unset uses the configured
                defaults, as before. The registry-backed Controller passes the values
                it resolved and recorded.
            bounded (`bool`, *optional*, defaults to `False`): Pin the allocation to
                [`backends.slurm.worker_allocation`]: one node, one task, and an
                explicit `--cpus-per-task`, so the job holds exactly what admission
                reserved. With `gpu` `0` the worker's own GPU default still applies,
                and is reserved.
            before_submit (`Callable[[], None]`, *optional*): Called after staging,
                immediately before `sbatch`. An exception it raises propagates and
                nothing is submitted.
            attempt (`int`, *optional*): Attempt number. Given, the payload, result
                and outcome record live in the attempt's own directory and the
                worker receives it as its seventh argument; only a worker that
                declares protocol 2 (see
                [`backends.slurm.SlurmBackend.worker_protocol`]) honors it.

        Returns:
            `tuple[int, str]`: Submitted job ID and the attempt's artifact directory.

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

        # Validate before any SSH command.
        execution_id = execution_id or new_execution_id()
        self.attempt_dir(execution_id, attempt)

        # Upload payload into the attempt's directory
        job_dir = self.stage_payload(payload, execution_id, attempt=attempt)
        if attempt is None:
            execution_id = job_dir.rsplit("/", 1)[-1]

        # Prepare script arguments
        script_args = (
            execution_id,
            environment_id or "",
            self.config.worker_workdir,
            current_request_id(),
            load_log_level()[0],
            # The worker enables GPU passthrough only for an explicit request, since the
            # script's own `#SBATCH` GPU default applies when `gpu` is `0`.
            str(gpu),
            "" if attempt is None else str(attempt),
        )

        if before_submit is not None:
            before_submit()

        # Submit Slurm job exactly once; an ambiguous outcome is reported, not retried.
        try:
            job_id = self.submit(
                workdir=self.config.worker_workdir,
                script=WORKER_SCRIPT,
                gpu=gpu,
                cpu=(worker_allocation(gpu, cpu)[1] if bounded else cpu),
                account=account,
                partition=partition,
                time=time,
                script_args=script_args,
                single_task=bounded,
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
        Polls a job until it completes, classifying failures, timeouts, and blockers.

        Every change of state or pending reason is logged. A QoS or association
        limit, or a reason the job cannot outwait, is logged as a warning with the
        job's partition, QoS, account, and requested resources, plus a suggested fix.
        Pending reasons seen while polling are kept in timeout messages. Normal
        congestion, such as `Priority` or `Resources`, only ever ends in the
        polling deadline.

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
            [`errors.JobBlockedError`]: If the job stayed pending for longer than
                `blocked_timeout` for a reason it cannot outwait, or longer than
                `limit_timeout` on a usage limit. Cancels the job first only when
                `cancel_blocked` is set.
            [`errors.QueueTimeoutError`]: If the job never started before the deadline.
            [`errors.InfrastructureError`]: If a status query fails over SSH. Its
                `job_id` is set to the polled job.
        """
        if timeout < 0 or poll_interval <= 0:
            raise ValueError("Timeout cannot be negative and poll interval must be positive")

        deadline = time.monotonic() + timeout
        previous: tuple[str, str | None] | None = None
        requested: str | None = None
        started = False
        reasons: list[str] = []
        category: str | None = None
        category_since = 0.0
        logger.info("Waiting for Slurm job job_id=%s timeout_s=%s", job_id, timeout)

        while True:
            try:
                info = self.job_info(job_id)
            except InfrastructureError as exc:
                # The job exists; let callers find it before calling again.
                exc.job_id = job_id
                raise

            state = info.state
            actionable = info.pending_category in (PENDING_LIMIT, PENDING_BLOCKED)
            if (state, info.reason) != previous:
                previous = (state, info.reason)
                requested = None
                if actionable:
                    info = self._with_details(info)
                    requested = info.requested
                    logger.warning(
                        "Slurm job pending on %s %s hint=%s",
                        info.pending_category,
                        info.describe(),
                        info.hint(),
                    )
                else:
                    logger.info("Slurm state changed %s", info.describe())
            elif actionable and requested:
                # Reuse the resources looked up when the reason first appeared.
                info = replace(info, requested=requested)

            if state == "COMPLETED":
                return

            pending_category = info.pending_category
            if info.reason and pending_category is not None and info.reason not in reasons:
                reasons.append(info.reason)
            history = _pending_history(reasons)

            if state.startswith("TIMEOUT"):
                raise ExecutionTimeoutError(
                    f"{label} {job_id} exceeded its Slurm time limit: {state}{history}",
                    job_id=job_id,
                )

            if state.startswith(_FAILED_STATES):
                raise SlurmJobError(f"{label} {job_id} failed: {state}{history}", job_id=job_id)

            started = started or state.startswith(_STARTED_STATES)
            now = time.monotonic()

            if pending_category != category:
                category, category_since = pending_category, now
            limit = {
                PENDING_BLOCKED: self.config.blocked_timeout,
                PENDING_LIMIT: self.config.limit_timeout,
            }.get(category or "")
            if limit is not None and now - category_since >= limit:
                raise self._blocked(label, info, now - category_since)

            if now >= deadline:
                if started:
                    raise ExecutionTimeoutError(
                        f"{label} {job_id} did not finish within {timeout:g} seconds. "
                        f"Last state: {state}.{history} The job was not cancelled.",
                        job_id=job_id,
                    )
                raise QueueTimeoutError(
                    f"{label} {job_id} did not start within {timeout:g} seconds. "
                    f"Last state: {state}.{_pending_detail(info)}{history} "
                    "The job was not cancelled.",
                    job_id=job_id,
                    pending_reason=info.reason,
                )

            time.sleep(poll_interval)

    def _blocked(self, label: str, info: SlurmJobInfo, waited: float) -> JobBlockedError:
        """
        Builds the error for a job Nest stopped waiting for, cancelling it if configured.

        A job is never resubmitted. A failed cancellation is reported in the message
        rather than replacing the diagnostic.

        Args:
            label (`str`): Job description used in messages, such as `"Slurm job"`.
            info ([`backends.slurm.SlurmJobInfo`]): Last observation of the pending job.
            waited (`float`): Seconds the job has been pending for this kind of reason.

        Returns:
            [`errors.JobBlockedError`]: The error to raise.
        """
        job_id = info.job_id
        if self.config.cancel_blocked:
            try:
                self.cancel(job_id)
                outcome = "Nest requested its cancellation and will not resubmit it."
            except InfrastructureError as exc:
                logger.error("Cancelling blocked job failed job_id=%s error=%s", job_id, exc)
                outcome = f"Cancelling it failed, so it may still be queued: {exc}"
        else:
            outcome = (
                f"The job was not cancelled; cancel it with `scancel {job_id}` or fix the cause."
            )
        logger.error("Stopped waiting for pending job %s waited_s=%.1f", info.describe(), waited)
        if info.pending_category == PENDING_LIMIT:
            why = "on a QoS or association limit"
        else:
            why = "for a reason it cannot outwait"
        return JobBlockedError(
            f"{label} {job_id} stopped waiting after {waited:g} seconds pending {why}: "
            f"{info.reason}.{_pending_detail(info)} {outcome}",
            job_id=job_id,
            pending_reason=info.reason,
        )

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
            [`errors.ResultTooLargeError`]: If the result exceeds `max_result_bytes`.
            [`errors.InfrastructureError`]: If a remote command fails or the result
                file is empty. Timeouts never cancel the job.
        """
        self._require_session(Operation.RESULT, job_id)
        self._wait_job(
            job_id,
            "Slurm job",
            self.config.result_timeout if timeout is None else timeout,
            self.config.poll_interval if poll_interval is None else poll_interval,
        )

        # Download binary result through SSH stdout
        output_path = f"{job_dir}/output.pkl"
        logger.info("Downloading result job_id=%s job_dir=%s", job_id, job_dir)
        limit = self.config.max_result_bytes
        try:
            if limit is None:
                result = self._ssh(f"cat {shlex.quote(output_path)}")
            else:
                # The remote side sends at most one byte past the bound, and the local
                # read stops there too, so the result never fills memory.
                result = self._ssh_bounded(f"head -c {limit + 1} {shlex.quote(output_path)}", limit)
        except InfrastructureError as exc:
            exc.job_id = job_id
            raise

        if not result:
            raise InfrastructureError("Worker returned an empty result file", job_id=job_id)

        logger.debug("Result downloaded job_id=%s result_bytes=%s", job_id, len(result))
        return result

    _OUTCOME_PRESENT = b"nest-outcome-present\n"
    _OUTCOME_ABSENT = b"nest-outcome-absent\n"
    _OUTCOME_NO_ACCESS = b"nest-outcome-no-access\n"

    def read_outcome(self, job_dir: str) -> outcome.RemoteOutcome | None:
        """
        Reads a completed worker's outcome record (`outcome.json`) over SSH, at most
        [`outcome.MAX_OUTCOME_BYTES`] of it, and parses it as plain JSON. The
        result file is never read or unpickled here.

        Args:
            job_dir (`str`): The attempt's artifact directory, as returned by
                [`backends.slurm.SlurmBackend.submit_payload`] or
                [`backends.slurm.SlurmBackend.attempt_dir`].

        Returns:
            [`outcome.RemoteOutcome`] or `None`: `None` when no record exists there.

        Raises:
            [`outcome.OutcomeUnreadableError`]: If the path exists but is not a
                regular file, or the directory or file cannot be read.
            [`outcome.InvalidOutcomeError`]: If the record is not valid.
            [`errors.SessionUnavailableError`], [`errors.SSHError`],
            [`errors.RemoteCommandError`], [`errors.SSHTimeoutError`]: If it cannot
                be read now; the outcome is unknown.
        """
        self._require_session(Operation.RESULT)
        directory = shlex.quote(job_dir)
        path = shlex.quote(f"{job_dir}/{outcome.OUTCOME_FILE}")
        present, absent, no_access = (
            marker.decode().strip()
            for marker in (self._OUTCOME_PRESENT, self._OUTCOME_ABSENT, self._OUTCOME_NO_ACCESS)
        )
        data = self._ssh(
            f"if [ -e {directory} ] && ! [ -x {directory} ]; then echo {no_access}; "
            f"elif [ -e {path} ] || [ -L {path} ]; then "
            f"if [ -f {path} ] && [ -r {path} ]; then echo {present} && "
            f"head -c {outcome.MAX_OUTCOME_BYTES + 1} {path}; else echo {no_access}; fi; "
            f"else echo {absent}; fi"
        )
        if data == self._OUTCOME_ABSENT:
            return None
        if data == self._OUTCOME_NO_ACCESS:
            raise outcome.OutcomeUnreadableError(
                "The outcome record is not a readable regular file"
            )
        if not data.startswith(self._OUTCOME_PRESENT):
            raise RemoteCommandError("Unexpected reply while reading the worker's outcome record")
        return outcome.parse(data[len(self._OUTCOME_PRESENT) :])

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

        self._require_session(Operation.PREPARE)

        names = ("pyproject.toml", "uv.lock")
        files = {
            "pyproject.toml": pyproject_toml,
            "uv.lock": uv_lock,
        }

        env_id = environment_id(pyproject_toml, uv_lock)

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

    def build_environment(
        self,
        env_dir: str,
        gpu: int = 0,
        *,
        cpu: int = 0,
        account: str | None = None,
        partition: str | None = None,
    ) -> int:
        """
        Submits the environment build script for a staged environment directory.

        By default, the build uses the resource counts and time limit in
        `scripts/environment-smoke.sbatch`.

        Args:
            env_dir (`str`): Directory under `worker_workdir/environments` whose name
                is a 64-character lowercase hexadecimal environment ID.
            gpu (`int`, *optional*, defaults to `0`): GPUs per node for the build.
                `0` keeps the script's value.
            cpu (`int`, *optional*, defaults to `0`): CPUs per task passed to
                [`backends.slurm.SlurmBackend.submit`]; `0` keeps the script's value.
            account, partition (`str`, *optional*): The Slurm account and partition
                to charge. The registry-backed Controller passes the ones it
                authorized for the execution, so the build is never charged to the
                Platform Profile's default account instead. Unset uses the defaults.

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
            account=account,
            partition=partition,
            # The build script sets its own limit; the worker job default is often too short.
            inherit_time=False,
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

        self._require_session(Operation.PREPARE)
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

    def prepare_environment(self, project_dir: str, gpu: int = 0, *, cpu: int = 0) -> str:
        """
        Stages a local project's dependency files and prepares its cached runtime.

        Args:
            project_dir (`str`): Local directory containing `pyproject.toml` and `uv.lock`.
            gpu (`int`, *optional*, defaults to `0`): GPUs per node for a required
                build job. `0` keeps the script's value.
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
        gpu: int = 0,
        cpu: int = 0,
        progress: BuildProgress | None = None,
        account: str | None = None,
        partition: str | None = None,
    ) -> str:
        """
        Stages dependency files and reuses or builds their runtime environment.

        Skips submission when the current runtime is ready. Otherwise, submits a
        build, waits with [`backends.slurm.SlurmBackend.wait_environment`], and checks
        readiness again before returning.

        Args:
            pyproject_toml (`bytes`): Contents of the project's `pyproject.toml`.
            uv_lock (`bytes`): Contents of the project's `uv.lock`.
            gpu (`int`, *optional*, defaults to `0`): GPUs per node for a required
                build job. `0` keeps the script's value.
            cpu (`int`, *optional*, defaults to `0`): CPUs per task for a required
                build job; `0` keeps the script's value.
            account, partition (`str`, *optional*): Passed to
                [`backends.slurm.SlurmBackend.build_environment`].
            progress ([`backends.slurm.BuildProgress`], *optional*): Told when a
                build job may exist and when it has finished, so a caller can tell a
                build that may still be running from one that is over.

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
        if progress is not None:
            progress.submitting()
        job_id = self.build_environment(
            env_dir, gpu=gpu, cpu=cpu, account=account, partition=partition
        )
        if progress is not None:
            progress.submitted(job_id)
        logger.info(
            "Environment build submitted job_id=%s environment_id=%s", job_id, Path(env_dir).name
        )
        self.wait_environment(job_id)
        if progress is not None:
            progress.finished()

        if not self.environment_ready(env_dir):
            raise InfrastructureError(
                "Environment build completed but cache is not ready", job_id=job_id
            )

        return env_dir
