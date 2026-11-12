"""
Environment-variable configuration contract for the Controller, client, and workers.

Every Nest entry point reads its settings through this module. Native processes and
Docker Compose deployments use the same variable names; no project checkout, config
file, or `.env` file is required. Loaders validate the whole environment at once and
raise one [`config.ConfigError`] that lists every problem by variable name. Secret
values never appear in error messages, `repr()`, or [`config.ControllerConfig.summary`].

SSH modes:

- **Mode A, trusted operator SSH**: `ssh <host>` resolves through the Controller
  user's own SSH configuration, so aliases, `ProxyCommand`, and the optional
  Tailscale sidecar work. Legacy `NEST_SLURM_*` settings create a Mode A profile.
- **Mode B, managed SSH**: the Controller builds the whole SSH command line from
  environment variables (`-F none`, `StrictHostKeyChecking=yes`, an explicit
  `known_hosts` and key) and connects only to the profile's approved login nodes.
  Mode B reads no SSH configuration file and no `NEST_TAILSCALE_*` setting.
"""

from __future__ import annotations

import math
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from ._logging import LOG_LEVELS, parse_log_level

DEFAULT_CONTROLLER_BIND = "127.0.0.1:50051"
"""Listener address used when `NEST_CONTROLLER_BIND` is unset."""

DEFAULT_CONTROLLER_ADDRESS = "127.0.0.1:50051"
"""Client address used when `NEST_CONTROLLER_ADDRESS` is unset."""

DEFAULT_RPC_TIMEOUT = 900.0
DEFAULT_SSH_TIMEOUT = 60.0
DEFAULT_RESULT_TIMEOUT = 600.0
DEFAULT_ENVIRONMENT_TIMEOUT = 1800.0
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_PARTITION = "dev"
DEFAULT_TIME = "00:05:00"
LEGACY_PROVIDER_ID = "default"
"""Provider Profile created from the `NEST_SLURM_*` settings."""

EXECUTORS = ("local", "slurm")
SSH_MODES = ("A", "B")

RESERVED_SETTINGS = (
    "NEST_DATABASE_URL",
    "NEST_AUTH_TOKEN_FILE",
    "NEST_RETENTION_DAYS",
    "NEST_INTEGRATIONS",
)
"""Settings reserved by the contract for features that do not exist yet."""

SECRET_SETTINGS = ("NEST_DATABASE_URL", "NEST_SSH_PRIVATE_KEY")
"""Variables whose values are never echoed in errors or logs."""

_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_SLURM_TIME = re.compile(r"\d+(?::\d{1,2}){0,2}|\d+-\d+(?::\d{1,2}){0,2}")
_SLURM_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_SSH_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")


class ConfigError(ValueError):
    """
    Reports every invalid or missing setting found while loading configuration.

    Attributes:
        problems (`tuple[str, ...]`): One actionable message per problem, each naming
            the variable to change. Messages never contain secret values.
    """

    def __init__(self, problems: list[str]):
        self.problems = tuple(problems)
        super().__init__(
            "Invalid Nest configuration:\n" + "\n".join(f"  - {p}" for p in self.problems)
        )


class Secret:
    """
    Holds a secret string that never appears in `str()`, `repr()`, or logs.

    Args:
        value (`str`): The secret value. Read it with [`config.Secret.reveal`].
    """

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        """Returns the secret value. Never log the result."""
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


def redact_url(url: str) -> str:
    """
    Replaces the password and query string of a URL with `***`.

    Args:
        url (`str`): URL such as `postgresql://user:pass@db/nest`.

    Returns:
        `str`: The URL with any password and query values masked.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]
        netloc = f"{user}:***@{host}" if ":" in userinfo else f"{user}@{host}"
    query = "***" if parts.query else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


@dataclass(frozen=True)
class LoginNode:
    """
    Approved SSH endpoint of a Compute Provider.

    Attributes:
        host (`str`): Host name or address. In Mode A it may be an SSH alias.
        port (`int`, *optional*): SSH port. `None` uses SSH's own default (Mode A)
            or 22 (Mode B).
    """

    host: str
    port: int | None = None

    def __str__(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"


@dataclass(frozen=True)
class ProviderProfile:
    """
    Operator-controlled settings for one Compute Provider (Slurm cluster).

    Attributes:
        id (`str`): Lowercase identifier from `NEST_PROVIDERS`.
        ssh_mode (`str`): `"A"` (trusted operator SSH) or `"B"` (managed SSH).
        login_nodes (`tuple[LoginNode, ...]`): Approved login nodes. The Controller
            connects only to these, using the first.
        account (`str`): Default Slurm account.
        partition (`str`): Default Slurm partition.
        time (`str`): Default Slurm time limit.
        workdir (`str | None`): Absolute shared HPC directory. Required for execution.
        ssh_user (`str | None`): Remote user. Required in Mode B.
    """

    id: str
    ssh_mode: str
    login_nodes: tuple[LoginNode, ...]
    account: str
    partition: str = DEFAULT_PARTITION
    time: str = DEFAULT_TIME
    workdir: str | None = None
    ssh_user: str | None = None


@dataclass(frozen=True)
class SSHPolicy:
    """
    Controller-wide SSH policy and Mode B credentials.

    Attributes:
        allowed_modes (`tuple[str, ...]`): Modes that profiles may use.
        known_hosts_file (`str | None`): Mode B host-key file.
        known_hosts_data (`str | None`): Mode B host keys given inline. Host keys are
            public, so this is not treated as a secret.
        identity_file (`str | None`): Mode B private key file.
        private_key (`Secret | None`): Mode B private key given inline.
    """

    allowed_modes: tuple[str, ...] = SSH_MODES
    known_hosts_file: str | None = None
    known_hosts_data: str | None = None
    identity_file: str | None = None
    private_key: Secret | None = None


@dataclass(frozen=True)
class TLSConfig:
    """
    Controller TLS server credentials.

    Attributes:
        cert_file (`str`): PEM certificate chain.
        key_file (`str`): PEM private key. The path is logged; the contents never are.
    """

    cert_file: str
    key_file: str


@dataclass(frozen=True)
class ControllerConfig:
    """
    Validated Controller configuration.

    Attributes:
        bind (`str`): Listener address.
        executor (`str`): `"local"` or `"slurm"`.
        log_level (`str`): Logging level name.
        ssh_timeout, result_timeout, environment_timeout, poll_interval (`float`):
            Deadlines in seconds.
        providers (`dict[str, ProviderProfile]`): Profiles by ID.
        default_provider (`str | None`): Profile used for Slurm calls.
        ssh (`SSHPolicy`): SSH policy and Mode B credentials.
        tls (`TLSConfig | None`): Server TLS, or `None` for plaintext.
        public_url (`str | None`): Advertised service URL, logged at startup.
    """

    bind: str = DEFAULT_CONTROLLER_BIND
    executor: str = "local"
    log_level: str = "INFO"
    ssh_timeout: float = DEFAULT_SSH_TIMEOUT
    result_timeout: float = DEFAULT_RESULT_TIMEOUT
    environment_timeout: float = DEFAULT_ENVIRONMENT_TIMEOUT
    poll_interval: float = DEFAULT_POLL_INTERVAL
    providers: dict[str, ProviderProfile] = field(default_factory=dict)
    default_provider: str | None = None
    ssh: SSHPolicy = field(default_factory=SSHPolicy)
    tls: TLSConfig | None = None
    public_url: str | None = None

    @property
    def provider(self) -> ProviderProfile | None:
        """The default Provider Profile, or `None` when no provider is configured."""
        return self.providers.get(self.default_provider) if self.default_provider else None

    def summary(self) -> dict[str, object]:
        """
        Returns a log-safe description of the configuration.

        Returns:
            `dict[str, object]`: Settings with secrets replaced by `***`.
        """
        return {
            "bind": self.bind,
            "executor": self.executor,
            "log_level": self.log_level,
            "tls": self.tls is not None,
            "public_url": self.public_url,
            "default_provider": self.default_provider,
            "providers": {
                p.id: {
                    "ssh_mode": p.ssh_mode,
                    "login_nodes": [str(n) for n in p.login_nodes],
                    "account": p.account,
                    "partition": p.partition,
                    "time": p.time,
                    "workdir": p.workdir,
                    "ssh_user": p.ssh_user,
                }
                for p in self.providers.values()
            },
            "ssh_modes_allowed": list(self.ssh.allowed_modes),
            "ssh_private_key": "***" if self.ssh.private_key else None,
            "ssh_identity_file": self.ssh.identity_file,
            "ssh_known_hosts_file": self.ssh.known_hosts_file,
        }


@dataclass(frozen=True)
class ClientConfig:
    """
    Validated SDK client configuration.

    Attributes:
        address (`str`): Controller `host:port`.
        rpc_timeout (`float`): `Execute` deadline in seconds.
        tls (`bool`): Whether to use a TLS channel.
        tls_ca_file (`str | None`): PEM roots for TLS. `None` uses the system roots.
    """

    address: str = DEFAULT_CONTROLLER_ADDRESS
    rpc_timeout: float = DEFAULT_RPC_TIMEOUT
    tls: bool = False
    tls_ca_file: str | None = None


class _Reader:
    """Reads stripped values from an environment and collects problems."""

    def __init__(self, environ: Mapping[str, str] | None):
        self.environ = os.environ if environ is None else environ
        self.problems: list[str] = []

    def get(self, name: str) -> str | None:
        value = self.environ.get(name, "").strip()
        return value or None

    def fail(self, message: str) -> None:
        self.problems.append(message)

    def seconds(self, name: str, default: float) -> float:
        raw = self.get(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if not 0 < value < math.inf:
            self.fail(f"{name} must be a positive number of seconds, got {raw!r}")
            return default
        return value

    def boolean(self, name: str, default: bool = False) -> bool:
        raw = self.get(name)
        if raw is None:
            return default
        lowered = raw.lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        self.fail(f"{name} must be true or false, got {raw!r}")
        return default

    def choice(self, name: str, options: tuple[str, ...], default: str, *, upper=False) -> str:
        raw = self.get(name)
        if raw is None:
            return default
        value = raw.upper() if upper else raw
        if value not in options:
            self.fail(f"{name} must be one of {', '.join(options)}, got {raw!r}")
            return default
        return value

    def readable_file(self, name: str) -> str | None:
        path = self.get(name)
        if path is None:
            return None
        if not os.path.isabs(path):
            self.fail(f"{name} must be an absolute path, got {path!r}")
        elif not os.path.isfile(path) or not os.access(path, os.R_OK):
            self.fail(f"{name} points to {path!r}, which is not a readable file")
        return path

    def address(self, name: str, default: str) -> str:
        raw = self.get(name) or default
        host, sep, port = raw.rpartition(":")
        if not sep or not host or not port.isdecimal() or not 0 <= int(port) <= 65535:
            self.fail(f"{name} must be host:port, got {raw!r}")
        return raw


def _login_nodes(reader: _Reader, name: str, raw: str) -> tuple[LoginNode, ...]:
    nodes = []
    invalid = False
    for entry in (item.strip() for item in raw.split(",")):
        if not entry:
            continue
        host, sep, port = entry.rpartition(":") if entry.count(":") == 1 else (entry, "", "")
        if not _HOST.fullmatch(host):
            reader.fail(f"{name} has an invalid host {host!r}; use host or host:port")
            invalid = True
            continue
        if sep and not (port.isdecimal() and 0 < int(port) <= 65535):
            reader.fail(f"{name} has an invalid port in {entry!r}")
            invalid = True
            continue
        nodes.append(LoginNode(host, int(port) if sep else None))
    if not nodes and not invalid:
        reader.fail(f"{name} must list at least one login node")
    return tuple(nodes)


def _slurm_fields(reader: _Reader, *, account: str, partition: str, time: str, workdir: str):
    account_value = reader.get(account)
    if account_value is not None and not _SLURM_NAME.fullmatch(account_value):
        reader.fail(f"{account} has unsupported characters: {account_value!r}")
    partition_value = reader.get(partition) or DEFAULT_PARTITION
    if not _SLURM_NAME.fullmatch(partition_value):
        reader.fail(f"{partition} has unsupported characters: {partition_value!r}")
    time_value = reader.get(time) or DEFAULT_TIME
    if not _SLURM_TIME.fullmatch(time_value):
        reader.fail(
            f"{time} must be a Slurm time such as 30, 00:05:00 or 1-00:00:00, got {time_value!r}"
        )
    workdir_value = reader.get(workdir)
    if workdir_value is not None and not workdir_value.startswith("/"):
        reader.fail(f"{workdir} must be an absolute HPC path, got {workdir_value!r}")
    return account_value, partition_value, time_value, workdir_value


def _profile(
    reader: _Reader, provider_id: str, executor: str, *, legacy: bool = False
) -> ProviderProfile | None:
    """
    Reads one Provider Profile from `<prefix>*` settings.

    The `default` profile uses the `NEST_SLURM_` prefix, accepts `NEST_SLURM_HOST` as
    a one-entry login-node list, and is optional in local mode.
    """
    prefix = "NEST_SLURM_" if legacy else f"NEST_PROVIDER_{provider_id.upper()}_"
    before = len(reader.problems)
    mode = reader.choice(prefix + "SSH_MODE", SSH_MODES, "A", upper=True)
    nodes_name = prefix + "LOGIN_NODES"
    nodes_raw = reader.get(nodes_name)
    if legacy and reader.get("NEST_SLURM_HOST") is not None:
        if nodes_raw is not None:
            reader.fail("Set only one of NEST_SLURM_HOST and NEST_SLURM_LOGIN_NODES")
        nodes_name, nodes_raw = "NEST_SLURM_HOST", reader.get("NEST_SLURM_HOST")
    account, partition, time, workdir = _slurm_fields(
        reader,
        account=prefix + "ACCOUNT",
        partition=prefix + "PARTITION",
        time=prefix + "TIME",
        workdir=prefix + "WORKDIR",
    )
    user = reader.get(prefix + "SSH_USER")

    if legacy and executor != "slurm" and (nodes_raw is None or account is None):
        # Without both a host and an account there is no backend, as before.
        return None
    if legacy and executor == "slurm":
        missing = [
            name
            for name, value in (
                ("NEST_SLURM_HOST", nodes_raw),
                ("NEST_SLURM_ACCOUNT", account),
                ("NEST_SLURM_WORKDIR", workdir),
            )
            if value is None
        ]
        if missing:
            reader.fail(
                "Slurm execution requires: "
                + ", ".join(missing)
                + " (or define Provider Profiles with NEST_PROVIDERS)"
            )
            return None

    if nodes_raw is None:
        reader.fail(f"{nodes_name} is required: list approved login nodes as host[:port]")
        nodes: tuple[LoginNode, ...] = ()
    else:
        nodes = _login_nodes(reader, nodes_name, nodes_raw)
    if account is None:
        reader.fail(f"{prefix}ACCOUNT is required")
    if executor == "slurm" and workdir is None:
        reader.fail(f"{prefix}WORKDIR is required for Slurm execution")
    if user is not None and not _SSH_USER.fullmatch(user):
        reader.fail(f"{prefix}SSH_USER is not a valid user name: {user!r}")
    if mode == "B" and user is None:
        reader.fail(f"{prefix}SSH_USER is required when {prefix}SSH_MODE=B")
    if len(reader.problems) > before or account is None:
        return None
    return ProviderProfile(
        id=provider_id,
        ssh_mode=mode,
        login_nodes=nodes,
        account=account,
        partition=partition,
        time=time,
        workdir=workdir,
        ssh_user=user,
    )


def _ssh_policy(reader: _Reader, profiles: list[ProviderProfile]) -> SSHPolicy:
    allowed_raw = reader.get("NEST_SSH_MODES_ALLOWED")
    allowed = SSH_MODES
    if allowed_raw is not None:
        modes = tuple(dict.fromkeys(m.strip().upper() for m in allowed_raw.split(",") if m.strip()))
        bad = [m for m in modes if m not in SSH_MODES]
        if bad or not modes:
            reader.fail(f"NEST_SSH_MODES_ALLOWED must list A and/or B, got {allowed_raw!r}")
        else:
            allowed = modes
    for profile in profiles:
        if profile.ssh_mode not in allowed:
            reader.fail(
                f"Provider {profile.id!r} uses SSH mode {profile.ssh_mode}, which "
                f"NEST_SSH_MODES_ALLOWED={','.join(allowed)} does not allow"
            )

    known_hosts_file = reader.readable_file("NEST_SSH_KNOWN_HOSTS")
    known_hosts_data = reader.get("NEST_SSH_KNOWN_HOSTS_DATA")
    identity_file = reader.readable_file("NEST_SSH_IDENTITY_FILE")
    private_key_raw = reader.get("NEST_SSH_PRIVATE_KEY")
    if private_key_raw is not None and "PRIVATE KEY-----" not in private_key_raw:
        reader.fail("NEST_SSH_PRIVATE_KEY does not look like a PEM or OpenSSH private key")
    if known_hosts_file and known_hosts_data:
        reader.fail("Set only one of NEST_SSH_KNOWN_HOSTS and NEST_SSH_KNOWN_HOSTS_DATA")
    if identity_file and private_key_raw:
        reader.fail("Set only one of NEST_SSH_IDENTITY_FILE and NEST_SSH_PRIVATE_KEY")
    if identity_file:
        mode = os.stat(identity_file).st_mode if os.path.exists(identity_file) else 0
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            reader.fail(
                f"NEST_SSH_IDENTITY_FILE {identity_file!r} is readable by other users; chmod 600 it"
            )

    if any(p.ssh_mode == "B" for p in profiles):
        if not (known_hosts_file or known_hosts_data):
            reader.fail(
                "Mode B needs verified host keys: set NEST_SSH_KNOWN_HOSTS (file) or "
                "NEST_SSH_KNOWN_HOSTS_DATA (known_hosts lines)"
            )
        if not (identity_file or private_key_raw):
            reader.fail(
                "Mode B needs a key: set NEST_SSH_IDENTITY_FILE (file) or "
                "NEST_SSH_PRIVATE_KEY (key contents)"
            )
    return SSHPolicy(
        allowed_modes=allowed,
        known_hosts_file=known_hosts_file,
        known_hosts_data=known_hosts_data,
        identity_file=identity_file,
        private_key=Secret(private_key_raw.replace("\\n", "\n").strip() + "\n")
        if private_key_raw
        else None,
    )


def _reserved(reader: _Reader) -> None:
    for name in RESERVED_SETTINGS:
        raw = reader.get(name)
        if raw is None:
            continue
        if name == "NEST_DATABASE_URL" and not urlsplit(raw).scheme:
            reader.fail("NEST_DATABASE_URL must be a URL such as postgresql://host/db")
        reader.fail(f"{name} is reserved for a future release and not supported yet; unset it")


def load_log_level(environ: Mapping[str, str] | None = None) -> tuple[str, bool]:
    """
    Reads `NEST_LOG_LEVEL`.

    Args:
        environ (`Mapping[str, str]`, *optional*): Environment. Defaults to `os.environ`.

    Returns:
        `tuple[str, bool]`: The level name and whether the requested value was valid.
            Invalid values fall back to `INFO` so workers never fail on logging.
    """
    return parse_log_level((os.environ if environ is None else environ).get("NEST_LOG_LEVEL"))


def load_executor(environ: Mapping[str, str] | None = None) -> str:
    """
    Reads and validates `NEST_EXECUTOR`.

    Raises:
        [`config.ConfigError`]: If the value is neither `local` nor `slurm`.
    """
    reader = _Reader(environ)
    executor = reader.choice("NEST_EXECUTOR", EXECUTORS, "local")
    if reader.problems:
        raise ConfigError(reader.problems)
    return executor


def load_controller_config(environ: Mapping[str, str] | None = None) -> ControllerConfig:
    """
    Reads and validates every Controller setting.

    Args:
        environ (`Mapping[str, str]`, *optional*): Environment. Defaults to `os.environ`.

    Returns:
        [`config.ControllerConfig`]: The validated configuration.

    Raises:
        [`config.ConfigError`]: Listing every missing or invalid setting.
    """
    reader = _Reader(environ)
    executor = reader.choice("NEST_EXECUTOR", EXECUTORS, "local")
    bind = reader.address("NEST_CONTROLLER_BIND", DEFAULT_CONTROLLER_BIND)
    level, valid_level = load_log_level(reader.environ)
    if not valid_level:
        reader.fail(f"NEST_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
    timeouts = {
        "ssh_timeout": reader.seconds("NEST_SSH_TIMEOUT", DEFAULT_SSH_TIMEOUT),
        "result_timeout": reader.seconds("NEST_RESULT_TIMEOUT", DEFAULT_RESULT_TIMEOUT),
        "environment_timeout": reader.seconds(
            "NEST_ENVIRONMENT_TIMEOUT", DEFAULT_ENVIRONMENT_TIMEOUT
        ),
        "poll_interval": reader.seconds("NEST_POLL_INTERVAL", DEFAULT_POLL_INTERVAL),
    }

    providers: dict[str, ProviderProfile] = {}
    ids_raw = reader.get("NEST_PROVIDERS")
    default_provider: str | None = None
    if ids_raw is not None:
        legacy = sorted(n for n in reader.environ if n.startswith("NEST_SLURM_") and reader.get(n))
        if legacy:
            reader.fail("NEST_PROVIDERS replaces the legacy settings; unset " + ", ".join(legacy))
        ids = [i.strip() for i in ids_raw.split(",") if i.strip()]
        for provider_id in ids:
            if not _PROVIDER_ID.fullmatch(provider_id):
                reader.fail(
                    f"NEST_PROVIDERS has invalid ID {provider_id!r}; use lowercase letters, "
                    "digits and _"
                )
            elif provider_id in providers:
                reader.fail(f"NEST_PROVIDERS lists {provider_id!r} twice")
            else:
                profile = _profile(reader, provider_id, executor)
                if profile is not None:
                    providers[provider_id] = profile
        if not ids:
            reader.fail("NEST_PROVIDERS must list at least one provider ID")
        default_provider = reader.get("NEST_DEFAULT_PROVIDER") or (ids[0] if ids else None)
        if default_provider is not None and default_provider not in ids:
            reader.fail(f"NEST_DEFAULT_PROVIDER {default_provider!r} is not in NEST_PROVIDERS")
    else:
        if reader.get("NEST_DEFAULT_PROVIDER"):
            reader.fail("NEST_DEFAULT_PROVIDER needs NEST_PROVIDERS")
        profile = _profile(reader, LEGACY_PROVIDER_ID, executor, legacy=True)
        if profile is not None:
            providers[profile.id] = profile
            default_provider = profile.id

    ssh = _ssh_policy(reader, list(providers.values()))

    cert = reader.readable_file("NEST_TLS_CERT_FILE")
    key = reader.readable_file("NEST_TLS_KEY_FILE")
    if (reader.get("NEST_TLS_CERT_FILE") is None) != (reader.get("NEST_TLS_KEY_FILE") is None):
        reader.fail("Set both NEST_TLS_CERT_FILE and NEST_TLS_KEY_FILE, or neither")
    tls = TLSConfig(cert, key) if cert and key else None

    public_url = reader.get("NEST_PUBLIC_URL")
    if public_url is not None:
        parts = urlsplit(public_url)
        if parts.scheme not in ("grpc", "grpcs", "http", "https") or not parts.hostname:
            reader.fail(
                f"NEST_PUBLIC_URL must be a grpc(s):// or http(s):// URL, got {public_url!r}"
            )
        elif parts.password or parts.query:
            reader.fail("NEST_PUBLIC_URL must not contain credentials or a query string")

    _reserved(reader)

    if reader.problems:
        raise ConfigError(reader.problems)
    return ControllerConfig(
        bind=bind,
        executor=executor,
        log_level=level,
        providers=providers,
        default_provider=default_provider,
        ssh=ssh,
        tls=tls,
        public_url=public_url,
        **timeouts,
    )


def load_client_config(environ: Mapping[str, str] | None = None) -> ClientConfig:
    """
    Reads and validates the SDK client settings.

    Args:
        environ (`Mapping[str, str]`, *optional*): Environment. Defaults to `os.environ`.

    Returns:
        [`config.ClientConfig`]: The validated configuration.

    Raises:
        [`config.ConfigError`]: Listing every invalid setting.
    """
    reader = _Reader(environ)
    address = reader.address("NEST_CONTROLLER_ADDRESS", DEFAULT_CONTROLLER_ADDRESS)
    timeout = reader.seconds("NEST_RPC_TIMEOUT", DEFAULT_RPC_TIMEOUT)
    ca_file = reader.readable_file("NEST_CONTROLLER_TLS_CA_FILE")
    tls = reader.boolean("NEST_CONTROLLER_TLS", default=ca_file is not None)
    if ca_file and not tls:
        reader.fail("NEST_CONTROLLER_TLS_CA_FILE is set but NEST_CONTROLLER_TLS is false")
    if reader.problems:
        raise ConfigError(reader.problems)
    return ClientConfig(address=address, rpc_timeout=timeout, tls=tls, tls_ca_file=ca_file)


def materialize_ssh_credentials(policy: SSHPolicy) -> tuple[str | None, str | None]:
    """
    Writes inline Mode B credentials to a private temporary directory.

    Args:
        policy ([`config.SSHPolicy`]): Validated SSH policy.

    Returns:
        `tuple[str | None, str | None]`: Paths of the identity and `known_hosts` files,
            using the configured files when no inline value is given.
    """
    identity, known_hosts = policy.identity_file, policy.known_hosts_file
    if policy.private_key is None and policy.known_hosts_data is None:
        return identity, known_hosts
    directory = tempfile.mkdtemp(prefix="nest-ssh-")
    os.chmod(directory, 0o700)
    if policy.private_key is not None:
        identity = os.path.join(directory, "id")
        fd = os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(policy.private_key.reveal())
    if policy.known_hosts_data is not None:
        known_hosts = os.path.join(directory, "known_hosts")
        with open(known_hosts, "w") as handle:
            # Allow one entry per line or entries separated by literal "\n".
            handle.write(policy.known_hosts_data.replace("\\n", "\n").strip() + "\n")
    return identity, known_hosts
