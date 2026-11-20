"""
Server-side discovery, default resolution and login-node selection for HPC
Platforms (D07).

[`authz.PlatformCatalog`] is the one place a caller learns which Platforms exist
and which login node the server will use. A User sees a Platform only through an
active membership in a live Project the operator granted it to; any other
Platform, disabled or not, is answered as not found, so its existence is not
revealed. Login nodes are always chosen by the server from the operator-approved
registry: a caller can at most ask for one of the approved node IDs, never supply
a host, port, host key or SSH option, and Mode B never consults an SSH
configuration file or Tailscale.

Nothing is cached: grants, memberships and node approvals are read for every call.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from .._logging import get_logger
from ..audit import AuditLog
from ..db._transaction import as_uuid
from ..db.connection import Database
from ..db.platforms import (
    EndpointNotApprovedError,
    LoginEndpoint,
    NoLoginNodeError,
    Platform,
    PlatformError,
    PlatformLoginNode,
    PlatformNotFoundError,
    PlatformStore,
    endpoint_for,
    order_login_nodes,
)
from .policy import Decision, Principal
from .service import AccessDeniedError

logger = get_logger("nest.authz.platforms")

PLATFORM_SCOPES = frozenset({"projects:read", "jobs:run"})
"""CLI scopes that may discover Platforms; a CLI session needs any one of them."""


class PlatformResolutionError(PlatformError):
    """
    No Platform was named and none can be chosen for the caller.

    Attributes:
        reason (`str`): `none_authorized` when the caller may use no Platform, or
            `ambiguous` when several are authorized and none is the default.
    """

    def __init__(self, reason: str):
        self.reason = reason
        message = {
            "none_authorized": "No HPC Platform is available to you",
            "ambiguous": "Several HPC Platforms are available to you; name one",
        }[reason]
        super().__init__(message)


@dataclass(frozen=True)
class PlatformSummary:
    """
    What a caller may know about an authorized Platform. No host, port or host key.

    Attributes:
        platform ([`db.Platform`]): The Platform.
        transport_modes (`tuple[str, ...]`): Modes at least one active node approves.
        login_node_count (`int`): Active approved login nodes.
    """

    platform: Platform
    transport_modes: tuple[str, ...]
    login_node_count: int


class PlatformCatalog:
    """
    Authorizes Platform discovery, default resolution and endpoint selection.

    Args:
        database ([`db.Database`]): Open database.
        audit ([`audit.AuditLog`], *optional*): Where refusals are recorded.
            Defaults to one on `database`.
    """

    def __init__(self, database: Database, audit: AuditLog | None = None):
        self.database = database
        self.store = PlatformStore(database)
        self.audit = audit or AuditLog(database)

    def _denied(
        self,
        principal: Principal,
        event: str,
        reason: str,
        project_id: UUID | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        logger.warning(
            "Platform access denied user_id=%s session_kind=%s event=%s reason=%s",
            principal.user_id,
            principal.session_kind,
            event,
            reason,
        )
        self.audit.record(
            event,
            "denied",
            actor_user_id=principal.user_id,
            session_id=principal.session_id,
            project_id=project_id,
            reason=reason,
            details={"session_kind": principal.session_kind, **(details or {})},
        )

    def _check_session(self, principal: Principal, event: str) -> None:
        if principal.session_kind == "browser":
            return
        if principal.session_kind == "cli" and PLATFORM_SCOPES & principal.scopes:
            return
        reason = "scope" if principal.session_kind == "cli" else "browser_only"
        self._denied(principal, event, reason)
        raise AccessDeniedError(event, Decision(False, reason))

    def _authorized(
        self, principal: Principal, event: str, project_id: UUID | str | None
    ) -> list[Platform]:
        self._check_session(principal, event)
        pid = as_uuid(project_id, "project_id") if project_id is not None else None
        return self.store.authorized_platforms(principal.user_id, project_id=pid)

    def list_platforms(
        self, principal: Principal, *, project_id: UUID | str | None = None
    ) -> list[PlatformSummary]:
        """
        The active Platforms the caller may use, by slug: those granted to a live
        Project they are a member of (only `project_id`, when given).

        Raises:
            [`authz.AccessDeniedError`]: If the session may not discover Platforms.
        """
        summaries = []
        for platform in self._authorized(principal, "platform.list", project_id):
            nodes = self.store.login_nodes(platform.id, include_disabled=False)
            modes = sorted({mode for node in nodes for mode in node.transport_modes})
            summaries.append(PlatformSummary(platform, tuple(modes), len(nodes)))
        return summaries

    def resolve_platform(
        self,
        principal: Principal,
        reference: UUID | str | None = None,
        *,
        project_id: UUID | str | None = None,
    ) -> Platform:
        """
        The Platform a request runs on, decided by the server.

        A named Platform (UUID or slug) must be authorized for the caller. With no
        name, the server's default Platform is used if the caller is authorized for
        it; otherwise the caller's only authorized Platform; otherwise none.

        Raises:
            [`db.PlatformNotFoundError`]: If the named Platform does not exist, is
                disabled, or is not authorized for the caller (indistinguishably).
            [`authz.PlatformResolutionError`]: If none was named and none or
                several, without a default, are authorized.
            [`authz.AccessDeniedError`]: If the session may not use Platforms.
        """
        authorized = self._authorized(principal, "platform.resolve", project_id)
        if reference is not None:
            ref = str(reference).strip()
            for platform in authorized:
                if ref in (str(platform.id), platform.slug):
                    return platform
            self._denied(
                principal, "platform.resolve", "platform_not_authorized", _uuid_or_none(project_id)
            )
            raise PlatformNotFoundError("No such Platform")
        if not authorized:
            raise PlatformResolutionError("none_authorized")
        default = next((p for p in authorized if p.is_default), None)
        if default is not None:
            return default
        if len(authorized) == 1:
            return authorized[0]
        raise PlatformResolutionError("ambiguous")

    def select_login_node(
        self,
        principal: Principal,
        reference: UUID | str | None = None,
        *,
        mode: str,
        project_id: UUID | str | None = None,
        node_id: int | None = None,
        exclude: Collection[int] = (),
    ) -> LoginEndpoint:
        """
        The login node the server uses for the caller's request.

        The Platform is resolved with [`authz.PlatformCatalog.resolve_platform`].
        Without `node_id`, the first approved node for `mode` in health and priority
        order (see [`db.order_login_nodes`]) that is not in `exclude` is chosen. A
        `node_id` must be an active node of that Platform approved for `mode`.

        Raises:
            [`db.EndpointNotApprovedError`]: If `node_id` is not such a node.
            [`db.NoLoginNodeError`]: If no approved node remains.
            Everything [`authz.PlatformCatalog.resolve_platform`] raises.
        """
        platform = self.resolve_platform(principal, reference, project_id=project_id)
        nodes = self.store.login_nodes(platform.id, include_disabled=False)
        if node_id is not None:
            node = next((n for n in nodes if n.id == node_id), None)
            try:
                if node is None:
                    raise EndpointNotApprovedError(
                        "The login node is not an approved node of this Platform"
                    )
                return endpoint_for(platform, node, mode)
            except EndpointNotApprovedError:
                self._denied(
                    principal,
                    "platform.endpoint.select",
                    "endpoint_not_approved",
                    _uuid_or_none(project_id),
                    {"platform_id": str(platform.id), "node_id": node_id, "mode": mode},
                )
                raise
        return select_endpoint(platform, nodes, mode, exclude=exclude)

    def failover(
        self,
        principal: Principal,
        failed: LoginEndpoint,
        *,
        project_id: UUID | str | None = None,
        tried: Collection[int] = (),
        reason: str = "connect_failed",
    ) -> LoginEndpoint:
        """
        Records that `failed` could not be reached and returns the next approved
        node of the same Platform, in the same mode. The Platform never changes.

        Raises:
            [`db.NoLoginNodeError`]: If every approved node has been tried.
            Everything [`authz.PlatformCatalog.resolve_platform`] raises, for
                example when the caller has lost access in the meantime.
        """
        # Authorized first, so a caller can only mark nodes of its own Platforms.
        platform = self.resolve_platform(principal, failed.platform_id, project_id=project_id)
        nodes = self.store.login_nodes(platform.id, include_disabled=False)
        if any(n.id == failed.node_id for n in nodes):
            self.store.record_health(failed.node_id, "unhealthy", reason=reason)
        return select_endpoint(platform, nodes, failed.mode, exclude={failed.node_id, *tried})


def select_endpoint(
    platform: Platform,
    nodes: Collection[PlatformLoginNode],
    mode: str,
    *,
    exclude: Collection[int] = (),
) -> LoginEndpoint:
    """
    The first approved node of `platform` for `mode` not in `exclude`.

    Raises:
        [`db.NoLoginNodeError`]: If there is none.
    """
    if not platform.active:
        raise NoLoginNodeError(platform.id, mode)
    for node in order_login_nodes(nodes, mode):
        if node.platform_id == platform.id and node.id not in exclude:
            try:
                return endpoint_for(platform, node, mode)
            except EndpointNotApprovedError:
                continue
    raise NoLoginNodeError(platform.id, mode)


def _uuid_or_none(value: UUID | str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return as_uuid(value, "project_id")
    except ValueError:
        return None
