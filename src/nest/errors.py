from typing import ClassVar

import grpc

ERROR_KIND_METADATA = "nest-error-kind"
"""Trailing-metadata key carrying an [`errors.InfrastructureError`] kind."""

JOB_ID_METADATA = "nest-job-id"
"""Trailing-metadata key carrying the Slurm job ID related to an error, if any."""

PENDING_REASON_METADATA = "nest-pending-reason"
"""Trailing-metadata key carrying the last Slurm pending reason of a job that never started."""

AUTHORIZATION_METADATA = "authorization"
"""Request-metadata key carrying `Bearer <Nest CLI access token>`."""

PROJECT_METADATA = "x-nest-project-id"
"""Request-metadata key naming the Project a submission runs in (`NEST_PROJECT_ID`)."""

PLATFORM_METADATA = "x-nest-platform"
"""Request-metadata key naming the HPC Platform (slug or ID) a submission asks for. The
server still decides: an ineligible Platform is refused, never replaced."""

IDEMPOTENCY_METADATA = "x-nest-idempotency-key"
"""Request-metadata key making a registry-backed submission idempotent for its owner
and Project: a repeated request with the same key never submits twice."""

EXECUTION_ID_METADATA = "nest-execution-id"
"""Trailing-metadata key carrying the Nest execution ID of a registry-backed submission."""


class RemoteExecutionError(RuntimeError):
    """An exception raised by a remotely executed function."""

    def __init__(
        self,
        remote_type: str,
        remote_message: str,
        remote_traceback: str,
    ):
        """
        Stores remote exception details and formats the local exception message.

        Args:
            remote_type (`str`): Fully qualified remote exception type.
            remote_message (`str`): Remote exception message.
            remote_traceback (`str`): Formatted traceback from the remote process.
        """
        self.remote_type = remote_type
        self.remote_message = remote_message
        self.remote_traceback = remote_traceback

        super().__init__(
            f"{remote_type}: {remote_message}\n\nRemote traceback:\n{remote_traceback}"
        )


class InfrastructureError(RuntimeError, grpc.RpcError):
    """
    A failure of SSH, Slurm, or the RPC itself rather than of the user's function.

    The Slurm backend raises subclasses directly. The Controller reports them with
    the class's gRPC status and its `kind` in the `nest-error-kind` trailing
    metadata, and the client raises the same class again. Subclasses remain
    `grpc.RpcError` instances, so `code()` and `details()` keep working for
    callers that handle RPC errors generically.

    Attributes:
        kind (`str`): Stable machine-readable error kind.
        status (`grpc.StatusCode`): gRPC status the Controller reports.
        job_id (`int`, *optional*): Related Slurm job ID, when one is known.
    """

    kind: ClassVar[str] = "infrastructure_error"
    status: ClassVar[grpc.StatusCode] = grpc.StatusCode.INTERNAL

    def __init__(self, message: str, *, job_id: int | None = None):
        """
        Stores the message and an optional related job ID.

        Args:
            message (`str`): Human-readable description.
            job_id (`int`, *optional*): Related Slurm job ID.
        """
        super().__init__(message)
        self.job_id = job_id

    def code(self) -> grpc.StatusCode:
        """Returns the gRPC status associated with this error kind."""
        return self.status

    def details(self) -> str:
        """Returns the error message, as `grpc.Call.details()` would."""
        return str(self)


class SSHError(InfrastructureError):
    """
    SSH could not reach or authenticate to the login node (`ssh` exited with 255).

    No remote command is known to have started.
    """

    kind = "ssh_error"
    status = grpc.StatusCode.UNAVAILABLE


class RemoteCommandError(InfrastructureError):
    """
    A remote command ran but exited unsuccessfully, for example a rejected `sbatch`.

    Attributes:
        returncode (`int`, *optional*): Exit status reported by SSH.
    """

    kind = "remote_command_error"

    def __init__(self, message: str, *, returncode: int | None = None, job_id: int | None = None):
        """
        Stores the message, remote exit status, and optional job ID.

        Args:
            message (`str`): Human-readable description, usually the remote stderr.
            returncode (`int`, *optional*): Exit status reported by SSH.
            job_id (`int`, *optional*): Related Slurm job ID.
        """
        super().__init__(message, job_id=job_id)
        self.returncode = returncode


class SlurmJobError(InfrastructureError):
    """A submitted Slurm job ended in a failure state such as `FAILED` or `NODE_FAIL`."""

    kind = "slurm_job_failed"


class SubmissionUnknownError(InfrastructureError):
    """
    `sbatch` may or may not have submitted a job; Nest did not receive a job ID.

    Raised when SSH times out during submission, SSH disconnects after the remote
    command started, or `sbatch` succeeds without printing a job ID. Nest never
    resubmits automatically. Check the queue and the execution directory named
    in the message before submitting again.
    """

    kind = "submission_unknown"
    status = grpc.StatusCode.UNKNOWN


class NestTimeoutError(InfrastructureError, TimeoutError):
    """
    Base class for Nest timeouts. Also a `TimeoutError`.

    A timeout never cancels a submitted Slurm job.
    """

    kind = "timeout"
    status = grpc.StatusCode.DEADLINE_EXCEEDED


class SSHTimeoutError(NestTimeoutError):
    """An SSH command or file transfer exceeded the SSH timeout and was killed."""

    kind = "ssh_timeout"


class QueueTimeoutError(NestTimeoutError):
    """
    The job was submitted but never started running before the polling deadline.

    Attributes:
        pending_reason (`str`, *optional*): Last Slurm pending reason observed, such
            as `Priority` or `MaxGRESPerAccount`, when Slurm reported one.
    """

    kind = "queue_timeout"

    def __init__(
        self, message: str, *, job_id: int | None = None, pending_reason: str | None = None
    ):
        """
        Stores the message, optional job ID, and last pending reason.

        Args:
            message (`str`): Human-readable description.
            job_id (`int`, *optional*): Related Slurm job ID.
            pending_reason (`str`, *optional*): Last Slurm pending reason observed.
        """
        super().__init__(message, job_id=job_id)
        self.pending_reason = pending_reason


class JobBlockedError(QueueTimeoutError):
    """
    The job stayed pending for a reason that it cannot outwait, so Nest stopped waiting.

    Raised before the polling deadline when Slurm keeps reporting a pending reason
    that needs a change to the request or to the cluster's configuration, such as
    `PartitionConfig`, `InvalidQOS`, or a held job. The message names the reason,
    the job's partition, QoS, and account, and a suggested fix. The job is left
    queued unless the Controller is configured to cancel it. Also a
    [`errors.QueueTimeoutError`].
    """

    kind = "job_blocked"
    status = grpc.StatusCode.FAILED_PRECONDITION


class ExecutionTimeoutError(NestTimeoutError):
    """
    The job started but did not finish before the polling deadline, or Slurm ended
    it with the `TIMEOUT` state after it exceeded its time limit.
    """

    kind = "execution_timeout"


class RpcTimeoutError(NestTimeoutError):
    """The client's `Execute` RPC deadline expired before the Controller replied."""

    kind = "rpc_timeout"


class ResultTooLargeError(InfrastructureError):
    """
    The job completed, but its result is larger than the Controller may return
    (`NEST_GRPC_MAX_MESSAGE_BYTES`), so it was not downloaded. The result stays in
    the job's execution directory on the cluster.
    """

    kind = "result_too_large"
    status = grpc.StatusCode.RESOURCE_EXHAUSTED


class DatabaseUnavailableError(InfrastructureError):
    """
    The Controller's PostgreSQL database could not record the request.

    Raised before any SSH or Slurm side effect, so nothing was submitted and the
    request can be retried once the database is reachable again.
    """

    kind = "database_unavailable"
    status = grpc.StatusCode.UNAVAILABLE


class AuthenticationRequiredError(PermissionError):
    """
    The Controller requires a Nest session and the call presented none it accepts.

    Sign in with `nest login`. The SDK sends the session to the hosted Controller,
    or to another Controller when `NEST_AUTH_URL` names its sign-in origin.
    """


class RemoteAccessDeniedError(PermissionError):
    """
    The Controller's Project policy or its Provider refused the call. Nothing was run.

    A Project or job the caller is not a member of is reported as not found, so this
    does not reveal whether it exists.
    """


class ProjectSelectionError(RuntimeError):
    """
    `NEST_PROJECT_ID` is unset and the SDK could not choose a Project. Nothing was run.

    Attributes:
        reason (`str`): `none_eligible` (no Project of yours may run on this
            Controller), `ambiguous` (several may, and there is no terminal to ask
            which), or `unsupported` (the Controller cannot list Projects; only
            `nest projects` reports it, while the SDK then sends no Project as
            before).
        project_ids (`tuple[str, ...]`): The eligible Projects, for `ambiguous`.
    """

    def __init__(self, message: str, reason: str, project_ids: tuple[str, ...] = ()):
        super().__init__(message)
        self.reason = reason
        self.project_ids = project_ids


INFRASTRUCTURE_ERRORS: dict[str, type[InfrastructureError]] = {
    cls.kind: cls
    for cls in (
        InfrastructureError,
        SSHError,
        RemoteCommandError,
        SlurmJobError,
        NestTimeoutError,
        SSHTimeoutError,
        SubmissionUnknownError,
        QueueTimeoutError,
        JobBlockedError,
        ExecutionTimeoutError,
        RpcTimeoutError,
        DatabaseUnavailableError,
        ResultTooLargeError,
    )
}
"""Maps each `kind` to its exception class."""
