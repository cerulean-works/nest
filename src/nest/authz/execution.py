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

Every operation that reaches the HPC Platform (a Slurm submission on the
Controller's legacy path, a status query, a cancellation) also needs the
Controller's served Platform to be eligible for the Project now, checked by
[`authz.PlatformCatalog.authorize_platform_action`] after the Project check: an
active Platform granted to the live Project. A revoked grant or a disabled
Platform is refused from the next call on, and answered as not found.

A job with no recorded Project, or one in a Project the caller cannot see, is
answered as not found. Submissions and cancellations, allowed or refused, and
every other refusal are recorded in the security audit log (see [`audit.AuditLog`]).

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
from ..db.approvals import ProviderApprovalStore
from ..db.connection import Database
from ..db.identity import hash_token
from ..db.sessions import SessionStore
from ..errors import AUTHORIZATION_METADATA, PROJECT_METADATA
from .platforms import PlatformCatalog
from .policy import Action, Decision, Principal, Target
from .service import AccessDeniedError, Authorizer, ProviderAuthority

logger = get_logger("nest.authz.execution")

__all__ = [
    "AUTHORIZATION_METADATA",
    "PROJECT_METADATA",
    "AllowedProjects",
    "DatabaseProviderApprovals",
    "ExecutionAccess",
    "JobAccess",
    "ProjectRequiredError",
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


class ProjectRequiredError(ValueError):
    """A submission named no Project, or not a valid Project ID."""


class AllowedProjects:
    """
    The Controller's [`authz.ProviderAuthority`]: its Provider accepts Runs from
    the Projects the operator listed in `NEST_PROVIDER_ALLOWED_PROJECTS`.

    Every job still runs as the Controller's single configured HPC account, so the
    approval is the operator's, per Project. No Project role grants it.

    Args:
        projects (`Iterable[str]`): Project IDs, or `"*"` for every Project.
    """

    def __init__(self, projects: Iterable[str]):
        items = set(projects)
        self.everyone = ALL_PROJECTS in items
        self.projects = frozenset(UUID(p) for p in items if p != ALL_PROJECTS)

    def permits(
        self, principal: Principal, project_id: UUID, action: Action, provider: str | None
    ) -> bool:
        return self.everyone or project_id in self.projects


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

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.store = ProviderApprovalStore(database)

    def permits(
        self, principal: Principal, project_id: UUID, action: Action, provider: str | None
    ) -> bool:
        return self.store.is_approved(provider, project_id)


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
        self.platform_id = platform_id
        self.sessions = SessionStore(database)
        self.provider_id = provider_id
        self._log_only = AuditLog(None)

    def authenticate(self, metadata: Iterable[tuple[str, str | bytes]], rpc: str) -> Principal:
        """
        The caller of a CLI session presented as `authorization: Bearer <token>`.

        A browser session's token is never accepted here, as a CLI token is never
        accepted as a browser cookie.

        Raises:
            [`authz.UnauthenticatedError`]: If no accepted session was presented.
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
        if reason is None:
            grant = self.sessions.authenticate(hash_token(token), kind="cli")
            if grant is not None:
                return Principal.from_session(grant.session)
            reason = "invalid_token"
        # Not written to the database: unauthenticated callers cannot fill it.
        self._log_only.record("auth.failed", "denied", reason=reason, details={"rpc": rpc})
        raise UnauthenticatedError(reason)

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
