"""Control-plane records: Provider Profiles and Slurm job records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, LiteralString
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from .._logging import get_logger
from ..config import ProviderProfile
from ..errors import DatabaseUnavailableError
from .connection import Database, DatabaseError

logger = get_logger("nest.db.store")

JOB_STATUSES = ("submitting", "submitted", "succeeded", "failed", "unknown")
_MAX_ERROR_MESSAGE = 4000
_BEGIN_FIELDS = (
    "script",
    "workdir",
    "job_dir",
    "account",
    "partition",
    "time_limit",
    "gpu",
    "cpu",
    "project_id",
    "submitted_by_user_id",
)


def sync_providers(
    database: Database, providers: Mapping[str, ProviderProfile], default_id: str | None
) -> None:
    """
    Writes the environment's Provider Profiles to `provider_profiles`.

    The environment stays the source of truth: each profile's row is created or
    overwritten, and rows for profiles no longer configured are marked inactive
    rather than deleted, so their job records keep a provider. Runs in one
    transaction.

    Args:
        database ([`db.Database`]): Open database.
        providers (`Mapping[str, ProviderProfile]`): Validated profiles by ID.
        default_id (`str`, *optional*): The default profile's ID.

    Raises:
        [`db.DatabaseError`]: If the rows cannot be written.
    """
    try:
        with database.connection() as conn:
            conn.execute("UPDATE provider_profiles SET is_default = false WHERE is_default")
            for profile in providers.values():
                conn.execute(
                    """
                    INSERT INTO provider_profiles (id, source, ssh_mode, login_nodes, ssh_user,
                        account, partition, time_limit, workdir, is_default, active)
                    VALUES (%(id)s, 'env', %(ssh_mode)s, %(login_nodes)s, %(ssh_user)s,
                        %(account)s, %(partition)s, %(time)s, %(workdir)s, %(is_default)s, true)
                    ON CONFLICT (id) DO UPDATE SET
                        source = EXCLUDED.source, ssh_mode = EXCLUDED.ssh_mode,
                        login_nodes = EXCLUDED.login_nodes, ssh_user = EXCLUDED.ssh_user,
                        account = EXCLUDED.account, partition = EXCLUDED.partition,
                        time_limit = EXCLUDED.time_limit, workdir = EXCLUDED.workdir,
                        is_default = EXCLUDED.is_default, active = true, updated_at = now()
                    """,
                    {
                        "id": profile.id,
                        "ssh_mode": profile.ssh_mode,
                        "login_nodes": [str(node) for node in profile.login_nodes],
                        "ssh_user": profile.ssh_user,
                        "account": profile.account,
                        "partition": profile.partition,
                        "time": profile.time,
                        "workdir": profile.workdir,
                        "is_default": profile.id == default_id,
                    },
                )
            conn.execute(
                "UPDATE provider_profiles SET active = false, updated_at = now() "
                "WHERE active AND NOT (id = ANY(%s))",
                (list(providers),),
            )
    except psycopg.Error as exc:
        raise DatabaseError(
            f"Cannot record Provider Profiles in {database.describe()}: " + database.scrub(str(exc))
        ) from None
    logger.info("Provider Profiles recorded providers=%s", ",".join(providers) or "-")


class JobStore:
    """
    Records the Slurm jobs the Controller submits.

    [`db.JobStore.begin`] runs before any SSH or Slurm side effect and raises when
    the database is unavailable, so a request is never run without its record.
    Later updates happen after a side effect that cannot be undone; when they fail
    they are logged as errors and the RPC still returns its result.

    Args:
        database ([`db.Database`]): Open database.
        provider_id (`str`): Provider Profile the Controller submits to.
        platform_id (`UUID`, *optional*): The HPC Platform (Slurm namespace) the
            Provider Profile is mirrored as (see [`db.PlatformStore.sync_environment`]).
            Recorded with each new job and never changed afterwards, whichever login
            node submits it.
    """

    def __init__(self, database: Database, provider_id: str, *, platform_id: UUID | None = None):
        self.database = database
        self.provider_id = provider_id
        self.platform_id = platform_id

    def begin(
        self, kind: str, execution_id: str, *, request_id: str | None = None, **fields: Any
    ) -> int:
        """
        Creates a `submitting` record for a request.

        Args:
            kind (`str`): `"execution"` or `"batch"`.
            execution_id (`str`): The request's Execution ID, generated before any
                side effect. For executions it is also the remote directory name.
            request_id (`str`, *optional*): Correlation ID from the logs.
            **fields: Any of `script`, `workdir`, `job_dir`, `account`, `partition`,
                `time_limit`, `gpu`, `cpu`, `project_id` and `submitted_by_user_id`.
                The last two record the Run's Project and submitter; they must be
                given together and can never be changed afterwards.

        Returns:
            `int`: The record's ID.

        Raises:
            [`errors.DatabaseUnavailableError`]: If the record cannot be written.
        """
        unknown = set(fields) - set(_BEGIN_FIELDS)
        if unknown:
            raise ValueError(f"Unknown job fields: {sorted(unknown)}")
        columns = ["execution_id", "provider_id", "kind", "request_id", *fields]
        values = [execution_id, self.provider_id, kind, request_id, *fields.values()]
        if self.platform_id is not None:
            columns.append("platform_id")
            values.append(self.platform_id)
        query = sql.SQL("INSERT INTO jobs ({}) VALUES ({}) RETURNING id").format(
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.SQL(", ").join(sql.Placeholder() * len(values)),
        )
        try:
            with self.database.connection() as conn:
                row = conn.execute(query, values).fetchone()
        except (psycopg.Error, DatabaseError) as exc:
            logger.error("Job record could not be created: %s", self.database.scrub(str(exc)))
            raise DatabaseUnavailableError(
                "The Controller database is unavailable, so the request was not run. "
                "Nothing was submitted; retry later."
            ) from None
        assert row is not None
        return row[0]

    def _update(self, query: LiteralString, params: tuple, *, what: str) -> None:
        try:
            with self.database.connection() as conn:
                conn.execute(query, params)
        except (psycopg.Error, DatabaseError) as exc:
            logger.error(
                "Job record update failed what=%s: %s", what, self.database.scrub(str(exc))
            )

    def submitted(self, record_id: int, slurm_job_id: int, job_dir: str | None = None) -> None:
        """Stores the Slurm job ID once `sbatch` returns it."""
        self._update(
            "UPDATE jobs SET status = 'submitted', slurm_job_id = %s, job_dir = %s, "
            "updated_at = now() WHERE id = %s",
            (slurm_job_id, job_dir, record_id),
            what="submitted",
        )

    def finish(
        self,
        record_id: int,
        status: str,
        *,
        slurm_job_id: int | None = None,
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> None:
        """
        Marks a record `succeeded`, `failed`, or `unknown`.

        Args:
            record_id (`int`): ID from [`db.JobStore.begin`].
            status (`str`): Final status.
            slurm_job_id (`int`, *optional*): Job ID, if learned only now.
            error_kind (`str`, *optional*): [`errors.InfrastructureError`] kind.
            error_message (`str`, *optional*): Error text, truncated to 4000 characters.
        """
        if status not in JOB_STATUSES:
            raise ValueError(f"Unknown job status {status!r}")
        self._update(
            "UPDATE jobs SET status = %s, slurm_job_id = COALESCE(%s, slurm_job_id), "
            "error_kind = %s, error_message = %s, updated_at = now(), finished_at = now() "
            "WHERE id = %s",
            (
                status,
                slurm_job_id,
                error_kind,
                error_message[:_MAX_ERROR_MESSAGE] if error_message else None,
                record_id,
            ),
            what=status,
        )

    def observe(self, slurm_job_id: int, state: str) -> None:
        """Stores the latest Slurm state seen for a job of this provider."""
        self._update(
            "UPDATE jobs SET slurm_state = %s, updated_at = now() WHERE id = ("
            "SELECT id FROM jobs WHERE provider_id = %s AND slurm_job_id = %s "
            "ORDER BY id DESC LIMIT 1)",
            (state, self.provider_id, slurm_job_id),
            what="state",
        )

    def get(self, record_id: int) -> dict[str, Any] | None:
        """Returns a record as a dictionary, or `None`."""
        with self.database.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
            return cursor.execute("SELECT * FROM jobs WHERE id = %s", (record_id,)).fetchone()

    def by_execution_id(self, execution_id: str) -> dict[str, Any] | None:
        """Returns the record for an Execution ID, or `None`."""
        with self.database.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
            return cursor.execute(
                "SELECT * FROM jobs WHERE execution_id = %s", (execution_id,)
            ).fetchone()

    def find(self, slurm_job_id: int) -> dict[str, Any] | None:
        """Returns this provider's latest record for a Slurm job ID, or `None`."""
        with self.database.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
            return cursor.execute(
                "SELECT * FROM jobs WHERE provider_id = %s AND slurm_job_id = %s "
                "ORDER BY id DESC LIMIT 1",
                (self.provider_id, slurm_job_id),
            ).fetchone()
