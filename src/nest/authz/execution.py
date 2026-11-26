"""
Authentication and authorization for the gRPC execution service.

[`authz.ExecutionAccess`] is the Controller's one check for every execution RPC. It
authenticates the caller by a Nest CLI session's access token, then applies the
same [`authz.Authorizer`] the Projects API uses, so a Run is submitted, read or
cancelled only by someone the Project policy and the Provider allow:

| RPC | Action | Project |
| --- | --- | --- |
| `Execute`, `SubmitJob` | `run.submit` | `x-nest-project-id` metadata |
| `GetJobStatus` | `run.read` | the job's recorded Project |
| `CancelJob` | `run.cancel` (own Runs only) | the job's recorded Project |
| `ListEligibleProjects` | `run.submit`, evaluated without recording | each of the caller's Projects |

Every operation that reaches the HPC Platform (a Slurm submission on the
Controller's legacy path, a status query, a cancellation) also needs the
Controller's served Platform to be eligible for the Project now, checked by
[`authz.PlatformCatalog.authorize_platform_action`] after the Project check: an
active Platform granted to the live Project. A revoked grant or a disabled
Platform is refused from the next call on, and answered as not found.

A job with no recorded Project, or one in a Project the caller cannot see, is
answered as not found. Submissions and cancellations, allowed or refused, and
every other refusal are recorded in the security audit log (see [`audit.AuditLog`]).

A service principal's token (B09, see [`authz.ServiceAccess`]) is recognized and
refused on every RPC with `PERMISSION_DENIED` and `nest-error-kind:
noninteractive_auth_unavailable`, recorded in the audit log: each of these RPCs
reaches the HPC Platform under the Controller's credentials, and no service
principal is given that authority, whatever it was granted or the call asks for.

A Nest session is not an HPC credential: the Controller still submits every job
with its own configured Provider credentials, and Slurm and UNIX permissions on
the cluster are unchanged by anything here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .._logging import get_logger
from ..audit import AuditLog
from ..config import ALL_PROJECTS
from ..db.accounts import SlurmAccountStore
from ..db.approvals import ProviderApprovalStore
from ..db.connection import Database
from ..db.identity import hash_token
from ..db.sessions import SessionStore
from ..errors import AUTHORIZATION_METADATA, PROJECT_METADATA
from .accounts import AccountResolutionError, HPCIdentity, resolve_account
from .platforms import PlatformCatalog
from .policy import Action, Decision, Principal, Target, decide
from .service import AccessDeniedError, Authorizer, ProviderAuthority
from .workload import ServiceAccess, ServiceCaller, is_service_token

logger = get_logger("nest.authz.execution")

__all__ = [
    "AUTHORIZATION_METADATA",
    "ELIGIBILITY_REASONS",
    "MAX_ELIGIBLE_PROJECTS",
    "PROJECT_METADATA",
    "AllowedProjects",
    "DatabaseProviderApprovals",
    "EligibleProject",
    "ExecutionAccess",
    "JobAccess",
    "ProjectEligibility",
    "ProjectRequiredError",
    "ServicePrincipalCall",
    "UnauthenticatedError",
]


class UnauthenticatedError(Exception):
    """
    The call carried no accepted Nest session. The message never contains a token.

    Attributes:
        reason (`str`): `missing_token`, `malformed_token` or `invalid_token`.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Authentication required: {reason}")


class ServicePrincipalCall(Exception):
    """
    The call was authenticated as a service principal, not a User.

    Attributes:
        caller ([`authz.ServiceCaller`]): Who called.
    """

    def __init__(self, caller: ServiceCaller):
        self.caller = caller
        super().__init__("The caller is a service principal")


SERVICE_RPC_ACTIONS = {
    "Execute": "run.submit",
    "SubmitJob": "run.submit",
    "GetJobStatus": "run.read",
    "CancelJob": "run.cancel",
    "ListEligibleProjects": "run.submit",
}
"""The audit event recorded when a service principal calls each RPC."""


class ProjectRequiredError(ValueError):
    """A submission named no Project, or not a valid Project ID."""


class AllowedProjects:
    """
    The Controller's [`authz.ProviderAuthority`] with `NEST_PROVIDER_APPROVALS=environment`:
    its Provider accepts Runs from the Projects the operator listed in
    `NEST_PROVIDER_ALLOWED_PROJECTS`.

    Every job still runs as the Controller's single configured HPC account, so the
    approval is the operator's, per Project. No Project role grants it.

    With `revocations`, a revocation recorded in the database
    ([`db.ProviderApprovalStore.revoke`]) refuses new submissions even for a listed
    Project, checked at every submission. The database never approves anything in
    this mode, so a stale or restored list can never regrant revoked access, and
    if the database cannot answer the submission is refused. Cancelling one's own
    Run only needs the list.

    Args:
        projects (`Iterable[str]`): Project IDs, or `"*"` for every Project.
        revocations ([`db.ProviderApprovalStore`], *optional*): Where recorded
            revocations are read. The Controller always passes one.
    """

    def __init__(self, projects: Iterable[str], revocations: ProviderApprovalStore | None = None):
        items = set(projects)
        self.everyone = ALL_PROJECTS in items
        self.projects = frozenset(UUID(p) for p in items if p != ALL_PROJECTS)
        self.revocations = revocations

    def permits(
        self, principal: Principal, project_id: UUID, action: Action, provider: str | None
    ) -> bool:
        if not (self.everyone or project_id in self.projects):
            return False
        if action == Action.RUN_SUBMIT and self.revocations is not None:
            return not self.revocations.is_revoked(provider, project_id)
        return True


class DatabaseProviderApprovals:
    """
    The Controller's [`authz.ProviderAuthority`] with `NEST_PROVIDER_APPROVALS=database`:
    its Provider accepts Runs from the Projects approved for it in PostgreSQL
    (`provider_project_approvals`, see [`db.ProviderApprovalStore`]).

    Every check reads the database now; nothing is cached, so a grant or a
    revocation applies from the next submission on every Controller replica
    sharing the database, with no restart. When the database cannot answer, the
    check raises [`db.DatabaseError`] and the caller refuses (fail closed): it is
    never treated as approved.

    `run.submit` needs a current approval. `run.cancel` needs only that the
    Project has an approval record, current or revoked, for this Provider: a
    revocation stops new submissions but still lets a member cancel their own
    existing Run. Everything else `run.cancel` needs is unchanged (the member's
    role, being the Run's submitter, the job recorded for the Project on the
    served Platform, and that Platform still granted), and a Project never
    approved cannot cancel anything.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.store = ProviderApprovalStore(database)

    def permits(
        self, principal: Principal, project_id: UUID, action: Action, provider: str | None
    ) -> bool:
        if action == Action.RUN_CANCEL:
            return self.store.has_record(provider, project_id)
        return self.store.is_approved(provider, project_id)


MAX_ELIGIBLE_PROJECTS = 200
"""Most Projects [`authz.ExecutionAccess.eligible_projects`] returns in one answer."""


@dataclass(frozen=True)
class EligibleProject:
    """
    A Project the caller may submit to on this Controller now.

    Attributes:
        id (`UUID`): The Project.
        name (`str`): Its display name (not unique).
        role (`str`): The caller's role in it.
    """

    id: UUID
    name: str
    role: str


ELIGIBILITY_REASONS = ("controller", "role", "provider", "platform", "account")
"""Why a Project is not eligible on this Controller, in the order they are checked:
the Controller serves no Platform, the caller's role may not submit, the Provider
does not permit the Project, the served Platform is not granted to it, or a
submission naming no Slurm account would get none."""


@dataclass(frozen=True)
class ProjectEligibility:
    """
    One of the caller's Projects and whether this Controller would accept a
    submission from it now (G02c), for the Management Console.

    Attributes:
        id (`UUID`): The Project.
        name (`str`): Its display name (not unique).
        role (`str`): The caller's role in it.
        reasons (`tuple[str, ...]`): Every unmet requirement, from
            [`authz.ELIGIBILITY_REASONS`]; empty when eligible. Never an account name.
    """

    id: UUID
    name: str
    role: str
    reasons: tuple[str, ...] = ()

    @property
    def eligible(self) -> bool:
        """Whether nothing is missing."""
        return not self.reasons


@dataclass(frozen=True)
class JobAccess:
    """
    An authorized job operation.

    Attributes:
        principal ([`authz.Principal`]): The caller.
        project_id (`UUID`): The job's Project.
        record (`dict`): The job's record.
    """

    principal: Principal
    project_id: UUID
    record: Mapping[str, Any]


def _metadata(metadata: Iterable[tuple[str, str | bytes]], key: str) -> list[str]:
    return [
        value.decode(errors="replace") if isinstance(value, bytes) else value
        for name, value in metadata
        if name.lower() == key
    ]


class ExecutionAccess:
    """
    Authenticates and authorizes execution RPCs.

    Args:
        database ([`db.Database`]): Open database with sessions and Projects.
        providers ([`authz.ProviderAuthority`]): Provider policy for Run actions.
        provider_id (`str`, *optional*): The Provider the Controller submits to,
            recorded with each decision.
        platform_id (`UUID`, *optional*): The catalog ID of the HPC Platform the
            Controller submits to (see [`controller.environment_platform_id`]).
            Without one, every Platform-bound operation is refused.
        audit ([`audit.AuditLog`], *optional*): Defaults to one on `database`.
    """

    def __init__(
        self,
        database: Database,
        providers: ProviderAuthority,
        *,
        provider_id: str | None = None,
        platform_id: UUID | None = None,
        audit: AuditLog | None = None,
    ):
        self.audit = audit or AuditLog(database)
        self.authorizer = Authorizer(database, providers, self.audit)
        self.platforms = PlatformCatalog(database, self.audit)
        self.accounts = SlurmAccountStore(database)
        self.platform_id = platform_id
        self.sessions = SessionStore(database)
        self.services = ServiceAccess(database, self.audit)
        self.provider_id = provider_id
        self._log_only = AuditLog(None)

    def authenticate(self, metadata: Iterable[tuple[str, str | bytes]], rpc: str) -> Principal:
        """
        The caller of a CLI session presented as `authorization: Bearer <token>`.

        A browser session's token is never accepted here, as a CLI token is never
        accepted as a browser cookie. A service session's token is never returned
        as a [`authz.Principal`]: it raises [`authz.ServicePrincipalCall`], which
        the caller must refuse with [`authz.ExecutionAccess.refuse_service`].

        Raises:
            [`authz.UnauthenticatedError`]: If no accepted session was presented.
            [`authz.ServicePrincipalCall`]: If an accepted service session was.
        """
        values = _metadata(metadata, AUTHORIZATION_METADATA)
        reason = None
        token = ""
        if not values:
            reason = "missing_token"
        elif len(values) > 1:
            reason = "malformed_token"
        else:
            scheme, _, token = values[0].partition(" ")
            token = token.strip()
            if scheme.lower() != "bearer" or not token or len(token) > 512:
                reason = "malformed_token"
        if reason is None and is_service_token(token):
            caller = self.services.authenticate(token)
            if caller is not None:
                raise ServicePrincipalCall(caller)
            reason = "invalid_token"
        if reason is None:
            grant = self.sessions.authenticate(hash_token(token), kind="cli")
            if grant is not None:
                return Principal.from_session(grant.session)
            reason = "invalid_token"
        # Not written to the database: unauthenticated callers cannot fill it.
        self._log_only.record("auth.failed", "denied", reason=reason, details={"rpc": rpc})
        raise UnauthenticatedError(reason)

    def refuse_service(
        self, caller: ServiceCaller, rpc: str, metadata: Iterable[tuple[str, str | bytes]]
    ) -> None:
        """
        Refuses an execution RPC from a service principal, recorded in the audit
        log. Always raises: B09 gives service principals no Controller HPC
        authority, and nothing in the call (Project, Platform, mode or any other
        metadata) changes that.

        Raises:
            [`authz.ServiceDeniedError`]: Always, with `noninteractive_auth_unavailable`.
        """
        try:
            project_id: UUID | None = self.project_of(metadata)
        except ProjectRequiredError:
            project_id = None
        self.services.refuse_hpc(
            caller,
            SERVICE_RPC_ACTIONS.get(rpc, "run.read"),
            project_id=project_id,
            details={"rpc": rpc, "provider": self.provider_id},
        )

    @staticmethod
    def project_of(metadata: Iterable[tuple[str, str | bytes]]) -> UUID:
        """
        The Project named by `x-nest-project-id`.

        Raises:
            [`authz.ProjectRequiredError`]: If none or more than one is named, or
                it is not a UUID.
        """
        values = _metadata(metadata, PROJECT_METADATA)
        if len(values) != 1:
            raise ProjectRequiredError(
                "Name the Project to run in with NEST_PROJECT_ID (x-nest-project-id metadata)"
            )
        try:
            return UUID(values[0].strip())
        except ValueError:
            raise ProjectRequiredError("NEST_PROJECT_ID is not a valid Project ID") from None

    def _record(
        self,
        action: Action,
        outcome: str,
        principal: Principal,
        project_id: UUID | None,
        *,
        resource: tuple[str, str] | None = None,
        reason: str | None = None,
        details: Mapping[str, object] | None = None,
        required: bool = False,
    ) -> None:
        self.audit.record(
            str(action),
            outcome,
            actor_user_id=principal.user_id,
            session_id=principal.session_id,
            project_id=project_id,
            resource_type=resource[0] if resource else None,
            resource_id=resource[1] if resource else None,
            reason=reason,
            details={"provider": self.provider_id, **(details or {})},
            required=required,
        )

    def authorize_submit(
        self,
        principal: Principal,
        project_id: UUID,
        *,
        execution_id: str | None = None,
        details: Mapping[str, object] | None = None,
        platform_bound: bool = False,
    ) -> None:
        """
        Checks `run.submit` and records the decision. An allowed submission is
        recorded before it runs, and refused if its record cannot be written.

        With `platform_bound`, for a submission this Controller sends to its
        Platform itself, the served Platform must also be eligible for the Project
        (see [`authz.PlatformCatalog.authorize_platform_action`]). Registry-backed
        admission selects and checks the Platform itself, so it leaves this off.

        Raises:
            [`authz.AccessDeniedError`]: If the policy, the Platform policy or the
                Provider refuses it.
            [`errors.DatabaseUnavailableError`]: If the audit record cannot be written.
        """
        decision = self.authorizer.check(
            principal,
            Action.RUN_SUBMIT,
            project_id,
            Target(provider=self.provider_id),
            details={"provider": self.provider_id, **(details or {})},
        )
        resource = ("job", execution_id) if execution_id else None
        if not decision:
            # Authorizer.check has recorded the refusal.
            raise AccessDeniedError(Action.RUN_SUBMIT, decision)
        if platform_bound:
            self._require_platform(
                principal, Action.RUN_SUBMIT, project_id, details, self.platform_id
            )
            details = {**(details or {}), "platform_id": str(self.platform_id)}
        self._record(
            Action.RUN_SUBMIT,
            "succeeded",
            principal,
            project_id,
            resource=resource,
            details=details,
            required=True,
        )

    def eligible_projects(self, principal: Principal) -> list[EligibleProject]:
        """
        The caller's Projects that this Controller would accept a submission from
        now (E07), for the SDK's Project selection.

        A Project is eligible when the caller's current role allows `run.submit`,
        the Provider permits it, the served Platform is active and granted to the
        Project, and the default Mode A submission could charge a Slurm account:
        [`authz.resolve_account`] with no account named, exactly as registry
        admission resolves it, so a Project that would be refused `no_account` is
        not offered. That check reads the Project's account mapping only: no SSH,
        no audit record, and no account name in the answer. Each is read now; nothing is cached, and nothing is recorded
        per Project, so listing does not fill the audit log with refusals. The
        list is advice only: every submission is authorized again from scratch
        ([`authz.ExecutionAccess.authorize_submit`] and registry admission), so a
        Project revoked after it was listed is refused and nothing runs.

        Returns:
            `list[EligibleProject]`: At most [`authz.MAX_ELIGIBLE_PROJECTS`], in the
                order the Projects were created.

        Raises:
            [`authz.AccessDeniedError`]: If the session may not read Projects.
            [`db.DatabaseError`]: If the database cannot answer (fail closed).
        """
        eligible: list[EligibleProject] = []
        if self.platform_id is None:
            return eligible
        for project, role in self.platforms.projects.list_projects(principal):
            if self._unmet(principal, project.id, role, first_only=True):
                continue
            eligible.append(EligibleProject(project.id, project.name, role))
            if len(eligible) >= MAX_ELIGIBLE_PROJECTS:
                break
        return eligible

    def project_eligibility(self, principal: Principal) -> list[ProjectEligibility]:
        """
        Every one of the caller's Projects, each with what it still needs to be
        eligible here (G02c): the same checks as
        [`authz.ExecutionAccess.eligible_projects`], all of them reported, so the
        Console can say what to ask for. Reads only, records nothing, and names no
        Slurm account. Advice, exactly like the listing: every submission is
        authorized again from scratch.

        Returns:
            `list[ProjectEligibility]`: At most [`authz.MAX_ELIGIBLE_PROJECTS`], in
                the order the Projects were created.

        Raises:
            [`authz.AccessDeniedError`]: If the session may not read Projects.
            [`db.DatabaseError`]: If the database cannot answer (fail closed).
        """
        answer: list[ProjectEligibility] = []
        for project, role in self.platforms.projects.list_projects(principal):
            reasons = self._unmet(principal, project.id, role, first_only=False)
            answer.append(ProjectEligibility(project.id, project.name, role, reasons))
            if len(answer) >= MAX_ELIGIBLE_PROJECTS:
                break
        return answer

    def _unmet(
        self, principal: Principal, project_id: UUID, role: str, *, first_only: bool
    ) -> tuple[str, ...]:
        """The [`authz.ELIGIBILITY_REASONS`] a Project fails, or only the first."""
        if self.platform_id is None:
            return ("controller",)
        unmet: list[str] = []
        checks = (
            ("role", lambda: bool(decide(principal, role, Action.RUN_SUBMIT))),
            (
                "provider",
                lambda: self.authorizer.providers.permits(
                    principal, project_id, Action.RUN_SUBMIT, self.provider_id
                ),
            ),
            (
                "platform",
                lambda: any(
                    p.id == self.platform_id
                    for p in self.platforms.store.project_platforms(principal.user_id, project_id)[
                        0
                    ]
                ),
            ),
            ("account", lambda: self._default_account_resolves(project_id)),
        )
        for reason, check in checks:
            if not check():
                unmet.append(reason)
                if first_only:
                    break
        return tuple(unmet)

    def _default_account_resolves(self, project_id: UUID) -> bool:
        """Whether a Mode A submission naming no account would get one (E07)."""
        assert self.platform_id is not None
        try:
            resolve_account(
                self.accounts.mapping(project_id, self.platform_id),
                mode="A",
                identity=HPCIdentity.controller(self.platform_id),
            )
        except AccountResolutionError:
            return False
        return True

    def _require_platform(
        self,
        principal: Principal,
        action: Action,
        project_id: UUID,
        details: Mapping[str, object] | None,
        platform_id: UUID | None,
    ) -> None:
        self.platforms.authorize_platform_action(
            principal,
            action,
            project_id,
            platform_id,
            details={"provider": self.provider_id, **(details or {})},
        )

    def authorize_job(
        self,
        principal: Principal,
        action: Action,
        record: Mapping[str, Any] | None,
        slurm_job_id: int,
    ) -> JobAccess:
        """
        Checks `run.read` or `run.cancel` on a recorded job.

        A cancellation is recorded, allowed or not, and refused if its record
        cannot be written. A job with no record or no Project is refused as
        `not_a_member`, so its existence is not revealed. Both operations reach
        the HPC Platform, so the job's Platform must be the served one and still
        eligible for the job's Project (`platform_not_authorized` otherwise, also
        answered as not found).

        Raises:
            [`authz.AccessDeniedError`]: If refused.
            [`errors.DatabaseUnavailableError`]: If a cancellation's record cannot
                be written.
        """
        project_id = record.get("project_id") if record else None
        resource = ("slurm_job", str(slurm_job_id))
        if record is None or project_id is None:
            decision = Decision(False, "not_a_member")
            self.authorizer.denied(
                principal,
                str(action),
                None,
                decision.reason,
                {"provider": self.provider_id, "slurm_job_id": slurm_job_id},
            )
            raise AccessDeniedError(action, decision)
        target = Target(
            run_submitted_by=record.get("submitted_by_user_id"), provider=self.provider_id
        )
        decision = self.authorizer.check(
            principal,
            action,
            project_id,
            target,
            details={"provider": self.provider_id, "slurm_job_id": slurm_job_id},
        )
        if not decision:
            raise AccessDeniedError(action, decision)
        recorded = record.get("platform_id")
        check: dict[str, object] = {"slurm_job_id": slurm_job_id}
        platform = self.platform_id
        if recorded is not None and recorded != self.platform_id:
            # A job of another Platform: this Controller would act on the wrong
            # Slurm namespace. Refused like an ineligible Platform.
            platform = None
            check["job_platform_id"] = str(recorded)
        self._require_platform(principal, action, project_id, check, platform)
        if action == Action.RUN_CANCEL:
            self._record(
                action,
                "succeeded",
                principal,
                project_id,
                resource=resource,
                details={"execution_id": record.get("execution_id")},
                required=True,
            )
        return JobAccess(principal, project_id, record)
