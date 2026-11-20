"""
The fixed Project permission matrix, as a pure function.

[`authz.decide`] answers whether a caller with a given Project role, through a given
kind of session, may take an action. It reads nothing and caches nothing: the
caller supplies the role it has just read (see [`authz.Authorizer`]). Every rule
is an explicit allow; anything not allowed below is denied, including unknown
actions, unknown roles and callers with no role.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from ..db.identity import Session
from ..db.projects import ASSIGNABLE_ROLES, PROJECT_ROLES


class Action(StrEnum):
    """Actions on a Project and its contents, each checked by the policy."""

    PROJECT_READ = "project.read"
    """See the Project's name, ID and Owner."""
    PROJECT_UPDATE = "project.update"
    """Change Project settings, such as its name."""
    PROJECT_DELETE = "project.delete"
    """Delete the Project. Owner only."""
    PROJECT_TRANSFER = "project.transfer"
    """Transfer ownership to another member. Owner only."""
    PROJECT_AUDIT_READ = "project.audit.read"
    """Read the Project's membership and settings audit history."""
    MEMBER_LIST = "member.list"
    """List members and their roles."""
    MEMBER_INVITE = "member.invite"
    """Invite or add a member with a role (`Target.new_role`)."""
    INVITATION_MANAGE = "invitation.manage"
    """List, resend and revoke the Project's email invitations and Invite Links."""
    MEMBER_ROLE_CHANGE = "member.role.change"
    """Change a member's role (`Target.member_role` to `Target.new_role`)."""
    MEMBER_REMOVE = "member.remove"
    """Remove a member, or leave when the member is the caller."""
    RUN_READ = "run.read"
    """See the Project's Runs and their status."""
    RUN_SUBMIT = "run.submit"
    """Submit a Run. Also needs the Provider's permission."""
    RUN_CANCEL = "run.cancel"
    """Cancel one's own Run. Also needs the Provider's permission."""
    ARTIFACT_READ = "artifact.read"
    """Read a Run's ordinary outputs and logs."""
    ARTIFACT_READ_SENSITIVE = "artifact.read_sensitive"
    """Read a Run's outputs marked sensitive."""


READ_ACTIONS = frozenset(
    {Action.PROJECT_READ, Action.MEMBER_LIST, Action.RUN_READ, Action.ARTIFACT_READ}
)
"""Actions every role, including `viewer`, may take."""

PROVIDER_ACTIONS = frozenset({Action.RUN_SUBMIT, Action.RUN_CANCEL})
"""Actions that act on a Provider and so also need its permission."""

CLI_ACTION_SCOPES: dict[Action, frozenset[str]] = {
    Action.PROJECT_READ: frozenset({"projects:read", "jobs:run"}),
    Action.MEMBER_LIST: frozenset({"projects:read"}),
    Action.RUN_READ: frozenset({"projects:read", "jobs:run"}),
    Action.RUN_SUBMIT: frozenset({"jobs:run"}),
    Action.RUN_CANCEL: frozenset({"jobs:run"}),
    Action.ARTIFACT_READ: frozenset({"jobs:run"}),
    Action.ARTIFACT_READ_SENSITIVE: frozenset({"jobs:run"}),
}
"""Which CLI scopes allow each action; a CLI session needs any one of them. An
action missing here, such as every change to a Project or its members, needs a
browser session."""

CREATE_PROJECT_KINDS = frozenset({"browser"})
"""Session kinds that may create a Project."""


@dataclass(frozen=True)
class Principal:
    """
    An authenticated caller: a Nest User acting through one session.

    Platform and Organization roles are deliberately absent: neither grants any
    Project role, and nothing here grants Provider or HPC authority.

    Attributes:
        user_id (`UUID`): The signed-in User.
        session_kind (`str`): `browser` or `cli`.
        scopes (`frozenset[str]`): A CLI session's scopes; ignored for browsers.
        session_id (`UUID`, *optional*): The session, recorded in the audit log.
    """

    user_id: UUID
    session_kind: str
    scopes: frozenset[str] = frozenset()
    session_id: UUID | None = None

    @classmethod
    def from_session(cls, session: Session) -> Principal:
        """The caller of an authenticated session (see [`db.AccessGrant`])."""
        return cls(session.user_id, session.kind, session.scopes, session.id)


@dataclass(frozen=True)
class Target:
    """
    What an action acts on, when the rule depends on it.

    Attributes:
        member_user_id (`UUID`, *optional*): The member being changed or removed.
        member_role (`str`, *optional*): That member's current role.
        new_role (`str`, *optional*): The role being granted.
        run_submitted_by (`UUID`, *optional*): The submitter of the Run acted on.
        provider (`str`, *optional*): The Provider a Run uses.
    """

    member_user_id: UUID | None = None
    member_role: str | None = None
    new_role: str | None = None
    run_submitted_by: UUID | None = None
    provider: str | None = None


@dataclass(frozen=True)
class Decision:
    """
    The policy's answer.

    Attributes:
        allowed (`bool`): Whether the action may proceed.
        reason (`str`): `allowed`, or why not: `not_a_member`, `role`,
            `owner_only`, `owner_protected`, `owner_by_transfer_only`,
            `invalid_role`, `member_not_found`, `not_own_run`, `scope`, `browser_only`,
            `provider_denied` or `unknown_action`.
    """

    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(True, "allowed")

NO_TARGET = Target()
"""The empty [`authz.Target`], for actions that act on the Project itself."""


def _deny(reason: str) -> Decision:
    return Decision(False, reason)


def session_allows(principal: Principal, action: Action) -> Decision:
    """Whether the caller's kind of session, and a CLI session's scopes, allow `action`."""
    if principal.session_kind == "browser":
        return ALLOW
    if principal.session_kind == "cli":
        scopes = CLI_ACTION_SCOPES.get(action)
        if scopes is None:
            return _deny("browser_only")
        return ALLOW if scopes & principal.scopes else _deny("scope")
    return _deny("browser_only")


def may_create_project(principal: Principal) -> Decision:
    """Whether the caller may create a Project, of which they become Owner."""
    if principal.session_kind not in CREATE_PROJECT_KINDS:
        return _deny("browser_only")
    return ALLOW


def decide(
    principal: Principal, role: str | None, action: Action | str, target: Target = NO_TARGET
) -> Decision:
    """
    Applies the Project permission matrix.

    | Action | Owner | Admin | Member | Viewer |
    | --- | --- | --- | --- | --- |
    | Read the Project, members, Runs, ordinary artifacts | yes | yes | yes | yes |
    | Change settings, read audit history | yes | yes | no | no |
    | Invite or add `admin`, `member`, `viewer` | yes | yes | no | no |
    | List, resend, revoke invitations and Invite Links | yes | yes | no | no |
    | Change a non-Owner's role among those | yes | yes | no | no |
    | Remove a non-Owner member | yes | yes | no | no |
    | Leave the Project | no (transfer first) | yes | yes | yes |
    | Transfer ownership, delete the Project | yes | no | no | no |
    | Submit a Run (if the Provider permits) | yes | yes | yes | no |
    | Cancel one's own Run (if the Provider permits) | yes | yes | yes | no |
    | Read sensitive artifacts | yes | yes | own Runs | no |

    Nobody can grant `owner` except by an ownership transfer, and nobody can
    change or remove the Owner's membership. Provider permission is checked by
    [`authz.Authorizer`], not here.

    Args:
        principal ([`authz.Principal`]): The caller.
        role (`str` or `None`): The caller's current role, `None` if not a member.
        action ([`authz.Action`]): What they want to do.
        target ([`authz.Target`], *optional*): What it acts on.
    """
    try:
        action = Action(action)
    except ValueError:
        return _deny("unknown_action")
    if role not in PROJECT_ROLES:
        return _deny("not_a_member")
    session = session_allows(principal, action)
    if not session:
        return session
    managers = ("owner", "admin")

    if action in READ_ACTIONS:
        return ALLOW
    if action in (Action.PROJECT_UPDATE, Action.PROJECT_AUDIT_READ, Action.INVITATION_MANAGE):
        return ALLOW if role in managers else _deny("role")
    if action in (Action.PROJECT_DELETE, Action.PROJECT_TRANSFER):
        return ALLOW if role == "owner" else _deny("owner_only")
    if action == Action.MEMBER_INVITE:
        if role not in managers:
            return _deny("role")
        return _grantable(target.new_role)
    if action == Action.MEMBER_ROLE_CHANGE:
        if role not in managers:
            return _deny("role")
        if target.member_role == "owner":
            return _deny("owner_protected")
        if target.member_role not in ASSIGNABLE_ROLES:
            return _deny("member_not_found")
        return _grantable(target.new_role)
    if action == Action.MEMBER_REMOVE:
        if target.member_role == "owner":
            return _deny("owner_protected")
        if target.member_role not in ASSIGNABLE_ROLES:
            return _deny("member_not_found")
        if target.member_user_id == principal.user_id or role in managers:
            return ALLOW
        return _deny("role")
    if action == Action.RUN_SUBMIT:
        return ALLOW if role in (*managers, "member") else _deny("role")
    if action == Action.RUN_CANCEL:
        if role not in (*managers, "member"):
            return _deny("role")
        if target.run_submitted_by != principal.user_id:
            return _deny("not_own_run")
        return ALLOW
    if action == Action.ARTIFACT_READ_SENSITIVE:
        if role in managers:
            return ALLOW
        if role == "member" and target.run_submitted_by == principal.user_id:
            return ALLOW
        return _deny("role")
    return _deny("unknown_action")


def _grantable(new_role: str | None) -> Decision:
    if new_role == "owner":
        return _deny("owner_by_transfer_only")
    if new_role not in ASSIGNABLE_ROLES:
        return _deny("invalid_role")
    return ALLOW
