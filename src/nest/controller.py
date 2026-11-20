import json
import re
import signal
import time
from concurrent import futures
from functools import wraps
from uuid import UUID

import cloudpickle
import grpc
import psycopg

from ._logging import (
    configure_logging,
    current_request_id,
    get_logger,
    register_secret,
    request_context,
)
from .audit import AuditLog, audit_interface
from .auth.web import start_auth_service
from .authz import Action, Principal
from .authz.execution import (
    AllowedProjects,
    ExecutionAccess,
    ProjectRequiredError,
    UnauthenticatedError,
)
from .authz.service import AccessDeniedError
from .backends.slurm import SlurmBackend, SlurmConfig, new_execution_id
from .config import (
    ConfigError,
    ControllerConfig,
    _is_loopback_address,
    load_controller_config,
    load_executor,
    materialize_ssh_credentials,
)
from .db import (
    Database,
    DatabaseError,
    JobStore,
    PlatformStore,
    bootstrap_state,
    sync_platforms,
    verify,
)
from .errors import (
    ERROR_KIND_METADATA,
    JOB_ID_METADATA,
    PENDING_REASON_METADATA,
    DatabaseUnavailableError,
    InfrastructureError,
    QueueTimeoutError,
    RemoteCommandError,
    SubmissionUnknownError,
)
from .executor import execute
from .mail import start_email_service
from .proto import execution_pb2, execution_pb2_grpc

logger = get_logger("nest.controller")


def _logged_rpc(method):
    @wraps(method)
    def wrapper(self, request, context):
        incoming_id = next(
            (value for key, value in context.invocation_metadata() if key == "x-nest-request-id"),
            None,
        )
        level = "debug" if method.__name__ == "GetJobStatus" else "info"
        with request_context(incoming_id), audit_interface("grpc"):
            started = time.monotonic()
            getattr(logger, level)("RPC started method=%s", method.__name__)
            try:
                response = method(self, request, context)
            except Exception:
                status = context.code()
                if status is not None and status != grpc.StatusCode.OK:
                    logger.warning(
                        "RPC aborted method=%s status=%s details=%s elapsed_ms=%.1f",
                        method.__name__,
                        status.name,
                        context.details(),
                        (time.monotonic() - started) * 1000,
                    )
                else:
                    logger.exception(
                        "RPC failed method=%s elapsed_ms=%.1f",
                        method.__name__,
                        (time.monotonic() - started) * 1000,
                    )
                raise
            getattr(logger, level)(
                "RPC completed method=%s elapsed_ms=%.1f",
                method.__name__,
                (time.monotonic() - started) * 1000,
            )
            return response

    return wrapper


MAX_STATUS_DETAILS_BYTES = 4096
"""
Largest status message the Controller sends, measured as gRPC encodes it.

gRPC sends the message in the `grpc-message` trailer and percent-encodes every
byte outside printable ASCII, so one such byte costs three. A client rejects
trailers above a hard limit of 16 KiB, and some trailers above a soft limit of
8 KiB, with `RESOURCE_EXHAUSTED`, which hides the intended status code.
"""

_HEAD_BYTES = 1024


def _encoded_size(text: str) -> int:
    """Returns the size of `text` once gRPC percent-encodes it as `grpc-message`."""
    return sum(1 if 0x20 <= byte <= 0x7E and byte != 0x25 else 3 for byte in text.encode())


def _prefix_within(text: str, budget: int) -> str:
    """Returns the longest prefix of `text` whose encoded size fits `budget`."""
    used = 0
    for index, char in enumerate(text):
        used += _encoded_size(char)
        if used > budget:
            return text[:index]
    return text


def _status_details(message: str) -> str:
    """
    Bounds a status message so the client receives the intended status code.

    Messages often carry remote stderr, which has no size limit. A longer
    message keeps its first kilobyte, which names the failure, and its end,
    where commands usually print the cause. The Controller log keeps the full
    message.

    Args:
        message (`str`): Status message to send.

    Returns:
        `str`: `message`, or a shortened copy whose encoded size is at most
            `MAX_STATUS_DETAILS_BYTES`.
    """
    if _encoded_size(message) <= MAX_STATUS_DETAILS_BYTES:
        return message
    marker = "\n[... {} characters omitted; see the Controller log ...]\n"
    # The omitted count is below len(message), so this reserves enough room for it.
    tail_budget = (
        MAX_STATUS_DETAILS_BYTES - _HEAD_BYTES - _encoded_size(marker.format(len(message)))
    )
    head = _prefix_within(message, _HEAD_BYTES)
    tail = _prefix_within(message[len(head) :][::-1], tail_budget)[::-1]
    omitted = len(message) - len(head) - len(tail)
    return head + marker.format(omitted) + tail


_METADATA_VALUE = re.compile(r"[\x21-\x7e]{1,200}")
"""Pending reasons sent as metadata: short and printable ASCII, as gRPC requires."""


def _abort_infrastructure(context, exc: InfrastructureError, status=None):
    """
    Aborts an RPC with an infrastructure error's status, kind, job ID, and pending reason.

    Args:
        context (`grpc.ServicerContext`): RPC context to abort.
        exc ([`errors.InfrastructureError`]): Error to report.
        status (`grpc.StatusCode`, *optional*): Overrides the error's own status.
    """
    metadata = [(ERROR_KIND_METADATA, exc.kind)]
    if exc.job_id is not None:
        metadata.append((JOB_ID_METADATA, str(exc.job_id)))
    reason = exc.pending_reason if isinstance(exc, QueueTimeoutError) else None
    if reason and _METADATA_VALUE.fullmatch(reason):
        metadata.append((PENDING_REASON_METADATA, reason))
    context.set_trailing_metadata(tuple(metadata))
    context.abort(status or exc.status, _status_details(str(exc)))


def _abort_denied(context, exc: AccessDeniedError, what: str):
    """
    Aborts a refused RPC without revealing what the caller may not see: a Project
    or job they are not a member of is answered as not found.
    """
    if not exc.visible:
        context.abort(grpc.StatusCode.NOT_FOUND, f"{what} not found")
    context.abort(grpc.StatusCode.PERMISSION_DENIED, f"Not allowed to {exc.action}: {exc.reason}")
    raise AssertionError("context.abort raises")


class ExecutionService(execution_pb2_grpc.ExecutionServiceServicer):
    """Handles execution requests and Slurm job operations for the controller."""

    def __init__(
        self,
        slurm_backend=None,
        executor: str | None = None,
        jobs: JobStore | None = None,
        access: ExecutionAccess | None = None,
    ):
        """
        Stores the optional Slurm backend and job store used by the RPC handlers.

        Args:
            slurm_backend ([`backends.slurm.SlurmBackend`], *optional*): Backend for
                Slurm execution and job operations. Local execution needs no backend.
            executor (`str`, *optional*): `"local"` or `"slurm"`. When omitted, each
                request reads and validates `NEST_EXECUTOR`.
            jobs ([`db.JobStore`], *optional*): Records Slurm submissions in
                PostgreSQL. Without it the Controller is stateless.
            access ([`authz.ExecutionAccess`], *optional*): Authenticates and
                authorizes every RPC (`NEST_CONTROLLER_AUTH=session`). Without it
                any caller that reaches the listener is served.
        """
        self.slurm_backend = slurm_backend
        self.executor = executor
        self.jobs = jobs
        self.access = access

    def _guarded(self, context, call):
        """Runs an access check, turning a database failure into `UNAVAILABLE`."""
        try:
            return call()
        except (DatabaseError, DatabaseUnavailableError) as exc:
            logger.error("Access check failed: the database is unavailable")
            error = (
                exc
                if isinstance(exc, DatabaseUnavailableError)
                else DatabaseUnavailableError(
                    "The Controller database is unavailable, so the request was not run. "
                    "Nothing was submitted; retry later."
                )
            )
            _abort_infrastructure(context, error)
            raise AssertionError("context.abort raises") from None

    def _authenticate(self, context, rpc: str) -> Principal | None:
        """
        Returns the caller, or `None` when authentication is off. Aborts with
        `UNAUTHENTICATED` when no accepted Nest session was presented.
        """
        if self.access is None:
            return None
        access = self.access
        try:
            return self._guarded(
                context, lambda: access.authenticate(context.invocation_metadata(), rpc)
            )
        except UnauthenticatedError:
            context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "This Controller needs a Nest session: run `nest login`, or set "
                "NEST_AUTH_URL so the SDK can sign in",
            )
            raise AssertionError("context.abort raises") from None

    def _authorize_submit(
        self, context, principal: Principal | None, execution_id: str, details: dict
    ) -> dict:
        """
        Checks `run.submit` for the Project in the call's metadata. Returns the job
        record's attribution fields, empty when authentication is off.
        """
        if self.access is None or principal is None:
            return {}
        access = self.access
        try:
            project_id = access.project_of(context.invocation_metadata())
        except ProjectRequiredError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise AssertionError("context.abort raises") from None
        try:
            self._guarded(
                context,
                lambda: access.authorize_submit(
                    principal, project_id, execution_id=execution_id, details=details
                ),
            )
        except AccessDeniedError as exc:
            _abort_denied(context, exc, "Project")
        return {"project_id": project_id, "submitted_by_user_id": principal.user_id}

    def _authorize_job(self, context, principal: Principal | None, action: Action, job_id: int):
        """Checks `run.read` or `run.cancel` on the recorded job `job_id`."""
        if self.access is None or principal is None:
            return
        access, jobs = self.access, self.jobs
        try:
            self._guarded(
                context,
                lambda: access.authorize_job(
                    principal, action, jobs.find(job_id) if jobs is not None else None, job_id
                ),
            )
        except AccessDeniedError as exc:
            _abort_denied(context, exc, "Job")

    def _begin_record(self, context, kind: str, execution_id: str, **fields) -> int | None:
        """
        Creates the job record before any side effect, aborting with `UNAVAILABLE`
        and kind `database_unavailable` when the database cannot store it.
        """
        if self.jobs is None:
            return None
        try:
            return self.jobs.begin(kind, execution_id, request_id=current_request_id(), **fields)
        except DatabaseUnavailableError as exc:
            # Log the full message, since the status message the client gets may be shortened.
            logger.exception("Job record could not be stored kind=%s", kind)
            _abort_infrastructure(context, exc)
            raise AssertionError("context.abort raises") from None

    def _fail_record(self, record: int | None, exc: Exception) -> None:
        if self.jobs is None or record is None:
            return
        infrastructure = isinstance(exc, InfrastructureError)
        self.jobs.finish(
            record,
            "unknown" if isinstance(exc, SubmissionUnknownError) else "failed",
            slurm_job_id=exc.job_id if infrastructure else None,
            error_kind=exc.kind if infrastructure else "internal",
            error_message=str(exc),
        )

    def _require_slurm_backend(self, context) -> SlurmBackend:
        """
        Returns the Slurm backend, aborting the RPC when none is configured.

        Args:
            context (`grpc.ServicerContext`): RPC context, aborted with
                `FAILED_PRECONDITION` when no backend is configured.

        Returns:
            [`backends.slurm.SlurmBackend`]: The configured backend.
        """
        if self.slurm_backend is None:
            context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                "Slurm backend is not configured",
            )
            raise AssertionError("context.abort raises")
        return self.slurm_backend

    @_logged_rpc
    def Execute(self, request, context):
        """
        Executes a serialized call locally or through the configured Slurm backend.

        Selects Slurm only when `NEST_EXECUTOR` is `slurm`; all other values use a
        local subprocess. Slurm execution prepares any supplied environment before
        submitting the worker once and waits for its result (600 seconds by default).
        The request's `gpu` and `cpu` counts are forwarded to both the environment
        build and the worker job; a count of `0` omits the corresponding `sbatch` flag.

        Args:
            request (`execution_pb2.ExecuteRequest`): Serialized call, resource counts,
                and optional environment files.
            context (`grpc.ServicerContext`): RPC context used to abort with
                `INVALID_ARGUMENT` for empty environment files or negative resource
                counts, `FAILED_PRECONDITION` for an absent Slurm backend, and the
                status of each [`errors.InfrastructureError`] with its kind in the
                `nest-error-kind` trailing metadata.

        Returns:
            `execution_pb2.ExecuteResponse`: Serialized return value or worker error record.

        With session authentication, the call needs a Nest CLI session
        (`UNAUTHENTICATED` otherwise) and the `run.submit` permission in the Project
        named by `x-nest-project-id` (`INVALID_ARGUMENT` when absent, `NOT_FOUND`
        when the caller is not a member, `PERMISSION_DENIED` when the role or the
        Provider refuses it). See [`authz.ExecutionAccess`].
        """
        principal = self._authenticate(context, "Execute")
        if request.HasField("environment"):
            env = request.environment

            if not env.pyproject_toml or not env.uv_lock:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "Environment requires pyproject.toml and uv.lock",
                )

            logger.debug(
                "Environment received pyproject_bytes=%s lock_bytes=%s",
                len(env.pyproject_toml),
                len(env.uv_lock),
            )

        if request.gpu < 0 or request.cpu < 0:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Resource counts cannot be negative",
            )

        mode = self.executor or load_executor()
        logger.info(
            "Execution received executor=%s gpu=%s cpu=%s payload_bytes=%s",
            mode,
            request.gpu,
            request.cpu,
            len(request.payload),
        )
        # The Execution ID is fixed, authorized and recorded before any side
        # effect; the backend then uses it for the remote directory and worker.
        execution_id = new_execution_id()
        attribution = self._authorize_submit(
            context,
            principal,
            execution_id,
            {
                "rpc": "Execute",
                "executor": mode,
                "gpu": request.gpu,
                "cpu": request.cpu,
                "environment": request.HasField("environment"),
            },
        )

        if mode == "slurm":
            backend = self._require_slurm_backend(context)
            job_dir = None
            if self.jobs is not None:
                try:
                    job_dir = backend.execution_dir(execution_id)
                except ValueError:
                    pass  # submit_payload reports it below, before any SSH command
            record = self._begin_record(
                context,
                "execution",
                execution_id,
                job_dir=job_dir,
                gpu=request.gpu,
                cpu=request.cpu,
                **attribution,
            )

            try:
                environment_id = None

                # Prepare Python environment
                if request.HasField("environment"):
                    env = request.environment
                    logger.info("Preparing Python environment")

                    env_dir = backend.prepare_environment_bytes(
                        pyproject_toml=env.pyproject_toml,
                        uv_lock=env.uv_lock,
                    )

                    environment_id = env_dir.rsplit("/", 1)[-1]

                    logger.info("Environment ready environment_id=%s", environment_id)

                # Submit worker with environment ID
                job_id, job_dir = backend.submit_payload(
                    request.payload,
                    gpu=request.gpu,
                    cpu=request.cpu,
                    environment_id=environment_id,
                    execution_id=execution_id,
                )

                logger.info("Worker submitted job_id=%s job_dir=%s", job_id, job_dir)
                if self.jobs is not None and record is not None:
                    self.jobs.submitted(record, job_id, job_dir)

                # Wait for the result
                result_bytes = backend.wait_result(job_id, job_dir)
                if self.jobs is not None and record is not None:
                    self.jobs.finish(record, "succeeded")

                return execution_pb2.ExecuteResponse(result=result_bytes)

            except InfrastructureError as exc:
                logger.exception("Slurm execution failed kind=%s", exc.kind)
                self._fail_record(record, exc)
                _abort_infrastructure(context, exc)
            except (RuntimeError, ValueError) as exc:
                logger.exception("Slurm execution failed")
                self._fail_record(record, exc)
                context.abort(
                    grpc.StatusCode.INTERNAL,
                    _status_details(str(exc)),
                )

        # Original local executor
        func, args, kwargs = cloudpickle.loads(request.payload)
        result = execute(func, *args, **kwargs)
        return execution_pb2.ExecuteResponse(result=cloudpickle.dumps(result))

    @_logged_rpc
    def GetJobStatus(self, request, context):
        """
        Returns the current or recorded Slurm state of a job.

        Args:
            request (`execution_pb2.JobStatusRequest`): Job operation parameters.
            context (`grpc.ServicerContext`): RPC context for reporting failures.
                Uses `FAILED_PRECONDITION` when the backend is absent, the status and
                kind of an [`errors.InfrastructureError`], and `INTERNAL` otherwise.

        Returns:
            `execution_pb2.JobStatusResponse`: The job operation response.

        With session authentication, the job must be recorded in a Project where
        the caller may `run.read`; any other job is `NOT_FOUND`.
        """
        principal = self._authenticate(context, "GetJobStatus")
        backend = self._require_slurm_backend(context)
        self._authorize_job(context, principal, Action.RUN_READ, request.job_id)

        try:
            state = backend.status(request.job_id)
        except InfrastructureError as exc:
            logger.exception("Job status query failed job_id=%s", request.job_id)
            _abort_infrastructure(context, exc)
        except Exception as exc:
            logger.exception("Job status query failed job_id=%s", request.job_id)
            context.abort(
                grpc.StatusCode.INTERNAL,
                _status_details(str(exc)),
            )

        logger.debug("Job status job_id=%s state=%s", request.job_id, state)
        if self.jobs is not None:
            self.jobs.observe(request.job_id, state)
        return execution_pb2.JobStatusResponse(state=state)

    @_logged_rpc
    def SubmitJob(self, request, context):
        """
        Submits a batch script through the Slurm backend.

        Each element of the request's `script_args` reaches the script verbatim as one
        argument after the script path. A `cpu` count of `0` omits `--cpus-per-task`, as
        `gpu` does for `--gpus-per-node`.

        Args:
            request (`execution_pb2.SubmitJobRequest`): Job operation parameters.
            context (`grpc.ServicerContext`): RPC context for reporting failures.
                Uses `INVALID_ARGUMENT` for a negative GPU or CPU count or a script
                argument containing a NUL byte, `FAILED_PRECONDITION` for an absent
                backend or a rejected submission, and the status and kind of other
                [`errors.InfrastructureError`] failures, such as `UNKNOWN` with kind
                `submission_unknown` when SSH timed out during `sbatch`. Never resubmits.

        Returns:
            `execution_pb2.SubmitJobResponse`: The job operation response.

        With session authentication, the call is authorized as `Execute` is.
        """
        principal = self._authenticate(context, "SubmitJob")
        backend = self._require_slurm_backend(context)

        if request.gpu < 0 or request.cpu < 0:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Resource counts cannot be negative",
            )
        if any("\0" in arg for arg in request.script_args):
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Script arguments cannot contain NUL bytes",
            )

        execution_id = new_execution_id()
        attribution = self._authorize_submit(
            context,
            principal,
            execution_id,
            {
                "rpc": "SubmitJob",
                "gpu": request.gpu,
                "cpu": request.cpu,
                "partition": request.partition or None,
            },
        )
        record = self._begin_record(
            context,
            "batch",
            execution_id,
            script=request.script,
            workdir=request.workdir,
            account=request.account or None,
            partition=request.partition or None,
            time_limit=request.time or None,
            gpu=request.gpu,
            cpu=request.cpu,
            **attribution,
        )
        try:
            job_id = backend.submit(
                workdir=request.workdir,
                script=request.script,
                gpu=request.gpu,
                cpu=request.cpu,
                account=request.account or None,
                partition=request.partition or None,
                time=request.time or None,
                script_args=tuple(request.script_args),
            )
        except RemoteCommandError as exc:
            logger.exception("Batch job submission failed script=%s", request.script)
            self._fail_record(record, exc)
            _abort_infrastructure(context, exc, grpc.StatusCode.FAILED_PRECONDITION)
        except InfrastructureError as exc:
            logger.exception(
                "Batch job submission failed script=%s kind=%s", request.script, exc.kind
            )
            self._fail_record(record, exc)
            _abort_infrastructure(context, exc)

        logger.info("Batch job submitted job_id=%s", job_id)
        if self.jobs is not None and record is not None:
            self.jobs.submitted(record, job_id)
        return execution_pb2.SubmitJobResponse(job_id=job_id)

    @_logged_rpc
    def CancelJob(self, request, context):
        """
        Requests cancellation of a Slurm job.

        Args:
            request (`execution_pb2.CancelJobRequest`): Job operation parameters.
            context (`grpc.ServicerContext`): RPC context for reporting failures.
                Uses `INVALID_ARGUMENT` for a nonpositive job ID,
                `FAILED_PRECONDITION` for an absent backend or a rejected cancellation,
                and the status and kind of other [`errors.InfrastructureError`] failures.

        Returns:
            `execution_pb2.CancelJobResponse`: The job operation response.

        With session authentication, only the job's submitter may cancel it, with
        `run.cancel` in its recorded Project; a job the caller cannot see is
        `NOT_FOUND`.
        """
        principal = self._authenticate(context, "CancelJob")
        backend = self._require_slurm_backend(context)

        if request.job_id <= 0:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "Invalid job ID",
            )
        self._authorize_job(context, principal, Action.RUN_CANCEL, request.job_id)

        try:
            backend.cancel(request.job_id)
        except RemoteCommandError as exc:
            logger.exception("Job cancellation failed job_id=%s", request.job_id)
            _abort_infrastructure(context, exc, grpc.StatusCode.FAILED_PRECONDITION)
        except InfrastructureError as exc:
            logger.exception("Job cancellation failed job_id=%s kind=%s", request.job_id, exc.kind)
            _abort_infrastructure(context, exc)

        logger.info("Job cancellation requested job_id=%s", request.job_id)
        return execution_pb2.CancelJobResponse()


def build_backend(config: ControllerConfig) -> SlurmBackend | None:
    """
    Creates the Slurm backend for the default Provider Profile.

    Inline Mode B credentials are written to a private temporary directory, and
    the private key is registered so Nest's log handler masks it.

    Args:
        config ([`config.ControllerConfig`]): Validated Controller configuration.

    Returns:
        [`backends.slurm.SlurmBackend`] or `None`: `None` when no Platform is set.
    """
    profile = config.platform
    if profile is None:
        return None
    identity = known_hosts = None
    if profile.ssh_mode == "B":
        if config.ssh.private_key is not None:
            register_secret(config.ssh.private_key.reveal())
        identity, known_hosts = materialize_ssh_credentials(config.ssh)
    node = profile.login_nodes[0]
    workdir = profile.workdir
    return SlurmBackend(
        SlurmConfig(
            ssh_host=node.host,
            ssh_port=node.port,
            ssh_user=profile.ssh_user,
            ssh_mode=profile.ssh_mode,
            identity_file=identity,
            known_hosts_file=known_hosts,
            allowed_hosts=tuple(n.host for n in profile.login_nodes),
            account=profile.account,
            partition=profile.partition,
            time=profile.time,
            work_root=f"{workdir.rstrip('/')}/jobs" if workdir else None,
            worker_workdir=workdir,
            ssh_timeout=config.ssh_timeout,
            result_timeout=config.result_timeout,
            environment_timeout=config.environment_timeout,
            poll_interval=config.poll_interval,
            blocked_timeout=config.pending_blocked_timeout,
            limit_timeout=config.pending_limit_timeout,
            cancel_blocked=config.cancel_blocked_jobs,
        )
    )


def _log_bootstrap(database: Database, config: ControllerConfig) -> None:
    """Says whether NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL can still take effect."""
    if config.bootstrap_admin_email is None:
        return
    state = bootstrap_state(database)
    if state is None:
        logger.info(
            "Platform bootstrap pending: the configured Google account becomes "
            "platform_admin at its first verified sign-in"
        )
    else:
        logger.info(
            "Platform bootstrap already completed method=%s; "
            "NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL has no effect and can be unset",
            state.method,
        )


def open_database(config: ControllerConfig) -> Database | None:
    """
    Connects to the configured database and checks it before the Controller serves.

    Verifies connectivity, the schema version, and every applied migration's
    checksum, then records the environment's HPC Platform Profiles and mirrors them
    in the HPC Platform Catalog (see [`db.PlatformStore.sync_environment`]). Never applies
    migrations; run `nest-db upgrade` first.

    Args:
        config ([`config.ControllerConfig`]): Validated Controller configuration.

    Returns:
        [`db.Database`] or `None`: The open database, or `None` when
            `NEST_DATABASE_URL` is unset (stateless Controller).

    Raises:
        [`db.DatabaseError`]: If the database is unreachable or not current.
    """
    if config.database is None:
        return None
    database = Database(config.database)
    try:
        database.open()
        with database.connection() as conn:
            verify(conn)
        sync_platforms(database, config.platforms, config.default_platform)
        PlatformStore(database).sync_environment(config.platforms)
        _log_bootstrap(database, config)
    except psycopg.Error as exc:
        database.close()
        raise DatabaseError(
            f"Cannot check PostgreSQL at {database.describe()}: {database.scrub(str(exc))}"
        ) from None
    except BaseException:
        database.close()
        raise
    return database


def environment_platform_id(database: Database, provider_id: str) -> UUID | None:
    """
    The immutable ID of the HPC Platform a Provider Profile is mirrored as, or
    `None` when an operator-managed Platform already uses that slug.
    """
    platform = PlatformStore(database).get_platform(provider_id)
    return platform.id if platform is not None and platform.source == "env" else None


def build_access(config: ControllerConfig, database: Database | None) -> ExecutionAccess | None:
    """
    Creates the execution service's access check for `NEST_CONTROLLER_AUTH=session`.

    Returns:
        [`authz.ExecutionAccess`] or `None`: `None` when authentication is off.
    """
    if config.auth != "session":
        if config.tls is None and not _is_loopback_address(config.bind):
            logger.warning(
                "gRPC authentication is off (NEST_CONTROLLER_AUTH=none): every caller that "
                "can reach %s may run jobs; restrict network access to trusted users",
                config.bind,
            )
        return None
    assert database is not None, "load_controller_config requires a database for session auth"
    if config.google is None:
        logger.warning(
            "NEST_CONTROLLER_AUTH=session without Google sign-in: no one can obtain a Nest "
            "session, so every RPC is refused"
        )
    if not config.allowed_projects:
        logger.warning(
            "NEST_PROVIDER_ALLOWED_PROJECTS is empty: the Provider accepts Runs from no "
            "Project, so every submission is refused"
        )
    return ExecutionAccess(
        database,
        AllowedProjects(config.allowed_projects),
        provider_id=config.default_platform or config.executor,
    )


def record_start(config: ControllerConfig, database: Database | None) -> None:
    """
    Records the Controller's security-relevant configuration in the audit log:
    whether each protection is on and where credentials come from, never a value.
    """
    AuditLog(database).record(
        "controller.started",
        "succeeded",
        interface="controller",
        details={
            "auth": config.auth,
            "tls": config.tls is not None,
            "allow_plaintext": config.allow_plaintext,
            "allowed_projects": (
                "*" if "*" in config.allowed_projects else len(config.allowed_projects)
            ),
            "executor": config.executor,
            "default_platform": config.default_platform,
            # Kept for audit consumers written before HPC Platforms; same value.
            "default_provider": config.default_platform,
            "ssh_modes_allowed": ",".join(config.ssh.allowed_modes),
            "ssh_identity_inline": config.ssh.private_key is not None,
            "database_sslmode": (config.database.effective_sslmode if config.database else None),
            "email_enabled": config.email is not None,
            "google_sign_in": config.google is not None,
        },
    )


def _add_port(server, config: ControllerConfig) -> int:
    if config.tls is None:
        return server.add_insecure_port(config.bind)
    with open(config.tls.key_file, "rb") as key, open(config.tls.cert_file, "rb") as cert:
        credentials = grpc.ssl_server_credentials([(key.read(), cert.read())])
    return server.add_secure_port(config.bind, credentials)


def main():
    """
    Validates the configuration, starts the controller, and waits until it stops.

    Reads every setting through [`config.load_controller_config`]; see
    *Environment variables and defaults* for the contract. Invalid settings are
    all reported at once, before the listener starts, and the process exits with
    status 2. When `NEST_DATABASE_URL` is set, the database must be reachable and
    its schema current (see [`controller.open_database`]); otherwise the process
    exits with status 3 instead of running without persistence. Email delivery
    starts only with `NEST_EMAIL_ENABLED=true` and never contacts the SMTP server
    at startup (see [`mail.start_email_service`]). Google sign-in is served on
    `NEST_AUTH_HTTP_BIND` only when `NEST_GOOGLE_CLIENT_ID` is set, and never
    contacts Google at startup (see [`auth.start_auth_service`]). Uses four RPC worker threads with port reuse disabled, and TLS when
    `NEST_TLS_CERT_FILE` and `NEST_TLS_KEY_FILE` are set. Handles SIGTERM and SIGINT
    by allowing active RPCs up to ten seconds to finish.

    Raises:
        `SystemExit`: If the configuration is invalid.
    """
    configure_logging()
    try:
        config = load_controller_config()
    except ConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from None
    for message in config.warnings:
        logger.warning("Configuration warning: %s", message)

    try:
        database = open_database(config)
    except DatabaseError as exc:
        logger.error("%s", exc)
        raise SystemExit(3) from None
    email = start_email_service(config.email, database)
    jobs = (
        JobStore(
            database,
            config.platform.id,
            platform_id=environment_platform_id(database, config.platform.id),
        )
        if database is not None and config.platform is not None
        else None
    )

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[("grpc.so_reuseport", 0)],
    )

    slurm_backend = build_backend(config)
    access = build_access(config, database)

    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(
        ExecutionService(slurm_backend, executor=config.executor, jobs=jobs, access=access),
        server,
    )

    def stop_server(signum, frame):
        """Stops accepting RPCs and gives active calls ten seconds to finish."""
        logger.info("Controller stopping signal=%s", signum)
        server.stop(grace=10)

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)

    auth = None
    try:
        logger.info("Controller configuration %s", json.dumps(config.summary(), sort_keys=True))
        if not _add_port(server, config):
            raise RuntimeError(f"Cannot bind Controller to {config.bind}")
        server.start()
        record_start(config, database)
        auth = start_auth_service(
            config.google,
            database,
            bootstrap_admin_email=config.bootstrap_admin_email,
            email=email,
        )
        logger.info(
            "Controller listening address=%s executor=%s slurm_configured=%s tls=%s auth=%s "
            "database=%s email=%s google_sign_in=%s",
            config.bind,
            config.executor,
            slurm_backend is not None,
            config.tls is not None,
            config.auth,
            database.describe() if database else "none",
            "enabled" if email.enabled else "disabled",
            auth.address or "disabled",
        )
        server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Controller interrupted")
    except BaseException:
        logger.exception("Controller stopped unexpectedly")
        raise
    finally:
        server.stop(grace=0)
        if auth is not None:
            auth.stop()
        if email.worker is not None:
            email.worker.stop(timeout=10)
        if database is not None:
            database.close()
        logger.info("Controller stopped")


if __name__ == "__main__":
    main()
