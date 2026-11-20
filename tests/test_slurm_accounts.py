"""Platform-scoped Slurm Account Registry and Project mapping (D08).

`ResolutionTests` check the account selection precedence without a database. The
other tests run against a real PostgreSQL (see `postgres_fixture`): schema
constraints and triggers for Platform and tenant isolation, the operator-side
`SlurmAccountStore`, the server-side `AccountResolver`, the execution record of
the resolved account and its origin, the upgrade from 0012, and `nest-db account`.
"""

import json
import os
import secrets
import subprocess
import sys
import unittest
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from postgres_fixture import PostgresTestCase, connect
from test_platforms import OPS, _Base, browser, cli, platform

from nest.authz import (
    AccessDeniedError,
    AccountResolution,
    AccountResolutionError,
    AccountResolver,
    HPCIdentity,
    resolve_account,
)
from nest.config import LoginNode, ProviderProfile, load_database_config
from nest.db import (
    AccountLinkNotFoundError,
    Database,
    DuplicateSlurmAccountError,
    JobStore,
    PlatformNotFoundError,
    PlatformNotGrantedError,
    ProjectAccountLink,
    ProjectAccountMapping,
    SlurmAccount,
    SlurmAccountError,
    SlurmAccountNotFoundError,
    SlurmAccountStore,
    load_migrations,
    normalize_account_name,
    sync_providers,
    upgrade,
)
from nest.errors import DatabaseUnavailableError

NOW = datetime.now(UTC)
PROJECT = uuid.uuid4()
USER = uuid.uuid4()


def account(platform_id: uuid.UUID, name: str, account_id: int, **fields) -> SlurmAccount:
    values: dict[str, Any] = {
        "id": account_id,
        "platform_id": platform_id,
        "account_name": name,
        "kind": "standard",
        "description": None,
        "status": "active",
        "is_mode_a_default": False,
        "created_at": NOW,
        "updated_at": NOW,
        "disabled_at": None,
    }
    values.update(fields)
    return SlurmAccount(**values)


def link(acct: SlurmAccount, modes: str = "AB", *, default: bool = False) -> ProjectAccountLink:
    return ProjectAccountLink(PROJECT, acct.platform_id, acct, tuple(modes), default)


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.nano4 = platform("nano4")
        pid = self.nano4.id
        self.chem = account(pid, "chem", 1)
        self.phys = account(pid, "phys", 2)
        self.ops = account(pid, "ops", 3, kind="chargeback", is_mode_a_default=True)
        self.old = account(pid, "old", 4, status="disabled", disabled_at=NOW)
        self.controller = HPCIdentity.controller(pid)
        self.user = HPCIdentity.user(pid, USER, verified_default_account="phys")

    def mapping(self, *links: ProjectAccountLink, native: bool = False, granted: bool = True):
        return ProjectAccountMapping(PROJECT, self.nano4, granted, tuple(links), native)

    def refused(self, mapping, reason: str, **kw) -> None:
        with self.assertRaises(AccountResolutionError) as caught:
            resolve_account(mapping, **kw)
        self.assertEqual(caught.exception.reason, reason)

    def test_explicit_permitted_account_wins(self):
        mapping = self.mapping(link(self.chem), link(self.phys, default=True))
        chosen = resolve_account(mapping, mode="B", identity=self.user, requested=" CHEM ")
        self.assertEqual((chosen.account_name, chosen.origin), ("chem", "explicit"))
        self.assertEqual((chosen.account_id, chosen.requested), (1, "chem"))
        self.assertEqual((chosen.platform_id, chosen.mode, chosen.identity_kind), (
            self.nano4.id, "B", "user"))  # fmt: skip

    def test_explicit_requests_never_fall_back(self):
        mapping = self.mapping(
            link(self.chem, "A"), link(self.phys, default=True), link(self.old), link(self.ops, "A")
        )
        self.refused(mapping, "not_permitted", mode="B", identity=self.user, requested="bio")
        self.refused(mapping, "mode_not_permitted", mode="B", identity=self.user, requested="chem")
        self.refused(mapping, "account_disabled", mode="B", identity=self.user, requested="old")
        self.refused(mapping, "mode_not_permitted", mode="B", identity=self.user, requested="ops")
        limited = HPCIdentity.user(self.nano4.id, USER, entitled_accounts={"chem"})
        self.refused(mapping, "not_entitled", mode="B", identity=limited, requested="phys")
        self.refused(mapping, "not_entitled", mode="A",
                     identity=HPCIdentity.controller(self.nano4.id, entitled_accounts=["ops"]),
                     requested="chem")  # fmt: skip
        with self.assertRaises(ValueError):
            resolve_account(mapping, mode="B", identity=self.user, requested="bad name")

    def test_wrong_platform_accounts_are_not_permitted(self):
        f1 = platform("f1")
        foreign = account(f1.id, "chem", 9)
        # A link to another Platform's account, as a corrupted mapping would carry.
        mapping = self.mapping(
            ProjectAccountLink(PROJECT, self.nano4.id, foreign, ("A", "B"), True)
        )
        self.refused(mapping, "not_permitted", mode="B", identity=self.user, requested="chem")
        self.refused(mapping, "no_account", mode="B",
                     identity=HPCIdentity.user(self.nano4.id, USER))  # fmt: skip
        self.refused(mapping, "wrong_platform", mode="B", identity=HPCIdentity.user(f1.id, USER))

    def test_project_default_comes_before_identity_defaults(self):
        mapping = self.mapping(link(self.chem, default=True), link(self.phys), link(self.ops, "A"))
        for mode, identity in (("A", self.controller), ("B", self.user)):
            chosen = resolve_account(mapping, mode=mode, identity=identity)
            self.assertEqual((chosen.account_name, chosen.origin), ("chem", "project_default"))

    def test_unusable_project_default_falls_through_to_step_three(self):
        mapping = self.mapping(link(self.chem, "A", default=True), link(self.phys, "B"))
        chosen = resolve_account(mapping, mode="B", identity=self.user)
        self.assertEqual((chosen.account_name, chosen.origin), ("phys", "mode_b_user_default"))
        limited = HPCIdentity.user(
            self.nano4.id, USER, entitled_accounts={"chem"}, verified_default_account="phys"
        )
        both = self.mapping(link(self.chem, "B", default=True), link(self.phys, "B"))
        self.assertEqual(
            resolve_account(both, mode="B", identity=limited).origin, "project_default"
        )
        self.refused(
            self.mapping(link(self.chem, "A", default=True), link(self.phys, "B")),
            "no_account", mode="B", identity=HPCIdentity.user(
                self.nano4.id, USER, entitled_accounts={"chem"},
                verified_default_account="phys"),
        )  # fmt: skip

    def test_mode_a_uses_the_linked_operator_default(self):
        mapping = self.mapping(link(self.chem), link(self.ops, "A"))
        chosen = resolve_account(mapping, mode="A", identity=self.controller)
        self.assertEqual((chosen.account_name, chosen.origin), ("ops", "mode_a_operator_default"))
        # The operator default is used only for Projects linked to it.
        self.refused(self.mapping(link(self.chem)), "no_account", mode="A",
                     identity=self.controller)  # fmt: skip
        # Mode A never uses a User's or the native default.
        self.refused(self.mapping(link(self.phys), native=True), "no_account", mode="A",
                     identity=self.controller)  # fmt: skip

    def test_mode_b_never_falls_back_to_the_controller_or_chargeback(self):
        operator_default = account(self.nano4.id, "shared", 5, is_mode_a_default=True)
        mapping = self.mapping(link(operator_default), link(self.ops, "A"))
        # Linked for both modes, the operator default still is no Mode B fallback.
        self.refused(mapping, "no_account", mode="B", identity=HPCIdentity.user(
            self.nano4.id, USER))  # fmt: skip
        # Corrupted data linking a chargeback account for Mode B is still refused.
        rogue = self.mapping(link(self.ops, "AB", default=True))
        self.refused(rogue, "no_account", mode="B", identity=self.user)
        self.refused(rogue, "mode_not_permitted", mode="B", identity=self.user, requested="ops")
        # The Controller identity can never charge in Mode B, nor a User in Mode A.
        self.refused(mapping, "identity_not_allowed", mode="B", identity=self.controller)
        self.refused(mapping, "identity_not_allowed", mode="A", identity=self.user)

    def test_mode_b_user_default_must_be_linked(self):
        mapping = self.mapping(link(self.chem))
        self.refused(mapping, "no_account", mode="B", identity=self.user)
        self.assertEqual(
            resolve_account(
                self.mapping(link(self.phys, "B")), mode="B", identity=self.user
            ).origin,
            "mode_b_user_default",
        )

    def test_native_default_is_mode_b_only_and_policy_gated(self):
        mapping = self.mapping(link(self.chem, "A"), native=True)
        chosen = resolve_account(mapping, mode="B", identity=HPCIdentity.user(self.nano4.id, USER))
        self.assertEqual((chosen.origin, chosen.account_id, chosen.account_name), (
            "native_default", None, None))  # fmt: skip
        self.assertEqual(
            chosen.job_fields(),
            {"account": None, "slurm_account_id": None, "slurm_account_origin": "native_default"},
        )
        self.refused(self.mapping(link(self.chem, "A")), "no_account", mode="B",
                     identity=self.user)  # fmt: skip

    def test_missing_grant_and_bad_input(self):
        self.refused(self.mapping(link(self.chem), granted=False), "not_granted", mode="A",
                     identity=self.controller, requested="chem")  # fmt: skip
        with self.assertRaises(ValueError):
            resolve_account(self.mapping(), mode="C", identity=self.controller)
        with self.assertRaises(ValueError):
            HPCIdentity("user", self.nano4.id)
        with self.assertRaises(ValueError):
            HPCIdentity("root", self.nano4.id)

    def test_details_hold_no_credentials(self):
        chosen = resolve_account(
            self.mapping(link(self.chem, default=True)), mode="A", identity=self.controller
        )
        self.assertEqual(
            set(chosen.details()),
            {"platform_id", "platform", "mode", "identity", "account_id", "account", "origin"},
        )

    def test_account_names_are_normalized(self):
        self.assertEqual(normalize_account_name("  MST110 "), "mst110")
        for bad in ("", "-x", "a b", "x" * 65, "acct;rm"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_account_name(bad)


class _AccountBase(_Base):
    def setUp(self):
        super().setUp()
        self.accounts = SlurmAccountStore(self.db)
        self.nano4, self.f1 = self.nchc()
        self.alice, self.bob = self.user("alice"), self.user("bob")
        self.lab = self.project(self.alice, "lab")
        self.other = self.project(self.bob, "other")
        for slug in ("nano4", "f1"):
            self.store.grant_project(slug, self.lab, **OPS)
        self.store.grant_project("nano4", self.other, **OPS)

    def operator(self):
        self.conn.execute("SELECT set_config('nest.audit_operator', 'psql-ops', false)")

    def ids(self, name: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT p.slug, a.id FROM slurm_accounts a JOIN hpc_platforms p "
            "ON p.id = a.platform_id WHERE a.account_name = %s",
            (name,),
        ).fetchall()
        return dict(rows)


class SchemaTests(_AccountBase):
    def test_migration_is_part_of_the_one_chain(self):
        labels = [m.label for m in load_migrations()]
        self.assertEqual(
            labels[labels.index("0012_hpc_platform_catalog") + 1], "0013_slurm_account_registry"
        )

    def test_identical_names_on_two_platforms_are_distinct_accounts(self):
        nano4 = self.accounts.register_account("nano4", "chem", **OPS)
        f1 = self.accounts.register_account("f1", "chem", **OPS)
        self.assertNotEqual(nano4.id, f1.id)
        self.assertEqual(self.ids("chem"), {"nano4": nano4.id, "f1": f1.id})
        self.accounts.link_project(self.lab, "nano4", "chem", modes=["A"], **OPS)
        # Linking on nano4 grants nothing on f1.
        self.assertEqual(len(self.accounts.mapping(self.lab, "nano4").links), 1)
        self.assertEqual(self.accounts.mapping(self.lab, "f1").links, ())
        self.operator()
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(
                "INSERT INTO slurm_accounts (platform_id, account_name) VALUES (%s, 'chem')",
                (self.nano4.id,),
            )

    def test_links_cannot_reference_another_platforms_account(self):
        f1_chem = self.accounts.register_account("f1", "chem", **OPS)
        self.operator()
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.conn.execute(
                "INSERT INTO project_platform_accounts (project_id, platform_id, "
                "slurm_account_id, modes) VALUES (%s, %s, %s, '{A}')",
                (self.lab, self.nano4.id, f1_chem.id),
            )
        # A Project not granted the Platform cannot be linked at all.
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.conn.execute(
                "INSERT INTO project_platform_accounts (project_id, platform_id, "
                "slurm_account_id, modes) VALUES (%s, %s, %s, '{A}')",
                (self.other, self.f1.id, f1_chem.id),
            )

    def test_changes_without_an_operator_are_refused(self):
        chem = self.accounts.register_account("nano4", "chem", **OPS)
        self.accounts.link_project(self.lab, "nano4", "chem", modes=["A"], **OPS)
        statements = [
            f"INSERT INTO slurm_accounts (platform_id, account_name) VALUES ('{self.nano4.id}', 'x')",
            f"UPDATE slurm_accounts SET status = 'disabled', disabled_at = now() WHERE id = {chem.id}",
            f"UPDATE project_platform_accounts SET modes = '{{A,B}}' WHERE slurm_account_id = {chem.id}",
            f"DELETE FROM project_platform_accounts WHERE slurm_account_id = {chem.id}",
            (
                "INSERT INTO project_platform_account_policies (project_id, platform_id, "
                f"allow_native_default) VALUES ('{self.lab}', '{self.nano4.id}', true)"
            ),
        ]  # fmt: skip
        for statement in statements:
            with (
                self.subTest(statement=statement),
                self.assertRaises(psycopg.errors.InsufficientPrivilege),
            ):
                self.conn.execute(statement)

    def test_accounts_are_immutable_and_never_removed(self):
        chem = self.accounts.register_account("nano4", "chem", **OPS)
        self.operator()
        for statement in (
            f"UPDATE slurm_accounts SET platform_id = '{self.f1.id}' WHERE id = {chem.id}",
            f"UPDATE slurm_accounts SET account_name = 'bio' WHERE id = {chem.id}",
            f"UPDATE slurm_accounts SET kind = 'chargeback' WHERE id = {chem.id}",
            f"DELETE FROM slurm_accounts WHERE id = {chem.id}",
            "TRUNCATE slurm_accounts CASCADE",
        ):
            with (
                self.subTest(statement=statement),
                self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            ):
                self.conn.execute(statement)

    def test_link_constraints(self):
        self.accounts.register_account("nano4", "chem", **OPS)
        ops = self.accounts.register_account("nano4", "ops", kind="chargeback", **OPS)
        self.operator()
        insert = (
            "INSERT INTO project_platform_accounts (project_id, platform_id, slurm_account_id, "
            "modes, is_default) VALUES (%s, %s, %s, %s, %s)"
        )
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(insert, (self.lab, self.nano4.id, ops.id, "{A,B}", False))
        for modes in ("{}", "{C}", "{A,A,B}"):
            with self.subTest(modes=modes), self.assertRaises(psycopg.errors.CheckViolation):
                self.conn.execute(insert, (self.lab, self.nano4.id, ops.id, modes, False))
        chem = self.ids("chem")["nano4"]
        self.conn.execute(insert, (self.lab, self.nano4.id, chem, "{A}", True))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.conn.execute(insert, (self.lab, self.nano4.id, ops.id, "{A}", True))
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE project_platform_accounts SET project_id = %s WHERE project_id = %s",
                (self.other, self.lab),
            )

    def test_revoking_the_grant_removes_the_links(self):
        self.accounts.register_account("nano4", "chem", **OPS)
        self.accounts.link_project(self.lab, "nano4", "chem", modes=["A"], default=True, **OPS)
        self.accounts.set_native_default(self.lab, "nano4", True, **OPS)
        self.store.revoke_project("nano4", self.lab, **OPS)
        mapping = self.accounts.mapping(self.lab, "nano4")
        self.assertEqual((mapping.granted, mapping.links, mapping.allow_native_default), (
            False, (), False))  # fmt: skip
        self.assertEqual(len(self.audit("slurm_account.link.removed")), 1)
        # The account itself stays registered.
        self.assertIsNotNone(self.accounts.get_account("nano4", "chem"))

    def test_project_ids_are_never_tied_to_account_names(self):
        self.accounts.register_account("nano4", "lab", **OPS)
        self.accounts.link_project(self.lab, "nano4", "lab", modes=["A"], **OPS)
        self.accounts.link_project(self.other, "nano4", "lab", modes=["A"], **OPS)
        # Two Projects sharing an account stay two Projects with their own IDs.
        for pid, name in ((self.lab, "lab"), (self.other, "other")):
            project = self.projects.get_project(pid)
            assert project is not None
            self.assertEqual((project.id, project.name), (pid, name))
        self.assertEqual(
            {m.project_id for m in (self.accounts.mapping(p, "nano4") for p in (self.lab, self.other))},
            {self.lab, self.other},
        )  # fmt: skip


class JobRecordTests(_AccountBase):
    def setUp(self):
        super().setUp()
        sync_providers(
            self.db,
            {"default": ProviderProfile("default", "A", (LoginNode("ln"),), account="acct")},
            "default",
        )
        self.chem = self.accounts.register_account("nano4", "chem", **OPS)
        self.f1_chem = self.accounts.register_account("f1", "chem", **OPS)
        self.jobs = JobStore(self.db, "default", platform_id=self.nano4.id)

    def test_resolved_account_and_origin_are_recorded(self):
        resolution = AccountResolution(
            self.lab, self.nano4.id, "nano4", "A", "controller", "project_default",
            self.chem.id, "chem",
        )  # fmt: skip
        record = self.jobs.begin("batch", secrets.token_hex(16), **resolution.job_fields())
        job = self.job(self.jobs, record)
        self.assertEqual(
            (job["account"], job["slurm_account_id"], job["slurm_account_origin"]),
            ("chem", self.chem.id, "project_default"),
        )
        native = self.jobs.begin(
            "batch", secrets.token_hex(16), slurm_account_origin="native_default"
        )
        self.assertEqual(self.job(self.jobs, native)["account"], None)
        with self.assertRaises(psycopg.errors.IntegrityConstraintViolation):
            self.conn.execute(
                "UPDATE jobs SET slurm_account_origin = 'explicit' WHERE id = %s", (record,)
            )

    def test_job_accounts_must_belong_to_the_job_platform(self):
        # chem on f1 is not chem on nano4.
        cases: tuple[dict[str, Any], ...] = (
            {"slurm_account_id": self.f1_chem.id, "slurm_account_origin": "explicit"},
            {"slurm_account_id": self.chem.id, "slurm_account_origin": "explicit",
             "account": "bio"},
            {"slurm_account_id": self.chem.id},
            {"slurm_account_id": self.chem.id, "slurm_account_origin": "native_default"},
            {"slurm_account_origin": "explicit"},
            {"slurm_account_origin": "native_default", "account": "chem"},
        )  # fmt: skip
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(DatabaseUnavailableError):
                self.jobs.begin("batch", secrets.token_hex(16), **fields)
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "INSERT INTO jobs (execution_id, provider_id, kind, slurm_account_id, "
                "slurm_account_origin) VALUES (%s, 'default', 'batch', %s, 'explicit')",
                (secrets.token_hex(16), self.chem.id),
            )


class StoreTests(_AccountBase):
    def test_register_and_update(self):
        chem = self.accounts.register_account("nano4", " CHEM ", description="Chemistry", **OPS)
        self.assertEqual(
            (chem.account_name, chem.kind, chem.status), ("chem", "standard", "active")
        )
        with self.assertRaises(DuplicateSlurmAccountError):
            self.accounts.register_account(self.nano4.id, "chem", **OPS)
        with self.assertRaises(ValueError):
            self.accounts.register_account("nano4", "bio", kind="root", **OPS)
        with self.assertRaises(PlatformNotFoundError):
            self.accounts.register_account("nowhere", "bio", **OPS)
        disabled = self.accounts.update_account("nano4", "chem", status="disabled", **OPS)
        self.assertIsNotNone(disabled.disabled_at)
        self.assertEqual([a.account_name for a in self.accounts.list_accounts("nano4")], ["chem"])
        self.assertEqual(self.accounts.list_accounts("f1"), [])
        events = self.audit("slurm_account.changed")
        self.assertEqual([e[0] for e in events], ["ops-alice", "ops-alice"])
        self.assertEqual(events[1][3]["changed"], "disabled_at,status")

    def test_mode_a_default(self):
        self.accounts.register_account("nano4", "ops", kind="chargeback", **OPS)
        self.accounts.register_account("nano4", "ops2", kind="chargeback", **OPS)
        self.accounts.register_account("f1", "ops", kind="chargeback", **OPS)
        self.accounts.set_mode_a_default("nano4", "ops", **OPS)
        self.accounts.set_mode_a_default("f1", "ops", **OPS)
        self.accounts.set_mode_a_default("nano4", "ops2", **OPS)
        defaults = {
            (a.platform_id, a.account_name)
            for p in ("nano4", "f1")
            for a in self.accounts.list_accounts(p)
            if a.is_mode_a_default
        }
        self.assertEqual(defaults, {(self.nano4.id, "ops2"), (self.f1.id, "ops")})
        self.accounts.update_account("nano4", "ops2", status="disabled", **OPS)
        disabled = self.accounts.get_account("nano4", "ops2")
        assert disabled is not None
        self.assertFalse(disabled.is_mode_a_default)
        with self.assertRaises(SlurmAccountError):
            self.accounts.set_mode_a_default("nano4", "ops2", **OPS)
        self.assertIsNone(self.accounts.set_mode_a_default("f1", None, **OPS))

    def test_links(self):
        for name in ("chem", "phys"):
            self.accounts.register_account("nano4", name, **OPS)
        self.accounts.register_account("nano4", "ops", kind="chargeback", **OPS)
        self.accounts.register_account("f1", "bio", **OPS)
        self.accounts.link_project(self.lab, "nano4", "chem", modes=["a", "B"], default=True, **OPS)
        self.accounts.link_project(self.lab, "nano4", "phys", modes=["B"], default=True, **OPS)
        mapping = self.accounts.mapping(self.lab, "nano4")
        self.assertEqual(
            [(x.account.account_name, x.modes, x.is_default) for x in mapping.links],
            [("chem", ("A", "B"), False), ("phys", ("B",), True)],
        )
        with self.assertRaises(SlurmAccountError):
            self.accounts.link_project(self.lab, "nano4", "ops", modes=["A", "B"], **OPS)
        # bio exists only on f1, so it cannot be linked on nano4.
        with self.assertRaises(SlurmAccountNotFoundError):
            self.accounts.link_project(self.lab, "nano4", "bio", modes=["A"], **OPS)
        with self.assertRaises(PlatformNotGrantedError):
            self.accounts.link_project(self.other, "f1", "bio", modes=["A"], **OPS)
        with self.assertRaises(PlatformNotGrantedError):
            self.accounts.set_native_default(self.other, "f1", True, **OPS)
        self.assertTrue(self.accounts.clear_project_default(self.lab, "nano4", **OPS))
        self.assertFalse(self.accounts.clear_project_default(self.lab, "nano4", **OPS))
        self.accounts.unlink_project(self.lab, "nano4", "phys", **OPS)
        with self.assertRaises(AccountLinkNotFoundError):
            self.accounts.unlink_project(self.lab, "nano4", "phys", **OPS)
        self.accounts.set_native_default(self.lab, "nano4", True, **OPS)
        self.assertTrue(self.accounts.mapping(self.lab, "nano4").allow_native_default)
        added = self.audit("slurm_account.link.added")
        self.assertEqual({e[4] for e in added}, {self.lab})
        self.assertEqual(len(self.audit("slurm_account.link.removed")), 1)
        self.assertEqual(len(self.audit("slurm_account.policy.changed")), 1)


class ResolverTests(_AccountBase):
    def setUp(self):
        super().setUp()
        self.resolver = AccountResolver(self.db)
        self.projects.add_member(self.lab, self.bob, "member")
        for slug in ("nano4", "f1"):
            self.accounts.register_account(slug, "chem", **OPS)
            self.accounts.register_account(slug, "ops", kind="chargeback", **OPS)
            self.accounts.set_mode_a_default(slug, "ops", **OPS)
        self.accounts.link_project(self.lab, "nano4", "chem", modes=["A", "B"], **OPS)
        self.accounts.link_project(self.lab, "nano4", "ops", modes=["A"], **OPS)

    def controller(self, slug: str = "nano4") -> HPCIdentity:
        return HPCIdentity.controller(self.get(slug).id)

    def test_mode_a_resolution(self):
        chosen = self.resolver.resolve(
            browser(self.alice), self.lab, platform="nano4", mode="A", identity=self.controller()
        )
        self.assertEqual((chosen.account_name, chosen.origin), ("ops", "mode_a_operator_default"))
        explicit = self.resolver.resolve(
            cli(self.bob, "jobs:run"), self.lab, platform="nano4", mode="A",
            identity=self.controller(), account="chem",
        )  # fmt: skip
        self.assertEqual(
            (explicit.account_id, explicit.origin), (self.ids("chem")["nano4"], "explicit")
        )

    def test_identical_names_resolve_per_platform(self):
        self.accounts.link_project(self.lab, "f1", "chem", modes=["A"], default=True, **OPS)
        on = {
            slug: self.resolver.resolve(
                browser(self.alice), self.lab, platform=slug, mode="A",
                identity=self.controller(slug), account="chem",
            ).account_id
            for slug in ("nano4", "f1")
        }  # fmt: skip
        self.assertEqual(on, self.ids("chem"))
        # A nano4 identity cannot charge f1.
        with self.assertRaises(AccountResolutionError) as caught:
            self.resolver.resolve(
                browser(self.alice), self.lab, platform="f1", mode="A",
                identity=self.controller("nano4"),
            )  # fmt: skip
        self.assertEqual(caught.exception.reason, "wrong_platform")

    def test_unlinked_platform_account_is_refused_and_audited(self):
        with self.assertRaises(AccountResolutionError) as caught:
            self.resolver.resolve(
                browser(self.alice), self.lab, platform="f1", mode="A",
                identity=self.controller("f1"), account="chem",
            )  # fmt: skip
        self.assertEqual(caught.exception.reason, "not_permitted")
        denied = self.conn.execute(
            "SELECT outcome, reason, project_id, details FROM security_audit_events "
            "WHERE event = 'slurm_account.resolve'"
        ).fetchall()
        self.assertEqual(len(denied), 1)
        outcome, reason, project_id, details = denied[0]
        self.assertEqual((outcome, reason, project_id), ("denied", "not_permitted", self.lab))
        self.assertEqual(details["requested_account"], "chem")
        self.assertEqual(details["platform_id"], str(self.f1.id))

    def test_mode_b_rules(self):
        own = HPCIdentity.user(self.nano4.id, self.bob, verified_default_account="chem")
        chosen = self.resolver.resolve(
            browser(self.bob), self.lab, platform="nano4", mode="B", identity=own
        )
        self.assertEqual((chosen.account_name, chosen.origin), ("chem", "mode_b_user_default"))
        for principal, identity, reason in (
            (browser(self.alice), own, "identity_not_allowed"),
            (browser(self.bob), self.controller(), "identity_not_allowed"),
            (browser(self.bob), HPCIdentity.user(self.nano4.id, self.bob), "no_account"),
        ):
            with self.subTest(reason=reason), self.assertRaises(AccountResolutionError) as caught:
                self.resolver.resolve(
                    principal, self.lab, platform="nano4", mode="B", identity=identity
                )
            self.assertEqual(caught.exception.reason, reason)
        with self.assertRaises(AccountResolutionError) as caught:
            self.resolver.resolve(
                browser(self.bob), self.lab, platform="nano4", mode="B", identity=own,
                account="ops",
            )  # fmt: skip
        self.assertEqual(caught.exception.reason, "mode_not_permitted")
        self.accounts.set_native_default(self.lab, "nano4", True, **OPS)
        native = self.resolver.resolve(
            browser(self.bob), self.lab, platform="nano4", mode="B",
            identity=HPCIdentity.user(self.nano4.id, self.bob),
        )  # fmt: skip
        self.assertEqual((native.origin, native.account_name), ("native_default", None))

    def test_non_members_and_ungranted_projects_see_no_platform(self):
        outsider = self.user("outsider")
        with self.assertRaises(PlatformNotFoundError):
            self.resolver.resolve(
                browser(outsider), self.lab, platform="nano4", mode="A",
                identity=self.controller(),
            )  # fmt: skip
        with self.assertRaises(PlatformNotFoundError):
            self.resolver.resolve(
                browser(self.bob), self.other, platform="f1", mode="A",
                identity=self.controller("f1"),
            )  # fmt: skip
        with self.assertRaises(AccessDeniedError):
            self.resolver.resolve(
                cli(self.bob), self.lab, platform="nano4", mode="A", identity=self.controller()
            )

    def test_disabling_an_account_takes_effect_at_once(self):
        self.accounts.update_account("nano4", "ops", status="disabled", **OPS)
        with self.assertRaises(AccountResolutionError) as caught:
            self.resolver.resolve(
                browser(self.alice), self.lab, platform="nano4", mode="A",
                identity=self.controller(),
            )  # fmt: skip
        self.assertEqual(caught.exception.reason, "no_account")

    def test_resolution_flows_into_the_execution_record(self):
        sync_providers(
            self.db,
            {"default": ProviderProfile("default", "A", (LoginNode("ln"),), account="acct")},
            "default",
        )
        chosen = self.resolver.resolve(
            browser(self.alice), self.lab, platform="nano4", mode="A", identity=self.controller()
        )
        jobs = JobStore(self.db, "default", platform_id=chosen.platform_id)
        record = jobs.begin(
            "batch", secrets.token_hex(16), project_id=self.lab,
            submitted_by_user_id=self.alice, **chosen.job_fields(),
        )  # fmt: skip
        job = self.job(jobs, record)
        self.assertEqual(
            (job["platform_id"], job["account"], job["slurm_account_origin"]),
            (self.nano4.id, "ops", "mode_a_operator_default"),
        )


class UpgradePathTests(PostgresTestCase):
    def test_upgrade_from_the_platform_catalog_migration(self):
        """A database at 0012 (D07) with job records upgrades to 0013."""
        url = self.database()
        migrations = load_migrations()
        at = [m.label for m in migrations].index("0013_slurm_account_registry")
        with connect(url) as conn:
            upgrade(conn, migrations[:at])
        db = Database(load_database_config({"NEST_DATABASE_URL": url}))
        db.open()
        self.addCleanup(db.close)
        profile = ProviderProfile("nano4", "A", (LoginNode("ln01"),), account="acct")
        sync_providers(db, {"nano4": profile}, "nano4")
        old_job = JobStore(db, "nano4").begin("batch", secrets.token_hex(16), account="acct")
        with connect(url) as conn:
            self.assertEqual(
                [m.label for m in upgrade(conn, migrations[: at + 1])],
                ["0013_slurm_account_registry"],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT account, slurm_account_id, slurm_account_origin FROM jobs "
                    "WHERE id = %s",
                    (old_job,),
                ).fetchone(),
                ("acct", None, None),
            )


class OperatorCLITests(_AccountBase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        """Runs `nest-db account` in its own process, as an operator would."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("NEST_DATABASE_")}
        env["NEST_DATABASE_URL"] = self.url
        result = subprocess.run(
            [sys.executable, "-m", "nest.db.cli", "account", *argv],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr

    def test_operator_manages_accounts(self):
        audit = ["--operator", "ops-bob", "--reason", "onboard"]
        project = ["--project-id", str(self.lab)]
        code, out, err = self.run_cli("register", "nano4", "Chem", "--description", "x", *audit)
        self.assertEqual(code, 0, err)
        self.assertIn("chem id=", out)
        self.assertEqual(self.run_cli("register", "nano4", "chem", *audit)[0], 1)
        self.assertEqual(self.run_cli("register", "f1", "chem", *audit)[0], 0)
        code, out, err = self.run_cli("register", "nano4", "ops", "--kind", "chargeback", *audit)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.run_cli("set-mode-a-default", "nano4", "ops", *audit)[0], 0)
        code, out, err = self.run_cli(
            "link", "nano4", "chem", "--modes", "A,B", "--default", *project, *audit
        )
        self.assertEqual(code, 0, err)
        code, _, err = self.run_cli("link", "nano4", "ops", "--modes", "B", *project, *audit)
        self.assertEqual(code, 1)
        self.assertIn("Mode A", err)
        self.assertEqual(
            self.run_cli("link", "nano4", "chem", "--modes", "C", *project, *audit)[0], 2
        )
        self.assertEqual(self.run_cli("native-default", "nano4", "--allow", *project, *audit)[0], 0)
        code, out, _ = self.run_cli("show", "nano4", *project)
        self.assertEqual(code, 0)
        self.assertIn("native_default=allowed", out)
        self.assertIn("link chem", out)
        self.assertIn("default=yes", out)
        code, out, _ = self.run_cli("list", "nano4")
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["chem", "ops"])
        self.assertIn(f"description={json.dumps('x')}", out)
        self.assertEqual(self.run_cli("unlink", "nano4", "chem", *project, *audit)[0], 0)
        self.assertEqual(self.run_cli("unlink", "nano4", "chem", *project, *audit)[0], 1)
        self.assertEqual(self.run_cli("clear-default", "nano4", *project, *audit)[0], 1)
        self.assertEqual(self.run_cli("disable", "nano4", "chem", *audit)[0], 0)
        self.assertEqual(self.run_cli("list", "nowhere")[0], 1)
        operators = {e[0] for e in self.audit("slurm_account.changed")}
        self.assertEqual(operators, {"ops-bob"})


if __name__ == "__main__":
    unittest.main()
