"""
Connection/session transports for the shared Slurm execution core.

[`backends.slurm.SlurmBackend`] holds the Slurm logic: preparing environments,
staging and submitting jobs, status, logs, results, cancellation and accounting. It
runs every remote command through a [`backends.transport.SlurmTransport`], which
owns the connection and says, through its
[`backends.transport.TransportCapabilities`], whose authority runs the commands and
what it can do while nobody is present.

- [`backends.transport.ControllerSSHTransport`] is the current trusted path (Mode A):
  the Controller's own noninteractive OpenSSH, with the operator's configuration or
  managed key. Its argv is exactly what `SlurmBackend` always ran.
- [`backends.transport.LocalOpenSSHTransport`] runs the user's local OpenSSH on the
  client, answering the HPC password/MFA prompts in the user's terminal. It is the
  client-owned transport for future user-owned Mode B. It never receives Controller
  credentials, cannot run unattended, and is not wired into the Controller: the
  opaque relay (`ProxyCommand`) and actual user-owned submission are later work.

A transport that cannot run a command (no active user session, no terminal for
the prompts) raises [`errors.SessionUnavailableError`]. Nothing ever falls back
to Controller-owned SSH.
"""

import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Self

from .._logging import get_logger
from ..config import child_environ
from ..errors import (
    InfrastructureError,
    RemoteCommandError,
    ResultTooLargeError,
    SSHError,
    SSHTimeoutError,
)

if TYPE_CHECKING:
    from .slurm import SlurmConfig

logger = get_logger(__name__)


class ExecutionAuthority(StrEnum):
    """Whose authenticated HPC identity runs the Slurm commands."""

    CONTROLLER_OWNED = "controller_owned"
    """The Controller's operator-provisioned SSH identity (trusted Mode A)."""

    USER_OWNED = "user_owned"
    """The user's own SSH session, authenticated by the user (future Mode B)."""


class Operation(StrEnum):
    """What a Slurm core operation does, for checking a transport's capabilities."""

    PREPARE = "prepare"
    SUBMIT = "submit"
    STATUS = "status"
    LOGS = "logs"
    RESULT = "result"
    CANCEL = "cancel"
    ACCOUNTING = "accounting"


_QUERIES = frozenset({Operation.STATUS, Operation.LOGS, Operation.RESULT, Operation.ACCOUNTING})
"""Read-only operations, which `can_query_offline` covers."""


@dataclass(frozen=True)
class TransportCapabilities:
    """
    What a transport can do, advertised to the Slurm core and its callers.

    Attributes:
        execution_authority ([`backends.transport.ExecutionAuthority`]): Whose HPC
            identity runs the commands.
        interactive_required (`bool`): A person may have to answer password or MFA
            prompts in a terminal before a command can run.
        can_query_offline (`bool`): Status, logs, results and accounting can be read
            without the user's live session (for example after the client exits).
        can_cancel_offline (`bool`): A job can be cancelled without the user's live
            session.
        can_submit_offline (`bool`): Environments can be prepared and jobs submitted
            without the user's live session.
    """

    execution_authority: ExecutionAuthority
    interactive_required: bool
    can_query_offline: bool
    can_cancel_offline: bool
    can_submit_offline: bool

    def allows_offline(self, operation: Operation) -> bool:
        """Whether `operation` can run without the user's live session."""
        if operation in _QUERIES:
            return self.can_query_offline
        if operation == Operation.CANCEL:
            return self.can_cancel_offline
        return self.can_submit_offline


CONTROLLER_SSH_CAPABILITIES = TransportCapabilities(
    execution_authority=ExecutionAuthority.CONTROLLER_OWNED,
    interactive_required=False,
    can_query_offline=True,
    can_cancel_offline=True,
    can_submit_offline=True,
)
"""The Controller's own SSH: noninteractive and always available to the Controller."""

LOCAL_OPENSSH_CAPABILITIES = TransportCapabilities(
    execution_authority=ExecutionAuthority.USER_OWNED,
    interactive_required=True,
    can_query_offline=False,
    can_cancel_offline=False,
    can_submit_offline=False,
)
"""The user's local OpenSSH: every operation needs the user's live, authenticated session."""


@dataclass(frozen=True)
class CommandResult:
    """
    The outcome of one remote command, before it is classified.

    Attributes:
        returncode (`int`): Exit status. `255` means the transport itself failed.
        stdout (`bytes`): Standard output.
        stderr (`bytes`): Standard error.
    """

    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class SlurmTransport:
    """
    Runs remote shell commands on a login node for the Slurm core.

    Subclasses implement [`backends.transport.SlurmTransport.run`] and may override
    the others. Implementations never retry a command: the core decides what an
    ambiguous outcome means.

    Attributes:
        capabilities ([`backends.transport.TransportCapabilities`]): What the
            transport can do.
        target (`str`): Login node named in messages, such as a host name.
        timeout (`float`): Seconds allowed for one command, used in messages.
    """

    capabilities: TransportCapabilities
    target: str
    timeout: float

    def session_active(self) -> bool:
        """
        Whether a command can run now without waiting for anyone.

        A user-owned transport is active only while the user's session is (or can
        be) authenticated in front of them; the Controller's own SSH always is.
        """
        return True

    def run(self, command: str, input: bytes | None = None) -> CommandResult:
        """
        Runs `command` once and returns its outcome, unclassified.

        Raises:
            [`errors.SSHTimeoutError`]: If the command exceeds the transport's
                timeout. The remote command may still have run.
            [`errors.SessionUnavailableError`]: If the transport cannot run commands.
        """
        raise NotImplementedError

    def run_bounded(self, command: str, limit: int) -> bytes:
        """
        Runs `command` and returns its standard output, refusing more than `limit` bytes.

        The default implementation reads the whole output first; streaming
        transports override it so memory stays bounded.

        Raises:
            [`errors.ResultTooLargeError`]: If the output exceeds `limit`.
            [`errors.SSHTimeoutError`], [`errors.SSHError`],
            [`errors.RemoteCommandError`]: As [`backends.transport.SlurmTransport.check`].
        """
        stdout = self.check(command)
        if len(stdout) > limit:
            raise _too_large(limit)
        return stdout

    def check(self, command: str, input: bytes | None = None) -> bytes:
        """
        Runs `command` and returns its standard output, raising on failure.

        Raises:
            [`errors.SSHTimeoutError`]: If the command exceeds the timeout.
            [`errors.SSHError`]: If the transport fails (exit status 255).
            [`errors.RemoteCommandError`]: If the remote command exits unsuccessfully.
        """
        result = self.run(command, input)
        if result.returncode != 0:
            raise self.error(result)
        return result.stdout

    def error(self, result: CommandResult) -> InfrastructureError:
        """
        Classifies an unsuccessful command.

        Returns:
            [`errors.InfrastructureError`]: [`errors.SSHError`] for exit status 255,
                which SSH reserves for its own failures, otherwise
                [`errors.RemoteCommandError`].
        """
        message = (result.stderr or result.stdout or b"").decode(errors="replace").strip()
        if result.returncode == 255:
            return SSHError(f"SSH to {self.target} failed:\n{message}")
        return RemoteCommandError(
            f"Remote command failed:\n{message}", returncode=result.returncode
        )

    def timed_out(self) -> SSHTimeoutError:
        """The error for a command that exceeded the timeout."""
        return SSHTimeoutError(
            f"SSH command to {self.target} timed out after {self.timeout:g} seconds. "
            "The remote operation status is unknown."
        )

    def for_endpoint(self, host: str, port: int | None) -> SlurmTransport:
        """
        The same transport and identity connected to another login node.

        The Slurm core checks that the node is approved before calling this.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot change login nodes")


def _too_large(limit: int) -> ResultTooLargeError:
    return ResultTooLargeError(
        f"The result is larger than {limit} bytes, the most this Controller returns "
        "(NEST_GRPC_MAX_MESSAGE_BYTES); it was not downloaded"
    )


class _OpenSSHTransport(SlurmTransport):
    """Runs commands with the local `ssh` executable; subclasses build its argv."""

    _STDERR_KEEP = 64 * 1024
    """Bytes of a bounded command's standard error kept for its error message."""

    def ssh_argv(self) -> list[str]:
        """The `ssh` command line, without the remote command."""
        raise NotImplementedError

    def _environ(self) -> dict[str, str]:
        """The environment `ssh` runs with: never Nest credentials."""
        return child_environ()

    def _process(
        self, command: str, input: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        """
        Runs one SSH command without classifying its outcome.

        Raises:
            `subprocess.TimeoutExpired`: If SSH exceeds the timeout. The process is
                killed, but the remote command may still have run.
        """
        return subprocess.run(
            [*self.ssh_argv(), command],
            check=False,
            # Never hand Nest credentials to ssh, where a SendEnv setting could
            # forward them to the Login Node.
            env=self._environ(),
            input=input,
            capture_output=True,
            timeout=self.timeout,
        )

    def run(self, command: str, input: bytes | None = None) -> CommandResult:
        started = time.monotonic()
        logger.debug("SSH command started host=%s", self.target)
        try:
            result = self._process(command, input)
        except subprocess.TimeoutExpired as exc:
            raise self.timed_out() from exc
        logger.debug(
            "SSH command finished host=%s returncode=%s elapsed_ms=%.1f",
            self.target,
            result.returncode,
            (time.monotonic() - started) * 1000,
        )
        # Standard error only explains a failure, so a successful command drops it.
        stderr = (result.stderr or b"") if result.returncode != 0 else b""
        return CommandResult(result.returncode, result.stdout or b"", stderr)

    def run_bounded(self, command: str, limit: int) -> bytes:
        """
        Runs an SSH command and returns its standard output, reading at most
        `limit` bytes of it into memory (and a bounded part of standard error).

        Raises:
            [`errors.ResultTooLargeError`]: If the output exceeds `limit`; SSH is
                killed and reaped.
            [`errors.SSHTimeoutError`], [`errors.SSHError`],
            [`errors.RemoteCommandError`]: As [`backends.transport.SlurmTransport.check`].
        """
        deadline = time.monotonic() + self.timeout
        process = subprocess.Popen(
            [*self.ssh_argv(), command],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._environ(),
        )
        assert process.stdout is not None and process.stderr is not None
        out_fd, err_fd = process.stdout.fileno(), process.stderr.fileno()
        out = bytearray()
        kept_err = bytearray()
        too_large = timed_out = False
        # One deadline covers reading both pipes, the process's exit and its reaping.
        # select() never blocks past it, even if the pipes stay open after EOF on one
        # of them or after SSH has exited.
        with selectors.DefaultSelector() as selector:
            selector.register(out_fd, selectors.EVENT_READ)
            selector.register(err_fd, selectors.EVENT_READ)
            try:
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(remaining):
                        fd = key.fd
                        if fd == out_fd:
                            chunk = os.read(fd, min(65536, limit + 1 - len(out)))
                            out.extend(chunk)
                            if len(out) > limit:
                                too_large = True
                        else:
                            chunk = os.read(fd, 65536)
                            # Keep the head for the message; discard the rest.
                            if len(kept_err) < self._STDERR_KEEP:
                                kept_err.extend(chunk[: self._STDERR_KEEP - len(kept_err)])
                        if not chunk:
                            selector.unregister(fd)
                    if too_large:
                        break
                if not (too_large or timed_out):
                    try:
                        process.wait(timeout=max(0.0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        # Output ended, but SSH is still running.
                        timed_out = True
            finally:
                if process.poll() is None:
                    process.kill()
                # SIGKILL cannot be ignored, so the reap is prompt.
                process.wait()
                process.stdout.close()
                process.stderr.close()
        if too_large:
            raise _too_large(limit)
        if timed_out:
            raise self.timed_out()
        if process.returncode != 0:
            raise self.error(CommandResult(process.returncode, bytes(out), bytes(kept_err)))
        return bytes(out)


class ControllerSSHTransport(_OpenSSHTransport):
    """
    The Controller's own noninteractive SSH to a login node (trusted Mode A).

    Uses the operator's SSH configuration (`ssh_mode` `"A"`) or the managed key and
    pinned host keys (`ssh_mode` `"B"`) from a [`backends.slurm.SlurmConfig`]. The
    command line is unchanged from the backend that owned SSH directly.

    Args:
        config ([`backends.slurm.SlurmConfig`]): Host, port, user, SSH mode,
            credential paths and timeout.
    """

    capabilities = CONTROLLER_SSH_CAPABILITIES

    def __init__(self, config: SlurmConfig):
        self.config = config
        self.target = config.ssh_host
        self.timeout = config.ssh_timeout

    def ssh_argv(self) -> list[str]:
        """
        Builds the SSH command line, without the remote command, for the SSH mode.

        Returns:
            `list[str]`: Mode A: `ssh -o BatchMode=yes [-p port] [-l user] host`.
                Mode B adds `-F none`, strict host-key checking against
                `known_hosts_file`, and only `identity_file` as a key, so no SSH
                configuration file, agent, or proxy setting can change the connection.
        """
        config = self.config
        argv = ["ssh"]
        if config.ssh_mode == "B":
            known_hosts = str(config.known_hosts_file)
            argv += ["-F", "none"]
            argv += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
            argv += ["-o", f"UserKnownHostsFile={known_hosts}"]
            argv += ["-o", f"GlobalKnownHostsFile={known_hosts}"]
            argv += ["-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none"]
            argv += ["-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes"]
            argv += ["-i", str(config.identity_file), "-p", str(config.ssh_port or 22)]
        else:
            argv += ["-o", "BatchMode=yes"]
            if config.ssh_port is not None:
                argv += ["-p", str(config.ssh_port)]
        if config.ssh_user:
            argv += ["-l", config.ssh_user]
        argv.append(config.ssh_host)
        return argv

    def for_endpoint(self, host: str, port: int | None) -> ControllerSSHTransport:
        return ControllerSSHTransport(replace(self.config, ssh_host=host, ssh_port=port))


_CONTROL_SOCKET_NAME = 40
"""Length of OpenSSH's `%C` control socket name (a SHA-1 hex digest)."""

_CONTROL_SOCKET_SUFFIX = 17
"""Length of the `.<16 random characters>` name a master listens on before renaming."""


def _socket_path_limit() -> int:
    """Longest Unix socket path, excluding its terminating NUL, on this platform."""
    return (108 if sys.platform.startswith("linux") else 104) - 1


def _check_control_dir(control_dir: str) -> None:
    """
    Raises `ValueError` if OpenSSH's control socket in `control_dir` would not fit a
    Unix socket address, which would make every connection fail.
    """
    length = len(os.fsencode(control_dir)) + 1 + _CONTROL_SOCKET_NAME + _CONTROL_SOCKET_SUFFIX
    if length > _socket_path_limit():
        raise ValueError(
            f"SSH control directory {control_dir!r} is too long for a Unix socket "
            f"({length} > {_socket_path_limit()} bytes); choose a shorter one"
        )


def _has_terminal() -> bool:
    """Whether this process has a controlling terminal OpenSSH can prompt on."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


class LocalOpenSSHTransport(_OpenSSHTransport):
    """
    The user's own local OpenSSH session to a login node (client-owned, for Mode B).

    Runs on the client. OpenSSH prompts for the HPC password and MFA on the user's
    terminal, never through Nest, and one authenticated connection is reused for
    later commands through a private OpenSSH control socket. No SSH configuration
    file is read, host keys are checked strictly against operator-distributed pins,
    and agent and port forwarding are disabled. Only the user's password and
    keyboard-interactive (MFA) answers authenticate: public keys, including the
    running process's default `~/.ssh/id_*` keys, the SSH agent, GSSAPI and
    host-based authentication are disabled, so it can never log in with an
    identity it happens to find, such as a Controller service account's.

    Before the first command, [`backends.transport.LocalOpenSSHTransport.login`]
    authenticates with OpenSSH's own messages (the server banner,
    keyboard-interactive MFA instructions) on the user's terminal; remote commands
    then run with their output captured. Use it as a context manager, or call `close()`, so the
    authenticated connection and its private directory end with the run.

    Without a terminal and without a live authenticated connection, the session is
    inactive and the Slurm core raises [`errors.SessionUnavailableError`]: a headless
    run fails clearly instead of hanging on a prompt or using another identity.

    Args:
        host (`str`): Approved login node.
        user (`str`): The user's own HPC username.
        known_hosts_file (`str`): Operator-distributed, verified host keys.
        port (`int`, *optional*, defaults to `22`): SSH port.
        timeout (`float`, *optional*, defaults to `60`): Seconds per remote command.
        login_timeout (`float`, *optional*, defaults to `300`): Seconds allowed for
            the login, including the time the user takes to answer the prompts.
        control_dir (`str`, *optional*): Private directory for the control socket.
            Defaults to a new directory under `/tmp` only this user can read, which
            [`backends.transport.LocalOpenSSHTransport.close`] removes. Its socket
            path must fit a Unix socket address (104 bytes on macOS and BSD).
        control_persist (`int`, *optional*, defaults to `600`): Seconds an idle
            authenticated connection stays open for reuse. `close()` (or leaving a
            `with` block) ends it at the end of the run instead.
        has_terminal (`Callable[[], bool]`, *optional*): Whether prompts can be shown.
            Defaults to checking for a controlling terminal.
    """

    capabilities = LOCAL_OPENSSH_CAPABILITIES

    def __init__(
        self,
        host: str,
        user: str,
        known_hosts_file: str,
        *,
        port: int = 22,
        timeout: float = 60,
        login_timeout: float = 300,
        control_dir: str | None = None,
        control_persist: int = 600,
        has_terminal: Callable[[], bool] = _has_terminal,
    ):
        for name, value in (("host", host), ("user", user)):
            if not value or value.startswith("-") or any(c.isspace() for c in value):
                raise ValueError(f"Invalid SSH {name} {value!r}")
        if not known_hosts_file:
            raise ValueError("Local OpenSSH requires verified host keys (known_hosts_file)")
        if timeout <= 0 or login_timeout <= 0 or control_persist < 0:
            raise ValueError("Timeouts must be positive and control_persist nonnegative")
        self.target = host
        self.user = user
        self.port = port
        self.known_hosts_file = known_hosts_file
        self.timeout = timeout
        self.login_timeout = login_timeout
        self.control_persist = control_persist
        if control_dir is not None:
            _check_control_dir(control_dir)
        self._control_dir = control_dir
        self._owns_control_dir = False
        self._has_terminal = has_terminal

    @property
    def control_dir(self) -> str:
        """The private control-socket directory, created on first use."""
        if self._control_dir is None:
            # A short base keeps the socket path within the Unix socket address
            # limit; macOS's per-user $TMPDIR alone is about 49 bytes. mkdtemp
            # creates a new directory only this user can access.
            base = "/tmp" if os.path.isdir("/tmp") else None
            path = tempfile.mkdtemp(prefix="nest-", dir=base)
            try:
                _check_control_dir(path)
            except ValueError:
                os.rmdir(path)
                raise
            self._control_dir = path
            self._owns_control_dir = True
        return self._control_dir

    def _base_argv(self) -> list[str]:
        known_hosts = self.known_hosts_file
        return [
            "ssh",
            "-F",
            "none",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            f"GlobalKnownHostsFile={known_hosts}",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            # Only the user's own password and keyboard-interactive (MFA) answers
            # authenticate: never keys, an agent, Kerberos or host-based identity
            # that the running process happens to have.
            "-o",
            "PreferredAuthentications=keyboard-interactive,password",
            "-o",
            "PubkeyAuthentication=no",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityFile=none",
            "-o",
            "IdentityAgent=none",
            "-o",
            "HostbasedAuthentication=no",
            "-o",
            "GSSAPIAuthentication=no",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={os.path.join(self.control_dir, '%C')}",
            "-o",
            f"ControlPersist={self.control_persist}",
            "-p",
            str(self.port),
            "-l",
            self.user,
        ]

    def ssh_argv(self) -> list[str]:
        """
        Builds the interactive SSH command line, without the remote command.

        Unlike the Controller's SSH, there is no `BatchMode`, so OpenSSH can ask the
        user for the password and MFA on their terminal. Only keyboard-interactive
        and password authentication are allowed: public keys (default
        `~/.ssh/id_*` files included), the SSH agent, GSSAPI and host-based
        authentication are all disabled.
        """
        return [*self._base_argv(), self.target]

    _AGENT_VARIABLES = ("SSH_AUTH_SOCK", "SSH_AGENT_PID")
    """Agent variables never passed to the user's `ssh`."""

    def _environ(self) -> dict[str, str]:
        env = child_environ()
        for name in self._AGENT_VARIABLES:
            env.pop(name, None)
        return env

    def connected(self) -> bool:
        """Whether an authenticated connection is open for reuse (`ssh -O check`)."""
        if self._control_dir is None:
            return False
        try:
            result = subprocess.run(
                [*self._base_argv(), "-O", "check", self.target],
                check=False,
                env=self._environ(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired, OSError:
            return False
        return result.returncode == 0

    def session_active(self) -> bool:
        return self._has_terminal() or self.connected()

    def login(self) -> None:
        """
        Authenticates the reusable connection unless one is already open.

        Runs a no-op command that leaves the authenticated master open. OpenSSH's
        standard error goes to the user's terminal here, so the server banner and
        keyboard-interactive MFA instructions are shown. Remote commands run after
        it, with their output captured as usual.

        Raises:
            [`errors.SSHError`]: If the login fails or does not finish within
                `login_timeout`. No remote command has run.
        """
        if self.connected():
            return
        try:
            result = subprocess.run(
                [*self.ssh_argv(), "true"],
                check=False,
                env=self._environ(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=None,
                timeout=self.login_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise SSHError(
                f"SSH login to {self.target} did not finish within "
                f"{self.login_timeout:g} seconds; no remote command ran"
            ) from exc
        if result.returncode != 0:
            raise SSHError(
                f"SSH login to {self.target} failed (exit status {result.returncode}); "
                "see the SSH output above. No remote command ran."
            )

    def run(self, command: str, input: bytes | None = None) -> CommandResult:
        self.login()
        return super().run(command, input)

    def _process(
        self, command: str, input: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        # Never hand the user's terminal to the remote command, where it would read
        # keystrokes typed while it runs; the prompts read /dev/tty, not stdin.
        return super()._process(command, b"" if input is None else input)

    def run_bounded(self, command: str, limit: int) -> bytes:
        self.login()
        return super().run_bounded(command, limit)

    def close(self) -> None:
        """
        Ends the reusable authenticated connection, if one is open, and removes the
        control directory this transport created. Never raises for SSH failures.
        """
        if self._control_dir is None:
            return
        try:
            subprocess.run(
                [*self._base_argv(), "-O", "exit", self.target],
                check=False,
                env=self._environ(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired, OSError:
            logger.debug("Closing the SSH control connection failed host=%s", self.target)
        if self._owns_control_dir:
            shutil.rmtree(self._control_dir, ignore_errors=True)
            self._control_dir = None
            self._owns_control_dir = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def for_endpoint(self, host: str, port: int | None) -> LocalOpenSSHTransport:
        return LocalOpenSSHTransport(
            host,
            self.user,
            self.known_hosts_file,
            port=port or 22,
            timeout=self.timeout,
            login_timeout=self.login_timeout,
            # A directory this transport created is removed by its close(), so a
            # derived transport gets its own; a caller's directory is shared.
            control_dir=None if self._owns_control_dir else self._control_dir,
            control_persist=self.control_persist,
            has_terminal=self._has_terminal,
        )
