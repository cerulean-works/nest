"""
HPC Platform discovery for the command line and the SDK.

[`platforms.list_platforms`] asks Nest which HPC Platforms (Slurm namespaces such as
`nano4` or `f1`) the signed-in User may use. Nest answers from its server-side
catalog with only the Platforms granted to the User's Projects; no host, port, host
key or SSH setting is ever sent to or taken from the client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode
from uuid import UUID

from .config import PLATFORMS_API_PATH, PROJECTS_API_PATH, CLIAuthConfig, load_cli_auth_config
from .login import AuthClient, AuthError, ensure_session


class PlatformNotAvailableError(AuthError):
    """The Platform does not exist or is not available to the signed-in User."""


@dataclass(frozen=True)
class PlatformInfo:
    """
    An HPC Platform the signed-in User may use.

    Attributes:
        id (`UUID`): Immutable Platform ID.
        slug (`str`): User-facing name, such as `nano4`.
        display_name (`str`): Human-readable name.
        operator_name (`str | None`): Who operates it, such as `NCHC`. Metadata only.
        scheduler (`str`): `slurm`.
        is_default (`bool`): Whether it is the server's default Platform.
        transport_modes (`tuple[str, ...]`): Approved SSH modes of its login nodes.
        login_node_count (`int`): Approved, active login nodes.
    """

    id: UUID
    slug: str
    display_name: str
    operator_name: str | None
    scheduler: str
    is_default: bool
    transport_modes: tuple[str, ...]
    login_node_count: int

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> PlatformInfo:
        return cls(
            id=UUID(str(payload["id"])),
            slug=str(payload["slug"]),
            display_name=str(payload["display_name"]),
            operator_name=payload.get("operator_name"),
            scheduler=str(payload.get("scheduler", "slurm")),
            is_default=bool(payload.get("is_default")),
            transport_modes=tuple(payload.get("transport_modes") or ()),
            login_node_count=int(payload.get("login_node_count") or 0),
        )


def _get(config: CLIAuthConfig, path: str, **options: Any) -> dict[str, Any]:
    session = ensure_session(config, **options)
    status, body = AuthClient(config.url).request(path, bearer=session.access_token)
    if status == 401:
        raise AuthError("Nest did not accept the session; run nest login")
    if status == 404:
        raise PlatformNotAvailableError("No such HPC Platform is available to you")
    if status == 403:
        raise AuthError(f"Not allowed: {body.get('reason', 'forbidden')}")
    if status != 200:
        raise AuthError(f"Nest refused the request (HTTP {status})")
    return body


def _query(project_id: UUID | str | None) -> str:
    return "?" + urlencode({"project_id": str(UUID(str(project_id)))}) if project_id else ""


def list_platforms(
    config: CLIAuthConfig | None = None,
    *,
    project_id: UUID | str | None = None,
    **options: Any,
) -> tuple[list[PlatformInfo], UUID | None]:
    """
    The HPC Platforms available to the signed-in User.

    Args:
        config ([`config.CLIAuthConfig`], *optional*): Defaults to the environment.
        project_id (`UUID`, *optional*): Only Platforms granted to this Project.
        **options: Passed to [`login.ensure_session`].

    Returns:
        `tuple[list[PlatformInfo], UUID | None]`: The Platforms by slug, and the
            Platform the server uses when none is named, if there is one.
    """
    config = config or load_cli_auth_config()
    body = _get(config, PLATFORMS_API_PATH + _query(project_id), **options)
    platforms = [PlatformInfo.from_json(item) for item in body.get("platforms") or ()]
    default = body.get("default_platform_id")
    return platforms, UUID(str(default)) if default else None


def get_platform(
    reference: str,
    config: CLIAuthConfig | None = None,
    *,
    project_id: UUID | str | None = None,
    **options: Any,
) -> PlatformInfo:
    """
    One available Platform by slug or ID.

    Raises:
        [`platforms.PlatformNotAvailableError`]: If it does not exist or is not
            available to the signed-in User.
    """
    config = config or load_cli_auth_config()
    path = f"{PLATFORMS_API_PATH}/{quote(reference.strip(), safe='')}" + _query(project_id)
    return PlatformInfo.from_json(_get(config, path, **options))


@dataclass(frozen=True)
class ProjectPlatformsInfo:
    """
    A Project's eligible HPC Platforms and the Platform its requests default to.

    Attributes:
        project_id (`UUID`): The Project.
        platforms (`list[PlatformInfo]`): Eligible Platforms, by slug: those the
            operator granted to the Project.
        project_default_platform_id (`UUID | None`): The default the Project's
            Owners and Admins chose, if any.
        default_platform_id (`UUID | None`): What a request naming no Platform
            runs on now, if anything.
        default_origin (`str | None`): Why: `project_default`, `server_default` or
            `only_eligible`.
        unresolved_reason (`str | None`): Why nothing is chosen: `none_authorized`,
            `ambiguous` or `default_unavailable`.
    """

    project_id: UUID
    platforms: list[PlatformInfo]
    project_default_platform_id: UUID | None
    default_platform_id: UUID | None
    default_origin: str | None
    unresolved_reason: str | None


def project_platforms(
    project_id: UUID | str,
    config: CLIAuthConfig | None = None,
    **options: Any,
) -> ProjectPlatformsInfo:
    """
    A Project's eligible Platforms and default, as decided by the server. Choosing
    the default needs a browser session (see *Register HPC Platforms*).

    Raises:
        [`platforms.PlatformNotAvailableError`]: If the Project does not exist or
            the signed-in User is not a member.
    """
    config = config or load_cli_auth_config()
    pid = UUID(str(project_id))
    body = _get(config, f"{PROJECTS_API_PATH}/{pid}/platforms", **options)

    def uuid_or_none(value: object) -> UUID | None:
        return UUID(str(value)) if value else None

    return ProjectPlatformsInfo(
        project_id=pid,
        platforms=[PlatformInfo.from_json(item) for item in body.get("platforms") or ()],
        project_default_platform_id=uuid_or_none(body.get("project_default_platform_id")),
        default_platform_id=uuid_or_none(body.get("default_platform_id")),
        default_origin=body.get("default_origin"),
        unresolved_reason=body.get("unresolved_reason"),
    )
