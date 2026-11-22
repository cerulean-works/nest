"""The single PostgreSQL migration chain, reconciled with D07 and D08 (Patch A05).

The per-migration suites (`test_platforms`, `test_slurm_accounts`,
`test_executions`, `test_project_platforms`) test each schema on a fresh install
and one upgrade step. These tests cover the chain as a whole:

- every released migration is pinned by checksum, in one version per file;
- a database populated at each release from 0011 upgrades, step by step, to the
  same schema a fresh install creates, keeping every Platform, login node,
  Platform-scoped account, Project link and job or execution record;
- the upgraded database still rejects cross-Platform account references and keeps
  identical account names on two Platforms apart, and keeps a disabled User's
  platform role without making it effective or granting a new one (0016);
- a failing migration after the chain is rolled back without touching D07/D08
  records;
- a restarted pool sees the same records, and a Controller refuses to start on a
  database one migration behind instead of running without it.

PostgreSQL tests run against the server named by `NEST_TEST_DATABASE_URL` (see
`postgres_fixture`).
"""

import secrets
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any, LiteralString

import psycopg
from postgres_fixture import PostgresTestCase, connect
from test_controller import controller_environment

from nest.config import LoginNode, ProviderProfile, load_database_config
from nest.db import (
    Database,
    ExecutionRegistry,
    HPCPrincipal,
    IdentityStore,
    JobStore,
    Migration,
    MigrationError,
    PlatformStore,
    ProjectStore,
    SlurmAccountStore,
    UserNotActiveError,
    load_migrations,
    schema_status,
    sync_providers,
    upgrade,
    verify,
)
from nest.db.platforms import endpoint_for

OPS: dict[str, Any] = {"operator": "ops-chain", "reason": "migration chain tests"}
PIN = "SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
ALICE = HPCPrincipal("alice", 1001, binding_id="binding-1", binding_version=1)

# SHA-256 of every released migration. A released migration is immutable: never
# edit, rename or renumber one (see "Migrations" in docs/source/database.md). A new
# migration is appended with the next version and its checksum added here.
RELEASED = {
    "0001_providers_and_jobs": "c3cb3a153cce43656ffa88a4095625a46be023372710de8f3665b27474b11b15",
    "0002_email_outbox": "01165aac7a2defbf6c74efcaa4888494a16516f8ab15d5283fd908c839e2cf4f",
    "0003_identity_and_projects": "47ca6e97cbc0be194f2e504bba243767c110cbb4cfc7db01631e535011c86063",
    "0004_oidc_login_flows": "95f73da2ddadb2c655c9443c252b7fd4ca55db7ffd5ae4be7392412b51c1803e",
    "0005_platform_admin_bootstrap": "c1651aa829d0198a69c1f4e87cd789854c558b516f902dbca24134709d530fec",
    "0006_browser_session_rotation": "3b5263569f3cb4cdbc07aa70e3dbb0da3cfbb5b91ac66cb86926b2e646c44cf0",
    "0007_cli_login_requests": "72436044a4d010ab940846b43000a429305c451fa194c32e8f7933275fc37e50",
    "0008_project_audit": "91a716aa3fc5aa4181c62b924a4d833afe7e9806f9cc4be988ed7c7e5f8249d6",
    "0009_security_audit": "036da8f0c4b6ae23b34bb2d07b256e02e2e87e2f1eb9e624de9f0e18c4eb881c",
    "0010_invitation_onboarding": "45a15527c44fd0a14c0f0a0100db378eb5304cbe684f1a91943f19ae1fc9963b",
    "0011_platform_metadata": "8129f50cf4e612c45fced10844746f100112ced127d54931d74e24e646e49fa5",
    "0012_hpc_platform_catalog": "e1ba254f630b571324271c6519f7ce2f8737e415bbc0c0fafd48fb57e4ca0cec",
    "0013_slurm_account_registry": "45a900fc4b847da9d53923624ff1e1ed6e9b0c1f5d8526e7938be5fabe12fb55",
    "0014_execution_registry": "c8e8536289f3a6e967ef343992c0f3cc45793a834c48bf571d883ca1844aa2d2",
    "0015_project_platform_policy": "db74337d18d5b84c2293e3f52d4cde959861af2966cdaddeeb921a2626c48324",
    "0016_platform_role_boundaries": "4d670bc13c35c6e2ae4b785744bc87c93bb18d6ad3676255b849483a69ec0c2e",
}  # fmt: skip

# The Platform and account schema and the migration that owns each table.
OWNERS = {
    "hpc_platforms": "0012_hpc_platform_catalog",  # D07
    "hpc_login_nodes": "0012_hpc_platform_catalog",  # D07
    "hpc_platform_project_grants": "0012_hpc_platform_catalog",  # D07
    "slurm_accounts": "0013_slurm_account_registry",  # D08
    "project_platform_accounts": "0013_slurm_account_registry",  # D08
    "project_platform_account_policies": "0013_slurm_account_registry",  # D08
    "executions": "0014_execution_registry",  # C01
    "execution_attempts": "0014_execution_registry",  # C01
    "project_default_platforms": "0015_project_platform_policy",  # Patch B06
}

# A catalog of the schema objects in `public`: what two databases must share to have
# the same schema, independent of the data in them.
_FINGERPRINT: dict[str, LiteralString] = {
    "columns": """
        SELECT table_name, column_name, data_type, udt_name, is_nullable,
               column_default, is_identity, identity_generation
        FROM information_schema.columns WHERE table_schema = 'public'""",
    "constraints": """
        SELECT conrelid::regclass::text, conname, contype, pg_get_constraintdef(oid),
               condeferrable, condeferred
        FROM pg_constraint WHERE connamespace = 'public'::regnamespace""",
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'",
    "triggers": """
        SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid)
        FROM pg_trigger WHERE NOT tgisinternal
          AND tgrelid IN (SELECT oid FROM pg_class WHERE relnamespace = 'public'::regnamespace)""",
    "functions": """
        SELECT proname, pg_get_function_identity_arguments(oid), md5(prosrc)
        FROM pg_proc WHERE pronamespace = 'public'::regnamespace""",
    "sequences": """
        SELECT sequencename, data_type::text, increment_by, min_value, max_value
        FROM pg_sequences WHERE schemaname = 'public'""",
}


def fingerprint(conn: psycopg.Connection) -> dict[str, list[tuple]]:
    return {name: sorted(conn.execute(query).fetchall()) for name, query in _FINGERPRINT.items()}


def index(label: str) -> int:
    """The position of a released migration in the packaged chain."""
    return [m.label for m in load_migrations()].index(label)


class ChainTests(unittest.TestCase):
    """The packaged chain itself; needs no server."""

    def test_released_migrations_are_unchanged_and_in_order(self):
        migrations = load_migrations()
        released = migrations[: len(RELEASED)]
        self.assertEqual([m.label for m in released], list(RELEASED))
        for migration in released:
            with self.subTest(migration=migration.label):
                self.assertEqual(
                    migration.checksum,
                    RELEASED[migration.label],
                    "a released migration was edited; restore it and add a new migration",
                )
        # Later migrations are only ever appended, each with a new version.
        self.assertEqual([m.version for m in migrations], list(range(1, len(migrations) + 1)))

    def test_platform_and_account_schema_is_owned_in_this_order(self):
        catalog, accounts = index("0012_hpc_platform_catalog"), index("0013_slurm_account_registry")
        self.assertEqual(
            [m.label for m in load_migrations()[catalog - 1 : catalog + 5]],
            [
                "0011_platform_metadata",
                "0012_hpc_platform_catalog",
                "0013_slurm_account_registry",
                "0014_execution_registry",
                "0015_project_platform_policy",
                "0016_platform_role_boundaries",
            ],
        )
        self.assertEqual(accounts, catalog + 1)
        by_label = {m.label: m.sql for m in load_migrations()}
        for table, owner in OWNERS.items():
            creators = [
                label for label, text in by_label.items() if f"CREATE TABLE {table} (" in text
            ]
            with self.subTest(table=table):
                self.assertEqual(creators, [owner])

    def test_one_file_per_version(self):
        for names in (["0001_a.sql", "0001_b.sql"], ["0001_a.sql", "0002_b.sql", "0002_c.sql"]):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as directory:
                for name in names:
                    Path(directory, name).write_text("SELECT 1;")
                with self.assertRaisesRegex(MigrationError, "without gaps or duplicates"):
                    load_migrations(Path(directory))


class _ChainBase(PostgresTestCase):
    def open(self, url: str) -> Database:
        db = Database(load_database_config({"NEST_DATABASE_URL": url}))
        db.open()
        self.addCleanup(db.close)
        return db

    def upgrade_to(self, url: str, label: str) -> list[str]:
        """Applies the chain up to and including `label`; returns what was applied."""
        with connect(url) as conn:
            return [m.label for m in upgrade(conn, load_migrations()[: index(label) + 1])]

    def populate(self, url: str) -> dict[str, Any]:
        """
        Upgrades an empty database release by release from 0011, writing the records
        each release supports before applying the next, as a deployment would.
        """
        self.upgrade_to(url, "0011_platform_metadata")
        db = self.open(url)
        r: dict[str, Any] = {}
        # 0011: a Project, environment Platform profiles and a legacy job record.
        r["owner"] = IdentityStore(db).create_user(display_name="owner").id
        projects = ProjectStore(db)
        r["lab"] = projects.create_project("lab", created_by_user_id=r["owner"]).id
        r["other"] = projects.create_project("lab", created_by_user_id=r["owner"]).id
        profile = ProviderProfile(
            "nano4", "A", (LoginNode("ln01"),), account="chem", operator="nchc"
        )
        sync_providers(db, {"nano4": profile}, "nano4")
        r["legacy_job"] = JobStore(db, "nano4").begin(
            "batch", secrets.token_hex(16), account="chem"
        )

        # 0012 (D07): immutable Platforms, login nodes and Project grants.
        self.assertEqual(
            self.upgrade_to(url, "0012_hpc_platform_catalog"), ["0012_hpc_platform_catalog"]
        )
        platforms = PlatformStore(db)
        alpha = platforms.create_platform("alpha", "Alpha", operator_name="Example", **OPS)
        beta = platforms.create_platform("beta", "Beta", operator_name="Example", **OPS)
        r["alpha"], r["beta"] = alpha, beta
        r["alpha_node"] = platforms.add_login_node(
            alpha.id, "login.alpha.example", transport_modes=["A"], **OPS
        )
        r["beta_node"] = platforms.add_login_node(
            beta.id, "login.beta.example", transport_modes=["B"], host_key_pin_ref=PIN, **OPS
        )
        for platform in (alpha, beta):
            platforms.grant_project(platform.id, r["lab"], **OPS)
        platforms.grant_project(alpha.id, r["other"], **OPS)

        # 0013 (D08): the same account name on both Platforms, linked per Platform.
        self.assertEqual(
            self.upgrade_to(url, "0013_slurm_account_registry"), ["0013_slurm_account_registry"]
        )
        accounts = SlurmAccountStore(db)
        r["alpha_chem"] = accounts.register_account(alpha.id, "chem", **OPS)
        r["beta_chem"] = accounts.register_account(beta.id, "chem", **OPS)
        accounts.link_project(r["lab"], alpha.id, "chem", modes=["A"], default=True, **OPS)
        accounts.link_project(r["lab"], beta.id, "chem", modes=["B"], **OPS)
        accounts.link_project(r["other"], alpha.id, "chem", modes=["A"], **OPS)
        r["job"] = JobStore(db, "nano4", platform_id=alpha.id).begin(
            "batch",
            secrets.token_hex(16),
            account="chem",
            slurm_account_id=r["alpha_chem"].id,
            slurm_account_origin="project_default",
        )

        # 0014 (C01): an execution attempt charged to beta's "chem".
        self.assertEqual(
            self.upgrade_to(url, "0014_execution_registry"), ["0014_execution_registry"]
        )
        registry = ExecutionRegistry(db)
        execution, _ = registry.register(r["lab"], r["owner"], "batch", {})
        r["execution"] = execution.execution_id
        r["attempt"] = registry.begin_attempt(
            execution.execution_id,
            endpoint=endpoint_for(beta, r["beta_node"], "B"),
            principal=ALICE,
            account="chem",
            slurm_account_id=r["beta_chem"].id,
            slurm_account_origin="explicit",
            options={},
        )

        # 0015 (Patch B06): a default Platform among the Project's grants.
        self.assertEqual(
            self.upgrade_to(url, "0015_project_platform_policy"), ["0015_project_platform_policy"]
        )
        with connect(url) as conn:
            conn.execute(
                "INSERT INTO project_default_platforms (project_id, platform_id, set_by_user_id) "
                "VALUES (%s, %s, %s)",
                (r["lab"], beta.id, r["owner"]),
            )

        # 0016 (Patch B01): a platform_admin granted before the User was disabled is
        # kept by the upgrade, but is not effective while the User is disabled.
        identity = IdentityStore(db)
        r["admin"] = identity.create_user(display_name="former admin").id
        identity.grant_platform_role(r["admin"], operator="ops-chain", reason="chain tests")
        identity.set_user_status(r["admin"], "disabled")
        self.assertEqual(
            self.upgrade_to(url, "0016_platform_role_boundaries"),
            ["0016_platform_role_boundaries"],
        )
        with connect(url) as conn:
            # Whatever follows 0016 on this branch applies on top of the records.
            upgrade(conn)
            verify(conn)
        r["db"], r["url"] = db, url
        return r


class UpgradeChainTests(_ChainBase):
    def test_populated_stepwise_upgrade_matches_a_fresh_install(self):
        upgraded = self.database()
        r = self.populate(upgraded)
        fresh = self.database()
        with connect(fresh) as conn:
            upgrade(conn)
            expected = fingerprint(conn)
        with connect(upgraded) as conn:
            self.assertEqual(fingerprint(conn), expected)
            self.assertEqual(
                conn.execute(
                    "SELECT version, name, checksum FROM schema_migrations ORDER BY 1"
                ).fetchall(),
                [(m.version, m.name, m.checksum) for m in load_migrations()],
            )
        self.assertTrue(set(OWNERS) <= {row[0] for row in expected["columns"]})
        self.assert_records(r)

    def assert_records(self, r: dict[str, Any]) -> None:
        db = r["db"]
        platforms, accounts = PlatformStore(db), SlurmAccountStore(db)
        for platform in (r["alpha"], r["beta"]):
            found = platforms.get_platform(platform.slug)
            assert found is not None
            self.assertEqual((found.id, found.slug), (platform.id, platform.slug))
        self.assertEqual(platforms.get_login_node(r["alpha_node"].id), r["alpha_node"])
        # One account name, two Platform-scoped accounts, each linked only on its Platform.
        self.assertNotEqual(r["alpha_chem"].id, r["beta_chem"].id)
        self.assertEqual(accounts.get_account(r["alpha"].id, "chem"), r["alpha_chem"])
        self.assertEqual(accounts.get_account(r["beta"].id, "chem"), r["beta_chem"])
        for platform, account in ((r["alpha"], r["alpha_chem"]), (r["beta"], r["beta_chem"])):
            links = accounts.mapping(r["lab"], platform.id).links
            self.assertEqual([link.account.id for link in links], [account.id])
        self.assertEqual(accounts.mapping(r["other"], r["beta"].id).links, ())
        # Two Projects with one name and one shared account keep their own IDs.
        self.assertNotEqual(r["lab"], r["other"])
        with connect(r["url"]) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT provider_id, platform_id, account, slurm_account_id FROM jobs "
                    "WHERE id = ANY(%s) ORDER BY id",
                    ([r["legacy_job"], r["job"]],),
                ).fetchall(),
                [
                    ("nano4", None, "chem", None),
                    ("nano4", r["alpha"].id, "chem", r["alpha_chem"].id),
                ],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT platform_id, slurm_account_id FROM execution_attempts WHERE id = %s",
                    (r["attempt"].id,),
                ).fetchone(),
                (r["beta"].id, r["beta_chem"].id),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT platform_id FROM project_default_platforms WHERE project_id = %s",
                    (r["lab"],),
                ).fetchone(),
                (r["beta"].id,),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT role FROM platform_role_assignments WHERE user_id = %s",
                    (r["admin"],),
                ).fetchall(),
                [("platform_admin",)],
            )
        self.assertEqual(IdentityStore(db).platform_roles(r["admin"]), set())

    def test_upgraded_database_refuses_platform_roles_for_disabled_users(self):
        url = self.database()
        r = self.populate(url)
        identity = IdentityStore(r["db"])
        disabled = identity.create_user(display_name="disabled").id
        identity.set_user_status(disabled, "disabled")
        with self.assertRaises(UserNotActiveError):
            identity.grant_platform_role(disabled, operator="ops-chain", reason="chain tests")
        with connect(url) as conn:
            # The 0016 trigger refuses a grant that bypasses the store, too.
            with self.assertRaises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO platform_role_assignments (user_id, role) "
                    "VALUES (%s, 'platform_admin')",
                    (disabled,),
                )
            conn.rollback()
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM platform_role_assignments WHERE user_id = %s",
                    (disabled,),
                ).fetchone(),
                (0,),
            )
        # Re-enabling restores exactly the assignment granted before the upgrade.
        identity.set_user_status(r["admin"], "active")
        self.assertEqual(identity.platform_roles(r["admin"]), {"platform_admin"})

    def test_upgraded_database_rejects_cross_platform_accounts(self):
        url = self.database()
        r = self.populate(url)
        alpha, beta = r["alpha"].id, r["beta"].id
        link = (
            "INSERT INTO project_platform_accounts (project_id, platform_id, "
            "slurm_account_id, modes) VALUES (%s, %s, %s, '{A,B}')"
        )
        job = (
            "INSERT INTO jobs (execution_id, provider_id, kind, platform_id, account, "
            "slurm_account_id, slurm_account_origin) "
            "VALUES (%s, 'nano4', 'batch', %s, 'chem', %s, 'explicit')"
        )
        refused = [
            # A Project link on alpha that names beta's "chem".
            (link, (r["other"], alpha, r["beta_chem"].id)),
            # A link on a Platform the Project was never granted.
            (link, (r["other"], beta, r["beta_chem"].id)),
            # A job on alpha charged to beta's "chem".
            (job, (secrets.token_hex(16), alpha, r["beta_chem"].id)),
            # A default Platform the Project was never granted.
            (
                "INSERT INTO project_default_platforms (project_id, platform_id) VALUES (%s, %s)",
                (r["other"], beta),
            ),
        ]
        with connect(url) as conn:
            conn.execute("SELECT set_config('nest.audit_operator', 'psql-ops', false)")
            for statement, params in refused:
                with (
                    self.subTest(statement=statement),
                    self.assertRaises(psycopg.errors.ForeignKeyViolation),
                ):
                    conn.execute(statement, params)
            # Platform-scoped identities never move to another Platform or name.
            for statement in (
                f"UPDATE slurm_accounts SET platform_id = '{beta}' WHERE id = {r['alpha_chem'].id}",
                f"UPDATE hpc_platforms SET id = gen_random_uuid() WHERE id = '{alpha}'",
                f"UPDATE hpc_login_nodes SET platform_id = '{beta}' WHERE id = {r['alpha_node'].id}",
                f"UPDATE projects SET id = gen_random_uuid() WHERE id = '{r['lab']}'",
            ):
                with (
                    self.subTest(statement=statement),
                    self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
                ):
                    conn.execute(statement)
            # The same name on another Platform is allowed; twice on one is not.
            with self.assertRaises(psycopg.errors.UniqueViolation):
                conn.execute(
                    "INSERT INTO slurm_accounts (platform_id, account_name) VALUES (%s, 'chem')",
                    (alpha,),
                )

    def test_failed_migration_after_the_chain_rolls_back_and_keeps_records(self):
        url = self.database()
        r = self.populate(url)
        chain = load_migrations()
        broken_sql = (
            "DELETE FROM project_platform_accounts;\n"
            "ALTER TABLE slurm_accounts ADD COLUMN note text;\n"
            "CREATE TABLE half_applied (id int);\n"
            "SELECT 1 / 0;\n"
        )
        broken = Migration(len(chain) + 1, "broken_step", broken_sql, "0" * 64)
        with connect(url) as conn:
            before = fingerprint(conn)
            with self.assertRaisesRegex(
                MigrationError, f"{broken.label} failed and was rolled back"
            ):
                upgrade(conn, [*chain, broken])
            self.assertEqual(fingerprint(conn), before)
            status = schema_status(conn, [*chain, broken])
            self.assertEqual(status.current_version, len(chain))
            self.assertEqual([m.label for m in status.pending], [broken.label])
            # The released chain is still current, so the Controller would start.
            verify(conn)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'"
                ).fetchone(),
                (0,),
            )
        self.assert_links(r)
        # A corrected migration then applies normally.
        fixed = Migration(
            broken.version, "fixed_step", "CREATE TABLE fixed_step (id int);", "f" * 64
        )
        with connect(url) as conn:
            self.assertEqual([m.label for m in upgrade(conn, [*chain, fixed])], [fixed.label])
        self.assert_links(r)

    def assert_links(self, r: dict[str, Any]) -> None:
        accounts = SlurmAccountStore(r["db"])
        self.assertEqual(len(accounts.mapping(r["lab"], r["alpha"].id).links), 1)
        self.assertEqual(len(accounts.mapping(r["lab"], r["beta"].id).links), 1)


class RestartTests(_ChainBase):
    def test_a_restarted_pool_sees_the_same_records(self):
        url = self.database()
        r = self.populate(url)
        r["db"].close()
        db = self.open(url)
        accounts = SlurmAccountStore(db)
        self.assertEqual(accounts.get_account(r["alpha"].id, "chem"), r["alpha_chem"])
        self.assertEqual(accounts.get_account(r["beta"].id, "chem"), r["beta_chem"])
        attempts = ExecutionRegistry(db).live_attempts()
        self.assertEqual(
            [(a.id, a.platform_id, a.slurm_account_id) for a in attempts],
            [(r["attempt"].id, r["beta"].id, r["beta_chem"].id)],
        )

    def test_controller_refuses_a_database_one_migration_behind(self):
        url = self.database()
        chain = load_migrations()
        with connect(url) as conn:
            upgrade(conn, chain[:-1])
            conn.execute(
                "SELECT set_config('nest.audit_operator', 'psql-ops', false);"
                "INSERT INTO hpc_platforms (slug, display_name) VALUES ('alpha', 'Alpha')"
            )
            before = conn.execute("SELECT id, slug FROM hpc_platforms").fetchall()
        result = subprocess.run(
            [sys.executable, "-m", "nest.controller"],
            env=controller_environment(NEST_CONTROLLER_BIND="127.0.0.1:0", NEST_DATABASE_URL=url),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(
            f"at version {len(chain) - 1} but this Nest needs {len(chain)}", result.stderr
        )
        self.assertIn("run `nest-db upgrade`", result.stderr)
        with connect(url) as conn:
            # Nothing was migrated or written by the refused start.
            self.assertEqual(schema_status(conn).current_version, len(chain) - 1)
            self.assertEqual(conn.execute("SELECT id, slug FROM hpc_platforms").fetchall(), before)
        # `nest-db upgrade` then brings it to the current version with its records.
        upgraded = subprocess.run(
            [sys.executable, "-m", "nest.db.cli", "upgrade"],
            env=controller_environment(NEST_DATABASE_URL=url),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
        with connect(url) as conn:
            verify(conn)
            self.assertEqual(conn.execute("SELECT id, slug FROM hpc_platforms").fetchall(), before)
        self.assertIsInstance(before[0][0], uuid.UUID)
