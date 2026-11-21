"""
The Platform-scoped Slurm Account Registry and Project mapping (D08).

A Slurm account lives in exactly one HPC Platform's namespace (see
[`db.PlatformStore`]): the account `chem` on `nano4` and the account `chem` on
`f1` are two records with their own IDs, links and usage. A Nest Project is never
identified, renamed or merged by an account name; it is only *linked* to accounts
of Platforms it is granted. The schema (migration `0013`) refuses a link to an
account of another Platform, and removes a Project's links on a Platform when its
grant is revoked.

Everything here is operator managed and audited, like the Platform Catalog.
[`db.SlurmAccountStore`] is the operator API (`nest-db account`); the server-side
choice of the account a job charges is [`authz.AccountResolver`].
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg import errors as pg_errors

from .._logging import get_logger
from ._transaction import as_uuid, audit_context, transaction
from .connection import Database
from .platforms import Platform, PlatformError, PlatformStore

logger = get_logger("nest.db.accounts")

ACCOUNT_KINDS = ("standard", "chargeback")
"""Kinds of Slurm account: `standard`, or `chargeback` (Mode A only)."""

ACCOUNT_MODES = ("A", "B")
"""Modes an account may be charged under: `A` (the Controller's operator HPC
identity) and `B` (the User's own HPC identity)."""

ACCOUNT_ORIGINS = (
    "explicit",
    "project_default",
    "mode_a_operator_default",
    "mode_b_user_default",
    "native_default",
)
"""Where a job's account came from, in order of precedence."""

_ACCOUNT_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_ACCOUNT_COLUMNS = (
    "id, platform_id, account_name, kind, description, status, is_mode_a_default, "
    "created_at, updated_at, disabled_at"
)
_A_ACCOUNT_COLUMNS = (
    "a.id, a.platform_id, a.account_name, a.kind, a.description, a.status, "
    "a.is_mode_a_default, a.created_at, a.updated_at, a.disabled_at"
)
_UNSET: Any = object()


class SlurmAccountError(PlatformError):
    """A Slurm Account Registry change was refused. Nothing was changed."""


class SlurmAccountNotFoundError(SlurmAccountError, LookupError):
    """No such Slurm account on the Platform."""


class DuplicateSlurmAccountError(SlurmAccountError):
    """The Platform already has an account with this name."""


class AccountLinkNotFoundError(SlurmAccountError, LookupError):
    """The account is not linked to the Project on that Platform."""


class PlatformNotGrantedError(SlurmAccountError):
    """The Project is not granted the Platform, so no account of it can be linked."""


@dataclass(frozen=True)
class SlurmAccount:
    """
    One Slurm account in one Platform's namespace.

    Attributes:
        id (`int`): Registry ID. Records that charge an account refer to this.
        platform_id (`UUID`): The Platform. Never changes.
        account_name (`str`): The Slurm account name, lowercase. Unique only
            within its Platform.
        kind (`str`): `standard`, or `chargeback` for an operator account charged
            through the Controller's own HPC identity, which is Mode A only.
        description (`str | None`): Operator note.
        status (`str`): `active` or `disabled`. A disabled account is never chosen.
        is_mode_a_default (`bool`): The Platform's operator-controlled Mode A default.
        created_at, updated_at (`datetime`): Row timestamps.
        disabled_at (`datetime | None`): When it was disabled.
    """

    id: int
    platform_id: UUID
    account_name: str
    kind: str
    description: str | None
    status: str
    is_mode_a_default: bool
    created_at: datetime
    updated_at: datetime
    disabled_at: datetime | None

    @property
    def active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class ProjectAccountLink:
    """
    A Slurm account a Project may charge on a Platform.

    Attributes:
        project_id (`UUID`): The Project.
        platform_id (`UUID`): The Platform; always the account's.
        account ([`db.SlurmAccount`]): The account.
        modes (`tuple[str, ...]`): `A` and/or `B`, the identities it may be
            charged under for this Project.
        is_default (`bool`): The Project x Platform default.
    """

    project_id: UUID
    platform_id: UUID
    account: SlurmAccount
    modes: tuple[str, ...]
    is_default: bool


@dataclass(frozen=True)
class ProjectAccountMapping:
    """
    Everything the account selection may choose from for one Project on one
    Platform, read in one transaction.

    Attributes:
        project_id (`UUID`): The Project.
        platform ([`db.Platform`]): The Platform.
        granted (`bool`): Whether the Project is granted the Platform. Without a
            grant there are no links.
        links (`tuple[ProjectAccountLink, ...]`): Linked accounts, by name.
        allow_native_default (`bool`): Whether a Mode B job may leave the account
            to Slurm's default for the User's HPC identity.
    """

    project_id: UUID
    platform: Platform
    granted: bool
    links: tuple[ProjectAccountLink, ...]
    allow_native_default: bool

    def link(self, account_name: str) -> ProjectAccountLink | None:
        """The link of the account named `account_name`, or `None`."""
        return next((x for x in self.links if x.account.account_name == account_name), None)

    @property
    def default(self) -> ProjectAccountLink | None:
        """The Project x Platform default link, or `None`."""
        return next((x for x in self.links if x.is_default), None)

    @property
    def mode_a_default(self) -> ProjectAccountLink | None:
        """The link of the Platform's Mode A operator default, if the Project has one."""
        return next((x for x in self.links if x.account.is_mode_a_default), None)


def normalize_account_name(value: str) -> str:
    """
    A Slurm account name as the registry stores it: trimmed and lowercased, as
    Slurm itself stores account names.

    Raises:
        `ValueError`: If it is not 1 to 64 letters, digits, `_`, `.` or `-`,
            starting with a letter or digit.
    """
    name = str(value).strip().lower()
    if not _ACCOUNT_NAME.fullmatch(name):
        raise ValueError(
            "A Slurm account name is 1 to 64 letters, digits, '_', '.' or '-', "
            "starting with a letter or digit"
        )
    return name


def _modes(modes: Iterable[str]) -> list[str]:
    result = list(dict.fromkeys(str(m).strip().upper() for m in modes if str(m).strip()))
    if not result or any(m not in ACCOUNT_MODES for m in result):
        raise ValueError("Account modes must be A and/or B")
    return sorted(result)


def _description(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not 1 <= len(text) <= 200:
        raise ValueError("description must be 1 to 200 characters")
    return text


def _account(row: Mapping[str, Any]) -> SlurmAccount:
    return SlurmAccount(**row)


class SlurmAccountStore:
    """
    Operator-side API of the Slurm Account Registry and Project mapping.

    Every change needs an `operator` and a `reason`, recorded in the security audit
    log; the schema refuses a change without an operator. Platforms are named by
    UUID or slug, accounts by name within their Platform.

    Args:
        database ([`db.Database`]): Open database.
    """

    def __init__(self, database: Database):
        self.database = database

    # Reading -----------------------------------------------------------------

    @staticmethod
    def _find(
        cursor: psycopg.Cursor[dict[str, Any]],
        platform: Platform,
        name: str,
        *,
        lock: bool = False,
    ) -> SlurmAccount:
        row = cursor.execute(
            f"SELECT {_ACCOUNT_COLUMNS} FROM slurm_accounts "
            "WHERE platform_id = %s AND account_name = %s" + (" FOR NO KEY UPDATE" if lock else ""),
            (platform.id, normalize_account_name(name)),
        ).fetchone()
        if row is None:
            raise SlurmAccountNotFoundError(
                f"Platform {platform.slug!r} has no Slurm account {normalize_account_name(name)!r}"
            )
        return _account(row)

    def get_account(self, platform: UUID | str, name: str) -> SlurmAccount | None:
        """An account by Platform and name, or `None`."""
        with transaction(self.database, None, "read a Slurm account") as cursor:
            found = PlatformStore._find(cursor, platform)
            try:
                return self._find(cursor, found, name)
            except SlurmAccountNotFoundError:
                return None

    def list_accounts(self, platform: UUID | str) -> list[SlurmAccount]:
        """A Platform's accounts, by name."""
        with transaction(self.database, None, "list Slurm accounts") as cursor:
            found = PlatformStore._find(cursor, platform)
            rows = cursor.execute(
                f"SELECT {_ACCOUNT_COLUMNS} FROM slurm_accounts WHERE platform_id = %s "
                "ORDER BY account_name",
                (found.id,),
            ).fetchall()
        return [_account(r) for r in rows]

    def mapping(self, project_id: UUID | str, platform: UUID | str) -> ProjectAccountMapping:
        """
        The Project's accounts and account policy on a Platform, in one read.

        Raises:
            [`db.PlatformNotFoundError`]: If there is no such Platform.
        """
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "read a Project's Slurm accounts") as cursor:
            found = PlatformStore._find(cursor, platform)
            return self._mapping(cursor, pid, found)

    @staticmethod
    def _mapping(
        cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID, platform: Platform
    ) -> ProjectAccountMapping:
        granted = (
            cursor.execute(
                "SELECT 1 FROM hpc_platform_project_grants WHERE platform_id = %s "
                "AND project_id = %s",
                (platform.id, project_id),
            ).fetchone()
            is not None
        )
        rows = cursor.execute(
            f"SELECT {_A_ACCOUNT_COLUMNS}, l.modes AS link_modes, l.is_default AS link_default "
            "FROM project_platform_accounts l "
            "JOIN slurm_accounts a ON a.platform_id = l.platform_id AND a.id = l.slurm_account_id "
            "WHERE l.project_id = %s AND l.platform_id = %s ORDER BY a.account_name",
            (project_id, platform.id),
        ).fetchall()
        links = []
        for row in rows:
            data = dict(row)
            modes = tuple(data.pop("link_modes"))
            is_default = data.pop("link_default")
            links.append(
                ProjectAccountLink(project_id, platform.id, _account(data), modes, is_default)
            )
        policy = cursor.execute(
            "SELECT allow_native_default FROM project_platform_account_policies "
            "WHERE project_id = %s AND platform_id = %s",
            (project_id, platform.id),
        ).fetchone()
        return ProjectAccountMapping(
            project_id,
            platform,
            granted,
            tuple(links),
            bool(policy and policy["allow_native_default"]),
        )

    # Accounts ----------------------------------------------------------------

    def register_account(
        self,
        platform: UUID | str,
        name: str,
        *,
        operator: str,
        reason: str,
        kind: str = "standard",
        description: str | None = None,
    ) -> SlurmAccount:
        """
        Registers a Slurm account in a Platform's namespace.

        Registering does not create the account on the cluster: it records an
        account the cluster already has, so Projects can be linked to it.

        Args:
            platform (`UUID | str`): The Platform.
            name (`str`): The Slurm account name. Stored lowercase.
            kind (`str`): `standard`, or `chargeback` for an operator account
                charged through the Controller's own identity (Mode A only).
            description (`str`, *optional*): Operator note.

        Raises:
            [`db.DuplicateSlurmAccountError`]: If the Platform has the name already.
            [`db.PlatformNotFoundError`]: If there is no such Platform.
            `ValueError`: If an argument is invalid.
        """
        account_name = normalize_account_name(name)
        if kind not in ACCOUNT_KINDS:
            raise ValueError(f"kind must be one of {ACCOUNT_KINDS}")
        note = _description(description)
        with transaction(self.database, None, "register a Slurm account") as cursor:
            found = PlatformStore._find(cursor, platform, lock=True)
            with audit_context(cursor, operator=operator, reason=reason):
                try:
                    with cursor.connection.transaction():
                        row = cursor.execute(
                            "INSERT INTO slurm_accounts (platform_id, account_name, kind, "
                            f"description) VALUES (%s, %s, %s, %s) RETURNING {_ACCOUNT_COLUMNS}",
                            (found.id, account_name, kind, note),
                        ).fetchone()
                except pg_errors.UniqueViolation:
                    raise DuplicateSlurmAccountError(
                        f"Platform {found.slug!r} already has a Slurm account {account_name!r}"
                    ) from None
        assert row is not None
        account = _account(row)
        logger.info(
            "Slurm account registered platform_id=%s account_id=%s account=%s",
            account.platform_id,
            account.id,
            account.account_name,
        )
        return account

    def update_account(
        self,
        platform: UUID | str,
        name: str,
        *,
        operator: str,
        reason: str,
        status: str | None = None,
        description: str | None = _UNSET,
    ) -> SlurmAccount:
        """
        Changes an account's status or description. Its Platform, name and kind
        never change. A disabled account is never chosen and stops being the Mode A
        default; its links are kept so re-enabling restores them.

        Raises:
            [`db.SlurmAccountNotFoundError`], `ValueError`
        """
        if status is not None and status not in ("active", "disabled"):
            raise ValueError("status must be 'active' or 'disabled'")
        with transaction(self.database, None, "change a Slurm account") as cursor:
            found = PlatformStore._find(cursor, platform)
            account = self._find(cursor, found, name, lock=True)
            new_status = status or account.status
            note = _description(description) if description is not _UNSET else account.description
            if (new_status, note) == (account.status, account.description):
                return account
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "UPDATE slurm_accounts SET status = %s, description = %s, "
                    "disabled_at = CASE WHEN %s = 'disabled' THEN COALESCE(disabled_at, now()) "
                    "END, is_mode_a_default = is_mode_a_default AND %s = 'active', "
                    f"updated_at = now() WHERE id = %s RETURNING {_ACCOUNT_COLUMNS}",
                    (new_status, note, new_status, new_status, account.id),
                ).fetchone()
        assert row is not None
        return _account(row)

    def set_mode_a_default(
        self, platform: UUID | str, name: str | None, *, operator: str, reason: str
    ) -> SlurmAccount | None:
        """
        Makes an account the Platform's operator-controlled Mode A default, or
        clears it with `None`. It is used only for Projects linked to it for Mode A.

        Raises:
            [`db.SlurmAccountNotFoundError`], [`db.SlurmAccountError`] if disabled.
        """
        with transaction(self.database, None, "set the Mode A default account") as cursor:
            found = PlatformStore._find(cursor, platform, lock=True)
            account = self._find(cursor, found, name, lock=True) if name is not None else None
            if account is not None and not account.active:
                raise SlurmAccountError("A disabled Slurm account cannot be the Mode A default")
            with audit_context(cursor, operator=operator, reason=reason):
                cursor.execute(
                    "UPDATE slurm_accounts SET is_mode_a_default = false, updated_at = now() "
                    "WHERE platform_id = %s AND is_mode_a_default AND id IS DISTINCT FROM %s",
                    (found.id, account.id if account else None),
                )
                if account is None:
                    return None
                row = cursor.execute(
                    "UPDATE slurm_accounts SET is_mode_a_default = true, updated_at = now() "
                    f"WHERE id = %s RETURNING {_ACCOUNT_COLUMNS}",
                    (account.id,),
                ).fetchone()
        assert row is not None
        return _account(row)

    # Project links -----------------------------------------------------------

    def link_project(
        self,
        project_id: UUID | str,
        platform: UUID | str,
        name: str,
        *,
        modes: Iterable[str],
        operator: str,
        reason: str,
        default: bool = False,
    ) -> ProjectAccountLink:
        """
        Lets a Project charge one of a Platform's accounts, or changes the modes
        and default of an existing link.

        Args:
            project_id (`UUID | str`): The Project. It must be granted the Platform.
            platform (`UUID | str`): The Platform.
            name (`str`): The account, looked up only on this Platform.
            modes (`Iterable[str]`): `A` and/or `B`. A chargeback account is `A` only.
            default (`bool`): Make it the Project x Platform default.

        Raises:
            [`db.PlatformNotGrantedError`]: If the Project is not granted the Platform.
            [`db.SlurmAccountNotFoundError`]: If the Platform has no such account.
            [`db.SlurmAccountError`]: If the link is not allowed, such as a
                chargeback account for Mode B, or a disabled account as default.
            `ValueError`: If an argument is invalid.
        """
        pid = as_uuid(project_id, "project_id")
        wanted = _modes(modes)
        with transaction(self.database, None, "link a Slurm account") as cursor:
            found = PlatformStore._find(cursor, platform, lock=True)
            account = self._find(cursor, found, name, lock=True)
            self._require_grant(cursor, pid, found)
            if account.kind == "chargeback" and "B" in wanted:
                raise SlurmAccountError(
                    f"{account.account_name!r} is a chargeback account of the Controller's "
                    "identity and can only be linked for Mode A"
                )
            if default and not account.active:
                raise SlurmAccountError("A disabled Slurm account cannot be a Project default")
            with audit_context(cursor, operator=operator, reason=reason):
                if default:
                    cursor.execute(
                        "UPDATE project_platform_accounts SET is_default = false, "
                        "updated_at = now() WHERE project_id = %s AND platform_id = %s "
                        "AND is_default AND slurm_account_id <> %s",
                        (pid, found.id, account.id),
                    )
                cursor.execute(
                    "INSERT INTO project_platform_accounts (project_id, platform_id, "
                    "slurm_account_id, modes, is_default) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT (project_id, platform_id, slurm_account_id) DO UPDATE SET "
                    "modes = EXCLUDED.modes, is_default = EXCLUDED.is_default, updated_at = now() "
                    "WHERE (project_platform_accounts.modes, project_platform_accounts.is_default) "
                    "IS DISTINCT FROM (EXCLUDED.modes, EXCLUDED.is_default)",
                    (pid, found.id, account.id, wanted, default),
                )
        logger.info(
            "Slurm account linked project_id=%s platform_id=%s account_id=%s modes=%s default=%s",
            pid,
            found.id,
            account.id,
            ",".join(wanted),
            default,
        )
        return ProjectAccountLink(pid, found.id, account, tuple(wanted), default)

    def unlink_project(
        self, project_id: UUID | str, platform: UUID | str, name: str, *, operator: str, reason: str
    ) -> None:
        """
        Stops a Project charging an account. Jobs already recorded keep it.

        Raises:
            [`db.AccountLinkNotFoundError`]: If the account was not linked.
        """
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "unlink a Slurm account") as cursor:
            found = PlatformStore._find(cursor, platform, lock=True)
            account = self._find(cursor, found, name)
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "DELETE FROM project_platform_accounts WHERE project_id = %s "
                    "AND platform_id = %s AND slurm_account_id = %s RETURNING project_id",
                    (pid, found.id, account.id),
                ).fetchone()
            if row is None:
                raise AccountLinkNotFoundError(
                    f"{account.account_name!r} is not linked to the Project on {found.slug!r}"
                )

    def clear_project_default(
        self, project_id: UUID | str, platform: UUID | str, *, operator: str, reason: str
    ) -> bool:
        """Clears the Project x Platform default. Returns `False` when there was none."""
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "clear a Project default account") as cursor:
            found = PlatformStore._find(cursor, platform, lock=True)
            with audit_context(cursor, operator=operator, reason=reason):
                row = cursor.execute(
                    "UPDATE project_platform_accounts SET is_default = false, updated_at = now() "
                    "WHERE project_id = %s AND platform_id = %s AND is_default "
                    "RETURNING project_id",
                    (pid, found.id),
                ).fetchone()
        return row is not None

    def set_native_default(
        self,
        project_id: UUID | str,
        platform: UUID | str,
        allowed: bool,
        *,
        operator: str,
        reason: str,
    ) -> None:
        """
        Allows or forbids Mode B jobs of the Project on the Platform to leave the
        account to Slurm's default for the User's own HPC identity. Never applies to
        Mode A.

        Raises:
            [`db.PlatformNotGrantedError`]: If the Project is not granted the Platform.
        """
        pid = as_uuid(project_id, "project_id")
        with transaction(self.database, None, "set a Project account policy") as cursor:
            found = PlatformStore._find(cursor, platform, lock=True)
            self._require_grant(cursor, pid, found)
            with audit_context(cursor, operator=operator, reason=reason):
                cursor.execute(
                    "INSERT INTO project_platform_account_policies (project_id, platform_id, "
                    "allow_native_default) VALUES (%s, %s, %s) "
                    "ON CONFLICT (project_id, platform_id) DO UPDATE SET "
                    "allow_native_default = EXCLUDED.allow_native_default, updated_at = now() "
                    "WHERE project_platform_account_policies.allow_native_default "
                    "IS DISTINCT FROM EXCLUDED.allow_native_default",
                    (pid, found.id, bool(allowed)),
                )

    @staticmethod
    def _require_grant(
        cursor: psycopg.Cursor[dict[str, Any]], project_id: UUID, platform: Platform
    ) -> None:
        row = cursor.execute(
            "SELECT 1 FROM hpc_platform_project_grants g JOIN projects p ON p.id = g.project_id "
            "WHERE g.platform_id = %s AND g.project_id = %s AND p.deleted_at IS NULL "
            "FOR KEY SHARE OF g",
            (platform.id, project_id),
        ).fetchone()
        if row is None:
            raise PlatformNotGrantedError(
                f"The Project is not granted Platform {platform.slug!r}; grant it first"
            )
