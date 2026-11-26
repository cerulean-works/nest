"""
Server-side choice of a job's execution authority and HPC identity (D01, D02).

Before any login node, relay or staging is touched, [`authz.AuthorityResolver`]
decides whose HPC identity a job may run as, from server-owned policy only:

1. The Controller's deployment setting (`NEST_EXECUTION_AUTHORITIES`).
2. The Platform's authority policy, and
3. the Project's authority policy on that Platform, both operator managed and
   versioned (see [`db.HPCAccessStore`]). A layer without a policy permits only
   `controller_owned`, so `user_owned` needs an explicit opt-in on both.

An authority the caller asks for (`x-nest-execution-authority`) must be one all
three permit; it is refused otherwise and never replaced by another. When the
caller asks for none, `user_owned` is chosen whenever it is permitted, so a User
whose Project may run under their own identity never silently runs under the
Controller's; otherwise `controller_owned`. There is no fallback: a `user_owned`
job that cannot proceed (no verified binding, no session, no transport) is
refused, even when the Controller has its own Mode A credentials configured.

For `user_owned`, the caller must be a human Nest User (a browser or CLI
session) with a **verified** HPC account binding on the Platform. A claimed
username, a successful SSH relay or a client-reported `whoami` is not evidence
of ownership. Service principals (B09) never have a user-owned binding.

The decision pins the authority and both policy versions; the registry re-checks
them, and the binding, in the transaction that records the attempt.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .._logging import get_logger
from ..audit import AuditLog
from ..db._transaction import as_uuid
from ..db.connection import Database
from ..db.executions import HPCPrincipal
from ..db.hpc_access import (
    EXECUTION_AUTHORITIES,
    AuthorityPin,
    AuthorityPolicy,
    HPCAccessStore,
    HPCBinding,
    normalize_authorities,
)
from .accounts import HPCIdentity
from .policy import Principal

logger = get_logger("nest.authz.hpc_access")

HUMAN_SESSION_KINDS = ("browser", "cli")
"""Session kinds of a human Nest User; only they can own an HPC binding."""


class AuthorityRefusedError(Exception):
    """
    No execution authority may be used for the request. Nothing was submitted, and
    no other authority was tried.

    Attributes:
        reason (`str`): Machine-readable: `invalid_authority`,
            `authority_not_permitted` (the requested authority is not permitted),
            `no_authority_permitted`, `hpc_binding_missing`,
            `hpc_binding_unverified` or `hpc_binding_not_allowed` (the caller is
            not a human Nest User).
        authority (`str | None`): The authority that was refused, if any.
    """

    def __init__(self, reason: str, message: str, authority: str | None = None):
        self.reason = reason
        self.authority = authority
        super().__init__(message)


@dataclass(frozen=True)
class AuthorityDecision:
    """
    The execution authority a job runs under and why. Holds no credential.

    Attributes:
        authority (`str`): `controller_owned` or `user_owned`.
        origin (`str`): `explicit` (requested) or `policy_default`.
        permitted (`tuple[str, ...]`): What the deployment, Platform and Project
            all permit.
        policy ([`db.hpc_access.AuthorityPolicy`]): The stored policy layers.
        binding ([`db.hpc_access.HPCBinding`] | None): The verified binding of a
            `user_owned` decision.
    """

    authority: str
    origin: str
    permitted: tuple[str, ...]
    policy: AuthorityPolicy
    binding: HPCBinding | None = None

    def pin(self) -> AuthorityPin:
        """What the attempt records ([`db.ExecutionRegistry.begin_attempt`])."""
        return AuthorityPin(
            self.authority, self.policy.platform_version, self.policy.project_version
        )

    def user_identity(self) -> tuple[HPCIdentity, HPCPrincipal]:
        """The User's own HPC identity for a `user_owned` decision."""
        binding = self.binding
        if self.authority != "user_owned" or binding is None:
            raise ValueError("Only a user_owned decision has a User HPC identity")
        return (
            HPCIdentity.user(binding.platform_id, binding.user_id),
            HPCPrincipal(
                binding.unix_user,
                binding.unix_uid,
                binding_id=str(binding.id),
                binding_version=binding.version,
            ),
        )

    def details(self) -> dict[str, Any]:
        """What logs and audit records may say about it."""
        details: dict[str, Any] = {
            "execution_authority": self.authority,
            "authority_origin": self.origin,
            "authorities_permitted": ",".join(self.permitted),
            "platform_authority_policy_version": self.policy.platform_version,
            "project_authority_policy_version": self.policy.project_version,
        }
        if self.binding is not None:
            details["hpc_binding_id"] = str(self.binding.id)
            details["hpc_binding_version"] = self.binding.version
        return details


class AuthorityResolver:
    """
    Resolves a job's execution authority on the server. See the module
    documentation. Refusals are recorded in the security audit log. Nothing is
    cached: every resolution reads the policies and the binding again.

    Args:
        database ([`db.Database`]): Open database.
        deployment (`Collection[str]`): The authorities this Controller deployment
            permits (`NEST_EXECUTION_AUTHORITIES`).
        audit ([`audit.AuditLog`], *optional*): Defaults to one on `database`.
    """

    def __init__(
        self,
        database: Database,
        deployment: Collection[str] = ("controller_owned",),
        audit: AuditLog | None = None,
    ):
        self.store = HPCAccessStore(database)
        self.deployment = normalize_authorities(deployment)
        self.audit = audit or AuditLog(database)

    def resolve(
        self,
        principal: Principal,
        project_id: UUID | str,
        platform_id: UUID | str,
        requested: str | None = None,
    ) -> AuthorityDecision:
        """
        The execution authority of the caller's job in `project_id` on the
        Platform. The caller must already be authorized for the Project and
        Platform (`run.submit`, the Platform grant).

        Raises:
            [`authz.AuthorityRefusedError`]: If no authority may be used.
        """
        pid = as_uuid(project_id, "project_id")
        platform = as_uuid(platform_id, "platform_id")
        try:
            decision = self._resolve(principal, pid, platform, requested)
        except AuthorityRefusedError as exc:
            logger.warning(
                "Execution authority refused user_id=%s project_id=%s platform_id=%s "
                "requested=%s reason=%s",
                principal.user_id,
                pid,
                platform,
                requested or "-",
                exc.reason,
            )
            self.audit.record(
                "execution.authority",
                "denied",
                actor_user_id=principal.user_id,
                session_id=principal.session_id,
                project_id=pid,
                resource_type="platform",
                resource_id=str(platform),
                reason=exc.reason,
                details={
                    "requested": (requested or "")[:40] or None,
                    "authority": exc.authority,
                    "session_kind": principal.session_kind,
                },
            )
            raise
        logger.info(
            "Execution authority resolved user_id=%s project_id=%s platform_id=%s "
            "authority=%s origin=%s",
            principal.user_id,
            pid,
            platform,
            decision.authority,
            decision.origin,
        )
        return decision

    def _resolve(
        self, principal: Principal, project_id: UUID, platform_id: UUID, requested: str | None
    ) -> AuthorityDecision:
        if requested is not None and requested not in EXECUTION_AUTHORITIES:
            raise AuthorityRefusedError(
                "invalid_authority",
                "The execution authority must be one of " + ", ".join(EXECUTION_AUTHORITIES),
            )
        policy = self.store.effective_policy(project_id, platform_id)
        permitted = tuple(a for a in policy.authorities if a in self.deployment)
        if requested is not None:
            if requested not in permitted:
                raise AuthorityRefusedError(
                    "authority_not_permitted",
                    f"Execution authority {requested} is not permitted for this Project on "
                    "this Platform; no other authority is used instead",
                    requested,
                )
            authority, origin = requested, "explicit"
        elif not permitted:
            raise AuthorityRefusedError(
                "no_authority_permitted",
                "No execution authority is permitted for this Project on this Platform",
            )
        else:
            authority = "user_owned" if "user_owned" in permitted else "controller_owned"
            origin = "policy_default"
        binding = None
        if authority == "user_owned":
            binding = self._verified_binding(principal, platform_id)
        return AuthorityDecision(authority, origin, permitted, policy, binding)

    def _verified_binding(self, principal: Principal, platform_id: UUID) -> HPCBinding:
        if principal.session_kind not in HUMAN_SESSION_KINDS:
            raise AuthorityRefusedError(
                "hpc_binding_not_allowed",
                "Only a signed-in Nest User can run under their own HPC account",
                "user_owned",
            )
        binding = self.store.current_binding(principal.user_id, platform_id)
        if binding is None:
            raise AuthorityRefusedError(
                "hpc_binding_missing",
                "You have no HPC account binding on this Platform; it is never replaced by "
                "the Controller's identity",
                "user_owned",
            )
        if not binding.verified:
            raise AuthorityRefusedError(
                "hpc_binding_unverified",
                "Your HPC account binding on this Platform is an unverified claim; an "
                "operator must record an independent attestation before it can be used",
                "user_owned",
            )
        return binding
