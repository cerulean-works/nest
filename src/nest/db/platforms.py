"""
The HPC Platform Catalog and Login-Node Registry (D07).

An HPC Platform is one Slurm job and account namespace, such as `nano4` or `f1`,
keyed by an immutable UUID and named by a unique, immutable slug. Its operator
(such as NCHC) is optional metadata: Platforms run by the same operator are
separate Platforms, and there is no Provider parent above them. Each Platform has
one or more approved login nodes, all reaching the same namespace, so failing over
from one to another never changes which Platform a job belongs to.

Everything a client could use to reach a cluster (hosts, ports, transport modes,
host-key pin references) and every Project grant is operator managed. The schema
refuses a change that names no operator (migration `0012`), and every change is
recorded in the security audit log. [`db.PlatformStore`] is the operator-side API,
used by `nest-db platform` and the Controller's environment sync; users discover
Platforms only through [`authz.PlatformCatalog`].
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, LiteralString
from uuid import UUID

import psycopg
from psycopg import errors as pg_errors

from .._logging import get_logger
from ..config import PlatformProfile
from ._transaction import as_uuid, audit_context, transaction
from .connection import Database, DatabaseError

logger = get_logger("nest.db.platforms")

TRANSPORT_MODES = ("A", "B")
"""Login-node transport modes: `A` (trusted operator SSH) and `B` (managed SSH)."""

NODE_HEALTH = ("unknown", "healthy", "degraded", "unhealthy")
"""Login-node health states the Controller records."""

DEFAULT_SSH_PORT = 22
"""Port a Mode B endpoint uses when its login node names none."""

ENVIRONMENT_OPERATOR = "nest-controller:environment"
"""Operator name recorded for changes made by the Controller's environment sync."""

ENVIRONMENT_HOST_KEY_PIN = "env:NEST_SSH_KNOWN_HOSTS"
"""Host-key pin reference of an environment Mode B login node: the operator-vetted
`NEST_SSH_KNOWN_HOSTS` or `NEST_SSH_KNOWN_HOSTS_DATA`."""

_SLUG = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_PIN_REF = re.compile(r"[A-Za-z0-9_.:/+=@-]{1,200}")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Healthy nodes first, unhealthy last; within one health, by priority then ID.
_HEALTH_RANK = {"healthy": 0, "unknown": 1, "degraded": 2, "unhealthy": 3}
_UNSET: Any = object()

_PLATFORM_COLUMNS = (
    "id, slug, display_name, operator_name, scheduler, source, status, is_default, "
    "created_at, updated_at, disabled_at"
)
_P_PLATFORM_COLUMNS = (
    "p.id, p.slug, p.display_name, p.operator_name, p.scheduler, p.source, p.status, "
    "p.is_default, p.created_at, p.updated_at, p.disabled_at"
)
_NODE_COLUMNS = (
    "id, platform_id, host, port, transport_modes, priority, status, host_key_pin_ref, "
    "health, health_checked_at, health_reason, created_at, updated_at"
)


class PlatformError(Exception):
    """A Platform Catalog change was refused. Nothing was changed."""


class PlatformNotFoundError(PlatformError, LookupError):
    """No such Platform, or the caller is not authorized to see it."""


class DuplicatePlatformError(PlatformError):
    """The slug is already used by another Platform (slugs are never reused)."""


class PlatformSlugConflictError(PlatformError, DatabaseError):
    """
    A Platform Profile in the environment has the slug of an operator-managed
    Platform, so it cannot be mirrored and its jobs would have no Platform.

    The Controller refuses to start, writing nothing. Give the profile another ID,
    or stop managing that Platform from the environment.
    """


class LoginNodeNotFoundError(PlatformError, LookupError):
    """No such login node on the Platform."""


class DuplicateLoginNodeError(PlatformError):
    """The Platform already has a login node with this host and port."""


class EnvironmentManagedError(PlatformError):
    """The Platform is mirrored from the environment; change `NEST_PLATFORMS` instead."""


class NoLoginNodeError(PlatformError):
    """
    No approved, active login node can serve the request.

    Attributes:
        platform_id (`UUID`): The Platform.
        mode (`str`): The transport mode asked for.
    """

    def __init__(self, platform_id: UUID, mode: str):
        self.platform_id = platform_id
        self.mode = mode
        super().__init__(f"Platform {platform_id} has no approved Mode {mode} login node available")


class EndpointNotApprovedError(PlatformError):
    """A requested login node is not an approved, active node of the Platform for the mode."""


@dataclass(frozen=True)
class Platform:
    """
    One HPC Platform: a Slurm job and account namespace.

    Attributes:
        id (`UUID`): Generated, immutable Platform ID.
        slug (`str`): Unique, immutable user-facing name, such as `nano4`.
        display_name (`str`): Human-readable name.
        operator_name (`str | None`): Optional operator metadata, such as `NCHC`.
            Not a key: several Platforms may share it.
        scheduler (`str`): `slurm`.
        source (`str`): `operator` (managed with `nest-db platform`) or `env`
            (mirrored from a Provider Profile at Controller start).
        status (`str`): `active` or `disabled`.
        is_default (`bool`): Whether this is the server's default Platform.
        created_at, updated_at (`datetime`): Row timestamps.
        disabled_at (`datetime | None`): When it was disabled.
    """

    id: UUID
    slug: str
    display_name: str
    operator_name: str | None
    scheduler: str
    source: str
    status: str
    is_default: bool
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class PlatformLoginNode:
    """
    One approved login node of a Platform.

    Attributes:
        id (`int`): Node ID.
        platform_id (`UUID`): The Platform. Never changes.
        host (`str`): Host name or address.
        port (`int | None`): SSH port; `None` uses the transport's default.
        transport_modes (`tuple[str, ...]`): Approved modes, `A` and/or `B`.
        priority (`int`): Lower is tried first among nodes of the same health.
        status (`str`): `active` or `disabled` (operator approval).
        host_key_pin_ref (`str | None`): Reference to the operator-vetted host key,
            such as `SHA256:...`. Always set for Mode B nodes.
        health (`str`): One of `unknown`, `healthy`, `degraded` or `unhealthy`.
        health_checked_at (`datetime | None`): When the health was recorded.
        health_reason (`str | None`): Machine-readable reason for the health.
        created_at, updated_at (`datetime`): Row timestamps.
    """

    id: int
    platform_id: UUID
    host: str
    port: int | None
    transport_modes: tuple[str, ...]
    priority: int
    status: str
    host_key_pin_ref: str | None
    health: str
    health_checked_at: datetime | None
    health_reason: str | None
    created_at: datetime
    updated_at: datetime

    def __str__(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"


@dataclass(frozen=True)
class LoginEndpoint:
    """
    A login node the server selected for one connection.

    Built only from the registry, never from client input or an SSH configuration
    file. A Mode B endpoint always has an explicit port and host-key pin reference.

    Attributes:
        platform_id (`UUID`): The Platform. The same for every endpoint of a
            Platform, so failover keeps the Platform identity.
        platform_slug (`str`): The Platform's slug.
        node_id (`int`): The selected login node.
        host (`str`): Host to connect to.
        port (`int | None`): Port; always set in Mode B.
        mode (`str`): Transport mode, `A` or `B`.
        host_key_pin_ref (`str | None`): The node's host-key pin reference.
    """

    platform_id: UUID
    platform_slug: str
    node_id: int
    host: str
    port: int | None
    mode: str
    host_key_pin_ref: str | None


def _platform(row: Mapping[str, Any]) -> Platform:
    return Platform(**row)


def _node(row: Mapping[str, Any]) -> PlatformLoginNode:
    data: dict[str, Any] = dict(row)
    data["transport_modes"] = tuple(data["transport_modes"])
    return PlatformLoginNode(**data)


def _slug(value: str) -> str:
    slug = value.strip()
    if not _SLUG.fullmatch(slug):
        raise ValueError(
            "A Platform slug is 1 to 32 lowercase letters, digits, '_' or '-', "
            "starting with a letter"
        )
    return slug


def _text(value: str | None, name: str, *, optional: bool = False) -> str | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{name} is required")
    text = value.strip()
    if not 1 <= len(text) <= 200:
        raise ValueError(f"{name} must be 1 to 200 characters")
    return text


def _modes(modes: Iterable[str]) -> list[str]:
    result = list(dict.fromkeys(str(m).strip().upper() for m in modes if str(m).strip()))
    if not result or any(m not in TRANSPORT_MODES for m in result):
        raise ValueError("Transport modes must be A and/or B")
    return sorted(result)


def _host(value: str) -> str:
    host = value.strip()
    if not _HOST.fullmatch(host):
        raise ValueError("The login node host must be a host name or address")
    return host


def _port(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 65535:
        raise ValueError("The login node port must be 1 to 65535")
    return value


def _priority(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
        raise ValueError("The login node priority must be 0 to 10000")
    return value


def _pin(value: str | None) -> str | None:
    if value is None:
        return None
    pin = value.strip()
    if not _PIN_REF.fullmatch(pin):
        raise ValueError(
            "The host-key pin reference must be 1 to 200 characters of letters, digits "
            "and _ . : / + = @ -, such as an OpenSSH SHA256: fingerprint"
        )
    return pin


def _check_mode_b_pin(modes: Sequence[str], pin: str | None) -> None:
    if "B" in modes and pin is None:
        raise ValueError("A Mode B login node needs an operator-vetted host-key pin reference")


def order_login_nodes(nodes: Iterable[PlatformLoginNode], mode: str) -> list[PlatformLoginNode]:
    """
    The order in which the server tries a Platform's login nodes for `mode`.

    Only active nodes approved for `mode` are included. Healthy nodes come first,
    then unknown, degraded and unhealthy ones; within one health, by priority and
    then ID. Unhealthy nodes stay last-resort candidates, so a Platform whose nodes
    are all marked unhealthy can still be tried.
    """
    if mode not in TRANSPORT_MODES:
        raise ValueError("The transport mode must be A or B")
    usable = [n for n in nodes if n.status == "active" and mode in n.transport_modes]
    return sorted(usable, key=lambda n: (_HEALTH_RANK.get(n.health, 1), n.priority, n.id))


def endpoint_for(platform: Platform, node: PlatformLoginNode, mode: str) -> LoginEndpoint:
    """The [`db.LoginEndpoint`] for one approved node. Mode B always gets a port."""
    if node.platform_id != platform.id:
        raise EndpointNotApprovedError("The login node belongs to another Platform")
    if node.status != "active" or mode not in node.transport_modes:
        raise EndpointNotApprovedError("The login node is not approved for this transport mode")
    if mode == "B" and node.host_key_pin_ref is None:
        raise EndpointNotApprovedError("The Mode B login node has no host-key pin reference")
    port = node.port if node.port is not None or mode == "A" else DEFAULT_SSH_PORT
    return LoginEndpoint(
        platform_id=platform.id,
        platform_slug=platform.slug,
        node_id=node.id,
        host=node.host,
        port=port,
        mode=mode,
        host_key_pin_ref=node.host_key_pin_ref,
    )


class PlatformStore:
    """
    Operator-side API of the Platform Catalog and Login-Node Registry.

    Every change needs an `operator` (who is making it) and a `reason`, recorded in
    the security audit log with the PostgreSQL role; the schema refuses a change
    without an operator. Methods that take a Platform accept its UUID or slug.
    Platforms mirrored from the environment (`source = 'env'`) can only be granted
    to or revoked from Projects here: their login nodes follow `NEST_PLATFORMS`.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    # Reading -----------------------------------------------------------------

    @staticmethod
    def _find(
        cursor: psycopg.Cursor[dict[str, Any]], ref: UUID | str, *, lock: bool = False
    ) -> Platform:
        column, value = _platform_key(ref)
        row = cursor.execute(
            f"SELECT {_PLATFORM_COLUMNS} FROM hpc_platforms WHERE {column} = %s"
            + (" FOR NO KEY UPDATE" if lock else ""),
            (value,),
        ).fetchone()
        if row is None:
            raise PlatformNotFoundError("No such Platform")
        return _platform(row)

    def get_platform(self, ref: UUID | str) -> Platform | None:
        """Returns a Platform by UUID or slug, or `None`."""
        with transaction(self.database, None, "read a Platform") as cursor:
            try:
                return self._find(cursor, ref)
            except PlatformNotFoundError:
                return None

    def list_platforms(self, *, include_disabled: bool = True) -> list[Platform]:
        """Every Platform, by slug."""
        with transaction(self.database, None, "list Platforms") as cursor:
            rows = cursor.execute(
                f"SELECT {_PLATFORM_COLUMNS} FROM hpc_platforms"
                + ("" if include_disabled else " WHERE status = 'active'")
                + " ORDER BY slug"
            ).fetchall()
        return [_platform(r) for r in rows]

    def login_nodes(
        self, ref: UUID | str, *, include_disabled: bool = True
    ) -> list[PlatformLoginNode]:
        """A Platform's login nodes, by priority."""
        with transaction(self.database, None, "list login nodes") as cursor:
            platform = self._find(cursor, ref)
            rows = cursor.execute(
                f"SELECT {_NODE_COLUMNS} FROM hpc_login_nodes WHERE platform_id = %s"
                + ("" if include_disabled else " AND status = 'active'")
                + " ORDER BY priority, id",
                (platform.id,),
            ).fetchall()
        return [_node(r) for r in rows]

    def get_login_node(self, node_id: int) -> PlatformLoginNode | None:
        """Returns a login node by ID, or `None`."""
        with transaction(self.database, None, "read a login node") as cursor:
            row = cursor.execute(
                f"SELECT {_NODE_COLUMNS} FROM hpc_login_nodes WHERE id = %s", (node_id,)
            ).fetchone()
        return _node(row) if row else None

    def granted_projects(self, ref: UUID | str) -> list[UUID]:
        """The Projects granted on a Platform."""
        with transaction(self.database, None, "list Platform grants") as cursor:
            platform = self._find(cursor, ref)
            rows = cursor.execute(
                "SELECT project_id FROM hpc_platform_project_grants WHERE platform_id = %s "
                "ORDER BY granted_at, project_id",
                (platform.id,),
            ).fetchall()
        return [r["project_id"] for r in rows]

    def authorized_platforms(
        self, user_id: UUID | str, *, project_id: UUID | str | None = None
    ) -> list[Platform]:
        """
        The active Platforms a User may use: those granted to a live Project the
        User is an active member of (only `project_id` when given). Empty for a
        disabled or unknown User.
        """
        uid = as_uuid(user_id, "user_id")
        pid = as_uuid(project_id, "project_id") if project_id is not None else None
        with transaction(self.database, None, "list authorized Platforms") as cursor:
            rows = cursor.execute(
                f"SELECT {_P_PLATFORM_COLUMNS} FROM hpc_platforms p "
                "WHERE p.status = 'active' AND EXISTS ("
                " SELECT 1 FROM hpc_platform_project_grants g"
                " JOIN projects pr ON pr.id = g.project_id AND pr.deleted_at IS NULL"
                " JOIN project_memberships m ON m.project_id = g.project_id"
                " JOIN users u ON u.id = m.user_id AND u.status = 'active'"
                " WHERE g.platform_id = p.id AND m.user_id = %s"
                " AND (%s::uuid IS NULL OR g.project_id = %s::uuid)"
                ") ORDER BY p.slug",
                (uid, pid, pid),
            ).fetchall()
        return [_platform(r) for r in rows]

    def project_platforms(
        self, user_id: UUID | str, project_id: UUID | str
    ) -> tuple[list[Platform], UUID | None]:
        """
        A Project's eligible Platforms for a User, and the Project's default.

        Read in one statement, so a concurrent grant revocation is either wholly
        seen or not at all. The Platforms are the active Platforms granted to the
        Project, by slug, when the Project is live and the User an active member of
        it; otherwise none. The default is the Project's chosen Platform ID, also
        when that Platform is disabled (so the caller can refuse it rather than
        silently use another); `None` when there is none or the User may not see it.
        """
        uid = as_uuid(user_id, "user_id")
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "list a Project's Platforms") as cursor:
            rows = cursor.execute(
                f"SELECT {_P_PLATFORM_COLUMNS}, (d.platform_id IS NOT NULL) AS project_default "
                "FROM hpc_platform_project_grants g "
                "JOIN hpc_platforms p ON p.id = g.platform_id "
                "JOIN projects pr ON pr.id = g.project_id AND pr.deleted_at IS NULL "
                "JOIN project_memberships m ON m.project_id = g.project_id AND m.user_id = %s "
                "JOIN users u ON u.id = m.user_id AND u.status = 'active' "
                "LEFT JOIN project_default_platforms d "
                " ON d.project_id = g.project_id AND d.platform_id = g.platform_id "
                "WHERE g.project_id = %s ORDER BY p.slug",
                (uid, pid),
            ).fetchall()
        default = next((r["id"] for r in rows if r["project_default"]), None)
        platforms = [
            _platform({k: v for k, v in r.items() if k != "project_default"})
            for r in rows
            if r["status"] == "active"
        ]
        return platforms, default

    # Platforms ---------------------------------------------------------------

    def create_platform(
        self,
        slug: str,
        display_name: str,
        *,
        operator: str,
        reason: str,
        operator_name: str | None = None,
        default: bool = False,
        conn: psycopg.Connection | None = None,
    ) -> Platform:
        """
        Registers a Platform with a new immutable ID.

        Args:
            slug (`str`): Unique user-facing name, such as `nano4`. Never reusable.
            display_name (`str`): Human-readable name.
            operator (`str`): Who is making the change, for the audit log.
            reason (`str`): Why, for the audit log.
            operator_name (`str`, *optional*): Who operates the cluster, such as
                `NCHC`. Metadata only.
            default (`bool`): Make it the server's default Platform.

        Raises:
            [`db.DuplicatePlatformError`]: If the slug is taken.
            `ValueError`: If an argument is invalid.
        """
        values = (
            _slug(slug),
            _text(display_name, "display_name"),
            _text(operator_name, "operator_name", optional=True),
        )
        with (
            transaction(self.database, conn, "register a Platform") as cursor,
            audit_context(cursor, operator=operator, reason=reason),
        ):
            if default:
                cursor.execute(
                    "UPDATE hpc_platforms SET is_default = false, updated_at = now() "
                    "WHERE is_default"
                )
            try:
                with cursor.connection.transaction():
                    row = cursor.execute(
                        "INSERT INTO hpc_platforms (slug, display_name, operator_name, "
                        f"is_default) VALUES (%s, %s, %s, %s) RETURNING {_PLATFORM_COLUMNS}",
                        (*values, default),
                    ).fetchone()
            except pg_errors.UniqueViolation:
                raise DuplicatePlatformError(
                    f"The slug {values[0]!r} is already used by a Platform"
                ) from None
        assert row is not None
        platform = _platform(row)
        logger.info("Platform registered platform_id=%s slug=%s", platform.id, platform.slug)
        return platform

    def update_platform(
        self,
        ref: UUID | str,
        *,
        operator: str,
        reason: str,
        display_name: str | None = None,
        operator_name: str | None = _UNSET,
    ) -> Platform:
        """
        Changes a Platform's display name or operator metadata. Its ID and slug
        never change. Pass `operator_name=None` to clear it.

        Raises:
            [`db.PlatformNotFoundError`], [`db.EnvironmentManagedError`]
        """
        changes: dict[str, Any] = {}
        if display_name is not None:
            changes["display_name"] = _text(display_name, "display_name")
        if operator_name is not _UNSET:
            changes["operator_name"] = _text(operator_name, "operator_name", optional=True)
        with transaction(self.database, None, "change a Platform") as cursor:
            platform = self._find(cursor, ref, lock=True)
            _operator_owned(platform)
            if not changes:
                return platform
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "UPDATE hpc_platforms SET display_name = %s, operator_name = %s, "
                    f"updated_at = now() WHERE id = %s RETURNING {_PLATFORM_COLUMNS}",
                    (
                        changes.get("display_name", platform.display_name),
                        changes.get("operator_name", platform.operator_name),
                        platform.id,
                    ),
                ).fetchone()
        assert row is not None
        return _platform(row)

    def set_status(self, ref: UUID | str, status: str, *, operator: str, reason: str) -> Platform:
        """
        Enables (`active`) or disables (`disabled`) a Platform. A disabled Platform
        is never discovered or selected and stops being the default; its ID, slug
        and records are kept.
        """
        if status not in ("active", "disabled"):
            raise ValueError("status must be 'active' or 'disabled'")
        with transaction(self.database, None, "change a Platform's status") as cursor:
            platform = self._find(cursor, ref, lock=True)
            _operator_owned(platform)
            if platform.status == status:
                return platform
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "UPDATE hpc_platforms SET status = %s, "
                    "disabled_at = CASE WHEN %s = 'disabled' THEN now() END, "
                    "is_default = is_default AND %s = 'active', updated_at = now() "
                    f"WHERE id = %s RETURNING {_PLATFORM_COLUMNS}",
                    (status, status, status, platform.id),
                ).fetchone()
        assert row is not None
        logger.info("Platform status changed platform_id=%s status=%s", platform.id, status)
        return _platform(row)

    def set_default(self, ref: UUID | str | None, *, operator: str, reason: str) -> Platform | None:
        """
        Makes a Platform the server's default, or clears the default with `None`.
        The default is used only for callers authorized for it.

        Raises:
            [`db.PlatformNotFoundError`]: If there is no such Platform.
            [`db.PlatformError`]: If it is disabled.
        """
        with transaction(self.database, None, "set the default Platform") as cursor:
            platform = self._find(cursor, ref, lock=True) if ref is not None else None
            if platform is not None and not platform.active:
                raise PlatformError("A disabled Platform cannot be the default")
            with audit_context(cursor, operator=operator, reason=reason):
                cursor.execute(
                    "UPDATE hpc_platforms SET is_default = false, updated_at = now() "
                    "WHERE is_default AND id IS DISTINCT FROM %s",
                    (platform.id if platform else None,),
                )
                if platform is None:
                    return None
                row = cursor.execute(
                    "UPDATE hpc_platforms SET is_default = true, updated_at = now() "
                    f"WHERE id = %s RETURNING {_PLATFORM_COLUMNS}",
                    (platform.id,),
                ).fetchone()
        assert row is not None
        return _platform(row)

    def default_platform(self) -> Platform | None:
        """The server's default Platform, or `None`."""
        with transaction(self.database, None, "read the default Platform") as cursor:
            row = cursor.execute(
                f"SELECT {_PLATFORM_COLUMNS} FROM hpc_platforms WHERE is_default"
            ).fetchone()
        return _platform(row) if row else None

    # Login nodes ---------------------------------------------------------------

    def add_login_node(
        self,
        ref: UUID | str,
        host: str,
        *,
        transport_modes: Iterable[str],
        operator: str,
        reason: str,
        port: int | None = None,
        priority: int = 100,
        host_key_pin_ref: str | None = None,
    ) -> PlatformLoginNode:
        """
        Approves a login node for a Platform.

        Args:
            ref (`UUID | str`): The Platform.
            host (`str`): Host name or address.
            transport_modes (`Iterable[str]`): `A` and/or `B`.
            port (`int`, *optional*): SSH port; `None` uses the transport's default.
            priority (`int`): Lower is tried first, 0 to 10000.
            host_key_pin_ref (`str`, *optional*): Reference to the operator-vetted
                host key. Required for Mode B.

        Raises:
            [`db.DuplicateLoginNodeError`]: If the Platform has this host and port.
            [`db.EnvironmentManagedError`]: If the Platform follows the environment.
            `ValueError`: If an argument is invalid.
        """
        values = (_host(host), _port(port), _modes(transport_modes), _priority(priority))
        pin = _pin(host_key_pin_ref)
        _check_mode_b_pin(values[2], pin)
        with transaction(self.database, None, "approve a login node") as cursor:
            platform = self._find(cursor, ref, lock=True)
            _operator_owned(platform)
            with audit_context(cursor, operator=operator, reason=reason):
                row = self._insert_node(cursor, platform.id, *values, pin)
        node = _node(row)
        logger.info(
            "Login node approved platform_id=%s node_id=%s modes=%s",
            platform.id,
            node.id,
            ",".join(node.transport_modes),
        )
        return node

    @staticmethod
    def _insert_node(
        cursor: psycopg.Cursor[dict[str, Any]],
        platform_id: UUID,
        host: str,
        port: int | None,
        modes: list[str],
        priority: int,
        pin: str | None,
    ) -> dict[str, Any]:
        try:
            with cursor.connection.transaction():
                row = cursor.execute(
                    "INSERT INTO hpc_login_nodes (platform_id, host, port, transport_modes, "
                    "priority, host_key_pin_ref) VALUES (%s, %s, %s, %s, %s, %s) "
                    f"RETURNING {_NODE_COLUMNS}",
                    (platform_id, host, port, modes, priority, pin),
                ).fetchone()
        except pg_errors.UniqueViolation:
            raise DuplicateLoginNodeError(
                "The Platform already has a login node with this host and port"
            ) from None
        assert row is not None
        return row

    def update_login_node(
        self,
        node_id: int,
        *,
        operator: str,
        reason: str,
        transport_modes: Iterable[str] | None = None,
        priority: int | None = None,
        status: str | None = None,
        host_key_pin_ref: str | None = _UNSET,
        port: int | None = _UNSET,
    ) -> PlatformLoginNode:
        """
        Changes an approved login node. The node never moves to another Platform.
        Pass `host_key_pin_ref=None` or `port=None` to clear them.

        Raises:
            [`db.LoginNodeNotFoundError`], [`db.EnvironmentManagedError`],
            [`db.DuplicateLoginNodeError`], `ValueError`
        """
        if status is not None and status not in ("active", "disabled"):
            raise ValueError("status must be 'active' or 'disabled'")
        with transaction(self.database, None, "change a login node") as cursor:
            node, platform = self._lock_node(cursor, node_id)
            _operator_owned(platform)
            modes = _modes(transport_modes) if transport_modes is not None else node.transport_modes
            pin = (
                _pin(host_key_pin_ref) if host_key_pin_ref is not _UNSET else node.host_key_pin_ref
            )
            new_port = _port(port) if port is not _UNSET else node.port
            _check_mode_b_pin(modes, pin)
            with audit_context(cursor, operator=operator, reason=reason):
                try:
                    with cursor.connection.transaction():
                        row = cursor.execute(
                            "UPDATE hpc_login_nodes SET transport_modes = %s, priority = %s, "
                            "status = %s, host_key_pin_ref = %s, port = %s, updated_at = now() "
                            f"WHERE id = %s RETURNING {_NODE_COLUMNS}",
                            (
                                list(modes),
                                _priority(priority) if priority is not None else node.priority,
                                status or node.status,
                                pin,
                                new_port,
                                node.id,
                            ),
                        ).fetchone()
                except pg_errors.UniqueViolation:
                    raise DuplicateLoginNodeError(
                        "The Platform already has a login node with this host and port"
                    ) from None
        assert row is not None
        return _node(row)

    def remove_login_node(self, node_id: int, *, operator: str, reason: str) -> None:
        """
        Withdraws a login node's approval by removing it. The Platform and its
        other nodes are unchanged; jobs keep their Platform.

        Raises:
            [`db.LoginNodeNotFoundError`], [`db.EnvironmentManagedError`]
        """
        with transaction(self.database, None, "remove a login node") as cursor:
            node, platform = self._lock_node(cursor, node_id)
            _operator_owned(platform)
            with audit_context(cursor, operator=operator, reason=reason):
                cursor.execute("DELETE FROM hpc_login_nodes WHERE id = %s", (node.id,))
        logger.info("Login node removed platform_id=%s node_id=%s", platform.id, node.id)

    def _lock_node(
        self, cursor: psycopg.Cursor[dict[str, Any]], node_id: int
    ) -> tuple[PlatformLoginNode, Platform]:
        if isinstance(node_id, bool) or not isinstance(node_id, int):
            raise TypeError("node_id must be an integer")
        row = cursor.execute(
            f"SELECT {_NODE_COLUMNS} FROM hpc_login_nodes WHERE id = %s FOR UPDATE", (node_id,)
        ).fetchone()
        if row is None:
            raise LoginNodeNotFoundError("No such login node")
        node = _node(row)
        return node, self._find(cursor, node.platform_id)

    def record_health(self, node_id: int, health: str, *, reason: str | None = None) -> None:
        """
        Records a login node's observed health. Needs no operator: health is an
        observation, not an approval, and changes no endpoint.
        """
        if health not in NODE_HEALTH:
            raise ValueError(f"health must be one of {NODE_HEALTH}")
        if reason is not None and not _REASON.fullmatch(reason):
            raise ValueError("reason must be a lowercase machine-readable word")
        with transaction(self.database, None, "record login node health") as cursor:
            cursor.execute(
                "UPDATE hpc_login_nodes SET health = %s, health_reason = %s, "
                "health_checked_at = now(), updated_at = now() WHERE id = %s",
                (health, reason, node_id),
            )

    # Project grants ------------------------------------------------------------

    def grant_project(
        self, ref: UUID | str, project_id: UUID | str, *, operator: str, reason: str
    ) -> bool:
        """
        Lets a Project's members discover and use a Platform. Returns `False` when
        the grant already exists.

        Raises:
            [`db.PlatformNotFoundError`]: If there is no such Platform.
            [`db.ProjectNotFoundError`]: If the Project does not exist or was deleted.
        """
        from .projects import ProjectNotFoundError

        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "grant a Platform") as cursor:
            platform = self._find(cursor, ref, lock=True)
            if (
                cursor.execute(
                    "SELECT 1 FROM projects WHERE id = %s AND deleted_at IS NULL", (pid,)
                ).fetchone()
                is None
            ):
                raise ProjectNotFoundError("The Project does not exist or was deleted")
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "INSERT INTO hpc_platform_project_grants (platform_id, project_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING project_id",
                    (platform.id, pid),
                ).fetchone()
        return row is not None

    def revoke_project(
        self, ref: UUID | str, project_id: UUID | str, *, operator: str, reason: str
    ) -> bool:
        """Withdraws a Project's grant. Returns `False` when there was none."""
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "revoke a Platform grant") as cursor:
            platform = self._find(cursor, ref, lock=True)
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "DELETE FROM hpc_platform_project_grants "
                    "WHERE platform_id = %s AND project_id = %s RETURNING project_id",
                    (platform.id, pid),
                ).fetchone()
        return row is not None

    # Environment bootstrap -----------------------------------------------------

    def sync_environment(self, providers: Mapping[str, PlatformProfile]) -> dict[str, UUID]:
        """
        Mirrors the environment's Platform Profiles (`NEST_PLATFORMS`, or the
        deprecated `NEST_PROVIDERS` and `NEST_SLURM_*`) in the catalog, in one
        transaction.

        Each profile becomes (or stays) the `env` Platform whose slug is the
        profile's ID, keeping its immutable Platform ID across restarts. Its display
        name and operator label follow `NEST_PLATFORM_<ID>_NAME` and `_OPERATOR`; a
        profile without an operator label keeps the recorded one. Its login
        nodes are rewritten to the profile's, in order of priority, approved for the
        profile's SSH mode; a Mode B node's host-key pin reference is the
        operator-vetted `NEST_SSH_KNOWN_HOSTS` (`env:NEST_SSH_KNOWN_HOSTS`).
        Platforms of profiles no longer configured are disabled. Project grants and
        the default Platform are never changed here.

        Returns:
            `dict[str, UUID]`: Platform ID of each profile, by profile ID.

        Raises:
            [`db.PlatformSlugConflictError`]: If an operator-managed Platform has a
                profile's slug. Nothing is written.
        """
        mirrored: dict[str, UUID] = {}
        with (
            transaction(self.database, None, "record environment Platforms") as cursor,
            audit_context(
                cursor, operator=ENVIRONMENT_OPERATOR, reason="NEST_PLATFORMS at Controller start"
            ),
        ):
            for profile in providers.values():
                mirrored[profile.id] = self._sync_profile(cursor, profile)
            cursor.execute(
                "UPDATE hpc_platforms SET status = 'disabled', disabled_at = now(), "
                "is_default = false, updated_at = now() "
                "WHERE source = 'env' AND status = 'active' AND NOT (slug = ANY(%s))",
                (list(mirrored),),
            )
        logger.info("Environment Platforms recorded platforms=%s", ",".join(mirrored) or "-")
        return mirrored

    def _sync_profile(
        self, cursor: psycopg.Cursor[dict[str, Any]], profile: PlatformProfile
    ) -> UUID:
        row = cursor.execute(
            "SELECT id, source FROM hpc_platforms WHERE slug = %s FOR NO KEY UPDATE",
            (profile.id,),
        ).fetchone()
        if row is not None and row["source"] != "env":
            raise PlatformSlugConflictError(
                f"Platform Profile {profile.id!r} cannot be mirrored: an operator-managed "
                "Platform already has that slug, so its jobs would have no Platform. Rename "
                "the profile in NEST_PLATFORMS, or remove it and submit to the operator's "
                "Platform"
            )
        display_name = profile.display_name or profile.id
        if row is None:
            row = cursor.execute(
                "INSERT INTO hpc_platforms (slug, display_name, operator_name, source) "
                "VALUES (%s, %s, %s, 'env') RETURNING id",
                (profile.id, display_name, profile.operator),
            ).fetchone()
            assert row is not None
        else:
            # A profile without an operator label keeps the recorded one, as
            # sync_platforms does for provider_profiles.
            cursor.execute(
                "UPDATE hpc_platforms SET status = 'active', disabled_at = NULL, "
                "display_name = %s, operator_name = COALESCE(%s, operator_name), "
                "updated_at = now() WHERE id = %s AND (status <> 'active' "
                "OR display_name IS DISTINCT FROM %s "
                "OR operator_name IS DISTINCT FROM COALESCE(%s, operator_name))",
                (display_name, profile.operator, row["id"], display_name, profile.operator),
            )
        platform_id: UUID = row["id"]
        pin = ENVIRONMENT_HOST_KEY_PIN if profile.ssh_mode == "B" else None
        wanted = {
            (node.host, node.port): index * 10 for index, node in enumerate(profile.login_nodes)
        }
        existing = cursor.execute(
            f"SELECT {_NODE_COLUMNS} FROM hpc_login_nodes WHERE platform_id = %s",
            (platform_id,),
        ).fetchall()
        for node in map(_node, existing):
            priority = wanted.pop((node.host, node.port), None)
            if priority is None:
                cursor.execute("DELETE FROM hpc_login_nodes WHERE id = %s", (node.id,))
            elif (node.transport_modes, node.priority, node.status, node.host_key_pin_ref) != (
                (profile.ssh_mode,),
                priority,
                "active",
                pin,
            ):
                cursor.execute(
                    "UPDATE hpc_login_nodes SET transport_modes = %s, priority = %s, "
                    "status = 'active', host_key_pin_ref = %s, updated_at = now() WHERE id = %s",
                    ([profile.ssh_mode], priority, pin, node.id),
                )
        for (host, port), priority in wanted.items():
            self._insert_node(cursor, platform_id, host, port, [profile.ssh_mode], priority, pin)
        return platform_id


def _platform_key(ref: UUID | str) -> tuple[LiteralString, object]:
    """The column and value that find a Platform by UUID or slug."""
    if isinstance(ref, UUID):
        return "id", ref
    text = str(ref).strip()
    try:
        return "id", UUID(text)
    except ValueError:
        pass
    if not _SLUG.fullmatch(text):
        raise PlatformNotFoundError("No such Platform")
    return "slug", text


def _operator_owned(platform: Platform) -> None:
    if platform.source == "env":
        raise EnvironmentManagedError(
            f"Platform {platform.slug!r} is mirrored from NEST_PLATFORMS; change the "
            "environment and restart the Controller instead"
        )
