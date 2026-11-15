"""PostgreSQL connections and the Controller's connection pool."""

from __future__ import annotations

import ipaddress
import logging
import math
import re
from collections.abc import Generator
from contextlib import contextmanager
from typing import Self
from urllib.parse import parse_qsl, urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool, PoolTimeout

from .._logging import get_logger, register_secret
from ..config import DatabaseConfig

logger = get_logger("nest.db")

UNENCRYPTED_SSLMODES = (None, "disable", "allow", "prefer")
"""`sslmode` values that do not guarantee an encrypted, verified connection."""


class DatabaseError(RuntimeError):
    """
    The database is unreachable, misconfigured, or has the wrong schema.

    Messages are actionable and never contain the database password or any value
    from the URL's query string.
    """


_QUOTED = re.compile(r"\"([^\"]*)\"|'([^']*)'")


class _ScrubFilter(logging.Filter):
    """Applies a scrubber to records from psycopg's pool, which logs libpq errors."""

    def __init__(self, scrub):
        super().__init__()
        self.scrub = scrub

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.scrub(record.getMessage())
        record.args = None
        return True


def _is_loopback(host: str | None) -> bool:
    if not host or host.startswith("/") or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Database:
    """
    Connects to the externally managed PostgreSQL database named by a
    [`config.DatabaseConfig`].

    Nest never creates the server or the database. [`db.Database.open`] fails with
    [`db.DatabaseError`] when the database cannot be reached; callers must not fall
    back to another store.

    Args:
        config ([`config.DatabaseConfig`]): Validated database settings.
        application_name (`str`, *optional*): Name shown in `pg_stat_activity`.
    """

    def __init__(self, config: DatabaseConfig, *, application_name: str = "nest-controller"):
        self.config = config
        self.application_name = application_name
        self._pool: ConnectionPool | None = None
        self._password: str | None = None
        if config.password_file:
            with open(config.password_file) as handle:
                self._password = handle.read().strip()
        parts = urlsplit(config.url.reveal())
        self._secrets = [
            value
            for value in (self._password, parts.password, *(v for _, v in parse_qsl(parts.query)))
            if value
        ]
        # Quoted strings libpq may echo that are not secret: query keys, the host
        # and port, and file paths from the NEST_DATABASE_* variables.
        self._safe = {key for key, _ in parse_qsl(parts.query)} | {
            str(value)
            for value in (
                parts.hostname,
                parts.port,
                config.sslrootcert,
                config.sslcert,
                config.sslkey,
                config.password_file,
            )
            if value
        }
        for secret in (self._password, parts.password):
            if secret:
                register_secret(secret)
        self._pool_filter = _ScrubFilter(self.scrub)

    def conninfo(self) -> str:
        """
        Returns the libpq connection string, including TLS and timeout settings.

        The result can contain the password; never log it.
        """
        settings: dict[str, object] = {
            "application_name": self.application_name,
            "connect_timeout": max(1, math.ceil(self.config.connect_timeout)),
            # Text columns always decode as str, even on a SQL_ASCII server.
            "client_encoding": "UTF8",
        }
        for key in ("sslmode", "sslrootcert", "sslcert", "sslkey"):
            value = getattr(self.config, key)
            if value is not None:
                settings[key] = value
        if self._password is not None:
            settings["password"] = self._password
        return make_conninfo(self.config.url.reveal(), **settings)

    def describe(self) -> str:
        """Returns the redacted URL, for messages and logs."""
        return self.config.redacted_url

    def scrub(self, text: str) -> str:
        """
        Removes the password and URL query values from a driver or server message.

        libpq and PostgreSQL quote the values they reject, so every quoted string is
        masked unless it is a query key, the host, the port, or a configured file
        path. The password and each query value are also masked wherever they appear
        as a whole word.
        """
        text = " ".join(text.split())

        def quoted(match: re.Match) -> str:
            value = match[1] if match[1] is not None else match[2]
            return match[0] if value in self._safe else '"***"'

        text = _QUOTED.sub(quoted, text)
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = re.sub(rf"(?<![\w-]){re.escape(secret)}(?![\w-])", "***", text)
        return text

    def _error(self, action: str, exc: BaseException) -> DatabaseError:
        detail = self.scrub(str(exc)) or type(exc).__name__
        return DatabaseError(
            f"Cannot {action} PostgreSQL at {self.describe()}: {detail}. Check NEST_DATABASE_URL, "
            "the database's network access and credentials, and the NEST_DATABASE_SSL* settings"
        )

    def connect(self, *, autocommit: bool = True) -> psycopg.Connection:
        """
        Opens one connection outside the pool, as `nest-db` does.

        Args:
            autocommit (`bool`, *optional*, defaults to `True`): Connection mode.
                Use explicit `connection.transaction()` blocks for atomic work.

        Raises:
            [`db.DatabaseError`]: If the connection fails.
        """
        try:
            return psycopg.connect(self.conninfo(), autocommit=autocommit)
        except psycopg.Error as exc:
            raise self._error("connect to", exc) from None

    def warn_if_unencrypted(self) -> None:
        """Logs a warning when a remote database connection may be unencrypted."""
        host = conninfo_to_dict(self.config.url.reveal()).get("host")
        if self.config.effective_sslmode in UNENCRYPTED_SSLMODES and not _is_loopback(
            str(host) if host else None
        ):
            logger.warning(
                "Database connection to %s may be unencrypted or unverified sslmode=%s; "
                "set NEST_DATABASE_SSLMODE=verify-full and NEST_DATABASE_SSLROOTCERT",
                self.describe(),
                self.config.effective_sslmode or "prefer",
            )

    def open(self) -> None:
        """
        Checks connectivity, then opens the connection pool.

        A direct connection is made first so a failure reports the server's own
        reason (refused, authentication failed, TLS required) rather than a pool
        timeout.

        Raises:
            [`db.DatabaseError`]: If the database is unreachable.
        """
        if self._pool is not None:
            return
        with self.connect() as probe:
            probe.execute("SELECT 1")
        self.warn_if_unencrypted()
        pool_logger = logging.getLogger("psycopg.pool")
        if self._pool_filter not in pool_logger.filters:
            pool_logger.addFilter(self._pool_filter)
        pool = ConnectionPool(
            self.conninfo(),
            min_size=self.config.pool_min_size,
            max_size=self.config.pool_max_size,
            timeout=self.config.pool_timeout,
            check=ConnectionPool.check_connection,
            name="nest",
            open=False,
        )
        try:
            pool.open(wait=True, timeout=self.config.connect_timeout)
        except PoolTimeout as exc:
            pool.close()
            raise self._error("open a connection pool to", exc) from None
        self._pool = pool
        logger.info(
            "Database connected url=%s sslmode=%s pool_min=%s pool_max=%s",
            self.describe(),
            self.config.effective_sslmode or "prefer",
            self.config.pool_min_size,
            self.config.pool_max_size,
        )

    def close(self) -> None:
        """Closes the pool and all its connections. Safe to call more than once."""
        pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()
            logging.getLogger("psycopg.pool").removeFilter(self._pool_filter)
            logger.info("Database pool closed")

    @contextmanager
    def connection(self) -> Generator[psycopg.Connection]:
        """
        Borrows a pooled connection for one transaction.

        The transaction commits when the block exits normally and rolls back when
        it raises.

        Raises:
            [`db.DatabaseError`]: If the pool is closed or no connection is free
                within `NEST_DATABASE_POOL_TIMEOUT`.
        """
        if self._pool is None:
            raise DatabaseError("The database pool is not open")
        try:
            with self._pool.connection() as conn:
                yield conn
        except PoolTimeout as exc:
            raise self._error("get a pooled connection to", exc) from None

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
