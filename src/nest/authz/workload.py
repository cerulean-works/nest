"""
Authorization of service principals (B09).

A service principal authenticates with a service session (see
[`auth.WorkloadExchange`]); [`authz.ServiceAccess`] decides what it may do. The rules
are deliberately separate from the human Project matrix ([`authz.decide`]):

- A service principal is never a Project member and has no role. Every action
  needs an explicit, operator-made grant ([`db.ServiceGrant`]) in force now: on a
  live Project, and for a Platform capability, on an active Platform still granted
  to that Project. Nothing is cached, so a revoked grant, principal, trust policy
  or session is refused from the next request.
- No service principal is given Controller HPC authority. Every call that would
  reach an HPC Platform under the Controller's credentials (`Execute`,
  `SubmitJob`, `GetJobStatus`, `CancelJob`) is refused with
  `noninteractive_auth_unavailable`, whatever it was granted and whatever the call
  asks for: no metadata, mode or Platform flag changes that. Granting `run.*`
  records intent for the later per-execution routing (D09/D10) only.
- Human sessions are unaffected: a service token is never accepted as a browser
  or CLI session, and a User's token is never accepted as a service session.

Every refusal, and every allowed read, is recorded in the security audit log with
`session_id` set to the service session and the principal in `details`;
`actor_user_id` stays empty, because no User acted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from .._logging import get_logger
from ..audit import AuditLog
from ..db.connection import Database
from ..db.identity import hash_token
from ..db.service_principals import (
    PLATFORM_CAPABILITIES,
    ServiceGrant,
    ServicePrincipalStore,
    ServiceSession,
)

logger = get_logger("nest.authz.workload")

__all__ = [
    "HPC_AUTHORITY_REASON",
    "SERVICE_TOKEN_PREFIX",
    "ServiceAccess",
    "ServiceCaller",
    "ServiceDeniedError",
    "is_service_token",
]

SERVICE_TOKEN_PREFIX = "nest_svc_"
"""Every service session token starts with this, so it is never mistaken for, or
looked up as, a browser or CLI session token."""

HPC_AUTHORITY_REASON = "noninteractive_auth_unavailable"
"""Why every HPC-bound call from a service principal is refused: no
platform-authorized noninteractive HPC credential exists for service principals
(D09/D10), and Nest never substitutes the Controller's or a person's."""


def is_service_token(token: str) -> bool:
    """Whether a bearer token is a service session token by its form."""
    return token.startswith(SERVICE_TOKEN_PREFIX)


class ServiceDeniedError(Exception):
    """
    A service principal was refused. Nothing was done.

    Attributes:
        action (`str`): The refused action.
        reason (`str`): `capability`, `not_found` or [`authz.HPC_AUTHORITY_REASON`].
        visible (`bool`): Whether the caller may learn the object exists.
    """

    def __init__(self, action: str, reason: str):
        self.action = action
        self.reason = reason
        self.visible = reason == HPC_AUTHORITY_REASON
        super().__init__(f"Service principal not allowed to {action}: {reason}")


@dataclass(frozen=True)
class ServiceCaller:
    """
    An authenticated service principal acting through one service session.

    Attributes:
        service_principal_id (`UUID`): The principal.
        session_id (`UUID`): The service session, recorded in the audit log.
        expires_at (`datetime`): When the session stops being accepted.
        provenance (`Mapping[str, Any]`): The workload token's non-secret claims.
    """

    service_principal_id: UUID
    session_id: UUID
    expires_at: datetime
    provenance: Mapping[str, Any] = field(default_factory=dict)

    principal_type = "service"

    @classmethod
    def from_session(cls, session: ServiceSession) -> ServiceCaller:
        return cls(session.service_principal_id, session.id, session.expires_at, session.provenance)


class ServiceAccess:
    """
    Authenticates service tokens and authorizes service principals.

    Args:
        database ([`db.Database`]): Open database.
        audit ([`audit.AuditLog`], *optional*): Defaults to one on `database`.
    """

    def __init__(self, database: Database, audit: AuditLog | None = None):
        self.store = ServicePrincipalStore(database)
        self.audit = audit or AuditLog(database)

    def authenticate(self, token: str) -> ServiceCaller | None:
        """The caller of a service token accepted now, or `None`."""
        if not is_service_token(token) or len(token) > 512:
            return None
        session = self.store.authenticate(hash_token(token))
        return ServiceCaller.from_session(session) if session else None

    def record(
        self,
        caller: ServiceCaller,
        action: str,
        outcome: str,
        *,
        project_id: UUID | None = None,
        reason: str | None = None,
        details: Mapping[str, object] | None = None,
        required: bool = False,
    ) -> None:
        """Records a decision about `caller` in the security audit log."""
        self.audit.record(
            action,
            outcome,
            session_id=caller.session_id,
            project_id=project_id,
            resource_type="service_principal",
            resource_id=str(caller.service_principal_id),
            reason=reason,
            details={
                "principal_type": "service",
                "service_principal_id": str(caller.service_principal_id),
                "repository_id": caller.provenance.get("repository_id"),
                "run_id": caller.provenance.get("run_id"),
                **(details or {}),
            },
            required=required,
        )

    def require(
        self,
        caller: ServiceCaller,
        capability: str,
        project_id: UUID,
        *,
        platform_id: UUID | None = None,
    ) -> None:
        """
        Checks one capability now. A refusal is recorded and answered as not found,
        so a principal cannot probe for Projects it was not granted.

        Raises:
            [`authz.ServiceDeniedError`]: `not_found` if refused.
        """
        if capability in PLATFORM_CAPABILITIES and capability.startswith("run."):
            self.refuse_hpc(caller, capability, project_id=project_id)
        if not self.store.has_capability(
            caller.service_principal_id, project_id, capability, platform_id=platform_id
        ):
            details = {"platform_id": str(platform_id)} if platform_id else None
            self.record(
                caller,
                capability,
                "denied",
                project_id=project_id,
                reason="capability",
                details=details,
            )
            raise ServiceDeniedError(capability, "not_found")

    def refuse_hpc(
        self,
        caller: ServiceCaller,
        action: str,
        *,
        project_id: UUID | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """
        Refuses an HPC-bound action, recorded with [`authz.HPC_AUTHORITY_REASON`].
        Always raises.

        Raises:
            [`authz.ServiceDeniedError`]: Always.
        """
        self.record(
            caller,
            action,
            "denied",
            project_id=project_id,
            reason=HPC_AUTHORITY_REASON,
            details=details,
        )
        logger.warning(
            "Service principal refused HPC authority service_principal_id=%s action=%s",
            caller.service_principal_id,
            action,
        )
        raise ServiceDeniedError(action, HPC_AUTHORITY_REASON)

    def grants(self, caller: ServiceCaller) -> list[ServiceGrant]:
        """The caller's grants in force now."""
        return self.store.effective_grants(caller.service_principal_id)
