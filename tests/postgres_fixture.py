"""Throwaway databases on a real PostgreSQL server for tests.

Set `NEST_TEST_DATABASE_URL` to a server URL whose user may create databases, such
as `postgresql://postgres:postgres@127.0.0.1:5432/postgres`. Each test gets its own
UTF-8 database, dropped afterwards. Without the URL, PostgreSQL tests are skipped,
unless `NEST_TEST_DATABASE_REQUIRED=1` (set in CI), which turns the skip into a
failure so the suite never passes without exercising PostgreSQL.
"""

import os
import unittest
import uuid
from collections.abc import Generator
from contextlib import contextmanager

from psycopg.conninfo import conninfo_to_dict, make_conninfo

ADMIN_URL = os.environ.get("NEST_TEST_DATABASE_URL", "").strip()
REQUIRED = os.environ.get("NEST_TEST_DATABASE_REQUIRED", "").strip() == "1"

# A second, TLS-enabled server whose certificate is valid only for DNS:localhost,
# signed by TLS_CA_FILE (see tests/make_postgres_tls_certs.sh). TLS_URL must use the
# host name `localhost`. TLS_OTHER_CA_FILE is an unrelated CA.
TLS_URL = os.environ.get("NEST_TEST_TLS_DATABASE_URL", "").strip()
TLS_CA_FILE = os.environ.get("NEST_TEST_TLS_CA_FILE", "").strip()
TLS_OTHER_CA_FILE = os.environ.get("NEST_TEST_TLS_OTHER_CA_FILE", "").strip()

# Directory holding pg_dump and pg_restore at least as new as the server; PATH when unset.
PG_BIN = os.environ.get("NEST_TEST_PG_BIN", "").strip()


class PostgresTestCase(unittest.TestCase):
    """Skips, or fails in CI, when no test PostgreSQL server is configured."""

    def setUp(self):
        super().setUp()
        if not ADMIN_URL:
            if REQUIRED:
                self.fail("NEST_TEST_DATABASE_REQUIRED=1 but NEST_TEST_DATABASE_URL is unset")
            self.skipTest("set NEST_TEST_DATABASE_URL to run PostgreSQL tests")

    def database(self) -> str:
        """Creates an empty database for this test and returns its URL."""
        context = temporary_database()
        url = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return url


class PostgresTLSTestCase(unittest.TestCase):
    """Skips, or fails in CI, when no TLS-enabled test PostgreSQL server is configured."""

    def setUp(self):
        super().setUp()
        if not (TLS_URL and TLS_CA_FILE and TLS_OTHER_CA_FILE):
            message = (
                "set NEST_TEST_TLS_DATABASE_URL, NEST_TEST_TLS_CA_FILE and "
                "NEST_TEST_TLS_OTHER_CA_FILE to run the PostgreSQL TLS tests"
            )
            if REQUIRED:
                self.fail("NEST_TEST_DATABASE_REQUIRED=1 but " + message)
            self.skipTest(message)


def database_url(name: str) -> str:
    """Returns the admin URL pointed at another database, as a URL."""
    params = conninfo_to_dict(ADMIN_URL)
    user = params.get("user", "")
    password = params.get("password")
    host = params.get("host", "127.0.0.1")
    port = params.get("port", 5432)
    auth = f"{user}:{password}@" if password else f"{user}@" if user else ""
    return f"postgresql://{auth}{host}:{port}/{name}"


@contextmanager
def temporary_database() -> Generator[str]:
    import psycopg
    from psycopg import sql

    name = f"nest_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(
                sql.Identifier(name)
            )
        )
    try:
        yield database_url(name)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (name,),
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def connect(url: str):
    import psycopg

    return psycopg.connect(make_conninfo(url, client_encoding="UTF8"), autocommit=True)
