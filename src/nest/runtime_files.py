"""
Private runtime files generated from environment values.

Some consumers only accept file paths: OpenSSH (keys, `known_hosts`, `ssh_config`)
and libpq (`sslrootcert`, `sslcert`, `sslkey`). When an operator supplies those
values directly in the environment, Nest writes them here instead of asking for a
mounted file. Every write is a literal byte copy (no shell, no expansion), creates
the file exclusively without following symlinks, and sets the mode before any byte
is written: private keys and other credentials are `0600`, directories `0700`.

Generated files live in a fresh directory under the system temporary directory,
which the Compose deployments mount as `tmpfs`, so they disappear with the
container and are recreated from the environment on every start.

The container entrypoint also calls [`runtime_files.main`] (`ssh <dir> <user>`) as
root before dropping privileges, to place Mode A SSH files given as `NEST_SSH_CONFIG`,
`NEST_SSH_PRIVATE_KEY` and `NEST_SSH_KNOWN_HOSTS_DATA` into the Controller user's
private `~/.ssh` copy (see [`runtime_files.materialize_ssh_home`]).
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tempfile
from collections.abc import Mapping

SSH_IDENTITY_NAME = "id_nest"
"""File name of an inline Mode A private key inside `~/.ssh`."""

# OpenSSH settings that disable or weaken host-key verification. Inline SSH
# configuration containing any of them is refused; a mounted file is the operator's
# own responsibility, as before.
_WEAK_SSH_CONFIG = (
    re.compile(
        r"^\s*StrictHostKeyChecking(?:\s+|\s*=\s*)[\"']?(?:no|off|false|accept-new|ask)\b",
        re.IGNORECASE | re.MULTILINE,
    ),
    re.compile(
        r"^\s*(?:Global|User)KnownHostsFile(?:\s+|\s*=\s*)[\"']?/dev/null\b",
        re.IGNORECASE | re.MULTILINE,
    ),
)


class RuntimeFileError(ValueError):
    """A runtime file cannot be written safely. Messages never contain file contents."""


def normalize_content(value: str) -> str:
    """
    Returns multiline content given in one environment value, ready to write.

    Carriage returns are dropped, a literal two-character `\\n` becomes a line break
    (for editors that cannot store real line breaks), surrounding blank space is
    trimmed, and the result ends with exactly one newline.

    Args:
        value (`str`): Raw environment value.

    Returns:
        `str`: The normalized content.
    """
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\\n", "\n")
    return text.strip() + "\n"


def private_directory(prefix: str = "nest-") -> str:
    """
    Creates a new directory only the current user can open.

    Args:
        prefix (`str`, *optional*): Name prefix inside the temporary directory.

    Returns:
        `str`: Absolute path of the new, empty `0700` directory.
    """
    directory = tempfile.mkdtemp(prefix=prefix)
    os.chmod(directory, 0o700)
    return directory


def write_private_file(
    directory: str,
    name: str,
    content: str,
    *,
    mode: int = 0o600,
    owner: tuple[int, int] | None = None,
) -> str:
    """
    Writes `content` to a new file, or accepts an identical existing one.

    The file is created with `O_EXCL | O_NOFOLLOW`, so an existing file or symlink is
    never replaced. An existing regular file with exactly the same content is
    accepted (the same deployment restarting); any other existing file is an error.

    Args:
        directory (`str`): Existing directory, normally from
            [`runtime_files.private_directory`].
        name (`str`): File name, without a path separator.
        content (`str`): Exact text to write.
        mode (`int`, *optional*, defaults to `0o600`): Permission bits.
        owner (`tuple[int, int]`, *optional*): `(uid, gid)` to assign, when running
            as root on behalf of another user.

    Returns:
        `str`: Path of the file.

    Raises:
        [`runtime_files.RuntimeFileError`]: If a different file already exists.
    """
    if not name or "/" in name or name in (".", ".."):
        raise RuntimeFileError(f"Invalid runtime file name {name!r}")
    path = os.path.join(directory, name)
    data = content.encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, mode)
    except FileExistsError:
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            with open(path, "rb") as handle:
                if handle.read() == data:
                    return path
        raise RuntimeFileError(
            f"{path} already exists with different content (for example from the mounted "
            "SSH directory); supply it either as a file or in the environment, not both"
        ) from None
    try:
        os.fchmod(fd, mode)
        if owner is not None:
            os.fchown(fd, *owner)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)
    return path


def check_ssh_config(text: str) -> None:
    """
    Refuses inline SSH configuration that weakens host-key verification.

    Raises:
        [`runtime_files.RuntimeFileError`]: If `StrictHostKeyChecking` is not `yes`
            or a known-hosts file is `/dev/null`.
    """
    for pattern in _WEAK_SSH_CONFIG:
        if pattern.search(text):
            raise RuntimeFileError(
                "NEST_SSH_CONFIG must keep host-key verification on: use "
                "StrictHostKeyChecking yes and a real known_hosts file"
            )


def check_private_key(name: str, text: str) -> None:
    """Raises [`runtime_files.RuntimeFileError`] unless `text` looks like a private key."""
    if "PRIVATE KEY-----" not in text:
        raise RuntimeFileError(f"{name} does not look like a PEM or OpenSSH private key")


def materialize_ssh_home(
    directory: str,
    environ: Mapping[str, str] | None = None,
    *,
    owner: tuple[int, int] | None = None,
) -> list[str]:
    """
    Writes Mode A SSH files given in the environment into a private `~/.ssh`.

    Runs only when `NEST_SSH_CONFIG` is set, so deployments that mount an SSH
    directory, and Mode B deployments, are unchanged. `NEST_SSH_CONFIG` becomes
    `config`, `NEST_SSH_KNOWN_HOSTS_DATA` becomes `known_hosts`, and
    `NEST_SSH_PRIVATE_KEY` becomes `id_nest` (reference it with
    `IdentityFile ~/.ssh/id_nest`). A file already copied from the mounted SSH
    directory is never replaced.

    Args:
        directory (`str`): The private `~/.ssh` directory.
        environ (`Mapping[str, str]`, *optional*): Environment. Defaults to `os.environ`.
        owner (`tuple[int, int]`, *optional*): `(uid, gid)` of the Controller user.

    Returns:
        `list[str]`: Names of the files written, for a log line without contents.

    Raises:
        [`runtime_files.RuntimeFileError`]: If a value is invalid or conflicts with a
            mounted file.
    """
    env = os.environ if environ is None else environ
    config = env.get("NEST_SSH_CONFIG", "").strip()
    if not config:
        return []
    files = {"config": normalize_content(config)}
    check_ssh_config(files["config"])
    known_hosts = env.get("NEST_SSH_KNOWN_HOSTS_DATA", "").strip()
    if known_hosts:
        files["known_hosts"] = normalize_content(known_hosts)
    key = env.get("NEST_SSH_PRIVATE_KEY", "").strip()
    if key:
        check_private_key("NEST_SSH_PRIVATE_KEY", key)
        files[SSH_IDENTITY_NAME] = normalize_content(key)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    for name, content in files.items():
        write_private_file(directory, name, content, owner=owner)
    return sorted(files)


def main(argv: list[str] | None = None) -> int:
    """
    Container entrypoint helper: `ssh <directory> [user]`.

    Returns:
        `int`: `0` on success, `2` when a value is invalid or conflicts.
    """
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (2, 3) or args[0] != "ssh":
        print("usage: nest.runtime_files ssh <directory> [user]", file=sys.stderr)
        return 2
    owner = None
    if len(args) == 3:
        import pwd

        entry = pwd.getpwnam(args[2])
        owner = (entry.pw_uid, entry.pw_gid)
    try:
        written = materialize_ssh_home(args[1], owner=owner)
    except RuntimeFileError as exc:
        print(f"nest: {exc}", file=sys.stderr)
        return 2
    if written:
        print(f"nest: wrote SSH files from the environment: {', '.join(written)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
