"""
Environment-variable configuration contract for the Controller, client, and workers.

Every Nest entry point reads its settings through this module. Native processes and
Docker Compose deployments use the same variable names; no project checkout, config
file, or `.env` file is required.

HPC Platforms: the Controller bootstraps one [`config.PlatformProfile`] per HPC
Platform listed in `NEST_PLATFORMS`, from `NEST_PLATFORM_<ID>_*` settings. The
Platform ID is the immutable identity that namespaces Slurm accounts and job records;
the operator label, approved login nodes, and Slurm defaults are separate fields of
the profile. The older `NEST_PROVIDERS` / `NEST_PROVIDER_<ID>_*` (Compute Provider)
and single-cluster `NEST_SLURM_*` forms remain supported and define the same
profiles; `NEST_PROVIDERS` is deprecated. Loaders validate the whole environment at once and
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

Email delivery is opt-in too: with `NEST_EMAIL_ENABLED=true` the Controller sends
queued messages (such as Project invitations) from a persistent outbox over SMTP
with verified TLS (see [`mail`]). Without it no SMTP setting is read or validated,
and nothing else in Nest depends on mail.

Google sign-in is opt-in as well: setting `NEST_GOOGLE_CLIENT_ID` enables the OAuth
authorization code flow with PKCE and OIDC ID-token verification (see [`auth`]), served
on a small HTTP listener at `NEST_AUTH_HTTP_BIND`. Without it no `NEST_GOOGLE_*` setting
is read.

With a database, the gRPC execution service authenticates every call by its Nest CLI
session and authorizes it with the same Project policy as the Projects API
(`NEST_CONTROLLER_AUTH=session`, see [`authz.ExecutionAccess`]). Session tokens are
sent only over TLS, unless `NEST_CONTROLLER_ALLOW_PLAINTEXT=true` approves an
already-encrypted network such as a Tailnet.

Persistence is opt-in: with `NEST_DATABASE_URL` set, the Controller stores Platform
Profiles and job records in an externally managed PostgreSQL database (see
[`db.Database`]). Without it the Controller is stateless, unless
`NEST_DATABASE_REQUIRED=true` makes the database mandatory.
"""

from __future__ import annotations

import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.utils import parseaddr
from ipaddress import ip_address
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit, urlunsplit
from uuid import UUID

from ._logging import LOG_LEVELS, parse_log_level
from .runtime_files import (
    RuntimeFileError,
    check_ssh_config,
    normalize_content,
    private_directory,
    write_private_file,
)

DEFAULT_CONTROLLER_BIND = "127.0.0.1:50051"
"""Listener address used when `NEST_CONTROLLER_BIND` is unset."""

DEFAULT_CONTROLLER_ADDRESS = "127.0.0.1:50051"
"""Client address used when `NEST_CONTROLLER_ADDRESS` is unset."""

HOSTED_AUTH_URL = "https://auth.nest.cerulean.works"
"""Sign-in origin of Cerulean-hosted Nest. `nest login` uses it when neither `--url`
nor `NEST_AUTH_URL` names another Nest, and the SDK uses it only for calls to
[`config.HOSTED_CONTROLLER_ADDRESS`]."""

HOSTED_CONTROLLER_ADDRESS = "nest.cerulean.works:50051"
"""gRPC address of the Cerulean-hosted Controller. It is never a default address: it
serves plaintext gRPC that only the access-controlled Tailnet encrypts, so the SDK
connects to it only when `NEST_CONTROLLER_ADDRESS` names it and
`NEST_CONTROLLER_TLS=false` says so explicitly."""

AUTH_URL_SOURCES = ("--url", "NEST_AUTH_URL", "hosted default")
"""Where [`config.resolve_auth_url`] found the sign-in origin, highest precedence first."""

DEFAULT_RPC_TIMEOUT = 900.0
DEFAULT_SSH_TIMEOUT = 60.0
DEFAULT_RESULT_TIMEOUT = 600.0
DEFAULT_ENVIRONMENT_TIMEOUT = 1800.0
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_PENDING_BLOCKED_TIMEOUT = 120.0
DEFAULT_PARTITION = "h200_256gpu"
DEFAULT_TIME = "00:05:00"
LEGACY_PROVIDER_ID = "default"
"""Platform Profile created from the `NEST_SLURM_*` settings."""
LEGACY_PLATFORM_ID = LEGACY_PROVIDER_ID

PLATFORM_FIELDS = (
    "SSH_MODE",
    "LOGIN_NODES",
    "SSH_USER",
    "ACCOUNT",
    "PARTITION",
    "TIME",
    "WORKDIR",
    "OPERATOR",
    "NAME",
)
"""Suffixes of the `NEST_PLATFORM_<ID>_*` settings that define one HPC Platform."""

EXECUTORS = ("local", "slurm")
SSH_MODES = ("A", "B")

DATABASE_SCHEMES = ("postgresql", "postgres")
DATABASE_SSLMODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")
DEFAULT_DATABASE_POOL_MIN_SIZE = 1
DEFAULT_DATABASE_POOL_MAX_SIZE = 10
DEFAULT_DATABASE_CONNECT_TIMEOUT = 10.0
DEFAULT_DATABASE_POOL_TIMEOUT = 30.0

DATABASE_SETTINGS = (
    "NEST_DATABASE_URL",
    "NEST_DATABASE_REQUIRED",
    "NEST_DATABASE_PASSWORD",
    "NEST_DATABASE_PASSWORD_FILE",
    "NEST_DATABASE_SSLMODE",
    "NEST_DATABASE_SSLROOTCERT",
    "NEST_DATABASE_SSLROOTCERT_DATA",
    "NEST_DATABASE_SSLCERT",
    "NEST_DATABASE_SSLCERT_DATA",
    "NEST_DATABASE_SSLKEY",
    "NEST_DATABASE_SSLKEY_DATA",
    "NEST_DATABASE_POOL_MIN_SIZE",
    "NEST_DATABASE_POOL_MAX_SIZE",
    "NEST_DATABASE_CONNECT_TIMEOUT",
    "NEST_DATABASE_POOL_TIMEOUT",
)
"""Controller database settings, all read by [`config.load_database_config`]."""

EMAIL_PROVIDERS = ("smtp",)
SMTP_TLS_MODES = ("starttls", "implicit")
DEFAULT_SMTP_STARTTLS_PORT = 587
DEFAULT_SMTP_IMPLICIT_TLS_PORT = 465
DEFAULT_SMTP_TIMEOUT = 30.0
DEFAULT_EMAIL_MAX_ATTEMPTS = 5
DEFAULT_EMAIL_RETRY_DELAY = 60.0
DEFAULT_EMAIL_RETRY_MAX_DELAY = 3600.0
DEFAULT_EMAIL_POLL_INTERVAL = 15.0

EMAIL_SETTINGS = (
    "NEST_EMAIL_ENABLED",
    "NEST_EMAIL_PROVIDER",
    "NEST_EMAIL_FROM",
    "NEST_EMAIL_MAX_ATTEMPTS",
    "NEST_EMAIL_RETRY_DELAY",
    "NEST_EMAIL_RETRY_MAX_DELAY",
    "NEST_EMAIL_POLL_INTERVAL",
    "NEST_SMTP_HOST",
    "NEST_SMTP_PORT",
    "NEST_SMTP_STARTTLS",
    "NEST_SMTP_IMPLICIT_TLS",
    "NEST_SMTP_USERNAME",
    "NEST_SMTP_PASSWORD",
    "NEST_SMTP_PASSWORD_FILE",
    "NEST_SMTP_CA",
    "NEST_SMTP_CA_FILE",
    "NEST_SMTP_TIMEOUT",
)
"""Email delivery settings, read by [`config.load_controller_config`] only when
`NEST_EMAIL_ENABLED=true`."""

DEFAULT_AUTH_HTTP_BIND = "127.0.0.1:8080"

DEFAULT_ACCESS_TOKEN_TTL = 900.0
"""Seconds a browser access token is accepted: 15 minutes."""

DEFAULT_REFRESH_TTL = 86_400.0
"""Seconds a refresh session stays usable without being refreshed: 24 hours."""

DEFAULT_SESSION_MAX_LIFETIME = 604_800.0
"""Seconds from sign-in after which a session family ends, however often it is
refreshed: 7 days."""

MAX_SESSION_LIFETIME = 7_776_000.0
"""Longest accepted `NEST_AUTH_SESSION_MAX_LIFETIME`: 90 days."""

AUTH_SESSION_PATHS = ("/auth/session", "/auth/session/refresh", "/auth/session/logout")
"""Browser session routes served by the auth listener."""

AUTH_CLI_PREFIX = "/auth/cli/"
"""Prefix of the CLI sign-in and CLI session routes served by the auth listener."""

PROJECTS_API_PATH = "/api/projects"
"""Root of the Projects API served by the auth listener."""

INVITATIONS_API_PATH = "/api/invitations"
"""Root of the signed-in User's own invitations API served by the auth listener."""

PLATFORMS_API_PATH = "/api/platforms"
"""Root of the HPC Platform discovery API served by the auth listener."""

ORGANIZATIONS_API_PATH = "/api/organizations"
"""Root of the Organizations API served by the auth listener."""

INVITATION_PAGE_PATH = "/invitations/accept"
"""Browser page where a signed-in User reviews and accepts an invitation link."""

CONSOLE_PATH = "/console"
"""Root of the browser Management Console served by the auth listener."""

CLI_SCOPES: dict[str, str] = {
    "jobs:run": "Run and manage jobs in your Projects",
    "projects:read": "See your Projects and their members",
}
"""Scopes a CLI session can be granted, with the description shown on approval."""

DEFAULT_CLI_SCOPE = "jobs:run projects:read"
"""Scope `nest login` asks for."""

CLIENT_AUTH_SETTINGS = ("NEST_AUTH_URL", "NEST_CLI_CREDENTIALS_FILE")
"""CLI sign-in settings, read by [`config.load_cli_auth_config`]."""

CONTROLLER_AUTH_MODES = ("session", "none")
"""`NEST_CONTROLLER_AUTH` values: `session` authenticates every gRPC call with a Nest
CLI session; `none` accepts any caller that can reach the listener."""

CONTROLLER_AUTH_SETTINGS = (
    "NEST_CONTROLLER_AUTH",
    "NEST_CONTROLLER_ALLOW_PLAINTEXT",
    "NEST_PROVIDER_ALLOWED_PROJECTS",
    "NEST_EXECUTION_REGISTRY",
)
"""gRPC authentication, transport and Provider approval settings, read by
[`config.load_controller_config`]."""

EXECUTION_REGISTRY_MODES = ("required", "legacy")
"""`NEST_EXECUTION_REGISTRY` values: `required` admits every Slurm submission through
the durable Execution Registry (the default with `NEST_CONTROLLER_AUTH=session`);
`legacy` keeps the trusted Mode A path that records only the `jobs` table (the only
choice without session authentication)."""

ALL_PROJECTS = "*"
"""`NEST_PROVIDER_ALLOWED_PROJECTS` value that approves every Project."""

GOOGLE_SETTINGS = (
    "NEST_GOOGLE_CLIENT_ID",
    "NEST_GOOGLE_CLIENT_SECRET",
    "NEST_GOOGLE_CLIENT_SECRET_FILE",
    "NEST_GOOGLE_REDIRECT_URL",
    "NEST_AUTH_HTTP_BIND",
    "NEST_AUTH_ACCESS_TOKEN_TTL",
    "NEST_AUTH_REFRESH_TTL",
    "NEST_AUTH_SESSION_MAX_LIFETIME",
)
"""Google sign-in settings, read by [`config.load_controller_config`] when any of the
`NEST_GOOGLE_*` settings is set."""

BOOTSTRAP_SETTINGS = ("NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL",)
"""The one-time platform administrator bootstrap, read by
[`config.load_controller_config`]. Needs `NEST_DATABASE_URL`."""

RESERVED_SETTINGS = (
    "NEST_AUTH_TOKEN_FILE",
    "NEST_RETENTION_DAYS",
    "NEST_INTEGRATIONS",
)
"""Settings reserved by the contract for features that do not exist yet."""

SECRET_SETTINGS = (
    "NEST_DATABASE_URL",
    "NEST_DATABASE_PASSWORD",
    "NEST_DATABASE_SSLKEY_DATA",
    "NEST_SSH_PRIVATE_KEY",
    "NEST_TLS_KEY",
    "NEST_SMTP_PASSWORD",
    "NEST_GOOGLE_CLIENT_SECRET",
    "NEST_TAILSCALE_AUTHKEY",
)
"""Variables whose values are never echoed in errors or logs, and never passed to
SSH, local workers, or other child processes (see [`config.child_environ`])."""

PLATFORMS_ENV = "NEST_PLATFORMS_ENV"
"""Inline HPC Platform settings: the lines of a `platforms.env` file in one value."""

_PLATFORMS_ENV_KEY = re.compile(
    r"NEST_PLATFORMS|NEST_DEFAULT_PLATFORM|NEST_PLATFORM_[A-Z][A-Z0-9_]*"
)

_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_PLATFORM_ID = _PROVIDER_ID
_OPERATOR = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?")
_DISPLAY_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,100}")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_SLURM_TIME = re.compile(r"\d+(?::\d{1,2}){0,2}|\d+-\d+(?::\d{1,2}){0,2}")
_SLURM_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_SSH_USER = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}")
_EMAIL_ADDRESS = re.compile(r"[^@\s<>,;\"]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


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
    Approved SSH endpoint of an HPC Platform.

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
class PlatformProfile:
    """
    Operator-controlled settings for one HPC Platform (a Slurm cluster).

    The profile is immutable. Its `id` is the Platform's identity: it namespaces the
    Slurm account and the job records, and does not change when the operator,
    login nodes, or defaults do. Use a new ID for a different cluster.

    Attributes:
        id (`str`): Lowercase Platform ID from `NEST_PLATFORMS` (or `NEST_PROVIDERS`,
            or `default` for the `NEST_SLURM_*` settings).
        ssh_mode (`str`): `"A"` (trusted operator SSH) or `"B"` (managed SSH).
        login_nodes (`tuple[LoginNode, ...]`): Approved login nodes. The Controller
            connects only to these, using the first.
        account (`str`): Default Slurm account, within this Platform's namespace.
        partition (`str`): Default Slurm partition.
        time (`str`): Default Slurm time limit.
        workdir (`str | None`): Absolute shared HPC directory. Required for execution.
        ssh_user (`str | None`): Remote user. Required in Mode B.
        operator (`str | None`): `NEST_PLATFORM_<ID>_OPERATOR`: label of the
            organization that operates the Platform. Metadata only; it never selects
            a login node or account.
        display_name (`str | None`): `NEST_PLATFORM_<ID>_NAME`: human-readable name.
    """

    id: str
    ssh_mode: str
    login_nodes: tuple[LoginNode, ...]
    account: str
    partition: str = DEFAULT_PARTITION
    time: str = DEFAULT_TIME
    workdir: str | None = None
    ssh_user: str | None = None
    operator: str | None = None
    display_name: str | None = None

    @property
    def slurm_account(self) -> tuple[str, str]:
        """The default Slurm account qualified by its Platform: `(platform_id, account)`."""
        return (self.id, self.account)


ProviderProfile = PlatformProfile
"""Deprecated alias of [`config.PlatformProfile`], kept for compatibility."""


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
    Controller TLS server credentials, each from a file or given inline.

    Attributes:
        cert_file (`str | None`): PEM certificate chain file (`NEST_TLS_CERT_FILE`).
        key_file (`str | None`): PEM private key file (`NEST_TLS_KEY_FILE`). The path
            is logged; the contents never are.
        cert_pem (`str | None`): The certificate chain given inline (`NEST_TLS_CERT`).
        key_pem (`Secret | None`): The private key given inline (`NEST_TLS_KEY`).
    """

    cert_file: str | None = None
    key_file: str | None = None
    cert_pem: str | None = None
    key_pem: Secret | None = None

    def credentials(self) -> tuple[bytes, bytes]:
        """Returns `(private_key, certificate_chain)` as PEM bytes. Never log them."""
        if self.key_pem is not None:
            key = self.key_pem.reveal().encode()
        else:
            with open(str(self.key_file), "rb") as handle:
                key = handle.read()
        if self.cert_pem is not None:
            cert = self.cert_pem.encode()
        else:
            with open(str(self.cert_file), "rb") as handle:
                cert = handle.read()
        return key, cert


@dataclass(frozen=True)
class DatabaseConfig:
    """
    Connection and pool settings for the Controller's PostgreSQL database.

    The database is always externally managed: Nest never creates a server or a
    database, and never falls back to another store when this one is unavailable.

    Attributes:
        url (`Secret`): libpq connection URL. It may hold a password, so it is
            wrapped; log [`config.DatabaseConfig.redacted_url`] instead.
        password (`Secret | None`): The password given directly in
            `NEST_DATABASE_PASSWORD`. It is passed to libpq as its own keyword, never
            spliced into the URL, so any character is safe.
        password_file (`str | None`): File whose contents are the password.
        sslmode (`str | None`): libpq `sslmode` from `NEST_DATABASE_SSLMODE`. `None`
            keeps the URL's own `sslmode`, or libpq's default (`prefer`). Defaults to
            `verify-full` when only a CA file is given.
        sslrootcert (`str | None`): CA bundle used to verify the server certificate.
        sslcert (`str | None`): Client certificate for certificate authentication.
        sslkey (`str | None`): Client private key. Must not be readable by others.
        sslrootcert_data, sslcert_data (`str | None`): The CA bundle and client
            certificate given inline. libpq needs files, so [`db.Database`] writes them
            to a private runtime directory.
        sslkey_data (`Secret | None`): The client private key given inline, written
            the same way with mode `0600`.
        pool_min_size (`int`): Connections the pool keeps open.
        pool_max_size (`int`): Most connections the pool opens.
        connect_timeout (`float`): Seconds to wait for a new connection.
        pool_timeout (`float`): Seconds a request waits for a free pooled connection.
    """

    url: Secret
    password_file: str | None = None
    sslmode: str | None = None
    sslrootcert: str | None = None
    sslcert: str | None = None
    sslkey: str | None = None
    password: Secret | None = None
    sslrootcert_data: str | None = None
    sslcert_data: str | None = None
    sslkey_data: Secret | None = None
    pool_min_size: int = DEFAULT_DATABASE_POOL_MIN_SIZE
    pool_max_size: int = DEFAULT_DATABASE_POOL_MAX_SIZE
    connect_timeout: float = DEFAULT_DATABASE_CONNECT_TIMEOUT
    pool_timeout: float = DEFAULT_DATABASE_POOL_TIMEOUT

    @property
    def redacted_url(self) -> str:
        """The URL with its password and query string replaced by `***`."""
        return redact_url(self.url.reveal())

    @property
    def effective_sslmode(self) -> str | None:
        """The `sslmode` libpq will use, or `None` for libpq's default."""
        if self.sslmode is not None:
            return self.sslmode
        values = parse_qs(urlsplit(self.url.reveal()).query).get("sslmode")
        return values[-1] if values else None

    def summary(self) -> dict[str, object]:
        """
        Returns a log-safe description of the database settings.

        Returns:
            `dict[str, object]`: Settings with the password and query masked.
        """
        return {
            "url": self.redacted_url,
            "password": "***" if self.password else None,
            "password_file": self.password_file,
            "sslmode": self.effective_sslmode,
            "sslrootcert": "inline" if self.sslrootcert_data else self.sslrootcert,
            "sslcert": "inline" if self.sslcert_data else self.sslcert,
            "pool_min_size": self.pool_min_size,
            "pool_max_size": self.pool_max_size,
        }


@dataclass(frozen=True)
class SMTPConfig:
    """
    SMTP transport settings. Every connection is encrypted and the server
    certificate and host name are always verified; there is no plaintext mode.

    Attributes:
        host (`str`): SMTP server (relay or submission) host name. Certificates are
            checked against this name.
        port (`int`): Server port: 587 for STARTTLS or 465 for implicit TLS by default.
        tls_mode (`str`): `"starttls"` (upgrade a plain connection, and fail when the
            server does not offer STARTTLS) or `"implicit"` (TLS from the first byte).
        username (`str | None`): Login name, or `None` for a relay that needs no
            authentication.
        password (`Secret | None`): Inline password from `NEST_SMTP_PASSWORD`.
        password_file (`str | None`): File holding the password, re-read on each
            connection so a rotated secret applies without a restart.
        ca_file (`str | None`): PEM CA bundle that replaces the system roots, for a
            relay with a private CA. Verification is never disabled.
        ca_data (`str | None`): The same CA bundle given inline in `NEST_SMTP_CA`.
        timeout (`float`): Seconds for the connection and each SMTP command.
    """

    host: str
    port: int
    tls_mode: str = "starttls"
    username: str | None = None
    password: Secret | None = None
    password_file: str | None = None
    ca_file: str | None = None
    timeout: float = DEFAULT_SMTP_TIMEOUT
    ca_data: str | None = None

    def read_password(self) -> str | None:
        """Returns the current password, reading the file when one is set. Never log it."""
        if self.password_file is not None:
            with open(self.password_file) as handle:
                return handle.read().strip()
        return self.password.reveal() if self.password is not None else None


@dataclass(frozen=True)
class EmailConfig:
    """
    Optional outbound email delivery. Present only when `NEST_EMAIL_ENABLED=true`.

    Attributes:
        provider (`str`): Transport; only `"smtp"`.
        sender (`str`): `NEST_EMAIL_FROM`, such as `Nest <nest@example.com>`. Used as
            the `From` header and the envelope sender. It must be an address the SMTP
            account may send as and whose domain publishes SPF/DKIM/DMARC.
        sender_address (`str`): The bare address part of `sender`.
        public_url (`str`): `NEST_PUBLIC_URL`, the base of every link in a message.
        smtp (`SMTPConfig`): Transport settings.
        max_attempts (`int`): Delivery attempts per message before it is `failed`.
        retry_delay (`float`): Seconds before the first retry; doubles per attempt.
        retry_max_delay (`float`): Longest wait between attempts.
        poll_interval (`float`): Seconds between outbox scans.
    """

    sender: str
    sender_address: str
    public_url: str
    smtp: SMTPConfig
    provider: str = "smtp"
    max_attempts: int = DEFAULT_EMAIL_MAX_ATTEMPTS
    retry_delay: float = DEFAULT_EMAIL_RETRY_DELAY
    retry_max_delay: float = DEFAULT_EMAIL_RETRY_MAX_DELAY
    poll_interval: float = DEFAULT_EMAIL_POLL_INTERVAL

    def summary(self) -> dict[str, object]:
        """
        Returns a log-safe description of the email settings.

        Returns:
            `dict[str, object]`: Settings with the password replaced by `***`.
        """
        return {
            "provider": self.provider,
            "from": self.sender_address,
            "public_url": self.public_url,
            "smtp_host": self.smtp.host,
            "smtp_port": self.smtp.port,
            "smtp_tls": self.smtp.tls_mode,
            "smtp_username": self.smtp.username,
            "smtp_password": "***" if self.smtp.password else None,
            "smtp_password_file": self.smtp.password_file,
            "smtp_ca_file": "inline" if self.smtp.ca_data else self.smtp.ca_file,
            "max_attempts": self.max_attempts,
        }


@dataclass(frozen=True)
class SessionPolicy:
    """
    Lifetimes of browser sessions, in seconds.

    Attributes:
        access_token_ttl (`float`): `NEST_AUTH_ACCESS_TOKEN_TTL`, how long an access
            token is accepted. Short, because it is presented on every request.
        refresh_ttl (`float`): `NEST_AUTH_REFRESH_TTL`, how long a refresh session
            stays usable without being refreshed. Each refresh rotates it and starts
            this period again.
        max_lifetime (`float`): `NEST_AUTH_SESSION_MAX_LIFETIME`, how long after
            sign-in the session ends however often it is refreshed. The User then
            signs in again.
    """

    access_token_ttl: float = DEFAULT_ACCESS_TOKEN_TTL
    refresh_ttl: float = DEFAULT_REFRESH_TTL
    max_lifetime: float = DEFAULT_SESSION_MAX_LIFETIME

    def __post_init__(self):
        if not 0 < self.access_token_ttl <= self.refresh_ttl <= self.max_lifetime:
            raise ValueError(
                "Session lifetimes must satisfy 0 < access_token_ttl <= refresh_ttl <= max_lifetime"
            )

    def summary(self) -> dict[str, float]:
        """Returns the lifetimes for logging."""
        return {
            "access_token_ttl": self.access_token_ttl,
            "refresh_ttl": self.refresh_ttl,
            "max_lifetime": self.max_lifetime,
        }


@dataclass(frozen=True)
class GoogleAuthConfig:
    """
    Google sign-in settings. Present only when `NEST_GOOGLE_CLIENT_ID` is set.

    Attributes:
        client_id (`str`): OAuth client ID from the Google Cloud console. ID tokens
            must name it as their audience.
        client_secret (`Secret | None`): Inline secret from `NEST_GOOGLE_CLIENT_SECRET`.
        client_secret_file (`str | None`): File holding the secret, re-read on each
            code exchange so a rotated secret applies without a restart.
        redirect_url (`str`): `NEST_GOOGLE_REDIRECT_URL`, the exact callback URL
            registered with Google. The auth listener serves its path.
        http_bind (`str`): `NEST_AUTH_HTTP_BIND`, the auth listener's `host:port`.
        sessions ([`config.SessionPolicy`]): Browser session lifetimes.
    """

    client_id: str
    redirect_url: str
    client_secret: Secret | None = None
    client_secret_file: str | None = None
    http_bind: str = DEFAULT_AUTH_HTTP_BIND
    sessions: SessionPolicy = field(default_factory=SessionPolicy)

    def read_client_secret(self) -> str:
        """Returns the current client secret, reading the file when one is set. Never log it."""
        if self.client_secret_file is not None:
            with open(self.client_secret_file) as handle:
                return handle.read().strip()
        assert self.client_secret is not None
        return self.client_secret.reveal()

    @property
    def secure_cookies(self) -> bool:
        """Whether cookies get the `Secure` attribute: `True` for an `https://` redirect URL."""
        return self.redirect_url.startswith("https://")

    def summary(self) -> dict[str, object]:
        """
        Returns a log-safe description of the Google sign-in settings.

        Returns:
            `dict[str, object]`: Settings with the client secret replaced by `***`.
        """
        return {
            "client_id": self.client_id,
            "client_secret": "***" if self.client_secret else None,
            "client_secret_file": self.client_secret_file,
            "redirect_url": self.redirect_url,
            "http_bind": self.http_bind,
            "sessions": self.sessions.summary(),
        }


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
        pending_blocked_timeout (`float`): Seconds a job may stay pending for a
            reason it cannot outwait before the Controller stops waiting. `0` stops
            at the first such observation.
        pending_limit_timeout (`float | None`): Seconds a job may stay pending on a
            QoS or association limit before the Controller stops waiting. `0` stops
            at the first such observation; `None` waits until the polling deadline.
        cancel_blocked_jobs (`bool`): Whether to cancel a job the Controller stopped
            waiting for because of its pending reason.
        platforms (`Mapping[str, PlatformProfile]`): HPC Platform Profiles by
            Platform ID, read-only.
        default_platform (`str | None`): Platform used for Slurm calls.
        warnings (`tuple[str, ...]`): Deprecated or ignored settings, one message
            each, logged at startup.
        ssh (`SSHPolicy`): SSH policy and Mode B credentials.
        tls (`TLSConfig | None`): Server TLS, or `None` for plaintext.
        public_url (`str | None`): Advertised service URL, logged at startup.
        database (`DatabaseConfig | None`): PostgreSQL settings, or `None` when the
            Controller is stateless.
        email (`EmailConfig | None`): Email delivery settings, or `None` when
            `NEST_EMAIL_ENABLED` is not `true`.
        google (`GoogleAuthConfig | None`): Google sign-in settings, or `None` when
            `NEST_GOOGLE_CLIENT_ID` is unset.
        bootstrap_admin_email (`str | None`): `NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL`,
            trimmed and lowercased: the Google account that becomes the first
            `platform_admin` at its first verified sign-in. Has no effect once the
            bootstrap has completed.
        auth (`str`): `NEST_CONTROLLER_AUTH`: `session` (the default with a
            database) or `none` (the default without one).
        allow_plaintext (`bool`): `NEST_CONTROLLER_ALLOW_PLAINTEXT`: the operator
            approves plaintext gRPC on a network that is already encrypted, so
            session tokens may be accepted without TLS.
        allowed_projects (`frozenset[str]`): `NEST_PROVIDER_ALLOWED_PROJECTS`:
            Project IDs whose members' Runs the Controller's Provider accepts, or
            `{"*"}` for every Project. Empty, the default, accepts none.
        execution_registry (`str`): `NEST_EXECUTION_REGISTRY`: `required` (every
            Slurm submission is admitted through the Execution Registry; see
            [`admission.RegistryAdmission`]) or `legacy` (the trusted Mode A path that
            records only the `jobs` table). `required` is the default with session
            authentication, and the only choice for a Mode B Platform under it;
            `legacy` is the only choice without session authentication.

    `providers`, `default_provider`, and `provider` are deprecated read-only aliases
    of `platforms`, `default_platform`, and `platform`.
    """

    bind: str = DEFAULT_CONTROLLER_BIND
    executor: str = "local"
    log_level: str = "INFO"
    ssh_timeout: float = DEFAULT_SSH_TIMEOUT
    result_timeout: float = DEFAULT_RESULT_TIMEOUT
    environment_timeout: float = DEFAULT_ENVIRONMENT_TIMEOUT
    poll_interval: float = DEFAULT_POLL_INTERVAL
    pending_blocked_timeout: float = DEFAULT_PENDING_BLOCKED_TIMEOUT
    pending_limit_timeout: float | None = None
    cancel_blocked_jobs: bool = False
    platforms: Mapping[str, PlatformProfile] = field(default_factory=lambda: MappingProxyType({}))
    default_platform: str | None = None
    ssh: SSHPolicy = field(default_factory=SSHPolicy)
    tls: TLSConfig | None = None
    public_url: str | None = None
    database: DatabaseConfig | None = None
    email: EmailConfig | None = None
    google: GoogleAuthConfig | None = None
    bootstrap_admin_email: str | None = None
    auth: str = "none"
    allow_plaintext: bool = False
    allowed_projects: frozenset[str] = frozenset()
    execution_registry: str = "legacy"
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.platforms, MappingProxyType):
            object.__setattr__(self, "platforms", MappingProxyType(dict(self.platforms)))

    @property
    def platform(self) -> PlatformProfile | None:
        """The default Platform Profile, or `None` when no Platform is configured."""
        return self.platforms.get(self.default_platform) if self.default_platform else None

    @property
    def providers(self) -> Mapping[str, PlatformProfile]:
        """Deprecated alias of `platforms`."""
        return self.platforms

    @property
    def default_provider(self) -> str | None:
        """Deprecated alias of `default_platform`."""
        return self.default_platform

    @property
    def provider(self) -> PlatformProfile | None:
        """Deprecated alias of `platform`."""
        return self.platform

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
            "default_platform": self.default_platform,
            "platforms": {
                p.id: {
                    "operator": p.operator,
                    "name": p.display_name,
                    "ssh_mode": p.ssh_mode,
                    "login_nodes": [str(n) for n in p.login_nodes],
                    "account": p.account,
                    "partition": p.partition,
                    "time": p.time,
                    "workdir": p.workdir,
                    "ssh_user": p.ssh_user,
                }
                for p in self.platforms.values()
            },
            "ssh_modes_allowed": list(self.ssh.allowed_modes),
            "ssh_private_key": "***" if self.ssh.private_key else None,
            "ssh_identity_file": self.ssh.identity_file,
            "ssh_known_hosts_file": self.ssh.known_hosts_file,
            "database": self.database.summary() if self.database else None,
            "email": self.email.summary() if self.email else None,
            "google": self.google.summary() if self.google else None,
            # The address is personal data, so only whether it is set is logged.
            "bootstrap_admin_configured": self.bootstrap_admin_email is not None,
            "auth": self.auth,
            "allow_plaintext": self.allow_plaintext,
            "allowed_projects": sorted(self.allowed_projects),
            "execution_registry": self.execution_registry,
        }


@dataclass(frozen=True)
class ClientConfig:
    """
    Validated SDK client configuration.

    Attributes:
        address (`str`): Controller `host:port`.
        rpc_timeout (`float`): `Execute` deadline in seconds.
        tls (`bool`): Whether to use a TLS channel.
        tls_ca_file (`str | None`): PEM roots for TLS. `None` uses gRPC's default roots.
        project_id (`str | None`): `NEST_PROJECT_ID`, the Project calls run in, for
            a Controller with session authentication.
        auth_url (`str | None`): The Nest origin whose stored session (see
            [`login.ensure_session`]) the SDK sends with each call: `NEST_AUTH_URL`,
            or [`config.HOSTED_AUTH_URL`] for the hosted Controller. `None` sends no
            session and never starts a sign-in.
        auth_url_source (`str | None`): Where `auth_url` came from, one of
            [`config.AUTH_URL_SOURCES`].
    """

    address: str = DEFAULT_CONTROLLER_ADDRESS
    rpc_timeout: float = DEFAULT_RPC_TIMEOUT
    tls: bool = False
    tls_ca_file: str | None = None
    project_id: str | None = None
    auth_url: str | None = None
    auth_url_source: str | None = None


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

    def seconds(self, name: str, default: float, *, allow_zero: bool = False) -> float:
        raw = self.get(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            value = math.nan
        if allow_zero and 0 <= value < math.inf:
            return value
        if not 0 < value < math.inf:
            kind = "nonnegative" if allow_zero else "positive"
            self.fail(f"{name} must be a {kind} number of seconds, got {raw!r}")
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

    def integer(self, name: str, default: int, minimum: int) -> int:
        raw = self.get(name)
        if raw is None:
            return default
        if not raw.isdecimal() or int(raw) < minimum:
            self.fail(f"{name} must be a whole number of at least {minimum}, got {raw!r}")
            return default
        return int(raw)

    def private_file(self, name: str) -> str | None:
        path = self.readable_file(name)
        if path and os.path.isfile(path) and os.stat(path).st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            self.fail(f"{name} {path!r} is readable by other users; chmod 600 it")
        return path

    def _file_text(self, name: str, path: str) -> str | None:
        """Reads a file named by setting `name`; reports it, without its contents, if unusable."""
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError, UnicodeDecodeError:
            self.fail(f"{name} {path!r} cannot be read as UTF-8 text")
            return None
        if not text.strip():
            self.fail(f"{name} {path!r} is empty")
            return None
        return text

    def secret_source(self, direct: str, file: str) -> tuple[str | None, str | None, bool]:
        """
        Reads a one-line secret given directly in `direct` or in the file named by `file`.

        Unset and blank values mean unset; surrounding blank space is trimmed. Setting
        both is an error, as is an empty file or a value with a line break. Messages
        name the variables and paths, never the value.

        Returns:
            `tuple[str | None, str | None, bool]`: The direct value, the file path, and
                whether either was set (even if invalid), for "needs a secret" checks.
        """
        raw = self.get(direct)
        file_set = self.get(file) is not None
        if raw is not None and file_set:
            self.fail(f"Set only one of {direct} and {file}")
            return None, None, True
        if raw is not None:
            if "\n" in raw or "\r" in raw:
                self.fail(f"{direct} must be a single line; remove the line break")
                return None, None, True
            return raw, None, True
        if not file_set:
            return None, None, False
        before = len(self.problems)
        path = self.readable_file(file)
        if path is None or len(self.problems) > before:
            return None, None, True
        text = self._file_text(file, path)
        if text is not None and "\n" in text.strip():
            self.fail(f"{file} {path!r} must hold the secret on one line")
        return None, (path if len(self.problems) == before else None), True

    def content_source(
        self, direct: str, file: str, *, kind: str | None = None, private: bool = False
    ) -> tuple[str | None, str | None, bool]:
        """
        Reads multiline content (PEM, `known_hosts`) given inline in `direct` or as a file.

        Inline content may use real line breaks or the two characters `\\n`; it is
        normalized by [`runtime_files.normalize_content`]. `kind` (`"certificate"` or
        `"private key"`) checks inline content for the matching PEM marker. With
        `private`, the file must not be readable by group or others. Setting both is
        an error.

        Returns:
            `tuple[str | None, str | None, bool]`: The inline content, the file path,
                and whether either was set.
        """
        raw = self.get(direct)
        file_set = self.get(file) is not None
        if raw is not None and file_set:
            self.fail(f"Set only one of {direct} and {file}")
            return None, None, True
        if raw is not None:
            content = normalize_content(raw)
            marker = _PEM_MARKERS.get(kind or "")
            if marker is not None and marker not in content:
                self.fail(f"{direct} does not look like a PEM {kind}")
                return None, None, True
            return content, None, True
        if not file_set:
            return None, None, False
        before = len(self.problems)
        path = self.private_file(file) if private else self.readable_file(file)
        if path is None or len(self.problems) > before:
            return None, None, True
        return None, path, True

    def address(self, name: str, default: str) -> str:
        raw = self.get(name) or default
        host, sep, port = raw.rpartition(":")
        if not sep or not host or not port.isdecimal() or not 0 <= int(port) <= 65535:
            self.fail(f"{name} must be host:port, got {raw!r}")
        return raw


_PEM_MARKERS = {"certificate": "-----BEGIN CERTIFICATE-----", "private key": "PRIVATE KEY-----"}


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
    reader: _Reader,
    provider_id: str,
    executor: str,
    *,
    legacy: bool = False,
    prefix: str | None = None,
) -> PlatformProfile | None:
    """
    Reads one Platform Profile from `<prefix>*` settings.

    The `default` profile uses the `NEST_SLURM_` prefix, accepts `NEST_SLURM_HOST` as
    a one-entry login-node list, and is optional in local mode. Only profiles under
    the `NEST_PLATFORM_<ID>_` prefix read the `OPERATOR` and `NAME` metadata.
    """
    if prefix is None:
        prefix = "NEST_SLURM_" if legacy else f"NEST_PROVIDER_{provider_id.upper()}_"
    platform = prefix.startswith("NEST_PLATFORM_")
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
                + " (or define HPC Platforms with NEST_PLATFORMS)"
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
    operator = display_name = None
    if platform:
        operator = reader.get(prefix + "OPERATOR")
        if operator is not None:
            operator = operator.lower()
            if not _OPERATOR.fullmatch(operator):
                reader.fail(
                    f"{prefix}OPERATOR must be a label of lowercase letters, digits, '.', "
                    f"'-' and '_' (at most 64), got {operator[:70]!r}"
                )
        display_name = reader.get(prefix + "NAME")
        if display_name is not None and not _DISPLAY_NAME.fullmatch(display_name):
            reader.fail(f"{prefix}NAME must be 1 to 100 printable characters")
    if len(reader.problems) > before or account is None:
        return None
    return PlatformProfile(
        id=provider_id,
        ssh_mode=mode,
        login_nodes=nodes,
        account=account,
        partition=partition,
        time=time,
        workdir=workdir,
        ssh_user=user,
        operator=operator,
        display_name=display_name,
    )


def _ssh_policy(reader: _Reader, profiles: list[PlatformProfile]) -> SSHPolicy:
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
                f"Platform {profile.id!r} uses SSH mode {profile.ssh_mode}, which "
                f"NEST_SSH_MODES_ALLOWED={','.join(allowed)} does not allow"
            )

    known_hosts_data, known_hosts_file, known_hosts_set = reader.content_source(
        "NEST_SSH_KNOWN_HOSTS_DATA", "NEST_SSH_KNOWN_HOSTS"
    )
    private_key, identity_file, key_set = reader.content_source(
        "NEST_SSH_PRIVATE_KEY", "NEST_SSH_IDENTITY_FILE", private=True
    )
    if private_key is not None and "PRIVATE KEY-----" not in private_key:
        reader.fail("NEST_SSH_PRIVATE_KEY does not look like a PEM or OpenSSH private key")
    ssh_config = reader.get("NEST_SSH_CONFIG")
    if ssh_config is not None:
        try:
            check_ssh_config(normalize_content(ssh_config))
        except RuntimeFileError as exc:
            reader.fail(str(exc))

    if any(p.ssh_mode == "B" for p in profiles):
        if not known_hosts_set:
            reader.fail(
                "Mode B needs verified host keys: set NEST_SSH_KNOWN_HOSTS (file) or "
                "NEST_SSH_KNOWN_HOSTS_DATA (known_hosts lines)"
            )
        if not key_set:
            reader.fail(
                "Mode B needs a key: set NEST_SSH_IDENTITY_FILE (file) or "
                "NEST_SSH_PRIVATE_KEY (key contents)"
            )
    return SSHPolicy(
        allowed_modes=allowed,
        known_hosts_file=known_hosts_file,
        known_hosts_data=known_hosts_data,
        identity_file=identity_file,
        private_key=Secret(private_key) if private_key is not None else None,
    )


def _bootstrap_admin_email(reader: _Reader, database: DatabaseConfig | None) -> str | None:
    """Reads `NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL`. The value is never echoed in errors."""
    name = "NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL"
    raw = reader.get(name)
    if raw is None:
        return None
    if not _EMAIL_ADDRESS.fullmatch(raw) or len(raw) > 320:
        reader.fail(f"{name} must be one Google account address, such as admin@example.com")
        return None
    if database is None and reader.get("NEST_DATABASE_URL") is None:
        reader.fail(
            f"{name} needs NEST_DATABASE_URL: the one-time bootstrap is recorded in the database"
        )
    if reader.get("NEST_GOOGLE_CLIENT_ID") is None:
        reader.fail(
            f"{name} has no effect without Google sign-in; set NEST_GOOGLE_CLIENT_ID or unset it"
        )
    return raw.lower()


def _reserved(reader: _Reader) -> None:
    for name in RESERVED_SETTINGS:
        if reader.get(name) is not None:
            reader.fail(f"{name} is reserved for a future release and not supported yet; unset it")


_DATABASE_URL_EXAMPLE = "postgresql://nest@db.example.internal:5432/nest"


def _database(reader: _Reader) -> DatabaseConfig | None:
    """
    Reads the `NEST_DATABASE_*` settings.

    Never echoes the URL: it can hold a password. Settings given both in the URL
    query and as their own variable are rejected so only one value can apply.
    """
    raw = reader.get("NEST_DATABASE_URL")
    required = reader.boolean("NEST_DATABASE_REQUIRED")
    if raw is None:
        if required:
            reader.fail(
                "NEST_DATABASE_REQUIRED=true but NEST_DATABASE_URL is unset; set it to the "
                f"externally managed PostgreSQL database, such as {_DATABASE_URL_EXAMPLE}"
            )
        stray = [name for name in DATABASE_SETTINGS[2:] if reader.get(name) is not None]
        if stray:
            reader.fail(
                ", ".join(stray) + " have no effect without NEST_DATABASE_URL; set or unset"
            )
        return None

    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - validates the port
        query = parse_qs(parts.query)
    except ValueError:
        reader.fail(f"NEST_DATABASE_URL is not a valid URL; use the form {_DATABASE_URL_EXAMPLE}")
        return None
    before = len(reader.problems)
    if parts.scheme not in DATABASE_SCHEMES:
        reader.fail(
            "NEST_DATABASE_URL must start with postgresql:// (Nest supports only PostgreSQL), "
            f"such as {_DATABASE_URL_EXAMPLE}"
        )
    if not parts.hostname and "host" not in query:
        reader.fail(
            "NEST_DATABASE_URL must name the database host; Nest never uses an implicit local "
            "server"
        )
    if not parts.path.strip("/") and "dbname" not in query:
        reader.fail("NEST_DATABASE_URL must name the database, such as .../nest")

    password, password_file, password_set = reader.secret_source(
        "NEST_DATABASE_PASSWORD", "NEST_DATABASE_PASSWORD_FILE"
    )
    if password_set and (parts.password or "password" in query):
        reader.fail(
            "Set the database password in only one of NEST_DATABASE_URL, "
            "NEST_DATABASE_PASSWORD and NEST_DATABASE_PASSWORD_FILE"
        )

    url_sslmode = query.get("sslmode", [None])[-1]
    if url_sslmode is not None and url_sslmode not in DATABASE_SSLMODES:
        # Query values are never echoed: the query string may carry secrets.
        reader.fail(
            "NEST_DATABASE_URL has an invalid sslmode in its query string (value not "
            "shown); use one of " + ", ".join(DATABASE_SSLMODES)
        )
    sslmode = reader.choice("NEST_DATABASE_SSLMODE", DATABASE_SSLMODES, "")
    files: dict[str, str | None] = {}
    inline: dict[str, str | None] = {}
    given: dict[str, bool] = {}
    for key, kind in (
        ("sslrootcert", "certificate"),
        ("sslcert", "certificate"),
        ("sslkey", "private key"),
    ):
        variable = f"NEST_DATABASE_{key.upper()}"
        inline[key], files[key], given[key] = reader.content_source(
            f"{variable}_DATA", variable, kind=kind, private=key == "sslkey"
        )
    for key, variable in (("sslmode", bool(sslmode)), *given.items()):
        if variable and key in query:
            reader.fail(
                f"{key} is set in both NEST_DATABASE_URL and NEST_DATABASE_{key.upper()}; keep one"
            )
    if given["sslcert"] != given["sslkey"]:
        reader.fail(
            "Set both NEST_DATABASE_SSLCERT and NEST_DATABASE_SSLKEY (or their _DATA forms), "
            "or neither"
        )
    if given["sslrootcert"] and not sslmode and url_sslmode is None:
        sslmode = "verify-full"
    effective = sslmode or url_sslmode
    if given["sslrootcert"] and effective in ("disable", "allow", "prefer"):
        reader.fail(
            f"NEST_DATABASE_SSLROOTCERT is not used with sslmode={effective}; "
            "use verify-full (recommended) or verify-ca"
        )

    pool_min = reader.integer("NEST_DATABASE_POOL_MIN_SIZE", DEFAULT_DATABASE_POOL_MIN_SIZE, 0)
    pool_max = reader.integer("NEST_DATABASE_POOL_MAX_SIZE", DEFAULT_DATABASE_POOL_MAX_SIZE, 1)
    if pool_min > pool_max:
        reader.fail(
            f"NEST_DATABASE_POOL_MIN_SIZE ({pool_min}) exceeds NEST_DATABASE_POOL_MAX_SIZE "
            f"({pool_max})"
        )
    connect_timeout = reader.seconds(
        "NEST_DATABASE_CONNECT_TIMEOUT", DEFAULT_DATABASE_CONNECT_TIMEOUT
    )
    pool_timeout = reader.seconds("NEST_DATABASE_POOL_TIMEOUT", DEFAULT_DATABASE_POOL_TIMEOUT)
    if len(reader.problems) > before:
        return None
    return DatabaseConfig(
        url=Secret(raw),
        password=Secret(password) if password is not None else None,
        password_file=password_file,
        sslmode=sslmode or None,
        pool_min_size=pool_min,
        pool_max_size=pool_max,
        connect_timeout=connect_timeout,
        pool_timeout=pool_timeout,
        sslrootcert_data=inline["sslrootcert"],
        sslcert_data=inline["sslcert"],
        sslkey_data=Secret(inline["sslkey"]) if inline["sslkey"] is not None else None,
        **files,
    )


_SENDER_EXAMPLE = "Nest <nest@example.com>"


def _loopback_host(host: str) -> bool:
    return host == "localhost" or host.endswith(".localhost") or host in ("127.0.0.1", "::1")


def _sender(reader: _Reader) -> tuple[str, str] | None:
    raw = reader.get("NEST_EMAIL_FROM")
    if raw is None:
        reader.fail(
            "NEST_EMAIL_FROM is required when NEST_EMAIL_ENABLED=true; set it to the "
            f"verified sender, such as {_SENDER_EXAMPLE!r}"
        )
        return None
    name, address = parseaddr(raw, strict=True)
    try:
        if not address or "\r" in raw or "\n" in raw:
            raise ValueError
        parsed = Address(display_name=name, addr_spec=address)
        if not parsed.username or "." not in parsed.domain:
            raise ValueError
    except ValueError:
        reader.fail(f"NEST_EMAIL_FROM must be one address such as {_SENDER_EXAMPLE!r}, got {raw!r}")
        return None
    return str(parsed), parsed.addr_spec


def _email_public_url(reader: _Reader) -> str | None:
    raw = reader.get("NEST_PUBLIC_URL")
    if raw is None:
        reader.fail(
            "NEST_PUBLIC_URL is required when NEST_EMAIL_ENABLED=true: links in messages "
            "are built from it, such as https://nest.example.com"
        )
        return None
    parts = urlsplit(raw)
    hostname = parts.hostname or ""
    if parts.scheme != "https" and not (parts.scheme == "http" and _loopback_host(hostname)):
        reader.fail(
            "NEST_PUBLIC_URL must be an https:// URL when email is enabled, because "
            f"invitation links carry tokens; got {raw!r}"
        )
        return None
    if parts.fragment:
        reader.fail("NEST_PUBLIC_URL must not contain a fragment when email is enabled")
        return None
    return raw.rstrip("/")


def _email(reader: _Reader, database: DatabaseConfig | None) -> EmailConfig | None:
    """
    Reads the `NEST_EMAIL_*` and `NEST_SMTP_*` settings.

    Nothing is read unless `NEST_EMAIL_ENABLED=true`, so a deployment without mail
    never fails on a stale SMTP setting. When enabled, every required setting is
    validated and an insecure transport is never chosen silently.
    """
    if not reader.boolean("NEST_EMAIL_ENABLED"):
        return None
    before = len(reader.problems)
    provider = reader.choice("NEST_EMAIL_PROVIDER", EMAIL_PROVIDERS, "smtp")
    sender = _sender(reader)
    public_url = _email_public_url(reader)
    if database is None and reader.get("NEST_DATABASE_URL") is None:
        reader.fail(
            "NEST_EMAIL_ENABLED=true needs NEST_DATABASE_URL: queued messages and their "
            "delivery status are kept in the database outbox"
        )

    host = reader.get("NEST_SMTP_HOST")
    if host is None:
        reader.fail("NEST_SMTP_HOST is required when NEST_EMAIL_ENABLED=true")
    elif not _HOST.fullmatch(host):
        reader.fail(f"NEST_SMTP_HOST must be a host name, got {host!r}")

    starttls = reader.boolean("NEST_SMTP_STARTTLS", default=True)
    implicit = reader.boolean("NEST_SMTP_IMPLICIT_TLS", default=False)
    if implicit and reader.get("NEST_SMTP_STARTTLS") is None:
        starttls = False
    if starttls and implicit:
        reader.fail("Set only one of NEST_SMTP_STARTTLS=true and NEST_SMTP_IMPLICIT_TLS=true")
    elif not (starttls or implicit):
        reader.fail(
            "NEST_SMTP_STARTTLS=false needs NEST_SMTP_IMPLICIT_TLS=true: Nest never sends "
            "mail over an unencrypted connection"
        )
    tls_mode = "implicit" if implicit and not starttls else "starttls"
    default_port = (
        DEFAULT_SMTP_IMPLICIT_TLS_PORT if tls_mode == "implicit" else DEFAULT_SMTP_STARTTLS_PORT
    )
    port = reader.integer("NEST_SMTP_PORT", default_port, 1)
    if port > 65535:
        reader.fail(f"NEST_SMTP_PORT must be at most 65535, got {port}")

    username = reader.get("NEST_SMTP_USERNAME")
    password_raw, password_file, has_password = reader.secret_source(
        "NEST_SMTP_PASSWORD", "NEST_SMTP_PASSWORD_FILE"
    )
    if username is None and has_password:
        reader.fail("NEST_SMTP_PASSWORD needs NEST_SMTP_USERNAME")
    if username is not None and not has_password:
        reader.fail(
            "NEST_SMTP_USERNAME needs a password: set NEST_SMTP_PASSWORD_FILE (recommended) "
            "or NEST_SMTP_PASSWORD"
        )
    ca_data, ca_file, _ = reader.content_source(
        "NEST_SMTP_CA", "NEST_SMTP_CA_FILE", kind="certificate"
    )
    timeout = reader.seconds("NEST_SMTP_TIMEOUT", DEFAULT_SMTP_TIMEOUT)

    max_attempts = reader.integer("NEST_EMAIL_MAX_ATTEMPTS", DEFAULT_EMAIL_MAX_ATTEMPTS, 1)
    if max_attempts > 100:
        reader.fail(f"NEST_EMAIL_MAX_ATTEMPTS must be at most 100, got {max_attempts}")
    retry_delay = reader.seconds("NEST_EMAIL_RETRY_DELAY", DEFAULT_EMAIL_RETRY_DELAY)
    retry_max_delay = reader.seconds("NEST_EMAIL_RETRY_MAX_DELAY", DEFAULT_EMAIL_RETRY_MAX_DELAY)
    if retry_max_delay < retry_delay:
        reader.fail("NEST_EMAIL_RETRY_MAX_DELAY must be at least NEST_EMAIL_RETRY_DELAY")
    poll_interval = reader.seconds("NEST_EMAIL_POLL_INTERVAL", DEFAULT_EMAIL_POLL_INTERVAL)

    if len(reader.problems) > before or sender is None or public_url is None or host is None:
        return None
    return EmailConfig(
        provider=provider,
        sender=sender[0],
        sender_address=sender[1],
        public_url=public_url,
        smtp=SMTPConfig(
            host=host,
            port=port,
            tls_mode=tls_mode,
            username=username,
            password=Secret(password_raw) if password_raw is not None else None,
            password_file=password_file,
            ca_file=ca_file,
            ca_data=ca_data,
            timeout=timeout,
        ),
        max_attempts=max_attempts,
        retry_delay=retry_delay,
        retry_max_delay=retry_max_delay,
        poll_interval=poll_interval,
    )


_REDIRECT_EXAMPLE = "https://nest.example.com/auth/google/callback"
_GOOGLE_CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")


def _google_redirect_url(reader: _Reader) -> str | None:
    raw = reader.get("NEST_GOOGLE_REDIRECT_URL")
    if raw is None:
        reader.fail(
            "NEST_GOOGLE_REDIRECT_URL is required with NEST_GOOGLE_CLIENT_ID; set it to the "
            f"callback URL registered with Google, such as {_REDIRECT_EXAMPLE!r}"
        )
        return None
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        reader.fail(f"NEST_GOOGLE_REDIRECT_URL must be a URL, got {raw!r}")
        return None
    hostname = parts.hostname or ""
    if parts.scheme != "https" and not (parts.scheme == "http" and _loopback_host(hostname)):
        reader.fail(
            "NEST_GOOGLE_REDIRECT_URL must be an https:// URL (http:// only for localhost), "
            f"because it receives authorization codes; got {raw!r}"
        )
        return None
    if not hostname or parts.username or parts.password:
        reader.fail("NEST_GOOGLE_REDIRECT_URL must name a host and contain no credentials")
        return None
    if parts.query or parts.fragment or "#" in raw:
        reader.fail("NEST_GOOGLE_REDIRECT_URL must not contain a query string or fragment")
        return None
    if not parts.path.startswith("/") or parts.path == "/":
        reader.fail(
            f"NEST_GOOGLE_REDIRECT_URL must have a callback path, such as {_REDIRECT_EXAMPLE!r}"
        )
        return None
    if parts.path == "/auth/google/login":
        reader.fail(
            "NEST_GOOGLE_REDIRECT_URL must not use /auth/google/login, which starts a "
            f"sign-in; use a callback path such as {_REDIRECT_EXAMPLE!r}"
        )
        return None
    if parts.path.startswith(AUTH_CLI_PREFIX):
        reader.fail(
            f"NEST_GOOGLE_REDIRECT_URL must not use {parts.path}, a CLI sign-in route; "
            f"use a callback path such as {_REDIRECT_EXAMPLE!r}"
        )
        return None
    if parts.path == PROJECTS_API_PATH or parts.path.startswith(PROJECTS_API_PATH + "/"):
        reader.fail(
            f"NEST_GOOGLE_REDIRECT_URL must not use {parts.path}, a Projects API route; "
            f"use a callback path such as {_REDIRECT_EXAMPLE!r}"
        )
        return None
    if parts.path == PLATFORMS_API_PATH or parts.path.startswith(PLATFORMS_API_PATH + "/"):
        reader.fail(
            f"NEST_GOOGLE_REDIRECT_URL must not use {parts.path}, a Platforms API route; "
            f"use a callback path such as {_REDIRECT_EXAMPLE!r}"
        )
        return None
    if parts.path in (INVITATIONS_API_PATH, INVITATION_PAGE_PATH) or parts.path.startswith(
        INVITATIONS_API_PATH + "/"
    ):
        reader.fail(
            f"NEST_GOOGLE_REDIRECT_URL must not use {parts.path}, an invitation route; "
            f"use a callback path such as {_REDIRECT_EXAMPLE!r}"
        )
        return None
    if parts.path == CONSOLE_PATH or parts.path.startswith(CONSOLE_PATH + "/"):
        reader.fail(
            f"NEST_GOOGLE_REDIRECT_URL must not use {parts.path}, a Management Console route; "
            f"use a callback path such as {_REDIRECT_EXAMPLE!r}"
        )
        return None
    if parts.path in AUTH_SESSION_PATHS:
        reader.fail(
            f"NEST_GOOGLE_REDIRECT_URL must not use {parts.path}, a browser session route; "
            f"use a callback path such as {_REDIRECT_EXAMPLE!r}"
        )
        return None
    return raw


def _session_policy(reader: _Reader) -> SessionPolicy:
    """Reads the `NEST_AUTH_*` session lifetimes and checks that they nest."""
    before = len(reader.problems)
    access = reader.seconds("NEST_AUTH_ACCESS_TOKEN_TTL", DEFAULT_ACCESS_TOKEN_TTL)
    refresh = reader.seconds("NEST_AUTH_REFRESH_TTL", DEFAULT_REFRESH_TTL)
    lifetime = reader.seconds("NEST_AUTH_SESSION_MAX_LIFETIME", DEFAULT_SESSION_MAX_LIFETIME)
    if not 60 <= access <= 3600:
        reader.fail(
            f"NEST_AUTH_ACCESS_TOKEN_TTL must be between 60 and 3600 seconds, got {access:g}"
        )
    elif refresh < access:
        reader.fail(
            f"NEST_AUTH_REFRESH_TTL ({refresh:.0f} seconds) must be at least "
            f"NEST_AUTH_ACCESS_TOKEN_TTL ({access:.0f} seconds)"
        )
    elif lifetime < refresh:
        reader.fail(
            f"NEST_AUTH_SESSION_MAX_LIFETIME ({lifetime:.0f} seconds) must be at least "
            f"NEST_AUTH_REFRESH_TTL ({refresh:.0f} seconds)"
        )
    if lifetime > MAX_SESSION_LIFETIME:
        reader.fail(
            f"NEST_AUTH_SESSION_MAX_LIFETIME must be at most {MAX_SESSION_LIFETIME:.0f} seconds "
            f"(90 days), got {lifetime:.0f}"
        )
    if len(reader.problems) > before:
        return SessionPolicy()
    return SessionPolicy(access, refresh, lifetime)


def _google(reader: _Reader, database: DatabaseConfig | None) -> GoogleAuthConfig | None:
    """
    Reads the `NEST_GOOGLE_*` settings and `NEST_AUTH_HTTP_BIND`.

    Google sign-in is enabled by `NEST_GOOGLE_CLIENT_ID`. A secret or redirect URL
    without a client ID is reported rather than ignored, so a half-configured
    deployment fails at startup instead of silently running without sign-in.
    """
    client_id = reader.get("NEST_GOOGLE_CLIENT_ID")
    if client_id is None:
        stray = [n for n in GOOGLE_SETTINGS if n != "NEST_GOOGLE_CLIENT_ID" and reader.get(n)]
        if stray:
            reader.fail(f"{', '.join(stray)} needs NEST_GOOGLE_CLIENT_ID")
        return None
    before = len(reader.problems)
    if not _GOOGLE_CLIENT_ID.fullmatch(client_id):
        reader.fail(
            "NEST_GOOGLE_CLIENT_ID must be the OAuth client ID, such as "
            "'1234-abc.apps.googleusercontent.com'"
        )
    secret_raw, secret_file, secret_set = reader.secret_source(
        "NEST_GOOGLE_CLIENT_SECRET", "NEST_GOOGLE_CLIENT_SECRET_FILE"
    )
    if not secret_set:
        reader.fail(
            "NEST_GOOGLE_CLIENT_ID needs a client secret: set NEST_GOOGLE_CLIENT_SECRET_FILE "
            "(recommended) or NEST_GOOGLE_CLIENT_SECRET"
        )
    redirect_url = _google_redirect_url(reader)
    http_bind = reader.address("NEST_AUTH_HTTP_BIND", DEFAULT_AUTH_HTTP_BIND)
    sessions = _session_policy(reader)
    if database is None and reader.get("NEST_DATABASE_URL") is None:
        reader.fail(
            "NEST_GOOGLE_CLIENT_ID needs NEST_DATABASE_URL: Users, their Google identities, "
            "sessions and pending sign-ins are stored in the database"
        )
    if len(reader.problems) > before or redirect_url is None:
        return None
    return GoogleAuthConfig(
        client_id=client_id,
        redirect_url=redirect_url,
        client_secret=Secret(secret_raw) if secret_raw is not None else None,
        client_secret_file=secret_file,
        http_bind=http_bind,
        sessions=sessions,
    )


def load_database_config(environ: Mapping[str, str] | None = None) -> DatabaseConfig:
    """
    Reads and validates the `NEST_DATABASE_*` settings for `nest-db`.

    Args:
        environ (`Mapping[str, str]`, *optional*): Environment. Defaults to `os.environ`.

    Returns:
        [`config.DatabaseConfig`]: The validated settings.

    Raises:
        [`config.ConfigError`]: If `NEST_DATABASE_URL` is unset or a setting is invalid.
    """
    reader = _Reader(environ)
    database = _database(reader)
    if database is None and not reader.problems:
        reader.fail(
            "NEST_DATABASE_URL is required; set it to the externally managed PostgreSQL "
            f"database, such as {_DATABASE_URL_EXAMPLE}"
        )
    if reader.problems or database is None:
        raise ConfigError(reader.problems)
    return database


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
    _merge_platforms_env(reader)
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
        "pending_blocked_timeout": reader.seconds(
            "NEST_PENDING_BLOCKED_TIMEOUT", DEFAULT_PENDING_BLOCKED_TIMEOUT, allow_zero=True
        ),
        "pending_limit_timeout": (
            reader.seconds("NEST_PENDING_LIMIT_TIMEOUT", 0, allow_zero=True)
            if reader.get("NEST_PENDING_LIMIT_TIMEOUT") is not None
            else None
        ),
        "cancel_blocked_jobs": reader.boolean("NEST_CANCEL_BLOCKED_JOBS"),
    }

    platforms, default_platform, platform_warnings = _platforms(reader, executor)

    ssh = _ssh_policy(reader, list(platforms.values()))

    cert_pem, cert_file, cert_set = reader.content_source(
        "NEST_TLS_CERT", "NEST_TLS_CERT_FILE", kind="certificate"
    )
    key_pem, key_file, key_set = reader.content_source(
        "NEST_TLS_KEY", "NEST_TLS_KEY_FILE", kind="private key"
    )
    if cert_set != key_set:
        reader.fail(
            "Set both NEST_TLS_CERT_FILE and NEST_TLS_KEY_FILE (or NEST_TLS_CERT and "
            "NEST_TLS_KEY), or neither"
        )
    tls = (
        TLSConfig(
            cert_file=cert_file,
            key_file=key_file,
            cert_pem=cert_pem,
            key_pem=Secret(key_pem) if key_pem is not None else None,
        )
        if (cert_file or cert_pem) and (key_file or key_pem)
        else None
    )

    public_url = reader.get("NEST_PUBLIC_URL")
    if public_url is not None:
        parts = urlsplit(public_url)
        if parts.scheme not in ("grpc", "grpcs", "http", "https") or not parts.hostname:
            reader.fail(
                f"NEST_PUBLIC_URL must be a grpc(s):// or http(s):// URL, got {public_url!r}"
            )
        elif parts.password or parts.query:
            reader.fail("NEST_PUBLIC_URL must not contain credentials or a query string")

    database = _database(reader)
    email = _email(reader, database)
    google = _google(reader, database)
    bootstrap_admin_email = _bootstrap_admin_email(reader, database)
    auth, allow_plaintext, allowed_projects = _controller_auth(reader, database, bind, tls)
    execution_registry = _execution_registry(
        reader, auth, platforms.get(default_platform) if default_platform else None
    )
    _reserved(reader)

    if reader.problems:
        raise ConfigError(reader.problems)
    return ControllerConfig(
        bind=bind,
        executor=executor,
        log_level=level,
        platforms=platforms,
        default_platform=default_platform,
        warnings=platform_warnings,
        ssh=ssh,
        tls=tls,
        public_url=public_url,
        database=database,
        email=email,
        google=google,
        bootstrap_admin_email=bootstrap_admin_email,
        auth=auth,
        allow_plaintext=allow_plaintext,
        allowed_projects=allowed_projects,
        execution_registry=execution_registry,
        **timeouts,
    )


def _merge_platforms_env(reader: _Reader) -> None:
    """
    Applies `NEST_PLATFORMS_ENV`: the lines of a `platforms.env` file in one value.

    Lines are `NAME=value` with blank lines and `#` comments ignored. Values are
    literal: one pair of matching surrounding quotes is removed and nothing is
    expanded. Only `NEST_PLATFORMS`, `NEST_DEFAULT_PLATFORM` and
    `NEST_PLATFORM_<ID>_<FIELD>` may appear, so no credential can be smuggled in, and
    a name also set as its own variable is an error rather than silently overridden.
    """
    raw = reader.environ.get(PLATFORMS_ENV, "")
    if not raw.strip():
        return
    merged = dict(reader.environ)
    seen: set[str] = set()
    for number, line in enumerate(normalize_content(raw).splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not _PLATFORMS_ENV_KEY.fullmatch(name):
            # The line is not echoed: a misplaced line could be a credential.
            reader.fail(
                f"{PLATFORMS_ENV} line {number} must be NEST_PLATFORMS=..., "
                "NEST_DEFAULT_PLATFORM=... or NEST_PLATFORM_<ID>_<FIELD>=..."
            )
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name in seen:
            reader.fail(f"{name} appears more than once in {PLATFORMS_ENV}")
        elif reader.get(name) is not None:
            reader.fail(f"{name} is set both in {PLATFORMS_ENV} and as its own variable; keep one")
        seen.add(name)
        merged[name] = value
    reader.environ = merged


def _set_names(reader: _Reader, prefix: str, *, exclude: tuple[str, ...] = ()) -> list[str]:
    """Names of the non-empty settings starting with `prefix`, sorted."""
    return sorted(
        name
        for name in reader.environ
        if name.startswith(prefix) and name not in exclude and reader.get(name)
    )


def _platforms(
    reader: _Reader, executor: str
) -> tuple[dict[str, PlatformProfile], str | None, tuple[str, ...]]:
    """
    Reads the HPC Platform Profiles and the default Platform.

    Three forms are accepted, and exactly one may be used:

    - `NEST_PLATFORMS` with `NEST_PLATFORM_<ID>_*` (current);
    - `NEST_PROVIDERS` with `NEST_PROVIDER_<ID>_*` (deprecated, same fields);
    - `NEST_SLURM_*`, which defines the Platform `default`.
    """
    platforms: dict[str, PlatformProfile] = {}
    slurm = _set_names(reader, "NEST_SLURM_")
    provider_fields = _set_names(
        reader, "NEST_PROVIDER_", exclude=("NEST_PROVIDER_ALLOWED_PROJECTS",)
    )
    platform_fields = _set_names(reader, "NEST_PLATFORM_")
    if reader.get("NEST_PLATFORMS") is not None:
        notes: tuple[str, ...] = ()
        mode_only = slurm == ["NEST_SLURM_SSH_MODE"]
        if mode_only and (reader.get("NEST_SLURM_SSH_MODE") or "").upper() == "B":
            # compose.managed-ssh.yaml always sets it; each Platform sets its own mode.
            slurm = []
            notes = (
                "NEST_SLURM_SSH_MODE=B is ignored with NEST_PLATFORMS; set "
                + "NEST_PLATFORM_<ID>_SSH_MODE for each Platform",
            )
        clash = [
            name
            for name in ("NEST_PROVIDERS", "NEST_DEFAULT_PROVIDER")
            if reader.get(name) is not None
        ]
        clash += provider_fields + slurm
        if clash:
            reader.fail(
                "NEST_PLATFORMS replaces the Compute Provider and single-cluster settings; "
                "unset " + ", ".join(clash)
            )
        ids = _platform_ids(reader, "NEST_PLATFORMS")
        for platform_id in ids:
            profile = _profile(
                reader, platform_id, executor, prefix=f"NEST_PLATFORM_{platform_id.upper()}_"
            )
            if profile is not None:
                platforms[platform_id] = profile
        _check_platform_names(reader, platform_fields, ids)
        _check_distinct_login_nodes(reader, platforms)
        default = _default_platform(reader, "NEST_DEFAULT_PLATFORM", "NEST_PLATFORMS", ids)
        return platforms, default, notes

    stray = platform_fields + (
        ["NEST_DEFAULT_PLATFORM"] if reader.get("NEST_DEFAULT_PLATFORM") is not None else []
    )
    if stray:
        reader.fail(", ".join(stray) + " need NEST_PLATFORMS, which lists the Platform IDs")
    if reader.get("NEST_PROVIDERS") is not None:
        if slurm:
            reader.fail("NEST_PROVIDERS replaces the legacy settings; unset " + ", ".join(slurm))
        ids = _platform_ids(reader, "NEST_PROVIDERS")
        for provider_id in ids:
            profile = _profile(reader, provider_id, executor)
            if profile is not None:
                platforms[provider_id] = profile
        default = _default_platform(reader, "NEST_DEFAULT_PROVIDER", "NEST_PROVIDERS", ids)
        deprecation = (
            "NEST_PROVIDERS, NEST_DEFAULT_PROVIDER and NEST_PROVIDER_<ID>_* are deprecated: "
            "rename them to NEST_PLATFORMS, NEST_DEFAULT_PLATFORM and NEST_PLATFORM_<ID>_*"
        )
        return platforms, default, (deprecation,)

    if reader.get("NEST_DEFAULT_PROVIDER"):
        reader.fail("NEST_DEFAULT_PROVIDER needs NEST_PROVIDERS")
    profile = _profile(reader, LEGACY_PLATFORM_ID, executor, legacy=True)
    if profile is None:
        return platforms, None, ()
    platforms[profile.id] = profile
    return platforms, profile.id, ()


def _platform_ids(reader: _Reader, name: str) -> list[str]:
    """Reads a comma-separated list of Platform IDs, reporting invalid and repeated ones."""
    entries = [i.strip() for i in (reader.get(name) or "").split(",") if i.strip()]
    if not entries:
        reader.fail(f"{name} must list at least one ID")
    ids: list[str] = []
    for platform_id in entries:
        if not _PLATFORM_ID.fullmatch(platform_id):
            reader.fail(
                f"{name} has invalid ID {platform_id[:40]!r}; use lowercase letters, "
                "digits and _, starting with a letter (at most 32)"
            )
        elif platform_id in ids:
            reader.fail(f"{name} lists {platform_id!r} twice")
        else:
            ids.append(platform_id)
    return ids


def _default_platform(reader: _Reader, name: str, list_name: str, ids: list[str]) -> str | None:
    default = reader.get(name)
    if default is None:
        return ids[0] if ids else None
    if default not in ids:
        reader.fail(f"{name} {default[:40]!r} is not in {list_name}")
    return default


def _check_platform_names(reader: _Reader, names: list[str], ids: list[str]) -> None:
    """Reports `NEST_PLATFORM_*` settings that belong to no listed Platform or field."""
    known = {f"NEST_PLATFORM_{i.upper()}_{f}" for i in ids for f in PLATFORM_FIELDS}
    unknown = [name for name in names if name not in known]
    if unknown:
        reader.fail(
            ", ".join(unknown)
            + " match no Platform in NEST_PLATFORMS and field "
            + "/".join(PLATFORM_FIELDS)
            + "; fix the name or unset it"
        )


def _check_distinct_login_nodes(reader: _Reader, platforms: Mapping[str, PlatformProfile]) -> None:
    """Reports a login-node endpoint approved for more than one Platform."""
    owners: dict[tuple[str, int | None], str] = {}
    for profile in platforms.values():
        for node in profile.login_nodes:
            # An omitted port is SSH's default, 22, so `host` and `host:22` are one endpoint.
            key = (node.host.lower(), node.port if node.port is not None else 22)
            owner = owners.setdefault(key, profile.id)
            if owner != profile.id:
                reader.fail(
                    f"Login node {node} is approved for both Platforms {owner!r} and "
                    f"{profile.id!r}; each endpoint belongs to one Platform"
                )


def _controller_auth(
    reader: _Reader, database: DatabaseConfig | None, bind: str, tls: TLSConfig | None
) -> tuple[str, bool, frozenset[str]]:
    """
    Reads `NEST_CONTROLLER_AUTH`, `NEST_CONTROLLER_ALLOW_PLAINTEXT` and
    `NEST_PROVIDER_ALLOWED_PROJECTS`.

    Session authentication needs the database, and its bearer tokens travel only
    over TLS, a loopback listener, or a network the operator has approved with
    `NEST_CONTROLLER_ALLOW_PLAINTEXT=true`.
    """
    auth = reader.choice(
        "NEST_CONTROLLER_AUTH", CONTROLLER_AUTH_MODES, "session" if database else "none"
    )
    allow_plaintext = reader.boolean("NEST_CONTROLLER_ALLOW_PLAINTEXT")
    raw = reader.get("NEST_PROVIDER_ALLOWED_PROJECTS")
    projects: set[str] = set()
    for item in (raw or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item == ALL_PROJECTS:
            projects.add(ALL_PROJECTS)
            continue
        try:
            projects.add(str(UUID(item)))
        except ValueError:
            reader.fail(
                f"NEST_PROVIDER_ALLOWED_PROJECTS has {item[:40]!r}, which is not a Project "
                "ID; list Project UUIDs separated by commas, or '*' for every Project"
            )
    if ALL_PROJECTS in projects and len(projects) > 1:
        reader.fail("NEST_PROVIDER_ALLOWED_PROJECTS cannot combine '*' with Project IDs")
    if auth == "session":
        if database is None:
            reader.fail(
                "NEST_CONTROLLER_AUTH=session needs NEST_DATABASE_URL, where Nest sessions "
                "and Project memberships are stored"
            )
        if tls is None and not allow_plaintext and not _is_loopback_address(bind):
            reader.fail(
                "NEST_CONTROLLER_AUTH=session sends Nest session tokens with every call: set "
                "NEST_TLS_CERT_FILE and NEST_TLS_KEY_FILE, or set "
                "NEST_CONTROLLER_ALLOW_PLAINTEXT=true only when the network to the listener is "
                "already encrypted and access-controlled, such as a Tailnet"
            )
    else:
        # Without authentication it would restrict nothing, so it is refused rather
        # than suggesting otherwise. NEST_CONTROLLER_ALLOW_PLAINTEXT only loosens a
        # check that does not apply then, so it is ignored.
        if raw is not None:
            reader.fail("NEST_PROVIDER_ALLOWED_PROJECTS needs NEST_CONTROLLER_AUTH=session")
        allow_plaintext = False
    return auth, allow_plaintext, frozenset(projects)


def _execution_registry(reader: _Reader, auth: str, platform: PlatformProfile | None) -> str:
    """
    Reads `NEST_EXECUTION_REGISTRY`.

    The registry needs an authenticated Nest User to own each execution, so it
    requires session authentication. Under session authentication the legacy path
    is an explicit, Mode A-only choice: Mode B must never run a User's job with the
    Controller's own credentials. A registry-backed Mode A Platform must name the
    operator's UNIX user (`SSH_USER`), which every attempt records as its effective
    HPC identity.
    """
    mode = reader.choice(
        "NEST_EXECUTION_REGISTRY",
        EXECUTION_REGISTRY_MODES,
        "required" if auth == "session" else "legacy",
    )
    if mode == "required" and auth != "session":
        reader.fail(
            "NEST_EXECUTION_REGISTRY=required needs NEST_CONTROLLER_AUTH=session: every "
            "registry-backed execution is owned by an authenticated Nest User"
        )
    if platform is None or auth != "session":
        return mode
    prefix = f"the {platform.id!r} Platform"
    if mode == "legacy" and platform.ssh_mode == "B":
        reader.fail(
            f"NEST_EXECUTION_REGISTRY=legacy cannot serve {prefix} in Mode B with session "
            "authentication: Mode B never falls back to the Controller's credentials; use "
            "NEST_EXECUTION_REGISTRY=required"
        )
    if mode == "required" and platform.ssh_mode == "A" and platform.ssh_user is None:
        reader.fail(
            f"NEST_EXECUTION_REGISTRY=required needs the operator's UNIX user for {prefix}: "
            "set its SSH_USER (such as NEST_PLATFORM_<ID>_SSH_USER or NEST_SLURM_SSH_USER), "
            "which every execution attempt records as its HPC identity"
        )
    return mode


def _is_loopback_address(address: str) -> bool:
    host, _, _ = address.rpartition(":")
    if host.lower().removesuffix(".") == "localhost":
        return True
    try:
        return ip_address(host.removeprefix("[").removesuffix("]")).is_loopback
    except ValueError:
        return False


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
    address_is_valid = not reader.problems
    timeout = reader.seconds("NEST_RPC_TIMEOUT", DEFAULT_RPC_TIMEOUT)
    ca_file = reader.readable_file("NEST_CONTROLLER_TLS_CA_FILE")
    tls = reader.boolean("NEST_CONTROLLER_TLS", default=ca_file is not None)
    if (
        address_is_valid
        and ca_file is None
        and reader.get("NEST_CONTROLLER_TLS") is None
        and not _is_loopback_address(address)
    ):
        reader.fail(
            "Non-loopback NEST_CONTROLLER_ADDRESS requires an explicit NEST_CONTROLLER_TLS "
            "setting (true or false) or NEST_CONTROLLER_TLS_CA_FILE"
        )
    if ca_file and not tls:
        reader.fail("NEST_CONTROLLER_TLS_CA_FILE is set but NEST_CONTROLLER_TLS is false")
    project_id = reader.get("NEST_PROJECT_ID")
    if project_id is not None:
        try:
            project_id = str(UUID(project_id))
        except ValueError:
            reader.fail("NEST_PROJECT_ID must be a Project ID (a UUID)")
            project_id = None
    auth_url, source = resolve_auth_url(
        reader, hosted=address_is_valid and is_hosted_controller(address)
    )
    if reader.problems:
        raise ConfigError(reader.problems)
    return ClientConfig(
        address=address,
        rpc_timeout=timeout,
        tls=tls,
        tls_ca_file=ca_file,
        project_id=project_id,
        auth_url=auth_url,
        auth_url_source=source,
    )


def is_hosted_controller(address: str) -> bool:
    """Whether `address` (`host:port`) names [`config.HOSTED_CONTROLLER_ADDRESS`]."""
    host, _, port = address.strip().rpartition(":")
    hosted_host, _, hosted_port = HOSTED_CONTROLLER_ADDRESS.rpartition(":")
    return host.lower().removesuffix(".") == hosted_host and port == hosted_port


def resolve_auth_url(
    reader: _Reader, *, url: str | None = None, hosted: bool
) -> tuple[str | None, str | None]:
    """
    Resolves the Nest sign-in origin, for the CLI and the SDK alike.

    Precedence: `url` (the CLI's `--url`), then `NEST_AUTH_URL`, then, only when
    `hosted`, [`config.HOSTED_AUTH_URL`]. Empty or blank values count as unset. An
    invalid explicit value is reported to `reader` and never replaced by the hosted
    default.

    Args:
        reader (`_Reader`): The environment being validated.
        url (`str`, *optional*): An explicit origin, such as from `--url`.
        hosted (`bool`): Whether the hosted default applies: always for the CLI, and
            for the SDK only when it calls the hosted Controller, so a custom or
            local Controller never receives a hosted session.

    Returns:
        `tuple[str | None, str | None]`: The normalized origin and its source (one of
            [`config.AUTH_URL_SOURCES`]), or `(None, None)` when none applies.
    """
    candidates = (
        (url.strip() if url else None, "--url"),
        (reader.get("NEST_AUTH_URL"), "NEST_AUTH_URL"),
    )
    for raw, source in candidates:
        if raw:
            try:
                return normalize_auth_url(raw), source
            except ValueError as exc:
                reader.fail(f"{source} {exc}; got {raw!r}")
                return None, None
    if hosted:
        return HOSTED_AUTH_URL, "hosted default"
    return None, None


@dataclass(frozen=True)
class CLIAuthConfig:
    """
    Where `nest login` signs in and keeps its Nest session.

    Attributes:
        url (`str`): The origin serving Nest's sign-in, such as
            `https://nest.example.com`: the origin of `NEST_GOOGLE_REDIRECT_URL`.
            `--url`, then `NEST_AUTH_URL`, then [`config.HOSTED_AUTH_URL`].
        credentials_file (`str`): `NEST_CLI_CREDENTIALS_FILE`, where CLI sessions
            are kept, one per sign-in origin, readable only by their owner. Defaults
            to `$XDG_CONFIG_HOME/nest/credentials.json` (`~/.config/nest/...`).
        url_source (`str`): Where `url` came from, one of
            [`config.AUTH_URL_SOURCES`].
    """

    url: str
    credentials_file: str
    url_source: str = "NEST_AUTH_URL"


def default_credentials_file(environ: Mapping[str, str] | None = None) -> str:
    """Returns `$XDG_CONFIG_HOME/nest/credentials.json`, or under `~/.config`."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CONFIG_HOME", "").strip()
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "nest", "credentials.json")


def normalize_auth_url(raw: str) -> str:
    """
    Returns the origin of a Nest sign-in URL, such as `https://nest.example.com`.

    Raises:
        `ValueError`: If it is not `https://` (or `http://` for localhost), names no
            host, or carries credentials, a query or a fragment.
    """
    try:
        parts = urlsplit(raw.strip())
        port = parts.port
    except ValueError:
        raise ValueError("must be a URL, such as 'https://nest.example.com'") from None
    host = parts.hostname or ""
    if parts.scheme != "https" and not (parts.scheme == "http" and _loopback_host(host)):
        raise ValueError(
            "must be an https:// URL (http:// only for localhost), because Nest sessions "
            "are sent to it"
        )
    if not host or parts.username or parts.password:
        raise ValueError("must name a host and contain no credentials")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("must be an origin, such as 'https://nest.example.com', with no path")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{parts.scheme}://{netloc.lower()}"


def load_cli_auth_config(
    environ: Mapping[str, str] | None = None, *, url: str | None = None
) -> CLIAuthConfig:
    """
    Reads and validates the CLI sign-in settings.

    The origin is `url` when given, else `NEST_AUTH_URL`, else
    [`config.HOSTED_AUTH_URL`] (see [`config.resolve_auth_url`]).

    Args:
        environ (`Mapping[str, str]`, *optional*): Environment. Defaults to `os.environ`.
        url (`str`, *optional*): Replaces `NEST_AUTH_URL`, such as from `--url`.

    Raises:
        [`config.ConfigError`]: Listing every invalid setting.
    """
    reader = _Reader(environ)
    origin, source = resolve_auth_url(reader, url=url, hosted=True)
    path = reader.get("NEST_CLI_CREDENTIALS_FILE") or default_credentials_file(reader.environ)
    if not os.path.isabs(path):
        reader.fail(f"NEST_CLI_CREDENTIALS_FILE must be an absolute path, got {path!r}")
    if reader.problems or origin is None or source is None:
        raise ConfigError(reader.problems)
    return CLIAuthConfig(url=origin, credentials_file=path, url_source=source)


def materialize_ssh_credentials(policy: SSHPolicy) -> tuple[str | None, str | None]:
    """
    Writes inline Mode B credentials to a new private runtime directory.

    The directory is `0700` and both files are `0600` (see
    [`runtime_files.write_private_file`]); configured files are used as they are.

    Args:
        policy ([`config.SSHPolicy`]): Validated SSH policy.

    Returns:
        `tuple[str | None, str | None]`: Paths of the identity and `known_hosts` files,
            using the configured files when no inline value is given.
    """
    identity, known_hosts = policy.identity_file, policy.known_hosts_file
    if policy.private_key is None and policy.known_hosts_data is None:
        return identity, known_hosts
    directory = private_directory("nest-ssh-")
    if policy.private_key is not None:
        identity = write_private_file(directory, "id", policy.private_key.reveal())
    if policy.known_hosts_data is not None:
        known_hosts = write_private_file(
            directory, "known_hosts", normalize_content(policy.known_hosts_data)
        )
    return identity, known_hosts


CHILD_EXCLUDED_SETTINGS = (
    *SECRET_SETTINGS,
    "NEST_SSH_CONFIG",
    "NEST_SSH_KNOWN_HOSTS_DATA",
    "NEST_TLS_CERT",
    "NEST_DATABASE_SSLROOTCERT_DATA",
    "NEST_DATABASE_SSLCERT_DATA",
    "NEST_SMTP_CA",
    PLATFORMS_ENV,
)
"""Settings removed from the environment of every child process the Controller starts."""


def child_environ(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """
    Returns a copy of the environment for a child process, without Controller credentials.

    SSH, `scp`, and local worker processes need `PATH`, `HOME` and similar settings,
    but never the Controller's secrets or inline credential material, which would
    otherwise be inherited by user code or forwarded by an SSH `SendEnv` setting.

    Args:
        environ (`Mapping[str, str]`, *optional*): Environment. Defaults to `os.environ`.

    Returns:
        `dict[str, str]`: The environment without [`config.CHILD_EXCLUDED_SETTINGS`].
    """
    env = dict(os.environ if environ is None else environ)
    for name in CHILD_EXCLUDED_SETTINGS:
        env.pop(name, None)
    return env
