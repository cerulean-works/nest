"""
Registry-backed admission and lifecycle of Controller submissions (C01).

[`admission.RegistryAdmission`] is the one path every Slurm submission of a
registry-backed Controller (`NEST_EXECUTION_REGISTRY=required`) takes, for both
`Execute` and `SubmitJob`. Before anything irreversible happens on the cluster it:

1. selects the Platform with [`authz.PlatformCatalog.select_platform`] for the
   authenticated caller and the immutable Project (an ineligible Platform is
   refused, never replaced), checks that this Controller serves it, and resolves
   the execution authority (D01) with [`authz.AuthorityResolver`] from the
   deployment, Platform and Project policies and, for `user_owned`, the caller's
   verified HPC binding (D02), before any login node is selected;
2. lets the server choose an approved login node for the Platform's transport mode
   ([`authz.PlatformCatalog.select_login_node`]);
3. takes the HPC identity from the server's own records (an
   [`admission.HPCIdentitySource`]), never from the client;
4. resolves the Slurm account with D08's [`authz.AccountResolver`];
5. registers the execution and durably records its `submitting` attempt, with every
   resolved option and its provenance, the Platform, the login-node snapshot, the
   mode, the HPC identity, the account and its origin, and the source and
   environment digests ([`db.ExecutionRegistry.register`] and
   [`db.ExecutionRegistry.begin_attempt`], each committed before it returns).

Only then does the caller run `sbatch`. A refusal or a database failure at any of
these steps leaves nothing submitted, and the Controller never falls back to the
legacy path. Afterwards the attempt follows what happened: `submitted` with the
Slurm job ID, `failed` when Slurm or SSH refused it, `unknown` when `sbatch` may
have run but no job ID arrived (it is never resubmitted; see
[`db.ExecutionRegistry.reconcile`]), and `running`, `succeeded`, `failed` or
`cancelled` as Slurm reports it.

A remote call whose function raised still completes in Slurm (`COMPLETED`): the
worker returns the remote error record as its result. Its attempt is recorded as
`failed` with `slurm_state` `COMPLETED` and error kind `remote_application_error`
(see [`admission.completion`]), decided from the worker's plain-JSON outcome
record ([`outcome`]), never by unpickling the result; the execution stays open
for a retry. Before staging, the worker protocol the deployed script declares is
pinned on the attempt ([`admission.RegistryAdmission.pin_worker_protocol`]): a
protocol 2 attempt has its own artifact directory, so a retry never touches an
earlier attempt's result or record, and its completion is never a success
without a record. Every path that settles a `COMPLETED` job (`Execute`,
[`admission.RegistryAdmission.observe`] for `GetJobStatus`, and
[`admission.RegistryAdmission.settle`]) decides the same way, from the
attempt's own directory, so none of them records an application failure as a
success.

Execution authority (D01): a `user_owned` (Mode B, User SSH Proxy) submission
that the policies permit and whose caller has a verified binding is still refused
(`user_owned_transport_unavailable`): this Controller has no user-owned SSH
session transport yet (D03/D05). It never runs such a job with its own
credentials, and never falls back to `controller_owned`. Every admitted attempt
pins its authority and the policy versions it was admitted under.

Legacy transport Mode B (`NEST_SLURM_SSH_MODE=B`, the Controller's managed-key
SSH) keeps its meaning and is not reinterpreted as a User's session: the default
[`admission.ControllerIdentitySource`] still refuses it
(`hpc_binding_unavailable`), and even a verified identity is refused at the
transport step (`mode_b_transport_unavailable`), until D09 routes it.

Admission limits (F02a): with [`config.AdmissionLimits`], step 5 also rechecks the
submitter's session and membership and reserves the attempt's CPUs, GPUs and
walltime against per-User, per-Project and per-Platform bounds, atomically with
the attempt (see [`db.ExecutionRegistry.begin_attempt`]). The reservation is held
while the attempt is live and released only when it reaches a final state:
`succeeded`, `failed` (including a failure known to precede `sbatch`) or
`cancelled`. A timeout, a lost RPC or a restart never releases it, and neither does
`unknown`. [`admission.RegistryAdmission.settle`] polls Slurm for live attempts
nobody is waiting on, so a finished job's reservation is released even when no
client asks for its status.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Collection, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

import grpc

from ._logging import get_logger
from .audit import AuditLog
from .authz.accounts import AccountResolution, AccountResolutionError, AccountResolver, HPCIdentity
from .authz.hpc_access import AuthorityRefusedError, AuthorityResolver
from .authz.platforms import PlatformCatalog, PlatformResolutionError, PlatformSelection
from .authz.policy import Principal
from .backends.slurm import (
    ENVIRONMENT_BUILD_CPUS,
    ENVIRONMENT_BUILD_GPUS,
    ENVIRONMENT_BUILD_TIME,
    SlurmBackend,
    worker_allocation,
)
from .config import AdmissionLimits, PlatformProfile, slurm_time_seconds
from .db.connection import Database, DatabaseError
from .db.executions import (
    FINAL_ATTEMPT_STATUSES,
    AccountNotPermittedError,
    AdmissionLimitError,
    Attempt,
    AttemptInFlightError,
    AuthorityNotPermittedError,
    BindingNotVerifiedError,
    Execution,
    ExecutionClosedError,
    ExecutionRegistry,
    ExecutionRegistryError,
    HPCPrincipal,
    IdempotencyConflictError,
    PlatformUnavailableError,
    ProviderNotApprovedError,
    Reservation,
    ResolvedOption,
    Submitter,
    SubmitterNotAuthorizedError,
    check_request_limits,
)
from .db.platforms import (
    EndpointNotApprovedError,
    LoginEndpoint,
    NoLoginNodeError,
    Platform,
    PlatformNotFoundError,
)
from .errors import (
    DatabaseUnavailableError,
    InfrastructureError,
    ResultTooLargeError,
    SlurmJobError,
    SubmissionUnknownError,
    WorkerProtocolError,
)
from .outcome import (
    WORKER_PROTOCOL,
    InvalidOutcomeError,
    OutcomeUnreadableError,
    RemoteOutcome,
)

logger = get_logger("nest.admission")

__all__ = [
    "Admission",
    "AdmissionRefusedError",
    "BuildSlot",
    "ControllerIdentitySource",
    "DuplicateSubmissionError",
    "HPCIdentitySource",
    "RecoverySweeper",
    "RegistryAdmission",
    "SubmissionRequest",
    "TrustedHPCIdentity",
    "completion",
    "digest",
    "missing_outcome",
]

REMOTE_APPLICATION_ERROR = "remote_application_error"
"""Error kind of an attempt whose job completed but whose function raised."""

REMOTE_OUTCOME_INVALID = "remote_outcome_invalid"
"""Error kind of an attempt whose job completed with a malformed outcome record."""

REMOTE_OUTCOME_UNREADABLE = "remote_outcome_unreadable"
"""Error kind of an attempt whose outcome record is not a readable regular file."""

REMOTE_OUTCOME_MISSING = "remote_outcome_missing"
"""Error kind of a protocol 2 attempt whose job completed without an outcome record."""

REMOTE_OUTCOME_PENDING = "remote_outcome_pending"
"""
Error kind of a live attempt whose job completed but whose outcome record was not
there yet; the settle sweep decides once it has stayed so for a full threshold.
"""


_PENDING = (
    "The job completed, but its outcome record is not readable yet; the Controller "
    "reads it again before settling the attempt"
)


def completion(
    remote: RemoteOutcome | InvalidOutcomeError | None,
    *,
    worker_protocol: int | None,
) -> tuple[str, str | None, str | None] | None:
    """
    How an `execution` job Slurm reports `COMPLETED` settles, from the worker's
    outcome record and the worker protocol pinned on its attempt.

    Args:
        remote: The parsed record; `None` when there is none; or the
            [`outcome.InvalidOutcomeError`] (or [`outcome.OutcomeUnreadableError`])
            raised reading it.
        worker_protocol: The attempt's pinned protocol. `None` for an attempt
            recorded before protocols were pinned.

    Returns:
        `(status, error_kind, error_message)`, or `None` when it cannot be decided
            yet. `succeeded` only when the call returned, or, for a legacy worker
            (protocol 1 or unpinned), when it wrote no record: an unverified
            success, identified by the attempt's `worker_protocol`. `failed` with
            `remote_application_error` when it raised, `remote_outcome_invalid`
            for a malformed record, `remote_outcome_unreadable` for a path that is
            not a readable regular file. A protocol 2 attempt without a record
            returns `None`: never a success (see
            [`admission.RegistryAdmission.settle`]). The message names only the
            exception type: the exception message and traceback are the caller's
            and stay in the result.
    """
    if isinstance(remote, OutcomeUnreadableError):
        return (
            "failed",
            REMOTE_OUTCOME_UNREADABLE,
            "The job completed, but its outcome record is not a readable regular file",
        )
    if isinstance(remote, InvalidOutcomeError):
        return (
            "failed",
            REMOTE_OUTCOME_INVALID,
            (
                f"The job completed, but its outcome record is invalid ({remote}); the "
                "result was returned to the caller unchanged"
            ),
        )
    if remote is not None and remote.raised:
        return (
            "failed",
            REMOTE_APPLICATION_ERROR,
            (
                f"The remote function raised {remote.error_type}; the job completed and "
                "the traceback was returned to the caller"
            ),
        )
    if remote is None and worker_protocol is not None and worker_protocol >= WORKER_PROTOCOL:
        return None
    return "succeeded", None, None


def missing_outcome() -> tuple[str, str, str]:
    """How a protocol 2 attempt whose record stayed missing for a full threshold settles."""
    return (
        "failed",
        REMOTE_OUTCOME_MISSING,
        "The job completed, but its worker wrote no outcome record; it is not a success",
    )


_ACCOUNT_DEFAULT_SOURCES = {
    "project_default": "project",
    "mode_a_operator_default": "platform",
    "mode_b_user_default": "hpc_identity",
}
_PLATFORM_DEFAULT_SOURCES = {
    "project_default": "project",
    "server_default": "server",
    "only_eligible": "catalog",
}
# Slurm states that settle an attempt observed by job status queries.
_RUNNING_STATES = ("RUNNING", "COMPLETING", "CONFIGURING", "STAGE_OUT", "SUSPENDED")
_FAILED_STATES = (
    "FAILED",
    "BOOT_FAIL",
    "DEADLINE",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "PREEMPTED",
    "TIMEOUT",
)


def digest(*parts: bytes) -> str:
    """A `sha256:<hex>` digest of `parts`, each length-prefixed."""
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(len(part).to_bytes(8, "big"))
        hasher.update(part)
    return "sha256:" + hasher.hexdigest()


class AdmissionRefusedError(Exception):
    """
    A submission was refused before anything was submitted.

    Attributes:
        reason (`str`): Machine-readable reason, such as `platform_not_served`,
            `hpc_binding_unavailable` or `not_permitted`.
        code (`grpc.StatusCode`): Status the RPC is answered with.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        code: grpc.StatusCode = grpc.StatusCode.FAILED_PRECONDITION,
        details: Mapping[str, Any] | None = None,
    ):
        self.reason = reason
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


_PER_REQUEST_LIMITS = frozenset(
    {"cpu_per_request", "gpu_per_request", "walltime", "walltime_unbounded"}
)


def _limit_refusal(exc: AdmissionLimitError) -> AdmissionRefusedError:
    """A refusal for an admission limit: per-request bounds are the caller's to fix."""
    return AdmissionRefusedError(
        f"limit_{exc.reason}",
        str(exc),
        (
            grpc.StatusCode.FAILED_PRECONDITION
            if exc.reason in _PER_REQUEST_LIMITS
            else grpc.StatusCode.RESOURCE_EXHAUSTED
        ),
        {"limit": exc.limit, "current": exc.current, "requested": exc.requested},
    )


class DuplicateSubmissionError(Exception):
    """
    An idempotent repeat of a request Slurm already accepted. Nothing was submitted
    again.

    Attributes:
        execution ([`db.executions.Execution`]): The existing execution.
        attempt ([`db.executions.Attempt`]): Its latest attempt, which has a Slurm
            job ID.
    """

    def __init__(self, execution: Execution, attempt: Attempt):
        self.execution = execution
        self.attempt = attempt
        super().__init__(
            f"Execution {execution.execution_id} was already submitted as Slurm job "
            f"{attempt.slurm_job_id}; it is not submitted again"
        )


@dataclass(frozen=True)
class SubmissionRequest:
    """
    What a caller asked for, exactly as sent. Nothing here is trusted.

    Attributes:
        kind (`str`): `execution` (`Execute`) or `batch` (`SubmitJob`).
        gpu, cpu (`int`): Requested counts; `0` omits the flag.
        platform (`str | None`): Platform asked for (`x-nest-platform`).
        account, partition, time (`str | None`): Slurm options asked for.
        workdir, script (`str | None`): The batch script (`SubmitJob`).
        script_args (`tuple[str, ...]`): Its arguments.
        source_digest (`str | None`): Digest of the serialized call (`Execute`).
        environment_digest (`str | None`): Digest of the environment files.
        idempotency_key (`str | None`): `x-nest-idempotency-key`.
        authority (`str | None`): Execution authority asked for
            (`x-nest-execution-authority`); only a request, never a grant.
    """

    kind: str
    gpu: int = 0
    cpu: int = 0
    platform: str | None = None
    account: str | None = None
    partition: str | None = None
    time: str | None = None
    workdir: str | None = None
    script: str | None = None
    script_args: tuple[str, ...] = ()
    source_digest: str | None = None
    environment_digest: str | None = None
    idempotency_key: str | None = None
    authority: str | None = None

    def requested_options(self) -> dict[str, Any]:
        """The options as requested, before any default; part of the request digest."""
        options: dict[str, Any] = {"gpu": self.gpu, "cpu": self.cpu}
        for name in ("platform", "account", "partition", "time", "workdir", "script"):
            value = getattr(self, name)
            if value is not None:
                options[name] = value
        if self.kind == "batch":
            options["script_args"] = list(self.script_args)
        if self.source_digest is not None:
            options["source_digest"] = self.source_digest
        if self.environment_digest is not None:
            options["environment_digest"] = self.environment_digest
        if self.authority is not None:
            options["execution_authority"] = self.authority
        return options


@dataclass(frozen=True)
class TrustedHPCIdentity:
    """
    The HPC identity a job runs as, built by the server from its own records.

    Attributes:
        identity ([`authz.HPCIdentity`]): What account selection needs.
        principal ([`db.HPCPrincipal`]): What the attempt records.
    """

    identity: HPCIdentity
    principal: HPCPrincipal


class HPCIdentitySource(Protocol):
    """
    Supplies the server-side HPC identity of a submission. Never consults client
    input: a client can neither assert a binding nor an entitlement nor a UNIX user.
    """

    def resolve(self, principal: Principal, platform: Platform, mode: str) -> TrustedHPCIdentity:
        """
        Raises:
            [`admission.AdmissionRefusedError`]: If no trusted identity exists.
        """
        ...


@dataclass(frozen=True)
class ControllerIdentitySource:
    """
    The identities this Controller can vouch for: in Mode A, the operator's own UNIX
    user from the Platform Profile (`SSH_USER`); in Mode B, none, since no verified
    per-User HPC binding exists yet, so Mode B is refused rather than run as the
    Controller.

    Args:
        profile ([`config.PlatformProfile`]): The served Platform Profile.
    """

    profile: PlatformProfile

    def resolve(self, principal: Principal, platform: Platform, mode: str) -> TrustedHPCIdentity:
        if mode == "B":
            raise AdmissionRefusedError(
                "hpc_binding_unavailable",
                "Mode B needs a verified HPC identity binding for your Nest account on "
                f"{platform.slug!r}, and this Controller has none; it never submits Mode B "
                "jobs with its own credentials",
            )
        if self.profile.ssh_user is None:
            raise AdmissionRefusedError(
                "hpc_identity_unconfigured",
                f"The operator's UNIX user for {platform.slug!r} is not configured",
            )
        return TrustedHPCIdentity(
            HPCIdentity.controller(platform.id), HPCPrincipal(unix_user=self.profile.ssh_user)
        )


@dataclass(frozen=True)
class Admission:
    """
    A submission the registry admitted: its execution and attempt are committed.

    Attributes:
        execution ([`db.executions.Execution`]): The logical execution.
        attempt ([`db.executions.Attempt`]): The `submitting` attempt.
        backend ([`backends.slurm.SlurmBackend`]): Connected to the attempt's login node.
        account (`str`): `--account`, as recorded.
        partition (`str`): `--partition`, as recorded.
        time (`str | None`): `--time`, as recorded.
        selection ([`authz.PlatformSelection`]): The Platform and why.
        resolution ([`authz.AccountResolution`]): The account and why.
    """

    execution: Execution
    attempt: Attempt
    backend: SlurmBackend
    account: str
    partition: str
    time: str | None
    selection: PlatformSelection
    resolution: AccountResolution
    details: Mapping[str, Any] = field(default_factory=dict)
    bounded: bool = False
    """Whether the job must be submitted pinned to its reservation (one node, one task,
    explicit CPUs); see [`backends.slurm.SlurmBackend.submit_payload`]."""

    @property
    def execution_id(self) -> str:
        return self.execution.execution_id


class BuildSlot:
    """An environment-build slot; [`admission.BuildSlot.release`] is idempotent."""

    def __init__(self, semaphore: threading.BoundedSemaphore | None):
        self._semaphore = semaphore
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            semaphore, self._semaphore = self._semaphore, None
        if semaphore is not None:
            semaphore.release()


class RegistryAdmission:
    """
    Admits a Controller's submissions through the Execution Registry and records
    their lifecycle. See the module documentation.

    Args:
        database ([`db.Database`]): Open database.
        backend ([`backends.slurm.SlurmBackend`]): The served Platform's backend.
        profile ([`config.PlatformProfile`]): The served Platform Profile.
        platform_id (`UUID`): The catalog Platform the profile is mirrored as.
        identities ([`admission.HPCIdentitySource`], *optional*): Defaults to
            [`admission.ControllerIdentitySource`] for `profile`.
        audit ([`audit.AuditLog`], *optional*): Defaults to one on `database`.
        limits ([`config.AdmissionLimits`], *optional*): Admission limits (F02a)
            checked and reserved with every attempt. Without them nothing is
            reserved, as before F02a.
        provider_approval (`str`, *optional*): With durable Provider approvals
            (`NEST_PROVIDER_APPROVALS=database`), the Provider whose approval of the
            Project is checked again in the transaction that records the attempt
            ([`db.ExecutionRegistry.begin_attempt`]), so a revocation between the
            `run.submit` check and the attempt is never missed.
        provider_revocations (`str`, *optional*): With environment-mode approvals
            (`NEST_PROVIDER_APPROVALS=environment`), the Provider whose recorded
            revocations are checked again in that transaction, for the same reason.
        authorities (`Collection[str]`, *optional*): The execution authorities this
            deployment permits (`NEST_EXECUTION_AUTHORITIES`); only
            `controller_owned` by default.
    """

    def __init__(
        self,
        database: Database,
        backend: SlurmBackend,
        profile: PlatformProfile,
        platform_id: UUID,
        *,
        identities: HPCIdentitySource | None = None,
        audit: AuditLog | None = None,
        provider_approval: str | None = None,
        provider_revocations: str | None = None,
        limits: AdmissionLimits | None = None,
        authorities: Collection[str] = ("controller_owned",),
        legacy_workers: str = "refuse",
    ):
        if legacy_workers not in ("refuse", "allow"):
            raise ValueError(f"Unknown legacy worker policy {legacy_workers!r}")
        self.legacy_workers = legacy_workers
        self.registry = ExecutionRegistry(database)
        self.limits = limits
        self._builds = threading.BoundedSemaphore(limits.environment_builds) if limits else None
        # Where the next settle() resumes, so a batch of jobs that stay pending or
        # unreadable never starves later ones.
        self._settle_cursor = 0
        # Attempts an RPC of this Controller is still waiting on; settle() skips them.
        self._waiting: set[int] = set()
        self._waiting_lock = threading.Lock()
        self.provider_approval = provider_approval
        self.provider_revocations = provider_revocations
        self.audit = audit or AuditLog(database)
        self.catalog = PlatformCatalog(database, self.audit)
        self.accounts = AccountResolver(database, self.audit)
        self.authority = AuthorityResolver(database, authorities, self.audit)
        self.backend = backend
        self.profile = profile
        self.platform_id = platform_id
        self.identities = identities or ControllerIdentitySource(profile)
        # Job IDs sbatch returned that could not be recorded yet, by attempt ID.
        self._unrecorded: dict[int, int] = {}
        self._unrecorded_lock = threading.Lock()

    # Admission ----------------------------------------------------------------

    def admit(
        self,
        principal: Principal,
        project_id: UUID,
        request: SubmissionRequest,
        *,
        execution_id: str,
    ) -> Admission:
        """
        Resolves, registers and durably records an attempt for `request`. Nothing
        has been submitted when this returns or raises.

        Args:
            principal ([`authz.Principal`]): The authenticated caller, already
                authorized for `run.submit` in `project_id`.
            project_id (`UUID`): The execution's immutable Project.
            request ([`admission.SubmissionRequest`]): What was asked for.
            execution_id (`str`): The ID to give a newly registered execution.

        Raises:
            [`admission.AdmissionRefusedError`]: If the request may not run.
            [`admission.DuplicateSubmissionError`]: If it repeats an idempotency key
                whose request Slurm already accepted.
            [`authz.AccessDeniedError`]: If the session may not use Platforms.
            [`db.DatabaseError`]: If the database is unavailable.
        """
        try:
            return self._admit(principal, project_id, request, execution_id)
        except AdmissionRefusedError as exc:
            logger.warning(
                "Submission refused user_id=%s project_id=%s kind=%s reason=%s",
                principal.user_id,
                project_id,
                request.kind,
                exc.reason,
            )
            self.audit.record(
                "execution.admit",
                "denied",
                actor_user_id=principal.user_id,
                session_id=principal.session_id,
                project_id=project_id,
                resource_type="execution",
                resource_id=execution_id,
                reason=exc.reason,
                details={
                    "kind": request.kind,
                    "platform_id": str(self.platform_id),
                    "gpu": request.gpu,
                    "cpu": request.cpu,
                    **exc.details,
                },
            )
            raise

    def _admit(
        self,
        principal: Principal,
        project_id: UUID,
        request: SubmissionRequest,
        execution_id: str,
    ) -> Admission:
        try:
            selection = self.catalog.select_platform(principal, project_id, request.platform)
        except PlatformNotFoundError:
            raise AdmissionRefusedError(
                "platform_not_found", "Platform not found", grpc.StatusCode.NOT_FOUND
            ) from None
        except PlatformResolutionError as exc:
            raise AdmissionRefusedError(exc.reason, str(exc)) from None
        platform = selection.platform
        if platform.id != self.platform_id:
            raise AdmissionRefusedError(
                "platform_not_served",
                f"This Controller does not submit to Platform {platform.slug!r}; it was "
                f"selected ({selection.origin}) but no other Platform is used instead",
            )
        # D01: authorized before any login node, relay or staging is touched.
        try:
            decision = self.authority.resolve(principal, project_id, platform.id, request.authority)
        except AuthorityRefusedError as exc:
            raise AdmissionRefusedError(
                exc.reason,
                str(exc),
                (
                    grpc.StatusCode.INVALID_ARGUMENT
                    if exc.reason == "invalid_authority"
                    else grpc.StatusCode.PERMISSION_DENIED
                ),
                {"execution_authority": exc.authority},
            ) from None
        if decision.authority == "user_owned":
            # Verified and permitted, but there is no user-owned session transport
            # yet. Never the Controller's credentials, never controller_owned.
            raise AdmissionRefusedError(
                "user_owned_transport_unavailable",
                f"Running on {platform.slug!r} under your own HPC account is authorized, but "
                "this Controller has no user-owned SSH session transport yet; nothing was "
                "submitted, and the Controller's own credentials are never used instead",
                details=decision.details(),
            )
        mode = self.profile.ssh_mode
        try:
            endpoint = self.catalog.select_login_node(
                principal, platform.id, mode=mode, project_id=project_id
            )
        except NoLoginNodeError, EndpointNotApprovedError:
            raise AdmissionRefusedError(
                "no_login_node",
                f"Platform {platform.slug!r} has no approved login node for Mode {mode}",
            ) from None
        trusted = self.identities.resolve(principal, platform, mode)
        try:
            resolution = self.accounts.resolve(
                principal,
                project_id,
                mode=mode,
                identity=trusted.identity,
                platform=platform.id,
                account=request.account,
            )
        except AccountResolutionError as exc:
            raise AdmissionRefusedError(
                exc.reason, str(exc), grpc.StatusCode.PERMISSION_DENIED
            ) from None
        except ValueError as exc:
            raise AdmissionRefusedError(
                "invalid_account", str(exc), grpc.StatusCode.INVALID_ARGUMENT
            ) from None
        backend = self._transport(endpoint)
        if trusted.identity.kind != "controller" or trusted.principal.binding_id is not None:
            # controller_owned runs as the Controller's own identity, never a User's.
            raise AdmissionRefusedError(
                "identity_not_allowed",
                "A controller_owned submission runs only as the Controller's own HPC identity",
                grpc.StatusCode.PERMISSION_DENIED,
            )
        if resolution.account_name is None:
            # Only Mode B resolves to the native default, and Mode B has no transport.
            raise AdmissionRefusedError(
                "native_default_unsupported", "The native Slurm default cannot be used here"
            )
        partition = request.partition or self.profile.partition
        time_limit = request.time or self.profile.time
        reservation = self._reservation(request, time_limit)
        if (
            self.limits is not None
            and request.kind == "batch"
            and (self.limits.batch_scripts == "refuse")
        ):
            raise AdmissionRefusedError(
                "limit_batch_scripts_unbounded",
                "This Controller admits no SubmitJob batch scripts: their own #SBATCH "
                "directives can allocate more than any admission limit, so they cannot be "
                "bounded (NEST_LIMIT_BATCH_SCRIPTS=refuse). Nothing was submitted",
            )
        if self.limits is not None:
            try:
                # Before anything is registered: a request no limit could admit.
                check_request_limits(self.limits, reservation)
            except AdmissionLimitError as exc:
                raise _limit_refusal(exc) from None
        options = self._options(request, selection, resolution, partition, time_limit)
        try:
            execution, created = self.registry.register(
                project_id,
                principal.user_id,
                request.kind,
                request.requested_options(),
                idempotency_key=request.idempotency_key,
                execution_id=execution_id,
            )
        except IdempotencyConflictError as exc:
            raise AdmissionRefusedError("idempotency_conflict", str(exc)) from None
        if not created:
            self._check_repeat(execution, project_id)
        try:
            attempt = self.registry.begin_attempt(
                execution.execution_id,
                endpoint=endpoint,
                principal=trusted.principal,
                **resolution.job_fields(),
                options=options,
                source_digest=request.source_digest,
                environment_digest=request.environment_digest,
                provider=self.provider_approval,
                provider_revocations=self.provider_revocations,
                submitter=Submitter(principal.user_id, principal.session_id),
                reservation=reservation if self.limits is not None else None,
                limits=self.limits,
                authority=decision.pin(),
            )
        except AuthorityNotPermittedError as exc:
            raise AdmissionRefusedError(
                "authority_policy_changed", str(exc), grpc.StatusCode.PERMISSION_DENIED
            ) from None
        except BindingNotVerifiedError as exc:
            raise AdmissionRefusedError(
                "hpc_binding_revoked", str(exc), grpc.StatusCode.PERMISSION_DENIED
            ) from None
        except AdmissionLimitError as exc:
            raise _limit_refusal(exc) from None
        except SubmitterNotAuthorizedError as exc:
            raise AdmissionRefusedError(
                "submitter_not_authorized", str(exc), grpc.StatusCode.PERMISSION_DENIED
            ) from None
        except ProviderNotApprovedError as exc:
            raise AdmissionRefusedError(
                "provider_denied", str(exc), grpc.StatusCode.PERMISSION_DENIED
            ) from None
        except ExecutionClosedError as exc:
            raise AdmissionRefusedError(
                "execution_closed", str(exc), grpc.StatusCode.ALREADY_EXISTS
            ) from None
        except AttemptInFlightError as exc:
            raise AdmissionRefusedError(
                "attempt_in_flight",
                f"{exc}; it is not submitted again. An attempt whose outcome is unknown "
                "must be reconciled first (nest-db execution reconcile)",
                grpc.StatusCode.ABORTED,
            ) from None
        except (PlatformUnavailableError, EndpointNotApprovedError) as exc:
            raise AdmissionRefusedError("platform_unavailable", str(exc)) from None
        except AccountNotPermittedError as exc:
            raise AdmissionRefusedError(
                "account_not_permitted", str(exc), grpc.StatusCode.PERMISSION_DENIED
            ) from None
        details = {
            "execution_id": execution.execution_id,
            "attempt": attempt.attempt_number,
            "platform": platform.slug,
            "platform_origin": selection.origin,
            "login_node_id": endpoint.node_id,
            "mode": mode,
            **decision.details(),
            "account": resolution.account_name,
            "account_origin": resolution.origin,
            "cpu": reservation.cpu,
            "gpu": reservation.gpu,
            "walltime_seconds": reservation.walltime_seconds,
            "reserved": self.limits is not None,
            "quota_enforced": self.limits is not None and reservation.bounded,
        }
        logger.info(
            "Submission admitted " + " ".join(f"{key}=%s" for key in details),
            *details.values(),
        )
        self.audit.record(
            "execution.admit",
            "succeeded",
            actor_user_id=principal.user_id,
            session_id=principal.session_id,
            project_id=project_id,
            resource_type="execution",
            resource_id=execution.execution_id,
            details={**details, "platform_id": str(platform.id), "kind": request.kind},
        )
        return Admission(
            execution,
            attempt,
            backend,
            resolution.account_name,
            partition,
            time_limit,
            selection,
            resolution,
            details,
            bounded=self.limits is not None and reservation.bounded,
        )

    @staticmethod
    def _reservation(request: SubmissionRequest, time_limit: str | None) -> Reservation:
        """
        What the job is allocated, not just what was asked for.

        `Execute`: the worker runs on one node as one task, with the request's GPUs
        and CPUs or the worker script's defaults where it asks for `0` (the default
        GPU is allocated even for a CPU-only call). With an environment, a build may
        run first with its own allocation, so the larger of the two is reserved.
        `SubmitJob`: only what was requested, marked not bounded.
        """
        walltime = slurm_time_seconds(time_limit) if time_limit else None
        if request.kind == "batch":
            return Reservation(
                cpu=request.cpu, gpu=request.gpu, walltime_seconds=walltime, bounded=False
            )
        gpu, cpu = worker_allocation(request.gpu, request.cpu)
        if request.environment_digest is not None:
            build = slurm_time_seconds(ENVIRONMENT_BUILD_TIME)
            gpu, cpu = max(gpu, ENVIRONMENT_BUILD_GPUS), max(cpu, ENVIRONMENT_BUILD_CPUS)
            walltime = max(walltime, build) if walltime and build else walltime
        return Reservation(cpu=cpu, gpu=gpu, walltime_seconds=walltime)

    def _check_repeat(self, execution: Execution, project_id: UUID) -> None:
        """Decides what a repeated idempotency key may do. Never resubmits blindly."""
        attempts = self.registry.attempts(execution.execution_id, project_id=project_id)
        latest = attempts[-1] if attempts else None
        if (
            latest is not None
            and latest.slurm_job_id is not None
            and latest.status
            in (
                "submitted",
                "running",
                "succeeded",
            )
        ):
            raise DuplicateSubmissionError(execution, latest)
        if latest is not None and latest.status == "unknown":
            raise AdmissionRefusedError(
                "outcome_unknown",
                f"Execution {execution.execution_id} has an attempt whose submission outcome "
                "is unknown; it is not submitted again until an operator reconciles it "
                "(nest-db execution reconcile)",
                grpc.StatusCode.ABORTED,
            )
        # A failed or cancelled attempt, or none: begin_attempt decides.

    def _transport(self, endpoint: LoginEndpoint) -> SlurmBackend:
        if endpoint.mode != "A" or self.backend.config.ssh_mode != "A":
            raise AdmissionRefusedError(
                "mode_b_transport_unavailable",
                "This Controller has no per-User Mode B transport; it never submits Mode B "
                "jobs with its own credentials",
            )
        try:
            return self.backend.for_endpoint(endpoint.host, endpoint.port)
        except ValueError:
            raise AdmissionRefusedError(
                "endpoint_not_served",
                "The selected login node is not one this Controller is configured to reach",
            ) from None

    def _options(
        self,
        request: SubmissionRequest,
        selection: PlatformSelection,
        resolution: AccountResolution,
        partition: str,
        time: str | None,
    ) -> dict[str, ResolvedOption]:
        platform_id = str(selection.platform.id)
        options = {
            "gpu": ResolvedOption.requested(request.gpu),
            "cpu": ResolvedOption.requested(request.cpu),
            "platform": (
                ResolvedOption.requested(platform_id)
                if selection.origin == "explicit"
                else ResolvedOption.defaulted(
                    platform_id, _PLATFORM_DEFAULT_SOURCES[selection.origin]
                )
            ),
            "account": (
                ResolvedOption.requested(resolution.account_name)
                if resolution.origin == "explicit"
                else ResolvedOption.defaulted(
                    resolution.account_name, _ACCOUNT_DEFAULT_SOURCES[resolution.origin]
                )
            ),
            "partition": (
                ResolvedOption.requested(
                    partition, default=self.profile.partition, default_source="platform"
                )
                if request.partition
                else ResolvedOption.defaulted(partition, "platform")
            ),
        }
        if time is not None:
            options["time"] = (
                ResolvedOption.requested(time, default=self.profile.time, default_source="platform")
                if request.time
                else ResolvedOption.defaulted(time, "platform")
            )
        if request.kind == "batch":
            options["workdir"] = ResolvedOption.requested(request.workdir)
            options["script"] = ResolvedOption.requested(request.script)
            options["script_args"] = ResolvedOption.requested(list(request.script_args))
        return options

    # Bounds -------------------------------------------------------------------

    def environment_slot(self) -> BuildSlot:
        """
        Takes one of this Controller's environment-build slots
        (`NEST_LIMIT_ENVIRONMENT_BUILDS`) without waiting for one. Release it with
        [`admission.BuildSlot.release`] once the build is over.

        Raises:
            [`admission.AdmissionRefusedError`]: `limit_environment_builds` with
                `RESOURCE_EXHAUSTED` when every slot is taken.
        """
        if self._builds is None:
            return BuildSlot(None)
        if not self._builds.acquire(blocking=False):
            assert self.limits is not None
            raise AdmissionRefusedError(
                "limit_environment_builds",
                f"This Controller is already building {self.limits.environment_builds} "
                "environments; nothing was submitted, retry shortly",
                grpc.StatusCode.RESOURCE_EXHAUSTED,
                {"limit": self.limits.environment_builds},
            )
        return BuildSlot(self._builds)

    @contextmanager
    def waiting(self, admission: Admission) -> Generator[None]:
        """Marks an attempt as followed by an RPC of this Controller, for `settle`."""
        with self._waiting_lock:
            self._waiting.add(admission.attempt.id)
        try:
            yield
        finally:
            with self._waiting_lock:
                self._waiting.discard(admission.attempt.id)

    # Lifecycle ----------------------------------------------------------------

    def _write(self, what: str, admission: Admission, call) -> None:
        """Writes a lifecycle change after an irreversible step; never raises."""
        try:
            call()
        except (DatabaseError, ExecutionRegistryError) as exc:
            logger.error(
                "Execution attempt update failed what=%s execution_id=%s attempt=%s: %s",
                what,
                admission.execution_id,
                admission.attempt.attempt_number,
                exc,
            )

    RECORD_RETRY_DELAYS = (0.0, 0.25, 1.0)
    """Seconds before each try to record a Slurm job ID that `sbatch` returned."""

    def submitted(self, admission: Admission, slurm_job_id: int) -> bool:
        """
        Durably records the Slurm job ID `sbatch` returned, retrying briefly.

        Returns:
            `bool`: Whether the job ID is committed. When it is not, the caller must
                not acknowledge the submission as successful: the attempt is still
                `submitting` without a job ID. The job ID is kept, and every
                [`admission.RegistryAdmission.recover`] tries to record it again (also
                after recovery has marked the attempt `unknown`), so the Controller
                reconciles it on its own while it keeps running.
        """
        attempt_id = admission.attempt.id
        for delay in self.RECORD_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                self.registry.record_submission(attempt_id, slurm_job_id)
                return True
            except ExecutionRegistryError as exc:
                logger.error(
                    "Slurm job ID refused by the registry execution_id=%s job_id=%s: %s",
                    admission.execution_id,
                    slurm_job_id,
                    exc,
                )
                break
            except DatabaseError as exc:
                logger.warning(
                    "Slurm job ID not recorded yet execution_id=%s job_id=%s: %s",
                    admission.execution_id,
                    slurm_job_id,
                    exc,
                )
        with self._unrecorded_lock:
            self._unrecorded[attempt_id] = slurm_job_id
        logger.error(
            "Slurm accepted job_id=%s for execution_id=%s attempt_id=%s, but the registry "
            "could not record it; it is not resubmitted and is recorded on the next "
            "recovery sweep, or with nest-db execution reconcile %s --slurm-job-id %s",
            slurm_job_id,
            admission.execution_id,
            attempt_id,
            attempt_id,
            slurm_job_id,
        )
        return False

    def _record_unrecorded(self) -> None:
        """Retries the job IDs [`admission.RegistryAdmission.submitted`] could not store."""
        with self._unrecorded_lock:
            pending = dict(self._unrecorded)
        for attempt_id, slurm_job_id in pending.items():
            try:
                self.registry.record_submission(attempt_id, slurm_job_id)
            except ExecutionRegistryError as exc:
                # Reconciled or settled meanwhile: that record stands.
                logger.warning(
                    "Kept Slurm job ID not recorded attempt_id=%s job_id=%s: %s",
                    attempt_id,
                    slurm_job_id,
                    exc,
                )
            except DatabaseError:
                continue
            else:
                logger.info(
                    "Slurm job ID recorded on recovery attempt_id=%s job_id=%s",
                    attempt_id,
                    slurm_job_id,
                )
            with self._unrecorded_lock:
                self._unrecorded.pop(attempt_id, None)

    def pin_worker_protocol(self, admission: Admission, backend: SlurmBackend) -> int:
        """
        Reads the deployed worker script's protocol and pins it on the attempt,
        before anything is staged or submitted, so its completion settles by the
        protocol it actually ran with.

        Returns:
            `int`: The protocol. With 2 or more, the caller stages the attempt in its
                own artifact directory (see [`admission.RegistryAdmission.artifact_dir`]).

        Raises:
            [`errors.WorkerProtocolError`]: If the worker predates the outcome record
                and legacy workers are refused (`NEST_LEGACY_WORKERS=refuse`).
            [`errors.DatabaseUnavailableError`]: If it cannot be recorded; nothing is
                submitted then.
            [`errors.InfrastructureError`]: If the script cannot be read.
        """
        protocol = backend.worker_protocol()
        if protocol < WORKER_PROTOCOL and self.legacy_workers != "allow":
            raise WorkerProtocolError(
                f"The worker script on {self.profile.id!r} declares protocol {protocol}, which "
                f"predates the outcome record (protocol {WORKER_PROTOCOL}), so this Controller "
                "could not tell a function that raised from one that returned. Nothing was "
                "submitted. Update the Platform's Nest checkout, or allow legacy workers "
                "during the rollout (NEST_LEGACY_WORKERS=allow)."
            )
        try:
            self.registry.record_worker_protocol(admission.attempt.id, protocol)
        except (DatabaseError, ExecutionRegistryError) as exc:
            raise DatabaseUnavailableError(
                f"The attempt's worker protocol could not be recorded ({exc}); nothing was "
                "submitted"
            ) from exc
        logger.info(
            "Worker protocol pinned execution_id=%s attempt=%s protocol=%s",
            admission.execution_id,
            admission.attempt.attempt_number,
            protocol,
        )
        return protocol

    @staticmethod
    def artifact_dir(
        backend: SlurmBackend, execution_id: str, attempt_number: int, protocol: int | None
    ) -> str:
        """
        Where an attempt's payload, result and outcome record live: its own
        directory for protocol 2, the shared execution directory otherwise.
        """
        own = protocol is not None and protocol >= WORKER_PROTOCOL
        return backend.attempt_dir(execution_id, attempt_number if own else None)

    def completed(
        self,
        admission: Admission,
        remote: RemoteOutcome | InvalidOutcomeError | None,
        *,
        worker_protocol: int | None,
        output_ref: str | None = None,
    ) -> None:
        """
        Records that the job completed and its result was received, settled by
        [`admission.completion`]. A protocol 2 attempt whose record is not there
        yet stays live, marked `remote_outcome_pending`, for the settle sweep.
        """
        decided = completion(remote, worker_protocol=worker_protocol)
        if decided is None:
            logger.warning(
                "Outcome record missing; attempt left for settling execution_id=%s",
                admission.execution_id,
            )
            self._write(
                "outcome pending",
                admission,
                lambda: self.registry.note_outcome_pending(admission.attempt.id, _PENDING),
            )
            return
        status, error_kind, message = decided
        if status == "failed":
            logger.info(
                "Remote call failed execution_id=%s error_kind=%s",
                admission.execution_id,
                error_kind,
            )
        self._write(
            status,
            admission,
            lambda: self.registry.finish_attempt(
                admission.attempt.id,
                status,
                slurm_state="COMPLETED",
                output_ref=output_ref,
                error_kind=error_kind,
                error_message=message,
            ),
        )

    @staticmethod
    def read_outcome(
        backend: SlurmBackend, job_dir: str
    ) -> RemoteOutcome | InvalidOutcomeError | None:
        """
        Reads a completed attempt's outcome record from its artifact directory, for
        [`admission.completion`].

        Raises:
            [`errors.InfrastructureError`]: If it cannot be read now; the attempt must
                then be left as it is, since its outcome is unknown.
        """
        try:
            return backend.read_outcome(job_dir)
        except InvalidOutcomeError as exc:
            return exc

    def failed(self, admission: Admission, exc: Exception, *, submitted: bool) -> None:
        """
        Records the outcome of an error.

        Before the worker's `sbatch` (`submitted=False`) every error is a known
        failure, except [`errors.SubmissionUnknownError`], which leaves the attempt
        `unknown`. Afterwards only a Slurm job failure, or a result too large to
        return from a job that completed, settles the attempt; a timeout or a lost
        status query leaves it `submitted`, since the job may still run.
        """
        kind = exc.kind if isinstance(exc, InfrastructureError) else "internal"
        job_id = exc.job_id if isinstance(exc, InfrastructureError) else None
        message = str(exc)
        if isinstance(exc, SubmissionUnknownError) and not submitted:
            status = "unknown"
        elif not submitted or isinstance(exc, (SlurmJobError, ResultTooLargeError)):
            status = "failed"
        else:
            logger.warning(
                "Execution attempt still submitted after error execution_id=%s kind=%s",
                admission.execution_id,
                kind,
            )
            return
        self._write(
            status,
            admission,
            lambda: self.registry.finish_attempt(
                admission.attempt.id,
                status,
                slurm_job_id=job_id,
                error_kind=kind,
                error_message=message,
            ),
        )

    def build_unknown(self, admission: Admission, exc: Exception, build_job_id: int | None):
        """
        Records an environment build whose job may still be running (its submission
        or wait timed out, or its status could not be read) as `unknown`, so the
        attempt keeps its reservation, on every replica and across restarts, until
        an operator confirms the build is over (`nest-db execution reconcile
        --not-submitted`; the worker never ran). Never raises.
        """
        kind = exc.kind if isinstance(exc, InfrastructureError) else "internal"
        job = f"environment build job {build_job_id}" if build_job_id else "an environment build"
        message = (
            f"{job} may still be running ({kind}: {exc}); the worker was not submitted. "
            "Reconcile with --not-submitted once Slurm shows the build finished."
        )
        logger.error(
            "Environment build outcome unknown execution_id=%s attempt_id=%s build_job_id=%s",
            admission.execution_id,
            admission.attempt.id,
            build_job_id,
        )
        self._write(
            "unknown",
            admission,
            lambda: self.registry.finish_attempt(
                admission.attempt.id,
                "unknown",
                error_kind="environment_build_unknown",
                error_message=message,
            ),
        )
        self.audit.record(
            "execution.environment_build_unknown",
            "failed",
            project_id=admission.execution.project_id,
            actor_user_id=admission.execution.owner_user_id,
            resource_type="execution",
            resource_id=admission.execution_id,
            reason=kind,
            details={"attempt_id": admission.attempt.id, "build_job_id": build_job_id},
        )

    # Job operations -----------------------------------------------------------

    def job_record(self, slurm_job_id: int) -> dict[str, Any] | None:
        """The owner of the served Platform's latest attempt with `slurm_job_id`."""
        return self.registry.job_owner(self.platform_id, slurm_job_id)

    def observe(self, record: Mapping[str, Any], state: str, *, sweep: bool = False) -> None:
        """
        Applies a Slurm state seen by a job status query to the attempt.

        `COMPLETED` settles an `execution` attempt only from the outcome record in
        the attempt's own artifact directory (see [`admission.completion`]). When it
        cannot be read now, the attempt is left as it is; when a protocol 2 record
        is missing, the attempt is marked `remote_outcome_pending`, and only the
        settle sweep (`sweep`) fails it (`remote_outcome_missing`) once it has
        stayed missing for a full threshold. Never `succeeded` without a record,
        except for a legacy worker. A `batch` job has no record: `COMPLETED` is a
        success.
        """
        attempt_id = record["attempt_id"]
        base = state.split()[0] if state else ""
        try:
            if base in _RUNNING_STATES:
                self.registry.record_running(attempt_id, slurm_state=base)
            elif base == "COMPLETED":
                self._observe_completed(record, sweep=sweep)
            elif base.startswith("CANCELLED"):
                self.registry.finish_attempt(attempt_id, "cancelled", slurm_state=base)
            elif base in _FAILED_STATES:
                self.registry.finish_attempt(
                    attempt_id, "failed", slurm_state=base, error_kind="slurm_job_failed"
                )
        except ExecutionRegistryError:
            pass  # Already final, or moved on: the earlier record stands.
        except DatabaseError as exc:
            logger.error("Execution attempt state update failed attempt_id=%s: %s", attempt_id, exc)

    def _observe_completed(self, record: Mapping[str, Any], *, sweep: bool) -> None:
        attempt_id = record["attempt_id"]
        if record.get("status") in FINAL_ATTEMPT_STATUSES:
            return  # Already settled; nothing to read.
        attempt = self.registry.get_attempt(attempt_id)
        if attempt is None or attempt.status in FINAL_ATTEMPT_STATUSES:
            return
        kind = record.get("kind") or self.registry.kind_of(attempt.execution_id)
        if kind != "execution":
            self.registry.finish_attempt(attempt_id, "succeeded", slurm_state="COMPLETED")
            return
        job_dir = self.artifact_dir(
            self.backend, attempt.execution_id, attempt.attempt_number, attempt.worker_protocol
        )
        try:
            remote = self.read_outcome(self.backend, job_dir)
        except InfrastructureError as exc:
            logger.warning(
                "Completed attempt not settled; its outcome record could not be read "
                "attempt_id=%s kind=%s",
                attempt_id,
                exc.kind,
            )
            return
        decided = completion(remote, worker_protocol=attempt.worker_protocol)
        if decided is None:
            if sweep and attempt.error_kind == REMOTE_OUTCOME_PENDING:
                decided = missing_outcome()
            else:
                self.registry.note_outcome_pending(attempt_id, _PENDING)
                return
        status, error_kind, message = decided
        self.registry.finish_attempt(
            attempt_id,
            status,
            slurm_state="COMPLETED",
            output_ref=f"{job_dir}/output.pkl",
            error_kind=error_kind,
            error_message=message,
        )

    # Recovery -----------------------------------------------------------------

    SETTLE_BATCH = 50
    """Most live attempts one [`admission.RegistryAdmission.settle`] queries Slurm for."""

    def settle(self, older_than: timedelta) -> int:
        """
        Applies Slurm's state to `submitted` and `running` attempts of the served
        Platform that no RPC of this Controller is waiting on and that have not
        changed for `older_than`, so a finished job releases its reservation even
        when no client asks for its status.

        Only a state Slurm reports settles an attempt. A failed query, a job Slurm
        no longer knows (`UNKNOWN`), and attempts without a job ID (`submitting`,
        `unknown`) are left as they are, so a reservation that may still be in use
        is never released.

        Each sweep queries at most `SETTLE_BATCH` attempts, resuming after the last
        one the previous sweep queried and wrapping around, so every eligible
        attempt is queried within a bounded number of sweeps however many stay
        pending or unreadable.

        Returns:
            `int`: How many attempts were queried.
        """
        now = time.time()
        with self._waiting_lock:
            waiting = set(self._waiting)
        eligible = sorted(
            (
                attempt
                for attempt in self.registry.live_attempts(self.platform_id)
                if attempt.status in ("submitted", "running")
                and attempt.slurm_job_id is not None
                and attempt.id not in waiting
                and now - attempt.updated_at.timestamp() > older_than.total_seconds()
            ),
            key=lambda attempt: attempt.id,
        )
        cursor = self._settle_cursor
        ordered = [a for a in eligible if a.id > cursor] + [a for a in eligible if a.id <= cursor]
        candidates = ordered[: self.SETTLE_BATCH]
        if candidates:
            self._settle_cursor = candidates[-1].id
        for attempt in candidates:
            assert attempt.slurm_job_id is not None
            try:
                state = self.backend.status(attempt.slurm_job_id)
            except InfrastructureError as exc:
                logger.warning(
                    "Live attempt not settled attempt_id=%s job_id=%s kind=%s",
                    attempt.id,
                    attempt.slurm_job_id,
                    exc.kind,
                )
                continue
            self.observe(
                {"attempt_id": attempt.id, "execution_id": attempt.execution_id},
                state,
                sweep=True,
            )
        return len(candidates)

    def recover(self, older_than: timedelta) -> list[Attempt]:
        """
        Marks attempts interrupted before a Slurm job ID arrived as `unknown`, for
        reconciliation (see [`db.ExecutionRegistry.mark_interrupted`]), after
        recording any Slurm job ID this Controller received but could not store.
        """
        self._record_unrecorded()
        interrupted = self.registry.mark_interrupted(older_than)
        live = self.registry.live_attempts(self.platform_id)
        unknown = sum(1 for attempt in live if attempt.status == "unknown")
        if interrupted or unknown:
            logger.warning(
                "Execution attempts need reconciliation interrupted=%s unknown=%s; "
                "see nest-db execution list",
                len(interrupted),
                unknown,
            )
        return interrupted


class RecoverySweeper:
    """
    Runs [`admission.RegistryAdmission.recover`] at start and then every `interval`
    seconds on a daemon thread, until stopped, followed by
    [`admission.RegistryAdmission.settle`] when `settle_after` is given. A failed
    sweep is logged and retried.
    """

    def __init__(
        self,
        admission: RegistryAdmission,
        older_than: timedelta,
        interval: float,
        *,
        settle_after: timedelta | None = None,
    ):
        self.admission = admission
        self.older_than = older_than
        self.settle_after = settle_after
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="nest-recovery", daemon=True)

    def sweep(self) -> None:
        try:
            self.admission.recover(self.older_than)
        except DatabaseError as exc:
            logger.error("Execution recovery sweep failed: %s", exc)
            return
        if self.settle_after is None:
            return
        try:
            self.admission.settle(self.settle_after)
        except DatabaseError as exc:
            logger.error("Execution settle sweep failed: %s", exc)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.sweep()

    def start(self) -> RecoverySweeper:
        self.sweep()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
