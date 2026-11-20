"""
Server-side choice of the Slurm account a job charges (D08).

The account is always one the Project is linked to on the job's Platform (see
[`db.SlurmAccountStore`]), usable under the job's mode and, when the HPC identity
binding lists them, among the identity's own Slurm entitlements. The first that
applies wins:

1. The account the caller asked for, which must be such an account. A request
   that names any other account is refused; it never falls through.
2. The Project x Platform default.
3. Mode B: the verified default account of the User's own HPC identity.
   Mode A: the Platform's operator-controlled default.
4. Mode B only, when the Project's policy allows it: no `--account`, so Slurm
   charges the User's own default. When the binding lists entitlements, only if
   the server verified that default and it is among them.

Otherwise the request is refused. The Controller's HPC identity and Mode A
chargeback accounts are never a Mode B fallback: Mode B never uses step 3 of Mode
A, never accepts the Controller identity, and a chargeback account cannot be
linked for Mode B. Every resolution says which step chose the account
([`authz.AccountResolution.origin`]), so the execution record keeps it.

Nothing here is an HPC credential, and nothing grants a Slurm or UNIX
permission: the cluster still decides whether the identity may charge the account.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .._logging import get_logger
from ..audit import AuditLog
from ..db._transaction import as_uuid
from ..db.accounts import (
    ACCOUNT_MODES,
    ProjectAccountLink,
    ProjectAccountMapping,
    SlurmAccountError,
    SlurmAccountStore,
    normalize_account_name,
)
from ..db.connection import Database
from .platforms import PlatformCatalog
from .policy import Principal

logger = get_logger("nest.authz.accounts")

IDENTITY_KINDS = ("controller", "user")
"""HPC identities a job can run as: the Controller's own, or a User's."""


class AccountResolutionError(SlurmAccountError):
    """
    No account may be charged for the request. Nothing was submitted.

    Attributes:
        reason (`str`): Machine-readable: `not_permitted` (the requested account is
            not linked to the Project on this Platform, or does not exist there),
            `mode_not_permitted`, `not_entitled` (not among the HPC identity's
            Slurm entitlements, including an unverified or unentitled native
            default), `account_disabled`, `identity_not_allowed` (the
            Controller identity for Mode B, or a User identity for Mode A),
            `wrong_platform` (the identity belongs to another Platform),
            `not_granted` or `no_account`.
    """

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class HPCIdentity:
    """
    The HPC identity a job would run as, as far as account selection needs it.

    Built by the server from its own records (the Controller's configuration, or a
    verified User binding), never from client input. It holds no credential.

    Attributes:
        kind (`str`): `controller` (Mode A, the operator's identity) or `user`
            (Mode B, the User's own identity).
        platform_id (`UUID`): The Platform the identity exists on.
        user_id (`UUID | None`): The Nest User a `user` identity is bound to.
        entitled_accounts (`frozenset[str] | None`): The Slurm accounts the binding
            says the identity may charge, or `None` when the binding does not
            restrict them (Slurm still checks).
        verified_default_account (`str | None`): The identity's own default account,
            verified by the server (Mode B, step 3).
    """

    kind: str
    platform_id: UUID
    user_id: UUID | None = None
    entitled_accounts: frozenset[str] | None = None
    verified_default_account: str | None = None

    def __post_init__(self):
        if self.kind not in IDENTITY_KINDS:
            raise ValueError(f"kind must be one of {IDENTITY_KINDS}")
        if self.kind == "user" and self.user_id is None:
            raise ValueError("A user HPC identity needs the Nest User it is bound to")
        if self.entitled_accounts is not None:
            object.__setattr__(
                self,
                "entitled_accounts",
                frozenset(normalize_account_name(a) for a in self.entitled_accounts),
            )
        if self.verified_default_account is not None:
            object.__setattr__(
                self,
                "verified_default_account",
                normalize_account_name(self.verified_default_account),
            )

    @classmethod
    def controller(
        cls, platform_id: UUID, *, entitled_accounts: Collection[str] | None = None
    ) -> HPCIdentity:
        """The Controller's operator identity on a Platform (Mode A)."""
        entitled = frozenset(entitled_accounts) if entitled_accounts is not None else None
        return cls("controller", platform_id, entitled_accounts=entitled)

    @classmethod
    def user(
        cls,
        platform_id: UUID,
        user_id: UUID,
        *,
        entitled_accounts: Collection[str] | None = None,
        verified_default_account: str | None = None,
    ) -> HPCIdentity:
        """A User's own, verified identity on a Platform (Mode B)."""
        entitled = frozenset(entitled_accounts) if entitled_accounts is not None else None
        return cls("user", platform_id, user_id, entitled, verified_default_account)


@dataclass(frozen=True)
class AccountResolution:
    """
    The account a job charges and why. Holds no credential.

    Attributes:
        project_id (`UUID`): The Project.
        platform_id (`UUID`): The Platform.
        platform_slug (`str`): The Platform's slug.
        mode (`str`): `A` or `B`.
        identity_kind (`str`): `controller` or `user`.
        origin (`str`): Which step chose it: `explicit`, `project_default`,
            `mode_a_operator_default`, `mode_b_user_default` or `native_default`.
        account_id (`int | None`): The registered account; `None` only for
            `native_default`.
        account_name (`str | None`): The Slurm account to pass as `--account`;
            `None` only for `native_default`, where `--account` is omitted.
        requested (`str | None`): The account the caller asked for, if any.
    """

    project_id: UUID
    platform_id: UUID
    platform_slug: str
    mode: str
    identity_kind: str
    origin: str
    account_id: int | None
    account_name: str | None
    requested: str | None = None

    def job_fields(self) -> dict[str, Any]:
        """
        The fields of [`db.JobStore.begin`] that record this resolution:
        `account`, `slurm_account_id` and `slurm_account_origin`. The job store's
        Platform must be this resolution's.
        """
        return {
            "account": self.account_name,
            "slurm_account_id": self.account_id,
            "slurm_account_origin": self.origin,
        }

    def details(self) -> dict[str, Any]:
        """What logs and audit records may say about it."""
        return {
            "platform_id": str(self.platform_id),
            "platform": self.platform_slug,
            "mode": self.mode,
            "identity": self.identity_kind,
            "account_id": self.account_id,
            "account": self.account_name,
            "origin": self.origin,
        }


def resolve_account(
    mapping: ProjectAccountMapping,
    *,
    mode: str,
    identity: HPCIdentity,
    requested: str | None = None,
) -> AccountResolution:
    """
    Chooses the account from a Project's mapping on one Platform, with no
    database access. See the module documentation for the order.

    Raises:
        [`authz.AccountResolutionError`]: If no account may be charged.
        `ValueError`: If `mode` is not `A` or `B`, or `requested` is not an
            account name.
    """
    if mode not in ACCOUNT_MODES:
        raise ValueError("mode must be A or B")
    platform = mapping.platform
    if identity.platform_id != platform.id:
        raise AccountResolutionError(
            "wrong_platform", "The HPC identity belongs to another Platform"
        )
    if (mode, identity.kind) not in (("A", "controller"), ("B", "user")):
        # Mode B must never run as, or fall back to, the Controller's identity.
        raise AccountResolutionError(
            "identity_not_allowed",
            f"Mode {mode} cannot charge accounts as the {identity.kind} HPC identity",
        )
    if not mapping.granted:
        raise AccountResolutionError(
            "not_granted", f"The Project is not granted Platform {platform.slug!r}"
        )
    name = normalize_account_name(requested) if requested is not None else None

    def refusal(link: ProjectAccountLink | None) -> str | None:
        if link is None:
            return "not_permitted"
        if link.account.platform_id != platform.id or link.platform_id != platform.id:
            return "not_permitted"
        if mode not in link.modes:
            return "mode_not_permitted"
        if mode == "B" and link.account.kind == "chargeback":
            return "mode_not_permitted"
        if not link.account.active:
            return "account_disabled"
        if (
            identity.entitled_accounts is not None
            and link.account.account_name not in identity.entitled_accounts
        ):
            return "not_entitled"
        return None

    def chosen(link: ProjectAccountLink, origin: str) -> AccountResolution:
        return AccountResolution(
            mapping.project_id,
            platform.id,
            platform.slug,
            mode,
            identity.kind,
            origin,
            link.account.id,
            link.account.account_name,
            name,
        )

    if name is not None:
        link = mapping.link(name)
        reason = refusal(link)
        if reason is not None:
            raise AccountResolutionError(
                reason,
                f"Slurm account {name!r} is not permitted for this Project on "
                f"{platform.slug!r} in Mode {mode}",
            )
        assert link is not None
        return chosen(link, "explicit")

    default = mapping.default
    if default is not None and refusal(default) is None:
        return chosen(default, "project_default")

    if mode == "A":
        operator_default = mapping.mode_a_default
        if operator_default is not None and refusal(operator_default) is None:
            return chosen(operator_default, "mode_a_operator_default")
    elif identity.verified_default_account is not None:
        user_default = mapping.link(identity.verified_default_account)
        if user_default is not None and refusal(user_default) is None:
            return chosen(user_default, "mode_b_user_default")

    if mode == "B" and mapping.allow_native_default:
        # Omitting --account lets Slurm charge the identity's own default. With
        # an entitlement set, that is allowed only when the server verified the
        # default and it is entitled; otherwise Slurm could charge an account
        # outside the binding's boundary.
        entitled = identity.entitled_accounts
        if entitled is not None and identity.verified_default_account not in entitled:
            raise AccountResolutionError(
                "not_entitled",
                "The native Slurm default is not a verified account the HPC identity is "
                f"entitled to on {platform.slug!r}; name a permitted account",
            )
        return AccountResolution(
            mapping.project_id,
            platform.id,
            platform.slug,
            mode,
            identity.kind,
            "native_default",
            None,
            None,
        )
    raise AccountResolutionError(
        "no_account",
        f"No Slurm account was named and none is authorized by default for this Project "
        f"on {platform.slug!r} in Mode {mode}; name a permitted account",
    )


class AccountResolver:
    """
    Resolves the Platform and Slurm account of a caller's job, on the server.

    The Platform is resolved by [`authz.PlatformCatalog.resolve_platform`], so the
    caller must be an active member of the Project and the Project granted the
    Platform; then [`authz.resolve_account`] chooses the account from the
    registry. Refusals are recorded in the security audit log; resolutions are
    logged with the Platform, account and origin, never with a credential.
    Nothing is cached.

    Args:
        database ([`db.Database`]): Open database.
        audit ([`audit.AuditLog`], *optional*): Defaults to one on `database`.
    """

    def __init__(self, database: Database, audit: AuditLog | None = None):
        self.audit = audit or AuditLog(database)
        self.catalog = PlatformCatalog(database, self.audit)
        self.store = SlurmAccountStore(database)

    def resolve(
        self,
        principal: Principal,
        project_id: UUID | str,
        *,
        mode: str,
        identity: HPCIdentity,
        platform: UUID | str | None = None,
        account: str | None = None,
    ) -> AccountResolution:
        """
        The Platform and account the caller's job in `project_id` charges.

        Args:
            principal ([`authz.Principal`]): The caller.
            project_id (`UUID | str`): The Project the job runs for.
            mode (`str`): `A` or `B`.
            identity ([`authz.HPCIdentity`]): The identity the server would submit
                as. For Mode B it must be the caller's own.
            platform (`UUID | str`, *optional*): The Platform asked for; the
                server's choice when omitted.
            account (`str`, *optional*): The account asked for.

        Raises:
            [`authz.AccountResolutionError`]: If no account may be charged.
            Everything [`authz.PlatformCatalog.resolve_platform`] raises.
        """
        pid = as_uuid(project_id, "project_id")
        resolved = self.catalog.resolve_platform(principal, platform, project_id=pid)
        try:
            if identity.kind == "user" and identity.user_id != principal.user_id:
                raise AccountResolutionError(
                    "identity_not_allowed", "The HPC identity is bound to another User"
                )
            result = resolve_account(
                self.store.mapping(pid, resolved.id),
                mode=mode,
                identity=identity,
                requested=account,
            )
        except AccountResolutionError as exc:
            details: dict[str, Any] = {
                "session_kind": principal.session_kind,
                "platform_id": str(resolved.id),
                "mode": mode,
                "identity": identity.kind,
            }
            if account is not None:
                details["requested_account"] = account.strip().lower()[:64]
            logger.warning(
                "Slurm account refused user_id=%s project_id=%s platform=%s mode=%s reason=%s",
                principal.user_id,
                pid,
                resolved.slug,
                mode,
                exc.reason,
            )
            self.audit.record(
                "slurm_account.resolve",
                "denied",
                actor_user_id=principal.user_id,
                session_id=principal.session_id,
                project_id=pid,
                resource_type="platform",
                resource_id=str(resolved.id),
                reason=exc.reason,
                details=details,
            )
            raise
        logger.info(
            "Slurm account resolved user_id=%s project_id=%s platform=%s mode=%s "
            "account=%s origin=%s",
            principal.user_id,
            pid,
            result.platform_slug,
            mode,
            result.account_name or "-",
            result.origin,
        )
        return result
