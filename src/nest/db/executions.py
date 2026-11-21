"""
Platform-aware durable execution registry.

A logical execution has a stable Nest execution ID, an owner and an immutable
Project, and one record per submission attempt. Each attempt pins the Platform it
targets, a snapshot of the selected login-node endpoint and transport, the HPC
binding and effective UNIX identity, the `(platform_id, slurm_account)`
entitlement, and every resolved configuration option with its provenance.

Durability comes first: [`db.ExecutionRegistry.register`] and
[`db.ExecutionRegistry.begin_attempt`] commit before they return, so the caller
runs `sbatch` only once the attempt it is about to make is on disk. After a
Controller restart, [`db.ExecutionRegistry.live_attempts`] lists the attempts that
were in flight, and [`db.ExecutionRegistry.mark_interrupted`] turns those that
never received a Slurm job ID into `unknown`, which blocks a blind resubmission
until someone reconciles them.

A Slurm job ID, a Slurm account and a node name are never global identifiers:
every lookup by one also names the Platform. Who may read an execution is decided
by the authorization layer; every read method takes the caller's Project, so a
lookup can never cross a Project boundary. The only exceptions are the recovery
methods [`db.ExecutionRegistry.live_attempts`] and
[`db.ExecutionRegistry.mark_interrupted`], which serve the Controller's own
restart reconciliation across all Projects and must never back a User-facing API.

Platforms and login nodes come from the HPC Platform Catalog (D07): an attempt
takes the [`db.LoginEndpoint`] the server selected, and is recorded only while that
Platform is active and granted to the execution's Project and the node is still
approved for the transport mode, checked in the same transaction. The account it
charges comes from the Platform-scoped Slurm Account Registry (D08), as chosen by
[`authz.AccountResolver`], and must still be linked to the Project for that mode.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, LiteralString
from uuid import UUID

from psycopg.types.json import Jsonb

from .._logging import get_logger
from ..backends.slurm import new_execution_id
from ._transaction import as_uuid, transaction
from .connection import Database
from .platforms import DEFAULT_SSH_PORT, EndpointNotApprovedError, LoginEndpoint

logger = get_logger("nest.db.executions")

EXECUTION_STATUSES = ("pending", "active", "succeeded", "failed", "cancelled")
"""Logical execution states. `succeeded` and `cancelled` are final."""

ATTEMPT_STATUSES = (
    "submitting",
    "submitted",
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "unknown",
)
"""Attempt states. `succeeded`, `failed` and `cancelled` are final."""

LIVE_ATTEMPT_STATUSES = ("submitting", "submitted", "running", "unknown")
"""Attempt states that hold an execution: no other attempt may start meanwhile."""

FINAL_ATTEMPT_STATUSES = ("succeeded", "failed", "cancelled")

# Attempt states Slurm must have accepted, so they always carry a Slurm job ID.
_NEEDS_SLURM_JOB = frozenset({"submitted", "running", "succeeded"})

_TRANSITIONS: dict[str, frozenset[str]] = {
    "submitting": frozenset({"submitted", "failed", "cancelled", "unknown"}),
    "submitted": frozenset({"running", "succeeded", "failed", "cancelled", "unknown"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "unknown"}),
    "unknown": frozenset({"submitted", "running", "succeeded", "failed", "cancelled"}),
}
_EXECUTION_AFTER_ATTEMPT = {"succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled"}
_MAX_ERROR_MESSAGE = 4000

_EXECUTION_COLUMNS = (
    "execution_id, project_id, owner_user_id, kind, idempotency_key, request_digest, "
    "requested_options, status, created_at, updated_at, finished_at"
)
_ATTEMPT_COLUMNS = (
    "a.id, a.execution_id, a.attempt_number, a.platform_id, p.slug AS platform_slug, "
    "a.login_node_id, a.login_node_host, a.login_node_port, a.transport_mode, "
    "a.host_key_pin_ref, a.hpc_binding_id, "
    "a.hpc_binding_version, a.unix_user, a.unix_uid, a.slurm_account_id, a.slurm_account, "
    "a.slurm_account_origin, a.source_digest, "
    "a.environment_digest, a.runtime_digest, a.status, a.slurm_job_id, a.slurm_state, "
    "a.output_ref, a.usage_ref, a.error_kind, a.error_message, a.created_at, a.updated_at, "
    "a.submitted_at, a.finished_at"
)
_ATTEMPT_FROM = "execution_attempts a JOIN hpc_platforms p ON p.id = a.platform_id"
# Restricts an attempt query to one Project; its parameter comes first.
_IN_PROJECT = "JOIN executions e ON e.execution_id = a.execution_id AND e.project_id = %s"


class ExecutionRegistryError(Exception):
    """An execution registry change was refused. Nothing was changed."""


class ExecutionNotFoundError(ExecutionRegistryError):
    """The execution or attempt does not exist (in the given Project)."""


class IdempotencyConflictError(ExecutionRegistryError):
    """The idempotency key was already used for a different request."""


class AttemptInFlightError(ExecutionRegistryError):
    """The execution already has an attempt in flight, possibly with an unknown outcome."""


class ExecutionClosedError(ExecutionRegistryError):
    """The execution already succeeded or was cancelled, so no new attempt may start."""


class InvalidTransitionError(ExecutionRegistryError):
    """The attempt cannot move to the requested status from its current one."""


class PlatformUnavailableError(ExecutionRegistryError):
    """The Platform is unknown, disabled, or not granted to the execution's Project."""


class AccountNotPermittedError(ExecutionRegistryError):
    """The Slurm account is not an active account linked to the Project for the mode."""


@dataclass(frozen=True)
class HPCPrincipal:
    """
    The HPC identity an attempt ran as on its Platform.

    Attributes:
        unix_user (`str`): Effective UNIX user name.
        unix_uid (`int`, *optional*): Effective UNIX user ID, when known.
        binding_id (`str`, *optional*): The HPC binding that mapped the Nest User
            to this identity.
        binding_version (`int`, *optional*): The binding's version at submission.
            Given together with `binding_id`.
    """

    unix_user: str
    unix_uid: int | None = None
    binding_id: str | None = None
    binding_version: int | None = None


@dataclass(frozen=True)
class ResolvedOption:
    """
    One configuration option as an attempt resolved it.

    Attributes:
        value (`Any`): The JSON-serializable value used.
        origin (`str`): `request` when the caller set it explicitly, `default` when
            a default supplied it.
        default_value (`Any`, *optional*): The default that applied, or that the
            request overrode.
        default_source (`str`, *optional*): The layer that supplied the default,
            such as `platform`, `project` or `nest`. Required with `default_value`.
    """

    value: Any
    origin: Literal["request", "default"]
    default_value: Any = None
    default_source: str | None = None

    @classmethod
    def requested(
        cls, value: Any, *, default: Any = None, default_source: str | None = None
    ) -> ResolvedOption:
        """An option set explicitly, optionally recording the default it overrode."""
        return cls(value, "request", default, default_source)

    @classmethod
    def defaulted(cls, value: Any, source: str) -> ResolvedOption:
        """An option the caller left unset, supplied by the `source` layer's default."""
        return cls(value, "default", value, source)


@dataclass(frozen=True)
class Execution:
    """
    A logical execution. Its identity fields never change.

    Attributes:
        execution_id (`str`): Stable Nest execution ID (32 lowercase hex characters).
        project_id (`UUID`): The Project it runs in.
        owner_user_id (`UUID`): The Nest User who owns it.
        kind (`str`): `execution` or `batch`.
        idempotency_key (`str | None`): The caller's idempotency key.
        request_digest (`bytes`): SHA-256 of the canonical request.
        requested_options (`dict`): Options exactly as requested.
        status (`str`): One of [`db.executions.EXECUTION_STATUSES`].
        created_at, updated_at (`datetime`): Row timestamps.
        finished_at (`datetime | None`): When it succeeded or was cancelled.
    """

    execution_id: str
    project_id: UUID
    owner_user_id: UUID
    kind: str
    idempotency_key: str | None
    request_digest: bytes
    requested_options: dict[str, Any]
    status: str
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class Attempt:
    """
    One submission attempt of an execution.

    Attributes:
        id (`int`): Attempt record ID.
        execution_id (`str`): The execution.
        attempt_number (`int`): 1 for the first attempt, then increasing.
        platform_id (`UUID`): The Platform (Slurm namespace) it targeted.
        endpoint (`LoginEndpoint`): Snapshot of the login-node endpoint used.
        principal (`HPCPrincipal`): The HPC identity used.
        slurm_account_id (`int | None`): The registered Slurm account on
            `platform_id`; `None` only for `native_default`.
        slurm_account (`str | None`): Its name, passed as `--account`.
        slurm_account_origin (`str`): The account selection step that chose it.
        source_digest, environment_digest, runtime_digest (`str | None`): Digests.
        status (`str`): One of [`db.executions.ATTEMPT_STATUSES`].
        slurm_job_id (`int | None`): Slurm job ID on `platform_id`.
        slurm_state (`str | None`): Last Slurm state observed.
        output_ref, usage_ref (`str | None`): Where the output and usage records are.
        error_kind, error_message (`str | None`): Failure details.
        nodes (`tuple[str, ...]`): Allocated compute nodes, sorted.
        options (`dict[str, ResolvedOption]`): Resolved options by name.
        created_at, updated_at, submitted_at, finished_at: Timestamps.
    """

    id: int
    execution_id: str
    attempt_number: int
    platform_id: UUID
    endpoint: LoginEndpoint
    principal: HPCPrincipal
    slurm_account_id: int | None
    slurm_account: str | None
    slurm_account_origin: str
    source_digest: str | None
    environment_digest: str | None
    runtime_digest: str | None
    status: str
    slurm_job_id: int | None
    slurm_state: str | None
    output_ref: str | None
    usage_ref: str | None
    error_kind: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    finished_at: datetime | None
    nodes: tuple[str, ...] = ()
    options: dict[str, ResolvedOption] = field(default_factory=dict)


def request_digest(kind: str, requested_options: Mapping[str, Any]) -> bytes:
    """Returns the SHA-256 of the canonical JSON of a request's kind and options."""
    canonical = json.dumps(
        {"kind": kind, "options": dict(requested_options)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).digest()


def _execution(row: dict[str, Any]) -> Execution:
    return Execution(
        execution_id=row["execution_id"],
        project_id=row["project_id"],
        owner_user_id=row["owner_user_id"],
        kind=row["kind"],
        idempotency_key=row["idempotency_key"],
        request_digest=bytes(row["request_digest"]),
        requested_options=row["requested_options"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


def _attempt(
    row: dict[str, Any], nodes: Iterable[str] = (), options: Mapping[str, ResolvedOption] = {}
) -> Attempt:
    return Attempt(
        id=row["id"],
        execution_id=row["execution_id"],
        attempt_number=row["attempt_number"],
        platform_id=row["platform_id"],
        endpoint=LoginEndpoint(
            platform_id=row["platform_id"],
            platform_slug=row["platform_slug"],
            node_id=row["login_node_id"],
            host=row["login_node_host"],
            port=row["login_node_port"],
            mode=row["transport_mode"],
            host_key_pin_ref=row["host_key_pin_ref"],
        ),
        principal=HPCPrincipal(
            unix_user=row["unix_user"],
            unix_uid=row["unix_uid"],
            binding_id=row["hpc_binding_id"],
            binding_version=row["hpc_binding_version"],
        ),
        slurm_account_id=row["slurm_account_id"],
        slurm_account=row["slurm_account"],
        slurm_account_origin=row["slurm_account_origin"],
        source_digest=row["source_digest"],
        environment_digest=row["environment_digest"],
        runtime_digest=row["runtime_digest"],
        status=row["status"],
        slurm_job_id=row["slurm_job_id"],
        slurm_state=row["slurm_state"],
        output_ref=row["output_ref"],
        usage_ref=row["usage_ref"],
        error_kind=row["error_kind"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        submitted_at=row["submitted_at"],
        finished_at=row["finished_at"],
        nodes=tuple(sorted(nodes)),
        options=dict(options),
    )


def _check_endpoint(cursor: Any, endpoint: LoginEndpoint, project_id: UUID) -> None:
    """Re-checks a selected endpoint against the catalog, share-locking what it read."""
    platform = cursor.execute(
        "SELECT p.slug FROM hpc_platforms p "
        "JOIN hpc_platform_project_grants g ON g.platform_id = p.id AND g.project_id = %s "
        "WHERE p.id = %s AND p.status = 'active' FOR SHARE",
        (project_id, endpoint.platform_id),
    ).fetchone()
    if platform is None:
        raise PlatformUnavailableError("The Platform is not available to this Project")
    node = cursor.execute(
        "SELECT host, port, transport_modes, host_key_pin_ref FROM hpc_login_nodes "
        "WHERE id = %s AND platform_id = %s AND status = 'active' FOR SHARE",
        (endpoint.node_id, endpoint.platform_id),
    ).fetchone()
    expected_port = node and (
        node["port"] if node["port"] is not None or endpoint.mode == "A" else DEFAULT_SSH_PORT
    )
    if (
        node is None
        or endpoint.mode not in node["transport_modes"]
        or node["host"] != endpoint.host
        or expected_port != endpoint.port
        or node["host_key_pin_ref"] != endpoint.host_key_pin_ref
        or platform["slug"] != endpoint.platform_slug
    ):
        raise EndpointNotApprovedError(
            "The login node is not an approved endpoint of the Platform for this transport mode"
        )


def _check_account(
    cursor: Any,
    endpoint: LoginEndpoint,
    project_id: UUID,
    account: str | None,
    slurm_account_id: int | None,
    origin: str,
) -> None:
    """Re-checks the account choice against the Project's links, share-locking them."""
    if origin == "native_default":
        if account is not None or slurm_account_id is not None:
            raise ValueError("The native Slurm default names no account")
        allowed = cursor.execute(
            "SELECT allow_native_default FROM project_platform_account_policies "
            "WHERE project_id = %s AND platform_id = %s FOR SHARE",
            (project_id, endpoint.platform_id),
        ).fetchone()
        if endpoint.mode != "B" or allowed is None or not allowed["allow_native_default"]:
            raise AccountNotPermittedError(
                "The native Slurm default is not permitted for this Project and mode"
            )
        return
    if slurm_account_id is None or account is None:
        raise ValueError("Every account origin but native_default names a registered account")
    link = cursor.execute(
        "SELECT a.account_name, a.kind, l.modes FROM project_platform_accounts l "
        "JOIN slurm_accounts a ON a.id = l.slurm_account_id AND a.platform_id = l.platform_id "
        "WHERE l.project_id = %s AND l.platform_id = %s AND l.slurm_account_id = %s "
        "AND a.status = 'active' FOR SHARE",
        (project_id, endpoint.platform_id, slurm_account_id),
    ).fetchone()
    if (
        link is None
        or link["account_name"] != account
        or endpoint.mode not in link["modes"]
        or (endpoint.mode == "B" and link["kind"] == "chargeback")
    ):
        raise AccountNotPermittedError(
            "The Slurm account is not linked to this Project for this mode on the Platform"
        )


def _jsonb(value: Any) -> Jsonb:
    # Reject values PostgreSQL would store differently from what was given.
    json.dumps(value, allow_nan=False)
    return Jsonb(value)


class ExecutionRegistry:
    """
    Stores logical executions and their attempts.

    Every method runs in one transaction. Methods that change an execution first
    lock its row, so concurrent attempts, retries and duplicate registrations for
    one execution apply one after another. The database enforces the same rules
    on its own (one live attempt per execution, allowed status transitions,
    immutable identity and history), whoever writes.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    # Registration -------------------------------------------------------------

    def register(
        self,
        project_id: UUID | str,
        owner_user_id: UUID | str,
        kind: str,
        requested_options: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        execution_id: str | None = None,
    ) -> tuple[Execution, bool]:
        """
        Registers a logical execution, or returns the one already registered.

        With an `idempotency_key`, a repeated call for the same Project, owner and
        key returns the existing execution, provided the request is identical.

        Args:
            project_id (`UUID`): The Project. Never changes afterwards.
            owner_user_id (`UUID`): The owning Nest User. Never changes afterwards.
            kind (`str`): `execution` or `batch`.
            requested_options (`Mapping[str, Any]`): Options as the caller sent them.
            idempotency_key (`str`, *optional*): Printable ASCII, 1 to 200 characters.
            execution_id (`str`, *optional*): A pre-generated execution ID; one is
                generated when omitted.

        Returns:
            `tuple[Execution, bool]`: The execution, and whether this call created it.

        Raises:
            [`db.executions.IdempotencyConflictError`]: If the key was used for a
                different request.
            [`db.DatabaseError`]: If the database refuses the row.
        """
        project = as_uuid(project_id, "project_id")
        owner = as_uuid(owner_user_id, "owner_user_id")
        if kind not in ("execution", "batch"):
            raise ValueError(f"Unknown execution kind {kind!r}")
        digest = request_digest(kind, requested_options)
        new_id = execution_id or new_execution_id()
        with transaction(self.database, None, "register an execution") as cursor:
            row = cursor.execute(
                f"""
                INSERT INTO executions (execution_id, project_id, owner_user_id, kind,
                    idempotency_key, request_digest, requested_options)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, owner_user_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL DO NOTHING
                RETURNING {_EXECUTION_COLUMNS}
                """,
                (
                    new_id,
                    project,
                    owner,
                    kind,
                    idempotency_key,
                    digest,
                    _jsonb(dict(requested_options)),
                ),
            ).fetchone()
            if row is not None:
                created = _execution(row)
                logger.info(
                    "Execution registered execution_id=%s project_id=%s kind=%s",
                    created.execution_id,
                    project,
                    kind,
                )
                return created, True
            existing = cursor.execute(
                f"SELECT {_EXECUTION_COLUMNS} FROM executions "
                "WHERE project_id = %s AND owner_user_id = %s AND idempotency_key = %s",
                (project, owner, idempotency_key),
            ).fetchone()
        assert existing is not None
        found = _execution(existing)
        if found.request_digest != digest:
            raise IdempotencyConflictError(
                "The idempotency key was already used for a different request"
            )
        return found, False

    # Attempts -----------------------------------------------------------------

    def begin_attempt(
        self,
        execution_id: str,
        *,
        endpoint: LoginEndpoint,
        principal: HPCPrincipal,
        account: str | None,
        slurm_account_id: int | None,
        slurm_account_origin: str,
        options: Mapping[str, ResolvedOption],
        source_digest: str | None = None,
        environment_digest: str | None = None,
        runtime_digest: str | None = None,
    ) -> Attempt:
        """
        Durably records a new `submitting` attempt before anything is submitted.

        The attempt, its resolved options and the execution's `active` status are
        committed together before this returns, so the caller may then run the
        irreversible submission. In the same transaction, the endpoint's Platform
        must be active and granted to the execution's Project, and its login node
        must still be approved for the transport mode with the same host, port and
        host-key pin reference. Those rows are share-locked until commit, so a
        concurrent revocation or disablement either waits or wins outright.

        Args:
            execution_id (`str`): The registered execution.
            endpoint ([`db.LoginEndpoint`]): The login node the server selected,
                such as from [`authz.PlatformCatalog.select_login_node`].
            principal (`HPCPrincipal`): The HPC identity it will run as.
            account, slurm_account_id, slurm_account_origin: The charged account, as
                [`authz.AccountResolution.job_fields`] gives them (pass
                `**resolution.job_fields()`). The account must still be active and
                linked to the Project on the endpoint's Platform for its mode;
                `native_default` needs Mode B and the Project's permission.
            options (`Mapping[str, ResolvedOption]`): Every resolved option.
            source_digest, environment_digest, runtime_digest (`str`, *optional*):
                `sha256:<hex>` digests of what is submitted.

        Raises:
            [`db.executions.ExecutionNotFoundError`]: If the execution does not exist.
            [`db.executions.ExecutionClosedError`]: If it succeeded or was cancelled.
            [`db.executions.AttemptInFlightError`]: If an attempt is still in flight
                or has an unknown outcome.
            [`db.executions.PlatformUnavailableError`]: If the Platform is unknown,
                disabled or not granted to the execution's Project.
            [`db.executions.AccountNotPermittedError`]: If the account may not be
                charged by the Project in the endpoint's mode.
            [`db.EndpointNotApprovedError`]: If the login node no longer matches an
                approved endpoint of the Platform.
            [`db.DatabaseError`]: If the database refuses a row.
        """
        if (principal.binding_id is None) != (principal.binding_version is None):
            raise ValueError("binding_id and binding_version must be given together")
        with transaction(self.database, None, "record an execution attempt") as cursor:
            execution = cursor.execute(
                "SELECT status, project_id FROM executions WHERE execution_id = %s FOR UPDATE",
                (execution_id,),
            ).fetchone()
            if execution is None:
                raise ExecutionNotFoundError("The execution does not exist")
            if execution["status"] in ("succeeded", "cancelled"):
                raise ExecutionClosedError(f"The execution already {execution['status']}")
            live = cursor.execute(
                "SELECT attempt_number, status FROM execution_attempts "
                "WHERE execution_id = %s AND status = ANY(%s)",
                (execution_id, list(LIVE_ATTEMPT_STATUSES)),
            ).fetchone()
            if live is not None:
                raise AttemptInFlightError(
                    f"Attempt {live['attempt_number']} is still {live['status']}"
                )
            _check_endpoint(cursor, endpoint, execution["project_id"])
            _check_account(
                cursor,
                endpoint,
                execution["project_id"],
                account,
                slurm_account_id,
                slurm_account_origin,
            )
            row = cursor.execute(
                """
                INSERT INTO execution_attempts (execution_id, attempt_number, platform_id,
                    login_node_id, login_node_host, login_node_port, transport_mode,
                    host_key_pin_ref, hpc_binding_id, hpc_binding_version, unix_user, unix_uid,
                    slurm_account_id, slurm_account, slurm_account_origin, source_digest,
                    environment_digest, runtime_digest)
                SELECT %s, COALESCE(max(attempt_number), 0) + 1, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s
                FROM execution_attempts WHERE execution_id = %s
                RETURNING id
                """,
                (
                    execution_id,
                    endpoint.platform_id,
                    endpoint.node_id,
                    endpoint.host,
                    endpoint.port,
                    endpoint.mode,
                    endpoint.host_key_pin_ref,
                    principal.binding_id,
                    principal.binding_version,
                    principal.unix_user,
                    principal.unix_uid,
                    slurm_account_id,
                    account,
                    slurm_account_origin,
                    source_digest,
                    environment_digest,
                    runtime_digest,
                    execution_id,
                ),
            ).fetchone()
            assert row is not None
            if options:
                cursor.executemany(
                    "INSERT INTO execution_attempt_options "
                    "(attempt_id, name, value, origin, default_value, default_source) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [
                        (
                            row["id"],
                            name,
                            _jsonb(option.value),
                            option.origin,
                            None if option.default_source is None else _jsonb(option.default_value),
                            option.default_source,
                        )
                        for name, option in options.items()
                    ],
                )
            cursor.execute(
                "UPDATE executions SET status = 'active', updated_at = now() "
                "WHERE execution_id = %s",
                (execution_id,),
            )
            attempt = self._load(cursor, row["id"])
        logger.info(
            "Execution attempt recorded execution_id=%s attempt=%s platform=%s node_id=%s",
            execution_id,
            attempt.attempt_number,
            endpoint.platform_slug,
            endpoint.node_id,
        )
        return attempt

    def record_submission(
        self, attempt_id: int, slurm_job_id: int, *, slurm_state: str | None = None
    ) -> Attempt:
        """Stores the Slurm job ID `sbatch` returned and marks the attempt `submitted`."""
        return self._transition(
            attempt_id,
            "submitted",
            "slurm_job_id = %s, slurm_state = COALESCE(%s, slurm_state), "
            "submitted_at = COALESCE(submitted_at, now())",
            (slurm_job_id, slurm_state),
            slurm_job_id=slurm_job_id,
        )

    def record_running(
        self, attempt_id: int, nodes: Iterable[str] = (), *, slurm_state: str = "RUNNING"
    ) -> Attempt:
        """Marks the attempt `running` and records the compute nodes allocated to it."""
        return self._transition(
            attempt_id, "running", "slurm_state = %s", (slurm_state,), nodes=nodes
        )

    def finish_attempt(
        self,
        attempt_id: int,
        status: str,
        *,
        slurm_job_id: int | None = None,
        slurm_state: str | None = None,
        output_ref: str | None = None,
        usage_ref: str | None = None,
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> Attempt:
        """
        Moves an attempt to `succeeded`, `failed`, `cancelled` or `unknown`.

        A final status also updates the execution: `succeeded` and `cancelled` close
        it, and `failed` leaves it open for a retry.

        `succeeded` needs a Slurm job ID, recorded earlier or given here (as when an
        `unknown` attempt is reconciled); `failed`, `cancelled` and `unknown` may
        have none, for an attempt that never reached Slurm.

        Raises:
            [`db.executions.ExecutionNotFoundError`]: If the attempt does not exist.
            [`db.executions.InvalidTransitionError`]: If the attempt is already final,
                or would be `succeeded` without a Slurm job ID.
        """
        if status not in (*FINAL_ATTEMPT_STATUSES, "unknown"):
            raise ValueError(f"Unknown final attempt status {status!r}")
        message = error_message[:_MAX_ERROR_MESSAGE] if error_message else None
        return self._transition(
            attempt_id,
            status,
            "submitted_at = CASE WHEN slurm_job_id IS NULL AND %s::bigint IS NOT NULL "
            "THEN now() ELSE submitted_at END, "
            "slurm_job_id = COALESCE(slurm_job_id, %s), "
            "slurm_state = COALESCE(%s, slurm_state), output_ref = COALESCE(%s, output_ref), "
            "usage_ref = COALESCE(%s, usage_ref), error_kind = %s, error_message = %s, "
            "finished_at = CASE WHEN %s THEN now() END",
            (
                slurm_job_id,
                slurm_job_id,
                slurm_state,
                output_ref,
                usage_ref,
                error_kind,
                message,
                status in FINAL_ATTEMPT_STATUSES,
            ),
            slurm_job_id=slurm_job_id,
        )

    def _transition(
        self,
        attempt_id: int,
        status: str,
        assignments: LiteralString,
        params: tuple,
        *,
        nodes: Iterable[str] = (),
        slurm_job_id: int | None = None,
    ) -> Attempt:
        with transaction(self.database, None, f"mark an execution attempt {status}") as cursor:
            current = cursor.execute(
                "SELECT e.execution_id, a.status, a.slurm_job_id FROM execution_attempts a "
                "JOIN executions e USING (execution_id) WHERE a.id = %s FOR UPDATE",
                (attempt_id,),
            ).fetchone()
            if current is None:
                raise ExecutionNotFoundError("The execution attempt does not exist")
            if current["status"] in FINAL_ATTEMPT_STATUSES or (
                status != current["status"] and status not in _TRANSITIONS[current["status"]]
            ):
                raise InvalidTransitionError(
                    f"An attempt cannot move from {current['status']} to {status}"
                )
            if (
                status in _NEEDS_SLURM_JOB
                and current["slurm_job_id"] is None
                and slurm_job_id is None
            ):
                raise InvalidTransitionError(
                    f"An attempt cannot be {status} without a Slurm job ID; "
                    "record the submission first"
                )
            row = cursor.execute(
                f"UPDATE execution_attempts a SET status = %s, updated_at = now(), "
                f"{assignments} WHERE a.id = %s RETURNING a.id",
                (status, *params, attempt_id),
            ).fetchone()
            assert row is not None
            node_list = sorted(set(nodes))
            if node_list:
                cursor.executemany(
                    "INSERT INTO execution_attempt_nodes (attempt_id, node_name) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    [(attempt_id, node) for node in node_list],
                )
            if status in FINAL_ATTEMPT_STATUSES:
                final = _EXECUTION_AFTER_ATTEMPT[status]
                cursor.execute(
                    "UPDATE executions SET status = %s, updated_at = now(), "
                    "finished_at = CASE WHEN %s THEN now() END WHERE execution_id = %s",
                    (final, final in ("succeeded", "cancelled"), current["execution_id"]),
                )
            attempt = self._load(cursor, row["id"])
        return attempt

    # Recovery -----------------------------------------------------------------

    def live_attempts(self, platform_id: UUID | str | None = None) -> list[Attempt]:
        """
        Returns every attempt still in flight, oldest first, for reconciliation
        after a restart. With `platform_id`, only that Platform's.

        Controller-internal: the result spans every Project, so it must never be
        returned to a User. User-facing reads go through the Project-scoped methods.
        """
        pid = as_uuid(platform_id, "platform_id") if platform_id is not None else None
        with transaction(self.database, None, "list live execution attempts") as cursor:
            rows = cursor.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM {_ATTEMPT_FROM} "
                "WHERE a.status = ANY(%s) AND (%s::uuid IS NULL OR a.platform_id = %s::uuid) "
                "ORDER BY a.id",
                (list(LIVE_ATTEMPT_STATUSES), pid, pid),
            ).fetchall()
            return [self._load_attempt(cursor, row) for row in rows]

    def mark_interrupted(self, older_than: timedelta) -> list[Attempt]:
        """
        Marks `submitting` attempts last updated more than `older_than` ago as
        `unknown`: their Controller stopped between recording them and receiving a
        Slurm job ID, so `sbatch` may or may not have run.

        `older_than` must exceed the longest submission any running Controller may
        still be making, so an attempt in progress elsewhere is never touched.

        Controller-internal, like [`db.ExecutionRegistry.live_attempts`]: it spans
        every Project and must never be exposed to a User.

        Returns:
            `list[Attempt]`: The attempts marked `unknown`.
        """
        if older_than <= timedelta(0):
            raise ValueError("older_than must be positive")
        with transaction(self.database, None, "mark interrupted execution attempts") as cursor:
            rows = cursor.execute(
                "UPDATE execution_attempts a SET status = 'unknown', updated_at = now(), "
                "error_kind = 'controller_interrupted' "
                "WHERE a.status = 'submitting' AND a.updated_at < now() - %s "
                "RETURNING a.id",
                (older_than,),
            ).fetchall()
            attempts = [self._load(cursor, row["id"]) for row in rows]
        for attempt in attempts:
            logger.warning(
                "Execution attempt interrupted before a Slurm job ID arrived "
                "execution_id=%s attempt=%s platform_id=%s",
                attempt.execution_id,
                attempt.attempt_number,
                attempt.platform_id,
            )
        return attempts

    # Reads --------------------------------------------------------------------

    def get(self, execution_id: str, *, project_id: UUID | str) -> Execution | None:
        """Returns the execution if it belongs to `project_id`, else `None`."""
        project = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "read an execution") as cursor:
            row = cursor.execute(
                f"SELECT {_EXECUTION_COLUMNS} FROM executions "
                "WHERE execution_id = %s AND project_id = %s",
                (execution_id, project),
            ).fetchone()
        return _execution(row) if row else None

    def attempts(self, execution_id: str, *, project_id: UUID | str) -> list[Attempt]:
        """Returns the execution's attempts in order, or `[]` outside `project_id`."""
        project = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "read execution attempts") as cursor:
            rows = cursor.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM {_ATTEMPT_FROM} "
                "JOIN executions e USING (execution_id) "
                "WHERE a.execution_id = %s AND e.project_id = %s ORDER BY a.attempt_number",
                (execution_id, project),
            ).fetchall()
            return [self._load_attempt(cursor, row) for row in rows]

    def list_for_project(self, project_id: UUID | str, *, limit: int = 100) -> list[Execution]:
        """Returns a Project's executions, newest first."""
        project = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "list executions") as cursor:
            rows = cursor.execute(
                f"SELECT {_EXECUTION_COLUMNS} FROM executions WHERE project_id = %s "
                "ORDER BY created_at DESC, execution_id LIMIT %s",
                (project, limit),
            ).fetchall()
        return [_execution(row) for row in rows]

    def find_by_slurm_job(
        self, platform_id: UUID | str, slurm_job_id: int, *, project_id: UUID | str
    ) -> Attempt | None:
        """
        Returns the latest attempt of `project_id` that received `slurm_job_id` on
        `platform_id`, or `None`.

        Slurm job IDs repeat across Platforms and after a Slurm reset, so a job ID
        is never looked up without its Platform, and never outside a Project: an
        attempt of another Project is answered as not found.
        """
        with transaction(self.database, None, "find an execution attempt") as cursor:
            row = cursor.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM {_ATTEMPT_FROM} {_IN_PROJECT} "
                "WHERE a.platform_id = %s AND a.slurm_job_id = %s ORDER BY a.id DESC LIMIT 1",
                (
                    as_uuid(project_id, "project_id"),
                    as_uuid(platform_id, "platform_id"),
                    slurm_job_id,
                ),
            ).fetchone()
            return self._load_attempt(cursor, row) if row else None

    def attempts_on_node(
        self, platform_id: UUID | str, node_name: str, *, project_id: UUID | str, limit: int = 100
    ) -> list[Attempt]:
        """
        Returns `project_id`'s attempts allocated `node_name` on `platform_id`,
        newest first.
        """
        with transaction(self.database, None, "find attempts on a node") as cursor:
            rows = cursor.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM {_ATTEMPT_FROM} {_IN_PROJECT} "
                "JOIN execution_attempt_nodes n ON n.attempt_id = a.id "
                "WHERE a.platform_id = %s AND n.node_name = %s ORDER BY a.id DESC LIMIT %s",
                (
                    as_uuid(project_id, "project_id"),
                    as_uuid(platform_id, "platform_id"),
                    node_name,
                    limit,
                ),
            ).fetchall()
            return [self._load_attempt(cursor, row) for row in rows]

    def attempts_for_account(
        self,
        platform_id: UUID | str,
        slurm_account: str,
        *,
        project_id: UUID | str,
        limit: int = 100,
    ) -> list[Attempt]:
        """
        Returns `project_id`'s attempts charged to `slurm_account` on `platform_id`,
        newest first. Another Project charging the same account sees only its own.
        """
        with transaction(self.database, None, "find attempts for an account") as cursor:
            rows = cursor.execute(
                f"SELECT {_ATTEMPT_COLUMNS} FROM {_ATTEMPT_FROM} {_IN_PROJECT} "
                "WHERE a.platform_id = %s AND a.slurm_account = %s "
                "ORDER BY a.created_at DESC, a.id DESC LIMIT %s",
                (
                    as_uuid(project_id, "project_id"),
                    as_uuid(platform_id, "platform_id"),
                    slurm_account,
                    limit,
                ),
            ).fetchall()
            return [self._load_attempt(cursor, row) for row in rows]

    @classmethod
    def _load(cls, cursor: Any, attempt_id: int) -> Attempt:
        row = cursor.execute(
            f"SELECT {_ATTEMPT_COLUMNS} FROM {_ATTEMPT_FROM} WHERE a.id = %s", (attempt_id,)
        ).fetchone()
        assert row is not None
        return cls._load_attempt(cursor, row)

    @staticmethod
    def _load_attempt(cursor: Any, row: dict[str, Any]) -> Attempt:
        nodes = [
            r["node_name"]
            for r in cursor.execute(
                "SELECT node_name FROM execution_attempt_nodes WHERE attempt_id = %s",
                (row["id"],),
            ).fetchall()
        ]
        options = {
            r["name"]: ResolvedOption(
                value=r["value"],
                origin=r["origin"],
                default_value=r["default_value"],
                default_source=r["default_source"],
            )
            for r in cursor.execute(
                "SELECT name, value, origin, default_value, default_source "
                "FROM execution_attempt_options WHERE attempt_id = %s ORDER BY name",
                (row["id"],),
            ).fetchall()
        }
        return _attempt(row, nodes, options)
