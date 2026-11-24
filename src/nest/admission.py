"""
Registry-backed admission and lifecycle of Controller submissions (C01).

[`admission.RegistryAdmission`] is the one path every Slurm submission of a
registry-backed Controller (`NEST_EXECUTION_REGISTRY=required`) takes, for both
`Execute` and `SubmitJob`. Before anything irreversible happens on the cluster it:

1. selects the Platform with [`authz.PlatformCatalog.select_platform`] for the
   authenticated caller and the immutable Project (an ineligible Platform is
   refused, never replaced), and checks that this Controller serves it;
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

Mode B: Nest has no verified per-User HPC binding or per-User transport yet. The
default [`admission.ControllerIdentitySource`] therefore refuses every Mode B
submission (`hpc_binding_unavailable`), and even a verified binding is refused at
the transport step (`mode_b_transport_unavailable`): the Controller's own SSH
credentials are never used for a User's Mode B job.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol
from uuid import UUID

import grpc

from ._logging import get_logger
from .audit import AuditLog
from .authz.accounts import AccountResolution, AccountResolutionError, AccountResolver, HPCIdentity
from .authz.platforms import PlatformCatalog, PlatformResolutionError, PlatformSelection
from .authz.policy import Principal
from .backends.slurm import SlurmBackend
from .config import PlatformProfile
from .db.connection import Database, DatabaseError
from .db.executions import (
    AccountNotPermittedError,
    Attempt,
    AttemptInFlightError,
    Execution,
    ExecutionClosedError,
    ExecutionRegistry,
    ExecutionRegistryError,
    HPCPrincipal,
    IdempotencyConflictError,
    PlatformUnavailableError,
    ProviderNotApprovedError,
    ResolvedOption,
)
from .db.platforms import (
    EndpointNotApprovedError,
    LoginEndpoint,
    NoLoginNodeError,
    Platform,
    PlatformNotFoundError,
)
from .errors import InfrastructureError, SlurmJobError, SubmissionUnknownError

logger = get_logger("nest.admission")

__all__ = [
    "Admission",
    "AdmissionRefusedError",
    "ControllerIdentitySource",
    "DuplicateSubmissionError",
    "HPCIdentitySource",
    "RecoverySweeper",
    "RegistryAdmission",
    "SubmissionRequest",
    "TrustedHPCIdentity",
    "digest",
]

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
        self, reason: str, message: str, code: grpc.StatusCode = grpc.StatusCode.FAILED_PRECONDITION
    ):
        self.reason = reason
        self.code = code
        super().__init__(message)


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

    @property
    def execution_id(self) -> str:
        return self.execution.execution_id


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
        provider_approval (`str`, *optional*): With durable Provider approvals
            (`NEST_PROVIDER_APPROVALS=database`), the Provider whose approval of the
            Project is checked again in the transaction that records the attempt
            ([`db.ExecutionRegistry.begin_attempt`]), so a revocation between the
            `run.submit` check and the attempt is never missed.
        provider_revocations (`str`, *optional*): With environment-mode approvals
            (`NEST_PROVIDER_APPROVALS=environment`), the Provider whose recorded
            revocations are checked again in that transaction, for the same reason.
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
    ):
        self.registry = ExecutionRegistry(database)
        self.provider_approval = provider_approval
        self.provider_revocations = provider_revocations
        self.audit = audit or AuditLog(database)
        self.catalog = PlatformCatalog(database, self.audit)
        self.accounts = AccountResolver(database, self.audit)
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
                details={"kind": request.kind, "platform_id": str(self.platform_id)},
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
        if resolution.account_name is None:
            # Only Mode B resolves to the native default, and Mode B has no transport.
            raise AdmissionRefusedError(
                "native_default_unsupported", "The native Slurm default cannot be used here"
            )
        partition = request.partition or self.profile.partition
        time_limit = request.time or self.profile.time
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
            )
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
            "account": resolution.account_name,
            "account_origin": resolution.origin,
        }
        logger.info(
            "Submission admitted " + " ".join(f"{key}=%s" for key in details),
            *details.values(),
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
        )

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

    def succeeded(self, admission: Admission, *, output_ref: str | None = None) -> None:
        """Records that the job completed and its result was received."""
        self._write(
            "succeeded",
            admission,
            lambda: self.registry.finish_attempt(
                admission.attempt.id, "succeeded", slurm_state="COMPLETED", output_ref=output_ref
            ),
        )

    def failed(self, admission: Admission, exc: Exception, *, submitted: bool) -> None:
        """
        Records the outcome of an error.

        Before the worker's `sbatch` (`submitted=False`) every error is a known
        failure, except [`errors.SubmissionUnknownError`], which leaves the attempt
        `unknown`. Afterwards only a Slurm job failure settles the attempt; a timeout
        or a lost status query leaves it `submitted`, since the job may still run.
        """
        kind = exc.kind if isinstance(exc, InfrastructureError) else "internal"
        job_id = exc.job_id if isinstance(exc, InfrastructureError) else None
        message = str(exc)
        if isinstance(exc, SubmissionUnknownError) and not submitted:
            status = "unknown"
        elif not submitted or isinstance(exc, SlurmJobError):
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

    # Job operations -----------------------------------------------------------

    def job_record(self, slurm_job_id: int) -> dict[str, Any] | None:
        """The owner of the served Platform's latest attempt with `slurm_job_id`."""
        return self.registry.job_owner(self.platform_id, slurm_job_id)

    def observe(self, record: Mapping[str, Any], state: str) -> None:
        """Applies a Slurm state seen by a job status query to the attempt."""
        attempt_id = record["attempt_id"]
        base = state.split()[0] if state else ""
        try:
            if base in _RUNNING_STATES:
                self.registry.record_running(attempt_id, slurm_state=base)
            elif base == "COMPLETED":
                self.registry.finish_attempt(attempt_id, "succeeded", slurm_state=base)
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

    # Recovery -----------------------------------------------------------------

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
    seconds on a daemon thread, until stopped. A failed sweep is logged and retried.
    """

    def __init__(self, admission: RegistryAdmission, older_than: timedelta, interval: float):
        self.admission = admission
        self.older_than = older_than
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="nest-recovery", daemon=True)

    def sweep(self) -> None:
        try:
            self.admission.recover(self.older_than)
        except DatabaseError as exc:
            logger.error("Execution recovery sweep failed: %s", exc)

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
