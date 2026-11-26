"""
Choosing the Nest Project a call runs in when `NEST_PROJECT_ID` is unset (E07).

A *Nest Project* is the server-side group whose membership, Provider approval and
Platform grants decide who may run what. It is unrelated to the local directory a
call's Python environment comes from (`remote(project=...)`).

[`project_selection.select_project`] decides, in this order:

1. `NEST_PROJECT_ID`, when set, always wins. The SDK does not check it first; the
   Controller authorizes it on every call.
2. The Project saved for this account on this Controller, if the Controller still
   lists it as eligible. A saved Project that is no longer eligible is forgotten.
3. The only eligible Project.
4. With several eligible Projects, a prompt on an interactive terminal; the
   answer is saved. Without a terminal, [`errors.ProjectSelectionError`] lists
   them and nothing runs.
5. With none, [`errors.ProjectSelectionError`] explains how to be invited.

"Eligible" is the Controller's answer to `ListEligibleProjects`: Projects where
the signed-in User's role allows submitting, the Provider approves the Project and
the Controller's Platform is granted to it, read when asked. It is advice only;
every execution is authorized again by the Controller, so a Project revoked after
it was chosen is refused and nothing runs (and a refused saved Project is
forgotten, see [`project_selection.forget_refused`]).

Saved choices live in `NEST_PREFERENCES_FILE` (default
`~/.config/nest/preferences.json`), readable only by their owner. They hold no
secret: only the sign-in origin, the Controller address, the Nest User ID and the
Project ID, so a choice made for one account or one Nest never applies to another.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any, cast
from uuid import UUID

import grpc

from ._logging import current_request_id, get_logger
from .config import ClientConfig
from .errors import AUTHORIZATION_METADATA, ProjectSelectionError
from .proto import execution_pb2, execution_pb2_grpc

logger = get_logger("nest.project_selection")

PREFERENCES_KIND = "nest-preferences"
PREFERENCES_VERSION = 1
LIST_TIMEOUT = 30.0
"""Seconds the SDK waits for `ListEligibleProjects`."""

_MAX_FILE_BYTES = 256 * 1024
_MAX_LISTED = 10
"""Most Projects named in an error message."""

SOURCES = ("NEST_PROJECT_ID", "saved", "only eligible", "chosen")
"""Where [`project_selection.ProjectChoice.source`] says the Project came from."""


@dataclass(frozen=True)
class EligibleProject:
    """A Project the Controller says the signed-in User may run in now."""

    id: str
    name: str
    role: str


@dataclass(frozen=True)
class ProjectChoice:
    """
    The Project a call runs in.

    Attributes:
        project_id (`str`): The Project's ID.
        source (`str`): One of [`project_selection.SOURCES`].
    """

    project_id: str
    source: str


@dataclass(frozen=True)
class PreferenceKey:
    """Whose choice, on which Nest: a choice applies only to the same three."""

    auth_url: str
    controller: str
    user_id: str


class PreferenceStore:
    """
    The saved Project choices, one per [`project_selection.PreferenceKey`], in a JSON
    file written atomically with mode `0600` in a `0700` directory. An unreadable or
    foreign file is treated as empty and replaced on the next save.

    Args:
        path (`str`): Absolute path of the preferences file.
    """

    def __init__(self, path: str):
        self.path = path

    def _records(self) -> list[dict[str, Any]]:
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return []
        with os.fdopen(fd) as handle:
            info = os.fstat(handle.fileno())
            if os.name == "posix" and info.st_uid != os.getuid():
                logger.warning("Ignoring a Nest preferences file owned by another user")
                return []
            try:
                data = json.loads(handle.read(_MAX_FILE_BYTES))
            except ValueError, UnicodeDecodeError:
                logger.warning("Ignoring an unreadable Nest preferences file")
                return []
        if (
            not isinstance(data, dict)
            or data.get("kind") != PREFERENCES_KIND
            or data.get("version") != PREFERENCES_VERSION
            or not isinstance(data.get("projects"), list)
        ):
            return []
        return [
            record
            for record in data["projects"]
            if isinstance(record, dict)
            and all(
                isinstance(record.get(k), str)
                for k in ("auth_url", "controller", "user_id", "project_id")
            )
        ]

    @staticmethod
    def _matches(record: Mapping[str, Any], key: PreferenceKey) -> bool:
        return (
            record["auth_url"] == key.auth_url
            and record["controller"] == key.controller
            and record["user_id"] == key.user_id
        )

    def get(self, key: PreferenceKey) -> str | None:
        """The saved Project ID for `key`, or `None`."""
        for record in self._records():
            if self._matches(record, key):
                return record["project_id"]
        return None

    def set(self, key: PreferenceKey, project_id: str) -> None:
        """Saves `project_id` for `key`, replacing only that key's choice."""
        records = [r for r in self._records() if not self._matches(r, key)]
        records.append(
            {
                "auth_url": key.auth_url,
                "controller": key.controller,
                "user_id": key.user_id,
                "project_id": str(UUID(project_id)),
                "saved_at": datetime.now(UTC).isoformat(),
            }
        )
        self._write(records)

    def remove(self, key: PreferenceKey) -> bool:
        """Forgets `key`'s choice. Returns whether there was one."""
        records = self._records()
        kept = [r for r in records if not self._matches(r, key)]
        if len(kept) == len(records):
            return False
        self._write(kept)
        return True

    def _write(self, records: list[dict[str, Any]]) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".preferences-", dir=directory)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(
                    {"kind": PREFERENCES_KIND, "version": PREFERENCES_VERSION, "projects": records},
                    handle,
                    indent=2,
                )
                handle.write("\n")
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise


def preference_key(config: ClientConfig, user_id: UUID | str) -> PreferenceKey | None:
    """The key of a choice for this account on this Controller; `None` without a sign-in origin."""
    if config.auth_url is None:
        return None
    return PreferenceKey(config.auth_url, config.address.lower(), str(user_id))


def store_for(config: ClientConfig) -> PreferenceStore | None:
    """The preferences file of `config`, or `None` when it names none."""
    return PreferenceStore(config.preferences_file) if config.preferences_file else None


def list_eligible(
    channel: grpc.Channel, token: str, *, timeout: float = LIST_TIMEOUT
) -> list[EligibleProject] | None:
    """
    Asks the Controller which Projects the session may run in now.

    Returns:
        `list[EligibleProject] | None`: The Projects, or `None` when the Controller
            does not authenticate callers, so no Project applies.

    Raises:
        [`errors.ProjectSelectionError`]: `unsupported` when the Controller predates
            Project selection.
        `grpc.RpcError`: For any other refusal, such as `UNAUTHENTICATED`.
    """
    stub = execution_pb2_grpc.ExecutionServiceStub(channel)
    try:
        response = stub.ListEligibleProjects(
            execution_pb2.ListEligibleProjectsRequest(),
            timeout=timeout,
            metadata=(
                ("x-nest-request-id", current_request_id()),
                (AUTHORIZATION_METADATA, f"Bearer {token}"),
            ),
        )
    except grpc.RpcError as exc:
        if cast(grpc.Call, exc).code() == grpc.StatusCode.UNIMPLEMENTED:
            raise ProjectSelectionError(
                "This Nest Controller cannot list your Projects (it predates Project "
                "selection). Set NEST_PROJECT_ID to the ID of the Project to run in.",
                "unsupported",
            ) from None
        raise
    if not response.projects_required:
        return None
    return [EligibleProject(p.id, p.name, p.role) for p in response.projects]


def describe(project: EligibleProject) -> str:
    name = "".join(ch if ch.isprintable() else "?" for ch in project.name)[:80]
    return f"{project.id}  {name} ({project.role})"


def _ask(
    projects: list[EligibleProject],
    out: IO[str],
    read: Callable[[str], str],
) -> EligibleProject:
    print("You can run in several Nest Projects on this Controller:", file=out)
    for number, project in enumerate(projects, 1):
        print(f"  {number}. {describe(project)}", file=out)
    while True:
        out.flush()
        answer = read(f"Run in which Project? [1-{len(projects)}] ").strip()
        if answer.isdecimal() and 1 <= int(answer) <= len(projects):
            return projects[int(answer) - 1]
        match = [p for p in projects if p.id == answer.lower()]
        if match:
            return match[0]
        print(f"Enter a number from 1 to {len(projects)}.", file=out)


def is_interactive() -> bool:
    """Whether a person can be asked: standard input and standard error are terminals."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except AttributeError, ValueError:
        return False


def select_project(
    config: ClientConfig,
    channel: grpc.Channel,
    token: str | None,
    user_id: UUID | str | None,
    *,
    interactive: bool | None = None,
    out: IO[str] | None = None,
    read: Callable[[str], str] | None = None,
) -> ProjectChoice | None:
    """
    Decides the Project a call runs in; see the module documentation.

    Args:
        config ([`config.ClientConfig`]): The client settings.
        channel (`grpc.Channel`): A channel to the Controller of `config`.
        token (`str`, *optional*): The session's access token. Without one no
            Project can be listed, and `None` is returned unless
            `NEST_PROJECT_ID` is set; the Controller then answers the call itself.
        user_id (`UUID`, *optional*): The signed-in User, keying the saved choice.
        interactive (`bool`, *optional*): Whether to prompt; defaults to
            [`project_selection.is_interactive`].
        out, read: Where the prompt is written and how the answer is read;
            standard error and `input` by default.

    Returns:
        [`project_selection.ProjectChoice`] or `None`: `None` when no Project
            applies (no sign-in origin, no session, a Controller without
            authentication, or one from before Project selection, which then
            answers the call as it always did).

    Raises:
        [`errors.ProjectSelectionError`]: If no Project can be chosen.
        `grpc.RpcError`: If the Controller refuses to list Projects.
    """
    if config.project_id is not None:
        return ProjectChoice(config.project_id, "NEST_PROJECT_ID")
    if config.auth_url is None or token is None or user_id is None:
        return None
    try:
        projects = list_eligible(channel, token, timeout=min(LIST_TIMEOUT, config.rpc_timeout))
    except ProjectSelectionError:
        # A Controller from before Project selection: send no Project, as before,
        # and let it answer (it asks for NEST_PROJECT_ID when it needs one).
        logger.info("The Controller cannot list Projects; sending none")
        return None
    if projects is None:
        return None
    out = out or sys.stderr
    store = store_for(config)
    key = preference_key(config, user_id)
    saved = store.get(key) if store is not None and key is not None else None
    if saved is not None:
        if any(p.id == saved for p in projects):
            return ProjectChoice(saved, "saved")
        logger.info("The saved Project is no longer eligible; forgetting it")
        print(
            f"nest: your saved Project {saved} is no longer available to you on this "
            "Controller; choosing again.",
            file=out,
        )
        if store is not None and key is not None:
            store.remove(key)
    if len(projects) == 1:
        return ProjectChoice(projects[0].id, "only eligible")
    if not projects:
        raise ProjectSelectionError(
            "You have no Nest Project that may run on this Controller. Ask a Project "
            "Owner or Admin to invite you (or to approve the Project for this HPC "
            "Platform), then try again. Nothing was run.",
            "none_eligible",
        )
    if not (is_interactive() if interactive is None else interactive):
        listed = "\n".join(f"  {describe(p)}" for p in projects[:_MAX_LISTED])
        more = (
            f"\n  ...and {len(projects) - _MAX_LISTED} more" if len(projects) > _MAX_LISTED else ""
        )
        raise ProjectSelectionError(
            "You can run in several Nest Projects and there is no terminal to ask which. "
            "Set NEST_PROJECT_ID to one of them, or save a choice with "
            "`nest projects use <ID>`. Nothing was run.\n" + listed + more,
            "ambiguous",
            tuple(p.id for p in projects),
        )
    chosen = _ask(projects, out, read or input)
    if store is not None and key is not None:
        store.set(key, chosen.id)
        print(
            f"nest: saved {chosen.id} as your Project here; change it with "
            "`nest projects use <ID>` or override it with NEST_PROJECT_ID.",
            file=out,
        )
    return ProjectChoice(chosen.id, "chosen")


def forget_refused(config: ClientConfig, user_id: UUID | str | None, choice: ProjectChoice) -> bool:
    """
    Forgets a saved choice the Controller refused at execution (membership,
    approval or grant revoked since it was listed), so the next call chooses
    again. Returns whether a saved choice was removed.
    """
    if choice.source != "saved" or user_id is None:
        return False
    store, key = store_for(config), preference_key(config, user_id)
    if store is None or key is None or store.get(key) != choice.project_id:
        return False
    return store.remove(key)
