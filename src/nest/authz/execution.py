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
from ..db.connection import Database
from ..db.identity import hash_token
from ..db.sessions import SessionStore
from ..errors import AUTHORIZATION_METADATA, PROJECT_METADATA
from .policy import Action, Decision, Principal, Target
from .service import AccessDeniedError, Authorizer, ProviderAuthority

logger = get_logger("nest.authz.execution")

__all__ = [
    "AUTHORIZATION_METADATA",
    "PROJECT_METADATA",
    "AllowedProjects",
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
        audit ([`audit.AuditLog`], *optional*): Defaults to one on `database`.
    """

    def __init__(
        self,
        database: Database,
        providers: ProviderAuthority,
        *,
        provider_id: str | None = None,
        audit: AuditLog | None = None,
    ):
        self.audit = audit or AuditLog(database)
        self.authorizer = Authorizer(database, providers, self.audit)
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
    ) -> None:
        """
        Checks `run.submit` and records the decision. An allowed submission is
        recorded before it runs, and refused if its record cannot be written.

        Raises:
            [`authz.AccessDeniedError`]: If the policy or the Provider refuses it.
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
        self._record(
            Action.RUN_SUBMIT,
            "succeeded",
            principal,
            project_id,
            resource=resource,
            details=details,
            required=True,
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
        `not_a_member`, so its existence is not revealed.

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
