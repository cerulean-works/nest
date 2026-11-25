"""
The privileged management API under `/api/admin` (G03), served by the auth listener.

Every route is authorized by [`authz.AdminService`]: a browser session of an
active `platform_admin`, checked in the database on every request. Changes are
JSON `POST`s that pass the listener's same-origin (CSRF) check, and each names a
`reason` for the audit log. Listings are paged with `limit` (1 to 200, default 50)
and the `after` cursor the previous page returned as `next_after`. No token,
session, identity subject, host, port, host key or HPC credential is returned.

| Method | Route | Does |
| --- | --- | --- |
| `GET` | `/api/admin/users?limit&after&email` | User directory |
| `GET` | `/api/admin/users/<user-id>` | One User |
| `GET` | `/api/admin/projects?limit&after&include_deleted` | Every Project |
| `POST` | `/api/admin/projects` | Create a Project with an explicit Owner |
| `GET` | `/api/admin/projects/<id>/access` | Its Platform grants, account links and Provider approvals |
| `POST` | `/api/admin/projects/<id>/platforms/grant`, `/revoke` | Platform grant |
| `POST` | `/api/admin/projects/<id>/accounts/link`, `/unlink` | Slurm account link |
| `GET` | `/api/admin/platforms` | The Platform catalog |
| `GET` | `/api/admin/platforms/<platform>/accounts` | A Platform's Slurm accounts |
| `GET` | `/api/admin/provider-approvals?provider&project_id&status&limit&after` | Provider approvals |
| `POST` | `/api/admin/provider-approvals/approve`, `/revoke` | Provider approval |
| `GET` | `/api/admin/provider-policy` | Where this Controller takes Provider approvals from |

Every Provider approval answer carries `policy`: the serving Controller's
`source` (`database`, `environment` or `unknown`), its `provider`, and
`approvals_effective`, which is `false` unless recorded approvals decide here.
Recorded revocations refuse new submissions in either mode.
| `GET` | `/api/admin/audit?limit&before&event&project_id&actor_user_id` | Security audit log |
"""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..audit import SecurityAuditEvent
from ..authz import DirectoryUser, Page, Principal, ProjectAccess, ProviderPolicy
from ..config import ADMIN_API_PATH
from ..db.accounts import ProjectAccountLink, SlurmAccount
from ..db.approvals import ProviderApproval
from ..db.platforms import Platform
from ..db.projects import Project

if TYPE_CHECKING:
    from .web import _Handler

__all__ = ["ADMIN_ROUTES", "admin_api"]

ADMIN_ROUTES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("users",): ("GET",),
    ("users", "{id}"): ("GET",),
    ("projects",): ("GET", "POST"),
    ("projects", "{id}", "access"): ("GET",),
    ("projects", "{id}", "platforms", "grant"): ("POST",),
    ("projects", "{id}", "platforms", "revoke"): ("POST",),
    ("projects", "{id}", "accounts", "link"): ("POST",),
    ("projects", "{id}", "accounts", "unlink"): ("POST",),
    ("platforms",): ("GET",),
    ("platforms", "{ref}", "accounts"): ("GET",),
    ("provider-approvals",): ("GET",),
    ("provider-approvals", "approve"): ("POST",),
    ("provider-approvals", "revoke"): ("POST",),
    ("provider-policy",): ("GET",),
    ("audit",): ("GET",),
}
"""Routes under `ADMIN_API_PATH` and their methods."""


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _id(value: UUID | None) -> str | None:
    return str(value) if value else None


def _text(body: dict, name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_text(body: dict, name: str) -> str | None:
    value = body.get(name)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value or None


def _uuid(value: str | None, name: str) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        raise ValueError(f"{name} is not a valid UUID") from None


def user_json(user: DirectoryUser) -> dict[str, object]:
    return {
        "id": str(user.id),
        "display_name": user.display_name,
        "email": user.email,
        "status": user.status,
        "created_at": _iso(user.created_at),
        "disabled_at": _iso(user.disabled_at),
        "platform_roles": list(user.platform_roles),
    }


def project_json(project: Project) -> dict[str, object]:
    return {
        "id": str(project.id),
        "name": project.name,
        "organization_id": _id(project.organization_id),
        "owner_user_id": str(project.owner_user_id),
        "created_by_user_id": str(project.created_by_user_id),
        "created_at": _iso(project.created_at),
        "deleted_at": _iso(project.deleted_at),
    }


def platform_json(platform: Platform) -> dict[str, object]:
    return {
        "id": str(platform.id),
        "slug": platform.slug,
        "display_name": platform.display_name,
        "operator_name": platform.operator_name,
        "scheduler": platform.scheduler,
        "source": platform.source,
        "status": platform.status,
        "is_default": platform.is_default,
    }


def account_json(account: SlurmAccount) -> dict[str, object]:
    return {
        "id": account.id,
        "platform_id": str(account.platform_id),
        "account_name": account.account_name,
        "kind": account.kind,
        "status": account.status,
        "is_mode_a_default": account.is_mode_a_default,
    }


def link_json(link: ProjectAccountLink) -> dict[str, object]:
    return {
        "project_id": str(link.project_id),
        "platform_id": str(link.platform_id),
        "account": account_json(link.account),
        "modes": list(link.modes),
        "is_default": link.is_default,
    }


def approval_json(approval: ProviderApproval) -> dict[str, object]:
    return {
        "provider": approval.provider,
        "project_id": str(approval.project_id),
        "status": approval.status,
        "created_at": _iso(approval.created_at),
        "updated_at": _iso(approval.updated_at),
    }


def audit_json(event: SecurityAuditEvent) -> dict[str, object]:
    return {
        "id": event.id,
        "occurred_at": _iso(event.occurred_at),
        "event": event.event,
        "outcome": event.outcome,
        "interface": event.interface,
        "actor_user_id": _id(event.actor_user_id),
        "actor_operator": event.actor_operator,
        "database_role": event.database_role,
        "project_id": _id(event.project_id),
        "target_user_id": _id(event.target_user_id),
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "reason": event.reason,
        "note": event.note,
        "details": event.details,
    }


def policy_json(policy: ProviderPolicy | None) -> dict[str, object]:
    if policy is None:
        return {"source": "unknown", "provider": None, "approvals_effective": False}
    return {
        "source": policy.source,
        "provider": policy.provider,
        "approvals_effective": policy.approvals_effective,
    }


def access_json(access: ProjectAccess) -> dict[str, object]:
    return {
        "project": project_json(access.project),
        "platforms": [platform_json(p) for p in access.platforms],
        "accounts": [link_json(link) for link in access.accounts],
        "provider_approvals": [approval_json(a) for a in access.approvals],
    }


def _page[T](page: Page[T], key: str, render: Callable[[T], dict[str, Any]]) -> dict[str, object]:
    return {key: [render(item) for item in page.items], "next_after": page.next_after}


def admin_api(handler: _Handler, method: str, path: str, who: Principal, body: dict) -> None:
    """Runs one `/api/admin` route. Authorization happens in [`authz.AdminService`]."""
    rest = path[len(ADMIN_API_PATH) :].strip("/")
    parts = rest.split("/") if rest else []
    shape: list[str] = []
    for i, part in enumerate(parts):
        if i == 1 and parts[0] in ("users", "projects"):
            if _uuid_or_none(part) is None:
                handler._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            shape.append("{id}")
        elif i == 1 and parts[0] == "platforms":
            shape.append("{ref}")
        else:
            shape.append(part)
    if not handler._route(method, ADMIN_ROUTES.get(tuple(shape))):
        return
    service = handler.app.admin
    query = handler._query_value
    route = tuple(shape)
    limit = int(query("limit") or 50)
    if route == ("users",):
        page = service.list_users(who, limit=limit, after=query("after"), email=query("email"))
        handler._json(HTTPStatus.OK, _page(page, "users", user_json))
    elif route == ("users", "{id}"):
        handler._json(HTTPStatus.OK, user_json(service.get_user(who, parts[1])))
    elif route == ("projects",) and method == "GET":
        page = service.list_projects(
            who,
            limit=limit,
            after=query("after"),
            include_deleted=query("include_deleted") == "true",
        )
        handler._json(HTTPStatus.OK, _page(page, "projects", project_json))
    elif route == ("projects",):
        project = service.create_project(
            who,
            _text(body, "name"),
            owner_user_id=_optional_text(body, "owner_user_id"),
            organization_id=_optional_text(body, "organization_id"),
            reason=_optional_text(body, "reason"),
        )
        handler._json(HTTPStatus.CREATED, project_json(project))
    elif route == ("projects", "{id}", "access"):
        access = access_json(service.project_access(who, parts[1]))
        handler._json(HTTPStatus.OK, {**access, "policy": policy_json(service.policy)})
    elif route[:3] == ("projects", "{id}", "platforms"):
        change = service.grant_platform if route[3] == "grant" else service.revoke_platform
        changed = change(
            who, parts[1], _text(body, "platform"), reason=_optional_text(body, "reason")
        )
        handler._json(HTTPStatus.OK, {"changed": changed})
    elif route == ("projects", "{id}", "accounts", "link"):
        modes = body.get("modes")
        if not isinstance(modes, list) or not all(isinstance(m, str) for m in modes):
            raise TypeError('modes must be a list such as ["A"]')
        default = body.get("default", False)
        if not isinstance(default, bool):
            raise TypeError("default must be true or false")
        link = service.link_account(
            who,
            parts[1],
            _text(body, "platform"),
            _text(body, "account"),
            modes=modes,
            default=default,
            reason=_optional_text(body, "reason"),
        )
        handler._json(HTTPStatus.OK, link_json(link))
    elif route == ("projects", "{id}", "accounts", "unlink"):
        service.unlink_account(
            who,
            parts[1],
            _text(body, "platform"),
            _text(body, "account"),
            reason=_optional_text(body, "reason"),
        )
        handler._json(HTTPStatus.OK, {"changed": True})
    elif route == ("platforms",):
        platforms = service.list_platforms(who)
        handler._json(HTTPStatus.OK, {"platforms": [platform_json(p) for p in platforms]})
    elif route == ("platforms", "{ref}", "accounts"):
        accounts = service.list_accounts(who, parts[1])
        handler._json(HTTPStatus.OK, {"accounts": [account_json(a) for a in accounts]})
    elif route == ("provider-approvals",):
        after = None
        raw = query("after")
        if raw is not None:
            provider, _, project = raw.partition(":")
            pid = _uuid(project, "after")
            assert pid is not None
            after = (provider, pid)
        page = service.list_approvals(
            who,
            provider=query("provider"),
            project_id=_uuid(query("project_id"), "project_id"),
            status=query("status"),
            limit=limit,
            after=after,
        )
        answer = _page(page, "provider_approvals", approval_json)
        handler._json(HTTPStatus.OK, {**answer, "policy": policy_json(service.policy)})
    elif route[0] == "provider-approvals":
        change = service.approve if route[1] == "approve" else service.revoke_approval
        changed = change(
            who,
            _text(body, "provider"),
            _text(body, "project_id"),
            reason=_optional_text(body, "reason"),
        )
        handler._json(HTTPStatus.OK, {"changed": changed, "policy": policy_json(service.policy)})
    elif route == ("provider-policy",):
        handler._json(HTTPStatus.OK, policy_json(service.provider_policy(who)))
    else:
        before = query("before")
        page = service.audit_events(
            who,
            limit=limit,
            before_id=int(before) if before is not None else None,
            event=query("event"),
            project_id=_uuid(query("project_id"), "project_id"),
            actor_user_id=_uuid(query("actor_user_id"), "actor_user_id"),
        )
        handler._json(HTTPStatus.OK, _page(page, "events", audit_json))


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None
