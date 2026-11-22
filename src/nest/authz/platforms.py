"""
Server-side discovery, Project eligibility, default resolution and login-node
selection for HPC Platforms (D07, Patch B06).

[`authz.PlatformCatalog`] is the one place a caller learns which Platforms exist,
which Platform a Project's request runs on and which login node the server will
use. Every interface (HTTP, CLI, SDK, gRPC and the Web UI) must go through it.
A Project's eligible Platforms are exactly the operator's grants to it; an
explicitly named Platform that is not eligible is refused, never replaced. A User sees a Platform only through an
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
from .policy import Action, Decision, Principal
from .service import AccessDeniedError, ProjectService

logger = get_logger("nest.authz.platforms")

PLATFORM_SCOPES = frozenset({"projects:read", "jobs:run"})
"""CLI scopes that may discover Platforms; a CLI session needs any one of them."""


class PlatformResolutionError(PlatformError):
    """
    No Platform was named and none can be chosen for the caller.

    Attributes:
        reason (`str`): `none_authorized` when the caller may use no Platform,
            `ambiguous` when several are authorized and none is the default, or
            `default_unavailable` when the Project's default Platform is disabled
            (the server does not silently run the Project elsewhere).
    """

    def __init__(self, reason: str):
        self.reason = reason
        message = {
            "none_authorized": "No HPC Platform is available to you",
            "ambiguous": "Several HPC Platforms are available to you; name one",
            "default_unavailable": (
                "The Project's default HPC Platform is unavailable; name another eligible "
                "Platform or ask a Project Owner or Admin to change the default"
            ),
        }[reason]
        super().__init__(message)


SELECTION_ORIGINS = ("explicit", "project_default", "server_default", "only_eligible")
"""How [`authz.PlatformCatalog.select_platform`] chose a Platform, in order."""


@dataclass(frozen=True)
class PlatformSelection:
    """
    The Platform a request runs on and why.

    Attributes:
        platform ([`db.Platform`]): The chosen, eligible Platform.
        origin (`str`): One of `SELECTION_ORIGINS`: `explicit` (named by the
            caller), `project_default` (the Project's chosen default),
            `server_default` (the operator's server-wide default) or
            `only_eligible` (the only eligible Platform).
        project_id (`UUID | None`): The Project it was resolved for, if any.
    """

    platform: Platform
    origin: str
    project_id: UUID | None = None


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


@dataclass(frozen=True)
class ProjectPlatforms:
    """
    A Project's eligible Platforms and its default, as its member may see them.

    Attributes:
        project_id (`UUID`): The Project.
        platforms (`list[PlatformSummary]`): Eligible Platforms, by slug.
        project_default_platform_id (`UUID | None`): The Project's chosen default,
            also when that Platform is currently disabled.
        selection ([`authz.PlatformSelection`] or `None`): What a request that
            names no Platform resolves to now.
        unresolved_reason (`str | None`): Why it resolves to none (see
            [`authz.PlatformResolutionError`]).
    """

    project_id: UUID
    platforms: list[PlatformSummary]
    project_default_platform_id: UUID | None
    selection: PlatformSelection | None
    unresolved_reason: str | None


class PlatformCatalog:
    """
    Authorizes Platform discovery, Project eligibility, default resolution and
    endpoint selection.

    Args:
        database ([`db.Database`]): Open database.
        audit ([`audit.AuditLog`], *optional*): Where refusals are recorded.
            Defaults to one on `database`.
        projects ([`authz.ProjectService`], *optional*): Checks Project roles for
            changing the Project default. Defaults to one on `database`.
    """

    def __init__(
        self,
        database: Database,
        audit: AuditLog | None = None,
        projects: ProjectService | None = None,
    ):
        self.database = database
        self.store = PlatformStore(database)
        self.audit = audit or AuditLog(database)
        self.projects = projects or ProjectService(database)

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
        return [
            self._summary(platform)
            for platform in self._authorized(principal, "platform.list", project_id)
        ]

    def _summary(self, platform: Platform) -> PlatformSummary:
        nodes = self.store.login_nodes(platform.id, include_disabled=False)
        modes = sorted({mode for node in nodes for mode in node.transport_modes})
        return PlatformSummary(platform, tuple(modes), len(nodes))

    def resolve_platform(
        self,
        principal: Principal,
        reference: UUID | str | None = None,
        *,
        project_id: UUID | str | None = None,
    ) -> Platform:
        """
        The Platform a request runs on, decided by the server. See
        [`authz.PlatformCatalog.select_platform`] for the order; this returns only
        the Platform. Without `project_id`, the caller's Platforms across all of
        their Projects are considered and no Project default applies.

        Raises:
            [`db.PlatformNotFoundError`]: If the named Platform does not exist, is
                disabled, or is not authorized for the caller (indistinguishably).
            [`authz.PlatformResolutionError`]: If none was named and none can be
                chosen.
            [`authz.AccessDeniedError`]: If the session may not use Platforms.
        """
        return self._select(principal, "platform.resolve", reference, project_id).platform

    def select_platform(
        self,
        principal: Principal,
        project_id: UUID | str,
        reference: UUID | str | None = None,
    ) -> PlatformSelection:
        """
        The Platform a request of `project_id` runs on, and why.

        This is the server-side policy contract every entry point uses, including
        the Controller's execution path. The caller must be an active member of the
        live Project, and only the Project's eligible Platforms (active Platforms the
        operator granted to it) are considered. The first that applies wins:

        1. `reference`, a Platform UUID or slug, which must be eligible. Anything
           else is refused as not found and audited; it never falls through.
        2. The Project's default Platform. If it is disabled, the request is
           refused (`default_unavailable`) instead of running elsewhere.
        3. The operator's server default, when it is eligible.
        4. The only eligible Platform.

        Otherwise: `none_authorized` when none is eligible, `ambiguous` when
        several are. Grants, memberships and statuses are read on every call.

        Raises:
            Everything [`authz.PlatformCatalog.resolve_platform`] raises.
        """
        return self._select(principal, "platform.resolve", reference, project_id)

    def _select(
        self,
        principal: Principal,
        event: str,
        reference: UUID | str | None,
        project_id: UUID | str | None,
        *,
        record: bool = True,
    ) -> PlatformSelection:
        self._check_session(principal, event)
        pid = as_uuid(project_id, "project_id") if project_id is not None else None
        if pid is None:
            eligible = self.store.authorized_platforms(principal.user_id)
            project_default = None
        else:
            eligible, project_default = self.store.project_platforms(principal.user_id, pid)
        if reference is not None:
            ref = str(reference).strip()
            for platform in eligible:
                if ref in (str(platform.id), platform.slug):
                    return PlatformSelection(platform, "explicit", pid)
            self._denied(principal, event, "platform_not_authorized", pid)
            raise PlatformNotFoundError("No such Platform")
        if project_default is not None:
            chosen = next((p for p in eligible if p.id == project_default), None)
            if chosen is None:
                if record:
                    self._denied(
                        principal,
                        event,
                        "default_unavailable",
                        pid,
                        {"platform_id": str(project_default)},
                    )
                raise PlatformResolutionError("default_unavailable")
            return PlatformSelection(chosen, "project_default", pid)
        if not eligible:
            raise PlatformResolutionError("none_authorized")
        default = next((p for p in eligible if p.is_default), None)
        if default is not None:
            return PlatformSelection(default, "server_default", pid)
        if len(eligible) == 1:
            return PlatformSelection(eligible[0], "only_eligible", pid)
        raise PlatformResolutionError("ambiguous")

    def authorize_platform_action(
        self,
        principal: Principal,
        action: Action | str,
        project_id: UUID | str,
        platform_id: UUID | str | None,
        *,
        details: dict[str, object] | None = None,
    ) -> Platform:
        """
        Checks that a Platform-bound operation of `project_id`, such as submitting,
        reading or cancelling a Slurm job, may use `platform_id` now.

        This is the Platform half of every such check; the Project half (the
        caller's role for `action`) is the caller's [`authz.Authorizer`] check and
        must come first, so a non-member learns nothing about the Project's
        Platforms. The Platform must be active, granted to the live Project, and
        the caller an active member of it, all read now. A Controller serving no
        Platform (`platform_id=None`) refuses every such operation: running with
        trusted Mode A credentials never skips the check.

        A refusal is recorded in the security audit log as `action` with reason
        `platform_not_authorized` and the Platform ID, and raised as not visible,
        so interfaces answer it as not found.

        Returns:
            [`db.Platform`]: The authorized Platform.

        Raises:
            [`authz.AccessDeniedError`]: If the Platform may not be used, with
                reason `platform_not_authorized` (or the session's reason).
        """
        event = str(action)
        self._check_session(principal, event)
        pid = as_uuid(project_id, "project_id")
        wanted = str(platform_id) if platform_id is not None else None
        eligible, _ = self.store.project_platforms(principal.user_id, pid)
        for platform in eligible:
            if wanted == str(platform.id):
                return platform
        self._denied(
            principal,
            event,
            "platform_not_authorized",
            pid,
            {"platform_id": wanted, **(details or {})},
        )
        raise AccessDeniedError(event, Decision(False, "platform_not_authorized"))

    def project_platforms(self, principal: Principal, project_id: UUID | str) -> ProjectPlatforms:
        """
        The Project's eligible Platforms, its default, and what a request naming
        no Platform resolves to. Any member may read them.

        Raises:
            [`authz.AccessDeniedError`]: If the caller is not a member (invisible)
                or the session lacks a `projects:read` or `jobs:run` scope.
        """
        pid = as_uuid(project_id, "project_id")
        self.projects.authorizer.require(principal, Action.PLATFORM_READ, pid)
        return self._project_view(principal, pid)

    def _project_view(self, principal: Principal, pid: UUID) -> ProjectPlatforms:
        eligible, project_default = self.store.project_platforms(principal.user_id, pid)
        try:
            selection: PlatformSelection | None = self._select(
                principal, "platform.resolve", None, pid, record=False
            )
            unresolved = None
        except PlatformResolutionError as exc:
            selection, unresolved = None, exc.reason
        return ProjectPlatforms(
            pid, [self._summary(p) for p in eligible], project_default, selection, unresolved
        )

    def set_project_default(
        self,
        principal: Principal,
        project_id: UUID | str,
        reference: UUID | str | None,
    ) -> ProjectPlatforms:
        """
        Chooses the Project's default Platform among its eligible ones, or clears
        it with `None`. Project Owners and Admins only, from a browser session.

        The Project's lock, the caller's role and the grant are read and the
        default written in one transaction, and the grant row is locked so a
        concurrent revocation either waits or wins (and then the choice is
        refused). The change is recorded in the security audit log.

        Raises:
            [`authz.AccessDeniedError`]: If the caller may not change it.
            [`db.PlatformNotFoundError`]: If `reference` is not an eligible
                Platform of the Project, whether or not it exists.
        """
        pid = as_uuid(project_id, "project_id")
        event = str(Action.PLATFORM_DEFAULT_SET)
        not_eligible = False
        with (
            self.projects._authorized(
                principal, pid, Action.PLATFORM_DEFAULT_SET, "set a Project's default Platform"
            ) as conn,
            conn.cursor() as cursor,
        ):
            if reference is None:
                cursor.execute(
                    "DELETE FROM project_default_platforms WHERE project_id = %s", (pid,)
                )
            else:
                ref = str(reference).strip()
                row = cursor.execute(
                    "SELECT g.platform_id FROM hpc_platform_project_grants g "
                    "JOIN hpc_platforms p ON p.id = g.platform_id AND p.status = 'active' "
                    "WHERE g.project_id = %s AND (p.id::text = %s OR p.slug = %s) "
                    "FOR KEY SHARE OF g",
                    (pid, ref, ref),
                ).fetchone()
                if row is None:
                    not_eligible = True
                else:
                    cursor.execute(
                        "INSERT INTO project_default_platforms "
                        "(project_id, platform_id, set_by_user_id) VALUES (%s, %s, %s) "
                        "ON CONFLICT (project_id) DO UPDATE SET "
                        "platform_id = EXCLUDED.platform_id, "
                        "set_by_user_id = EXCLUDED.set_by_user_id, set_at = now() "
                        "WHERE project_default_platforms.platform_id "
                        "IS DISTINCT FROM EXCLUDED.platform_id",
                        (pid, row[0], principal.user_id),
                    )
        if not_eligible:
            self._denied(principal, event, "platform_not_authorized", pid)
            raise PlatformNotFoundError("No such Platform")
        logger.info(
            "Project default Platform %s project_id=%s user_id=%s",
            "cleared" if reference is None else "set",
            pid,
            principal.user_id,
        )
        return self._project_view(principal, pid)

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
