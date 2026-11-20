"""HPC Platform Catalog and Login-Node Registry (D07).

`SelectionTests` check login-node ordering and endpoint construction without a
database. The other tests run against a real PostgreSQL (see `postgres_fixture`):
schema constraints and triggers, the operator-side `PlatformStore`, the Controller's
environment sync, the server-side `PlatformCatalog` (discovery, default
resolution, endpoint selection and failover), the Platforms API over real HTTP,
and the `nest-db platform` and `nest platforms` commands.
"""

import json
import os
import secrets
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

import psycopg
from fake_oidc import CLIENT_ID, CLIENT_SECRET, FakeGoogle
from postgres_fixture import PostgresTestCase, connect
from test_google_auth import free_port

from nest.auth import GoogleSignIn, start_auth_service
from nest.authz import (
    AccessDeniedError,
    PlatformCatalog,
    PlatformResolutionError,
    Principal,
    select_endpoint,
)
from nest.config import (
    LoginNode,
    ProviderProfile,
    load_cli_auth_config,
    load_controller_config,
    load_database_config,
)
from nest.db import (
    Database,
    DatabaseError,
    DuplicateLoginNodeError,
    DuplicatePlatformError,
    EndpointNotApprovedError,
    EnvironmentManagedError,
    IdentityStore,
    JobStore,
    NoLoginNodeError,
    Platform,
    PlatformError,
    PlatformLoginNode,
    PlatformNotFoundError,
    PlatformSlugConflictError,
    PlatformStore,
    ProjectStore,
    SessionStore,
    hash_token,
    load_migrations,
    order_login_nodes,
    sync_providers,
    upgrade,
)
from nest.db.platforms import ENVIRONMENT_HOST_KEY_PIN, ENVIRONMENT_OPERATOR, endpoint_for
from nest.platforms import PlatformNotAvailableError, get_platform, list_platforms

OPS: dict[str, Any] = {"operator": "ops-alice", "reason": "onboard the cluster"}
PIN = "SHA256:" + "A" * 43
NOW = datetime.now(UTC)


def browser(user_id: uuid.UUID) -> Principal:
    return Principal(user_id, "browser")


def cli(user_id: uuid.UUID, *scopes: str) -> Principal:
    return Principal(user_id, "cli", frozenset(scopes))


def platform(slug: str = "nano4", **fields) -> Platform:
    values = {
        "id": uuid.uuid4(),
        "slug": slug,
        "display_name": slug,
        "operator_name": None,
        "scheduler": "slurm",
        "source": "operator",
        "status": "active",
        "is_default": False,
        "created_at": NOW,
        "updated_at": NOW,
        "disabled_at": None,
    }
    values.update(fields)
    return Platform(**values)


def node(platform_id: uuid.UUID, node_id: int, **fields) -> PlatformLoginNode:
    values = {
        "id": node_id,
        "platform_id": platform_id,
        "host": f"ln{node_id:02d}.example.org",
        "port": None,
        "transport_modes": ("A", "B"),
        "priority": 100,
        "status": "active",
        "host_key_pin_ref": PIN,
        "health": "unknown",
        "health_checked_at": None,
        "health_reason": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(fields)
    return PlatformLoginNode(**values)


class SelectionTests(unittest.TestCase):
    def test_order_prefers_health_then_priority_and_skips_unapproved(self):
        p = platform()
        nodes = [
            node(p.id, 1, priority=10, health="unhealthy"),
            node(p.id, 2, priority=50, health="healthy"),
            node(p.id, 3, priority=20, health="unknown"),
            node(p.id, 4, priority=5, health="degraded"),
            node(p.id, 5, priority=1, status="disabled"),
            node(p.id, 6, priority=1, transport_modes=("A",), host_key_pin_ref=None),
        ]
        self.assertEqual([n.id for n in order_login_nodes(nodes, "B")], [2, 3, 4, 1])
        self.assertEqual([n.id for n in order_login_nodes(nodes, "A")], [2, 6, 3, 4, 1])
        with self.assertRaises(ValueError):
            order_login_nodes(nodes, "C")

    def test_mode_b_endpoint_has_an_explicit_port_and_pin(self):
        p = platform()
        endpoint = endpoint_for(p, node(p.id, 1), "B")
        self.assertEqual((endpoint.port, endpoint.host_key_pin_ref), (22, PIN))
        self.assertIsNone(endpoint_for(p, node(p.id, 1), "A").port)
        self.assertEqual(endpoint_for(p, node(p.id, 1, port=2222), "B").port, 2222)

    def test_endpoint_refuses_foreign_unapproved_and_unpinned_nodes(self):
        p, other = platform(), platform("f1")
        for bad, mode in (
            (node(other.id, 1), "A"),
            (node(p.id, 2, transport_modes=("A",)), "B"),
            (node(p.id, 3, status="disabled"), "A"),
            (node(p.id, 4, host_key_pin_ref=None), "B"),
        ):
            with self.subTest(node=bad.id), self.assertRaises(EndpointNotApprovedError):
                endpoint_for(p, bad, mode)

    def test_selection_keeps_the_platform_and_runs_out(self):
        p = platform()
        nodes = [node(p.id, 1, priority=1), node(p.id, 2, priority=2)]
        first = select_endpoint(p, nodes, "B")
        second = select_endpoint(p, nodes, "B", exclude={first.node_id})
        self.assertEqual((first.node_id, second.node_id), (1, 2))
        self.assertEqual({first.platform_id, second.platform_id}, {p.id})
        with self.assertRaises(NoLoginNodeError):
            select_endpoint(p, nodes, "B", exclude={1, 2})
        with self.assertRaises(NoLoginNodeError):
            select_endpoint(platform(status="disabled"), nodes, "B")


class _Base(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = Database(
            load_database_config(
                {"NEST_DATABASE_URL": self.url, "NEST_DATABASE_POOL_MAX_SIZE": "8"}
            )
        )
        self.db.open()
        self.addCleanup(self.db.close)
        self.identities = IdentityStore(self.db)
        self.projects = ProjectStore(self.db)
        self.store = PlatformStore(self.db)
        self.catalog = PlatformCatalog(self.db)
        self.conn = connect(self.url)
        self.addCleanup(self.conn.close)

    def user(self, name: str = "u") -> uuid.UUID:
        return self.identities.create_user(display_name=name).id

    def project(self, owner: uuid.UUID, name: str = "p") -> uuid.UUID:
        return self.projects.create_project(name, created_by_user_id=owner).id

    def get(self, ref) -> Platform:
        found = self.store.get_platform(ref)
        assert found is not None
        return found

    def get_node(self, node_id: int) -> PlatformLoginNode:
        found = self.store.get_login_node(node_id)
        assert found is not None
        return found

    @staticmethod
    def job(jobs: JobStore, record_id: int) -> dict[str, Any]:
        found = jobs.get(record_id)
        assert found is not None
        return found

    def nchc(self) -> tuple[Platform, Platform]:
        nano4 = self.store.create_platform("nano4", "Nano 4", operator_name="NCHC", **OPS)
        f1 = self.store.create_platform("f1", "Forerunner 1", operator_name="NCHC", **OPS)
        return nano4, f1

    def audit(self, event: str) -> list[tuple]:
        return self.conn.execute(
            "SELECT actor_operator, note, resource_id, details, project_id "
            "FROM security_audit_events WHERE event = %s ORDER BY id",
            (event,),
        ).fetchall()


class SchemaTests(_Base):
    def test_migration_is_part_of_the_one_chain(self):
        self.assertIn("0012_hpc_platform_catalog", [m.label for m in load_migrations()])

    def operator(self):
        self.conn.execute("SELECT set_config('nest.audit_operator', 'psql-ops', false)")

    def test_changes_without_an_operator_are_refused(self):
        nano4, _ = self.nchc()
        statements = [
            "INSERT INTO hpc_platforms (slug, display_name) VALUES ('rogue', 'Rogue')",
            f"UPDATE hpc_platforms SET display_name = 'x' WHERE id = '{nano4.id}'",
            (
                "INSERT INTO hpc_login_nodes (platform_id, host, transport_modes) "
                f"VALUES ('{nano4.id}', 'evil.example.org', '{{A}}')"
            ),
        ]
        for statement in statements:
            with (
                self.subTest(statement=statement),
                self.assertRaises(psycopg.errors.InsufficientPrivilege),
            ):
                self.conn.execute(statement)

    def test_only_health_changes_without_an_operator(self):
        nano4, _ = self.nchc()
        added = self.store.add_login_node(
            nano4.id, "ln01.example.org", transport_modes=["A"], **OPS
        )
        self.conn.execute(
            "UPDATE hpc_login_nodes SET health = 'degraded', health_checked_at = now() "
            "WHERE id = %s",
            (added.id,),
        )
        for change in ("host = 'evil.example.org'", "port = 2222", "transport_modes = '{A,B}'"):
            with (
                self.subTest(change=change),
                self.assertRaises(psycopg.errors.InsufficientPrivilege),
            ):
                self.conn.execute(f"UPDATE hpc_login_nodes SET {change} WHERE id = {added.id}")
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.conn.execute(f"DELETE FROM hpc_login_nodes WHERE id = {added.id}")

    def test_platform_id_and_slug_are_immutable_and_never_removed(self):
        nano4, f1 = self.nchc()
        self.operator()
        for statement in (
            f"UPDATE hpc_platforms SET slug = 'nano5' WHERE id = '{nano4.id}'",
            f"UPDATE hpc_platforms SET id = gen_random_uuid() WHERE id = '{nano4.id}'",
            f"DELETE FROM hpc_platforms WHERE id = '{f1.id}'",
            "TRUNCATE hpc_platforms CASCADE",
        ):
            with (
                self.subTest(statement=statement),
                self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            ):
                self.conn.execute(statement)

    def test_login_node_constraints(self):
        nano4, f1 = self.nchc()
        self.operator()
        bad = [
            ("ln01.example.org", "{B}", None),  # Mode B without a pin
            ("ln01.example.org", "{}", None),
            ("ln01.example.org", "{C}", None),
            ("-bad host", "{A}", None),
            ("ln01.example.org", "{A}", "not a pin!"),
        ]
        for host, modes, pin in bad:
            with (
                self.subTest(host=host, modes=modes),
                self.assertRaises(psycopg.errors.CheckViolation),
            ):
                self.conn.execute(
                    "INSERT INTO hpc_login_nodes (platform_id, host, transport_modes, "
                    "host_key_pin_ref) VALUES (%s, %s, %s, %s)",
                    (nano4.id, host, modes, pin),
                )
        insert = (
            "INSERT INTO hpc_login_nodes (platform_id, host, transport_modes) "
            "VALUES (%s, 'ln01.example.org', '{A}')"
        )
        self.conn.execute(insert, (nano4.id,))
        # The same host may serve another Platform, but not twice for one.
        self.conn.execute(insert, (f1.id,))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(insert, (nano4.id,))
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE hpc_login_nodes SET platform_id = %s WHERE platform_id = %s",
                (f1.id, nano4.id),
            )

    def test_at_most_one_default(self):
        nano4, f1 = self.nchc()
        self.operator()
        self.conn.execute("UPDATE hpc_platforms SET is_default = true WHERE id = %s", (nano4.id,))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute("UPDATE hpc_platforms SET is_default = true WHERE id = %s", (f1.id,))


class UpgradePathTests(PostgresTestCase):
    def test_upgrade_from_the_platform_metadata_migration(self):
        """A database at 0011 (Platform metadata, A04) with records upgrades to 0012."""
        url = self.database()
        migrations = load_migrations()
        labels = [m.label for m in migrations]
        self.assertEqual(labels[-2:], ["0011_platform_metadata", "0012_hpc_platform_catalog"])
        with connect(url) as conn:
            self.assertEqual(len(upgrade(conn, migrations[:-1])), len(migrations) - 1)
        db = Database(load_database_config({"NEST_DATABASE_URL": url}))
        db.open()
        self.addCleanup(db.close)
        profile = ProviderProfile(
            "nano4", "A", (LoginNode("ln01"),), account="acct", operator="nchc"
        )
        sync_providers(db, {"nano4": profile}, "nano4")
        old_job = JobStore(db, "nano4").begin("batch", secrets.token_hex(16))
        with connect(url) as conn:
            self.assertEqual([m.label for m in upgrade(conn)], ["0012_hpc_platform_catalog"])
            self.assertEqual(
                conn.execute(
                    "SELECT operator, platform_id FROM provider_profiles p "
                    "JOIN jobs j ON j.provider_id = p.id WHERE j.id = %s",
                    (old_job,),
                ).fetchone(),
                ("nchc", None),
            )
        mirrored = PlatformStore(db).sync_environment({"nano4": profile})
        platform = PlatformStore(db).get_platform("nano4")
        assert platform is not None
        self.assertEqual((platform.id, platform.operator_name), (mirrored["nano4"], "nchc"))


class RegistryTests(_Base):
    def test_same_operator_platforms_are_independent(self):
        nano4, f1 = self.nchc()
        self.assertNotEqual(nano4.id, f1.id)
        self.assertEqual((nano4.operator_name, f1.operator_name), ("NCHC", "NCHC"))
        self.store.add_login_node(nano4.id, "ln01.nchc.example", transport_modes=["A"], **OPS)
        self.store.add_login_node(f1.id, "ln01.nchc.example", transport_modes=["A"], **OPS)
        self.store.set_status("f1", "disabled", **OPS)
        self.assertTrue(self.get("nano4").active)
        self.assertEqual(len(self.store.login_nodes("nano4")), 1)
        self.assertEqual(self.get(f1.id).status, "disabled")
        self.store.set_status("f1", "active", **OPS)
        self.assertEqual(self.get("f1").id, f1.id)
        with self.assertRaises(DuplicatePlatformError):
            self.store.create_platform("nano4", "Again", operator_name="NCHC", **OPS)
        with self.assertRaises(ValueError):
            self.store.create_platform("Nano 4", "Bad slug", **OPS)

    def test_slurm_job_ids_are_scoped_to_their_platform(self):
        nano4, f1 = self.nchc()
        sync_providers(
            self.db,
            {"default": ProviderProfile("default", "A", (LoginNode("ln"),), account="acct")},
            "default",
        )
        jobs = {p.slug: JobStore(self.db, "default", platform_id=p.id) for p in (nano4, f1)}
        records = {}
        for slug, store in jobs.items():
            record = store.begin("batch", secrets.token_hex(16))
            store.submitted(record, 4242)
            records[slug] = self.job(store, record)
        self.assertEqual(records["nano4"]["platform_id"], nano4.id)
        self.assertEqual(records["f1"]["platform_id"], f1.id)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(DISTINCT platform_id) FROM jobs WHERE slurm_job_id = 4242"
            ).fetchone()[0],
            2,
        )
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE jobs SET platform_id = %s WHERE platform_id = %s", (f1.id, nano4.id)
            )

    def test_login_node_management_is_validated_and_audited(self):
        nano4, _ = self.nchc()
        with self.assertRaisesRegex(ValueError, "pin"):
            self.store.add_login_node(nano4.id, "ln01.example.org", transport_modes=["B"], **OPS)
        added = self.store.add_login_node(
            "nano4", "ln01.example.org", transport_modes=["B", "A"], host_key_pin_ref=PIN, **OPS
        )
        self.assertEqual(added.transport_modes, ("A", "B"))
        with self.assertRaises(DuplicateLoginNodeError):
            self.store.add_login_node(nano4.id, "ln01.example.org", transport_modes=["A"], **OPS)
        with self.assertRaisesRegex(ValueError, "pin"):
            self.store.update_login_node(added.id, host_key_pin_ref=None, **OPS)
        changed = self.store.update_login_node(
            added.id, transport_modes=["A"], host_key_pin_ref=None, priority=5, **OPS
        )
        self.assertEqual((changed.transport_modes, changed.priority), (("A",), 5))
        with self.assertRaises(ValueError):
            self.store.add_login_node(
                nano4.id, "x.example.org", transport_modes=["A"], operator=" ", reason="r"
            )
        self.store.remove_login_node(added.id, **OPS)
        self.assertEqual(self.store.login_nodes(nano4.id), [])
        events = self.audit("platform.login_node.changed")
        self.assertEqual(
            [e[3]["changed"] for e in events],
            ["created", "host_key_pin_ref,priority,transport_modes", "removed"],
        )
        self.assertEqual({(e[0], e[1]) for e in events}, {("ops-alice", "onboard the cluster")})
        self.assertNotIn("health", json.dumps([e[3] for e in events]))
        created = self.audit("platform.changed")
        self.assertEqual([e[2] for e in created], [str(nano4.id), str(self.get("f1").id)])

    def test_health_is_recorded_without_an_operator_or_audit(self):
        nano4, _ = self.nchc()
        added = self.store.add_login_node(
            nano4.id, "ln01.example.org", transport_modes=["A"], **OPS
        )
        before = len(self.audit("platform.login_node.changed"))
        self.store.record_health(added.id, "unhealthy", reason="connect_failed")
        self.assertEqual(self.get_node(added.id).health, "unhealthy")
        self.assertEqual(len(self.audit("platform.login_node.changed")), before)
        with self.assertRaises(ValueError):
            self.store.record_health(added.id, "great")

    def test_default_platform(self):
        nano4, f1 = self.nchc()
        self.store.set_default("nano4", **OPS)
        self.store.set_default("f1", **OPS)
        default = self.store.default_platform()
        self.assertEqual(default.id if default else None, f1.id)
        self.store.set_status("f1", "disabled", **OPS)
        self.assertIsNone(self.store.default_platform())
        with self.assertRaises(PlatformError):
            self.store.set_default("f1", **OPS)
        self.store.set_default(nano4.id, **OPS)
        self.store.set_default(None, **OPS)
        self.assertIsNone(self.store.default_platform())

    def test_grants(self):
        nano4, _ = self.nchc()
        pid = self.project(self.user())
        self.assertTrue(self.store.grant_project("nano4", pid, **OPS))
        self.assertFalse(self.store.grant_project("nano4", pid, **OPS))
        self.assertEqual(self.store.granted_projects(nano4.id), [pid])
        self.assertTrue(self.store.revoke_project("nano4", pid, **OPS))
        self.assertFalse(self.store.revoke_project("nano4", pid, **OPS))
        self.assertEqual(
            [(e[2], e[4]) for e in self.audit("platform.grant.added")], [(str(nano4.id), pid)]
        )
        self.assertEqual(len(self.audit("platform.grant.removed")), 1)
        with self.assertRaises(PlatformNotFoundError):
            self.store.grant_project("nope", pid, **OPS)


class EnvironmentSyncTests(_Base):
    def profiles(self, *nodes: str, mode: str = "A", user: str | None = None):
        login_nodes = []
        for entry in nodes:
            host, _, port = entry.partition(":")
            login_nodes.append(LoginNode(host, int(port) if port else None))
        login_nodes = tuple(login_nodes)
        return {"nano4": ProviderProfile("nano4", mode, login_nodes, account="acct", ssh_user=user)}

    def test_profiles_become_stable_env_platforms(self):
        first = self.store.sync_environment(self.profiles("ln01", "ln02:2222"))
        nodes = self.store.login_nodes("nano4")
        self.assertEqual(
            [(n.host, n.port, n.priority) for n in nodes], [("ln01", None, 0), ("ln02", 2222, 10)]
        )
        platform_row = self.get("nano4")
        self.assertEqual((platform_row.source, platform_row.id), ("env", first["nano4"]))

        # A restart with a changed node list keeps the Platform ID.
        second = self.store.sync_environment(
            self.profiles("ln02:2222", "ln03", mode="B", user="svc")
        )
        self.assertEqual(second, first)
        nodes = self.store.login_nodes("nano4")
        self.assertEqual(
            [(n.host, n.transport_modes) for n in nodes], [("ln02", ("B",)), ("ln03", ("B",))]
        )
        self.assertEqual({n.host_key_pin_ref for n in nodes}, {ENVIRONMENT_HOST_KEY_PIN})
        operators = {e[0] for e in self.audit("platform.login_node.changed")}
        self.assertEqual(operators, {ENVIRONMENT_OPERATOR})

        # Unchanged restarts record nothing new.
        count = len(self.audit("platform.login_node.changed"))
        self.store.sync_environment(self.profiles("ln02:2222", "ln03", mode="B", user="svc"))
        self.assertEqual(len(self.audit("platform.login_node.changed")), count)

        self.store.sync_environment({})
        self.assertEqual(self.get(first["nano4"]).status, "disabled")
        self.assertEqual(self.store.sync_environment(self.profiles("ln01")), first)
        self.assertTrue(self.get("nano4").active)

    def test_env_platforms_are_not_changed_by_hand(self):
        mirrored = self.store.sync_environment(self.profiles("ln01"))
        with self.assertRaises(EnvironmentManagedError):
            self.store.add_login_node("nano4", "evil", transport_modes=["A"], **OPS)
        with self.assertRaises(EnvironmentManagedError):
            self.store.update_login_node(self.store.login_nodes("nano4")[0].id, priority=1, **OPS)
        # Grants stay operator decisions for env Platforms too.
        self.assertTrue(self.store.grant_project("nano4", self.project(self.user()), **OPS))

        self.assertIn("nano4", mirrored)

    def test_a_slug_collision_with_an_operator_platform_refuses_startup(self):
        from nest.controller import environment_platform_id, main, open_database

        mirrored = self.store.sync_environment(self.profiles("ln01"))
        self.store.create_platform("f1", "F1", **OPS)
        before = self.conn.execute("SELECT count(*) FROM security_audit_events").fetchone()
        profiles = {
            **self.profiles("ln01"),
            "f1": ProviderProfile("f1", "A", (LoginNode("ln09"),), account="acct"),
        }
        with self.assertRaises(PlatformSlugConflictError):
            self.store.sync_environment(profiles)
        # Nothing was written: no nodes on the operator Platform, nano4 untouched.
        self.assertEqual(self.store.login_nodes("f1"), [])
        self.assertEqual(self.get("f1").source, "operator")
        self.assertTrue(self.get("nano4").active)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM security_audit_events").fetchone(), before
        )
        # Nor can a job be attributed to the operator Platform's slug.
        with self.assertRaises(DatabaseError):
            environment_platform_id(self.db, "f1")
        self.assertEqual(environment_platform_id(self.db, "nano4"), mirrored["nano4"])

        environ = {
            "NEST_DATABASE_URL": self.url,
            "NEST_CONTROLLER_AUTH": "none",
            "NEST_PLATFORMS": "f1",
            "NEST_PLATFORM_F1_LOGIN_NODES": "ln09.example.org",
            "NEST_PLATFORM_F1_ACCOUNT": "acct",
        }
        with self.assertRaises(PlatformSlugConflictError):
            open_database(load_controller_config(environ))
        with (
            mock.patch.dict(os.environ, environ, clear=True),
            self.assertRaises(SystemExit) as caught,
        ):
            main()
        self.assertEqual(caught.exception.code, 3)

    def test_controller_mirrors_and_records_jobs_on_the_env_platform(self):
        from nest.controller import environment_platform_id, open_database

        config = load_controller_config(
            {
                "NEST_DATABASE_URL": self.url,
                "NEST_CONTROLLER_AUTH": "none",
                "NEST_PLATFORMS": "nano4",
                "NEST_PLATFORM_NANO4_LOGIN_NODES": "ln01.example.org,ln02.example.org",
                "NEST_PLATFORM_NANO4_ACCOUNT": "acct",
                "NEST_PLATFORM_NANO4_OPERATOR": "nchc",
                "NEST_PLATFORM_NANO4_NAME": "Nano 4",
            }
        )
        database = open_database(config)
        assert database is not None
        self.addCleanup(database.close)
        platform_id = environment_platform_id(database, "nano4")
        self.assertEqual(platform_id, self.get("nano4").id)
        mirrored = self.get("nano4")
        self.assertEqual((mirrored.display_name, mirrored.operator_name), ("Nano 4", "nchc"))
        self.assertEqual(len(self.store.login_nodes("nano4")), 2)


class CatalogTests(_Base):
    def setUp(self):
        super().setUp()
        self.nano4, self.f1 = self.nchc()
        self.store.create_platform("other", "Other cluster", **OPS)
        self.alice, self.bob, self.carol = self.user("alice"), self.user("bob"), self.user("carol")
        self.lab = self.project(self.alice, "lab")
        self.projects.add_member(self.lab, self.bob, "viewer")
        self.solo = self.project(self.carol, "solo")
        self.store.grant_project("nano4", self.lab, **OPS)
        self.store.grant_project("f1", self.lab, **OPS)
        self.store.grant_project("f1", self.solo, **OPS)
        for host, priority in (("ln01.nano4.example", 10), ("ln02.nano4.example", 20)):
            self.store.add_login_node(
                "nano4",
                host,
                transport_modes=["A", "B"],
                priority=priority,
                host_key_pin_ref=PIN,
                **OPS,
            )

    def slugs(self, principal, **kw) -> list[str]:
        return [s.platform.slug for s in self.catalog.list_platforms(principal, **kw)]

    def test_discovery_returns_only_authorized_platforms(self):
        self.assertEqual(self.slugs(browser(self.alice)), ["f1", "nano4"])
        self.assertEqual(self.slugs(cli(self.bob, "projects:read")), ["f1", "nano4"])
        self.assertEqual(self.slugs(browser(self.carol)), ["f1"])
        self.assertEqual(self.slugs(browser(self.alice), project_id=self.solo), [])
        self.assertEqual(self.slugs(browser(self.user("outsider"))), [])
        summary = self.catalog.list_platforms(browser(self.alice))[1]
        self.assertEqual((summary.transport_modes, summary.login_node_count), (("A", "B"), 2))

    def test_discovery_follows_grants_memberships_and_status_at_once(self):
        self.store.set_status("f1", "disabled", **OPS)
        self.assertEqual(self.slugs(browser(self.carol)), [])
        self.store.set_status("f1", "active", **OPS)
        self.store.revoke_project("nano4", self.lab, **OPS)
        self.assertEqual(self.slugs(browser(self.bob)), ["f1"])
        self.projects.remove_member(self.lab, self.bob)
        self.assertEqual(self.slugs(browser(self.bob)), [])
        self.identities.set_user_status(self.carol, "disabled")
        self.assertEqual(self.slugs(browser(self.carol)), [])
        self.projects.delete_project(self.lab, actor_user_id=self.alice)
        self.assertEqual(self.slugs(browser(self.alice)), [])

    def test_cli_sessions_need_a_scope(self):
        with self.assertRaises(AccessDeniedError) as caught:
            self.catalog.list_platforms(cli(self.alice))
        self.assertEqual(caught.exception.reason, "scope")
        with self.assertRaises(AccessDeniedError):
            self.catalog.resolve_platform(cli(self.alice, "unrelated"), "nano4")
        self.assertEqual(self.slugs(cli(self.alice, "jobs:run")), ["f1", "nano4"])

    def test_unauthorized_platforms_look_missing(self):
        for reference in ("other", self.get("other").id, "missing", "Not A Slug"):
            with self.subTest(reference=reference), self.assertRaises(PlatformNotFoundError):
                self.catalog.resolve_platform(browser(self.carol), reference)
        with self.assertRaises(PlatformNotFoundError):
            self.catalog.resolve_platform(browser(self.carol), "nano4")
        with self.assertRaises(PlatformNotFoundError):
            self.catalog.select_login_node(browser(self.carol), "nano4", mode="B")
        denied = self.conn.execute(
            "SELECT count(*) FROM security_audit_events "
            "WHERE event = 'platform.resolve' AND outcome = 'denied' AND actor_user_id = %s",
            (self.carol,),
        ).fetchone()[0]
        self.assertGreaterEqual(denied, 5)

    def test_default_resolution_is_server_authorized(self):
        # Several authorized and no default: the caller must choose.
        with self.assertRaises(PlatformResolutionError) as caught:
            self.catalog.resolve_platform(browser(self.alice))
        self.assertEqual(caught.exception.reason, "ambiguous")
        # Only one authorized: that one.
        self.assertEqual(self.catalog.resolve_platform(browser(self.carol)).slug, "f1")
        # The default is used only for callers authorized for it.
        self.store.set_default("nano4", **OPS)
        self.assertEqual(self.catalog.resolve_platform(browser(self.alice)).slug, "nano4")
        self.assertEqual(self.catalog.resolve_platform(browser(self.carol)).slug, "f1")
        # Naming a Platform overrides the default.
        self.assertEqual(self.catalog.resolve_platform(browser(self.alice), "f1").slug, "f1")
        with self.assertRaises(PlatformResolutionError) as caught:
            self.catalog.resolve_platform(browser(self.user("outsider")))
        self.assertEqual(caught.exception.reason, "none_authorized")

    def test_failover_preserves_platform_identity(self):
        who = browser(self.alice)
        first = self.catalog.select_login_node(who, "nano4", mode="B")
        self.assertEqual(
            (first.host, first.port, first.host_key_pin_ref), ("ln01.nano4.example", 22, PIN)
        )
        second = self.catalog.failover(who, first)
        self.assertEqual(second.host, "ln02.nano4.example")
        self.assertEqual({first.platform_id, second.platform_id}, {self.nano4.id})
        self.assertEqual(self.get_node(first.node_id).health, "unhealthy")
        # The unhealthy node is now tried last, so a new selection starts at ln02.
        self.assertEqual(
            self.catalog.select_login_node(who, "nano4", mode="B").node_id, second.node_id
        )
        with self.assertRaises(NoLoginNodeError):
            self.catalog.failover(who, second, tried={first.node_id})
        # Jobs keep the Platform whichever node submitted them.
        sync_providers(
            self.db,
            {"default": ProviderProfile("default", "A", (LoginNode("x"),), account="acct")},
            "default",
        )
        jobs = JobStore(self.db, "default", platform_id=second.platform_id)
        self.assertEqual(
            self.job(jobs, jobs.begin("batch", secrets.token_hex(16)))["platform_id"], self.nano4.id
        )

    def test_only_approved_endpoints_can_be_selected(self):
        who = browser(self.alice)
        f1_node = self.store.add_login_node("f1", "ln01.f1.example", transport_modes=["A"], **OPS)
        nano4_nodes = self.store.login_nodes("nano4")
        # Another Platform's node, or an unapproved mode, is refused.
        for node_id, mode in ((f1_node.id, "A"), (999999, "A")):
            with self.subTest(node_id=node_id), self.assertRaises(EndpointNotApprovedError):
                self.catalog.select_login_node(who, "nano4", mode=mode, node_id=node_id)
        with self.assertRaises(NoLoginNodeError):
            self.catalog.select_login_node(who, "f1", mode="B")
        chosen = self.catalog.select_login_node(who, "nano4", mode="A", node_id=nano4_nodes[1].id)
        self.assertEqual(chosen.host, "ln02.nano4.example")
        self.store.update_login_node(nano4_nodes[1].id, status="disabled", **OPS)
        with self.assertRaises(EndpointNotApprovedError):
            self.catalog.select_login_node(who, "nano4", mode="A", node_id=nano4_nodes[1].id)
        refused = self.conn.execute(
            "SELECT count(*) FROM security_audit_events "
            "WHERE event = 'platform.endpoint.select' AND outcome = 'denied'"
        ).fetchone()[0]
        self.assertEqual(refused, 3)


class PlatformsAPITests(_Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeGoogle()
        self.addCleanup(self.fake.close)
        port = free_port()
        self.base = f"http://127.0.0.1:{port}"
        config = load_controller_config(
            {
                "NEST_DATABASE_URL": self.url,
                "NEST_GOOGLE_CLIENT_ID": CLIENT_ID,
                "NEST_GOOGLE_CLIENT_SECRET": CLIENT_SECRET,
                "NEST_GOOGLE_REDIRECT_URL": f"{self.base}/auth/google/callback",
                "NEST_AUTH_HTTP_BIND": f"127.0.0.1:{port}",
            }
        ).google
        assert config is not None
        sign_in = GoogleSignIn(config, self.db, endpoints=self.fake.endpoints, timeout=5)
        self.http = start_auth_service(config, self.db, sign_in=sign_in)
        self.addCleanup(self.http.stop)
        self.sessions = SessionStore(self.db)
        self.nano4, self.f1 = self.nchc()
        self.alice, self.carol = self.user("alice"), self.user("carol")
        self.lab = self.project(self.alice)
        self.store.grant_project("nano4", self.lab, **OPS)
        self.store.add_login_node(
            "nano4", "secret-ln.nchc.example", transport_modes=["B"], host_key_pin_ref=PIN, **OPS
        )

    def token(self, user_id: uuid.UUID, scope: str = "projects:read") -> str:
        session = self.identities.create_session(
            user_id,
            kind="cli",
            token_hash=hash_token(secrets.token_urlsafe()),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scope=scope,
        )
        access = secrets.token_urlsafe()
        self.sessions.issue_access_token(session.id, token_hash=hash_token(access), ttl_seconds=600)
        return access

    def call(self, path: str, bearer: str | None = None, method: str = "GET"):
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        data = b"{}" if method == "POST" else None
        if data:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read() or b"{}")

    def test_discovery_over_http(self):
        alice = self.token(self.alice)
        status, body = self.call("/api/platforms", alice)
        self.assertEqual(status, 200)
        self.assertEqual([p["slug"] for p in body["platforms"]], ["nano4"])
        self.assertEqual(body["default_platform_id"], str(self.nano4.id))
        self.assertEqual(body["platforms"][0]["operator_name"], "NCHC")
        self.assertNotIn("secret-ln", json.dumps(body))
        self.assertNotIn(PIN, json.dumps(body))
        status, one = self.call("/api/platforms/nano4", alice)
        self.assertEqual(
            (status, one["id"], one["transport_modes"]), (200, str(self.nano4.id), ["B"])
        )
        status, _ = self.call(f"/api/platforms/{self.nano4.id}?project_id={self.lab}", alice)
        self.assertEqual(status, 200)

    def test_unauthorized_discovery_over_http(self):
        carol = self.token(self.carol)
        self.assertEqual(
            self.call("/api/platforms", carol),
            (200, {"platforms": [], "default_platform_id": None}),
        )
        for path in ("/api/platforms/nano4", "/api/platforms/f1", "/api/platforms/missing"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, carol), (404, {"error": "not_found"}))
        self.assertEqual(self.call("/api/platforms")[0], 401)
        self.assertEqual(self.call("/api/platforms", self.token(self.alice, "unrelated"))[0], 403)
        self.assertEqual(self.call("/api/platforms?project_id=nope", carol)[0], 400)
        self.assertEqual(self.call("/api/platforms/nano4/nodes", carol)[0], 404)
        # Platforms cannot be created or changed through the API.
        self.assertEqual(self.call("/api/platforms", self.token(self.alice), method="POST")[0], 405)

    def test_cli_client(self):
        config = load_cli_auth_config(
            {"NEST_CLI_CREDENTIALS_FILE": "/nonexistent/credentials.json"}, url=self.base
        )
        session = mock.Mock(access_token=self.token(self.alice))
        with mock.patch("nest.platforms.ensure_session", return_value=session):
            platforms, default = list_platforms(config)
            self.assertEqual(([p.slug for p in platforms], default), (["nano4"], self.nano4.id))
            self.assertEqual(get_platform("nano4", config).id, self.nano4.id)
            with self.assertRaises(PlatformNotAvailableError):
                get_platform("f1", config)


class OperatorCLITests(_Base):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        """Runs `nest-db platform` in its own process, as an operator would."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("NEST_DATABASE_")}
        env["NEST_DATABASE_URL"] = self.url
        result = subprocess.run(
            [sys.executable, "-m", "nest.db.cli", "platform", *argv],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr

    def test_operator_manages_the_catalog(self):
        audit = ["--operator", "ops-bob", "--reason", "onboard"]
        code, out, err = self.run_cli(
            "create", "--slug", "nano4", "--name", "Nano 4", "--operated-by", "NCHC", *audit
        )
        self.assertEqual(code, 0, err)
        self.assertIn("nano4 id=", out)
        code, out, err = self.run_cli(
            "add-node", "nano4", "--host", "ln01.example.org", "--modes", "B",
            "--host-key-pin", PIN, *audit,
        )  # fmt: skip
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli(
            "add-node", "nano4", "--host", "ln02.example.org", "--modes", "B", *audit
        )
        self.assertEqual(code, 2)
        self.assertIn("pin", err)
        pid = self.project(self.user())
        self.assertEqual(self.run_cli("grant", "nano4", "--project-id", str(pid), *audit)[0], 0)
        self.assertEqual(self.run_cli("grant", "nano4", "--project-id", str(pid), *audit)[0], 1)
        code, out, _ = self.run_cli("show", "nano4")
        self.assertEqual(code, 0)
        self.assertIn("ln01.example.org", out)
        self.assertIn(f"grant project={pid}", out)
        self.assertEqual(self.run_cli("set-default", "nano4", *audit)[0], 0)
        self.assertEqual(self.run_cli("disable", "missing", *audit)[0], 1)
        # Every change names its operator.
        self.assertEqual(self.run_cli("create", "--slug", "f1", "--name", "F1")[0], 2)
        operators = {
            row[0]
            for row in self.conn.execute(
                "SELECT actor_operator FROM security_audit_events WHERE event LIKE 'platform.%'"
            )
        }
        self.assertEqual(operators, {"ops-bob"})
