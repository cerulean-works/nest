from typing import ClassVar

import grpc

ERROR_KIND_METADATA = "nest-error-kind"
"""Trailing-metadata key carrying an [`errors.InfrastructureError`] kind."""

JOB_ID_METADATA = "nest-job-id"
"""Trailing-metadata key carrying the Slurm job ID related to an error, if any."""


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
    """The job was submitted but never started running before the polling deadline."""

    kind = "queue_timeout"


class ExecutionTimeoutError(NestTimeoutError):
    """
    The job started but did not finish before the polling deadline, or Slurm ended
    it with the `TIMEOUT` state after it exceeded its time limit.
    """

    kind = "execution_timeout"


class RpcTimeoutError(NestTimeoutError):
    """The client's `Execute` RPC deadline expired before the Controller replied."""

    kind = "rpc_timeout"


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
        ExecutionTimeoutError,
        RpcTimeoutError,
    )
}
"""Maps each `kind` to its exception class."""
