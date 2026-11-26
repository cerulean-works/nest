"""Server-enforced execution authority (D01) and verified HPC account bindings (D02).

PostgreSQL tests run against the server named by `NEST_TEST_DATABASE_URL` (see
`postgres_fixture`). The admission tests serve the real gRPC `ExecutionService`
with `RegistryAdmission` against the `FakeHpc` login node, like
`test_registry_admission`, whose fixture they reuse: a served Mode A Platform
`nano` granted to Project `alpha`, whose member submits.

The central assertions: an authority the policies do not permit is refused and
never replaced by another, a `user_owned` request never runs as the Controller,
and nothing reaches the cluster when either happens.
"""

import os
import secrets
import unittest
import uuid
from concurrent import futures
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from typing import Any, ClassVar, cast
from unittest import mock

import cloudpickle
import grpc
import psycopg
from postgres_fixture import PostgresTestCase, connect
from test_registry_admission import _Base as _AdmissionBase

from nest.admission import RegistryAdmission
from nest.audit import AuditLog
from nest.authz import AuthorityRefusedError, AuthorityResolver, Principal
from nest.config import ConfigError, load_controller_config, load_database_config
from nest.controller import ExecutionService, build_admission
from nest.db import (
    EXECUTION_AUTHORITIES,
    AuthorityNotPermittedError,
    AuthorityPin,
    BindingConflictError,
    BindingNotFoundError,
    BindingNotVerifiedError,
    BindingStateError,
    Database,
    DatabaseError,
    ExecutionRegistry,
    HPCAccessStore,
    HPCPrincipal,
    IdentityStore,
    PlatformStore,
    ProjectStore,
    ResolvedOption,
    SlurmAccountStore,
    load_migrations,
    upgrade,
)
from nest.db.cli import main as nest_db
from nest.db.platforms import endpoint_for
from nest.proto import execution_pb2, execution_pb2_grpc

OPS: dict[str, Any] = {"operator": "ops-test", "reason": "hpc access tests"}
PIN = "SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
BOTH = ("controller_owned", "user_owned")


def attest(store, user, platform, unix_user, **kw):
    return store.attest(
        user,
        platform,
        unix_user,
        method=kw.pop("method", "nchc_attestation"),
        evidence_ref=kw.pop("evidence_ref", "NCHC-ATTEST-1"),
        **{**OPS, **kw},
    )


class _Database(PostgresTestCase):
    """Two Platforms `nano4` and `f1` (both operated by NCHC), two Users, two Projects."""

    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
        self.db = Database(
            load_database_config(
                {"NEST_DATABASE_URL": self.url, "NEST_DATABASE_POOL_MAX_SIZE": "4"}
            )
        )
        self.db.open()
        self.addCleanup(self.db.close)
        identities = IdentityStore(self.db)
        self.alice = identities.create_user(display_name="alice").id
        self.bob = identities.create_user(display_name="bob").id
        projects = ProjectStore(self.db)
        self.project = projects.create_project("p", created_by_user_id=self.alice).id
        self.other_project = projects.create_project("q", created_by_user_id=self.bob).id
        self.platforms = PlatformStore(self.db)
        self.nano4 = self.platforms.create_platform("nano4", "Nano 4", operator_name="NCHC", **OPS)
        self.f1 = self.platforms.create_platform("f1", "F1", operator_name="NCHC", **OPS)
        self.node = self.platforms.add_login_node(
            self.nano4.id, "login1.nano4.example", transport_modes=["A"], **OPS
        )
        for platform in (self.nano4, self.f1):
            self.platforms.grant_project(platform.id, self.project, **OPS)
        self.access = HPCAccessStore(self.db)
        self.audit = AuditLog(self.db)
        self.conn = connect(self.url)
        self.conn.autocommit = True
        self.addCleanup(self.conn.close)

    def events(self, event):
        return list(reversed(self.audit.events(event=event, limit=100)))


class PolicyTests(_Database):
    def test_without_a_policy_only_controller_owned_is_permitted(self):
        policy = self.access.effective_policy(self.project, self.nano4.id)
        self.assertEqual(policy.authorities, ("controller_owned",))
        self.assertEqual((policy.platform_version, policy.project_version), (0, 0))

    def test_both_layers_must_opt_in_to_user_owned(self):
        self.assertEqual(self.access.set_platform_policy(self.nano4.id, BOTH, **OPS), 1)
        # The Platform alone does not grant user_owned to a Project.
        policy = self.access.effective_policy(self.project, self.nano4.id)
        self.assertEqual(policy.authorities, ("controller_owned",))
        self.assertEqual(
            self.access.set_project_policy(self.project, "nano4", ["user_owned"], **OPS), 1
        )
        policy = self.access.effective_policy(self.project, self.nano4.id)
        self.assertEqual(policy.authorities, ("user_owned",))
        self.assertEqual((policy.platform_version, policy.project_version), (1, 1))
        # Policies are per Project and per Platform.
        self.assertEqual(
            self.access.effective_policy(self.other_project, self.nano4.id).authorities,
            ("controller_owned",),
        )
        self.assertEqual(
            self.access.effective_policy(self.project, self.f1.id).authorities,
            ("controller_owned",),
        )
        # A Platform may permit nothing at all.
        self.assertEqual(self.access.set_platform_policy("nano4", [], **OPS), 2)
        self.assertEqual(self.access.effective_policy(self.project, self.nano4.id).authorities, ())

    def test_policies_are_versioned_append_only_operator_managed_and_audited(self):
        for authorities in (BOTH, ["controller_owned"], ["user_owned"]):
            self.access.set_platform_policy(self.nano4.id, authorities, **OPS)
        self.access.set_project_policy(self.project, self.nano4.id, BOTH, **OPS)
        rows = self.conn.execute(
            "SELECT version, authorities FROM hpc_platform_authority_policies "
            "WHERE platform_id = %s ORDER BY version",
            (self.nano4.id,),
        ).fetchall()
        self.assertEqual(
            rows,
            [(1, list(BOTH)), (2, ["controller_owned"]), (3, ["user_owned"])],
        )
        for statement in (
            "UPDATE hpc_platform_authority_policies SET authorities = '{}'",
            "DELETE FROM hpc_platform_authority_policies",
            "DELETE FROM project_platform_authority_policies",
            "TRUNCATE hpc_platform_authority_policies",
            "UPDATE project_platform_authority_policies SET authorities = '{}'",
        ):
            with self.subTest(statement=statement), self.assertRaises(psycopg.Error):
                self.conn.execute(statement)
        # No operator named: refused for every writer.
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.conn.execute(
                "INSERT INTO hpc_platform_authority_policies (platform_id, version, authorities) "
                "VALUES (%s, 1, '{user_owned}')",
                (self.nano4.id,),
            )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.conn.execute(
                "INSERT INTO project_platform_authority_policies "
                "(project_id, platform_id, version, authorities) VALUES (%s, %s, 1, '{user_owned}')",
                (self.project, self.nano4.id),
            )
        events = self.events("platform.authority_policy.set")
        self.assertEqual([e.details["version"] for e in events], [1, 2, 3])
        self.assertEqual(events[0].details["authorities"], "controller_owned,user_owned")
        self.assertEqual({e.actor_operator for e in events}, {"ops-test"})
        [project_event] = self.events("project.authority_policy.set")
        self.assertEqual(project_event.project_id, self.project)

    def test_invalid_authorities_are_refused(self):
        for bad in (["mode_a"], ["user_owned", "user_owned"], ["A"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.access.set_platform_policy(self.nano4.id, bad, **OPS)
        with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
            self.conn.execute("SELECT set_config('nest.audit_operator', 'ops', true)")
            self.conn.execute(
                "INSERT INTO hpc_platform_authority_policies "
                "(platform_id, version, authorities) VALUES (%s, 1, '{controller}')",
                (self.nano4.id,),
            )


class BindingTests(_Database):
    def test_a_claim_is_recorded_but_never_verified(self):
        claim = self.access.claim(self.alice, self.nano4.id, "alice01")
        self.assertEqual(
            (claim.status, claim.version, claim.claim_source, claim.verified),
            ("claimed", 1, "user_claim", False),
        )
        self.assertIsNone(claim.verified_at)
        with self.assertRaises(BindingConflictError):
            self.access.claim(self.alice, "nano4", "alice02")
        # Without an operator the User cannot verify their own claim or insert a
        # verified binding.
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.conn.execute(
                "UPDATE hpc_account_bindings SET status = 'verified', "
                "verification_method = 'operator_attestation', verification_evidence_ref = 'x', "
                "verified_by = 'me', verified_at = now() WHERE id = %s",
                (claim.id,),
            )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.conn.execute(
                "INSERT INTO hpc_account_bindings (user_id, platform_id, version, unix_user, "
                "claim_source) VALUES (%s, %s, 1, 'bob01', 'operator')",
                (self.bob, self.nano4.id),
            )
        [event] = self.events("hpc_binding.claimed")
        self.assertEqual(
            (event.target_user_id, event.details["unix_user"]), (self.alice, "alice01")
        )

    def test_an_operator_attestation_verifies_and_is_audited(self):
        claim = self.access.claim(self.alice, self.nano4.id, "alice01")
        verified = attest(self.access, self.alice, "nano4", "alice01", unix_uid=1001)
        self.assertEqual(
            (verified.id, verified.status, verified.version), (claim.id, "verified", 1)
        )
        self.assertEqual(
            (
                verified.verification_method,
                verified.verification_evidence_ref,
                verified.verified_by,
            ),
            ("nchc_attestation", "NCHC-ATTEST-1", "ops-test"),
        )
        self.assertEqual(verified.unix_uid, 1001)
        [event] = self.events("hpc_binding.verified")
        self.assertEqual(event.actor_operator, "ops-test")
        self.assertEqual(event.details["verification_evidence_ref"], "NCHC-ATTEST-1")
        # A recorded verification is never edited, even by an operator.
        for column, value in (
            ("verification_evidence_ref", "'other'"),
            ("verified_by", "'someone'"),
            ("unix_uid", "2002"),
            ("unix_user", "'mallory'"),
            ("platform_id", f"'{self.f1.id}'"),
            ("user_id", f"'{self.bob}'"),
        ):
            with (
                self.subTest(column=column),
                self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
                self.conn.transaction(),
            ):
                self.conn.execute("SELECT set_config('nest.audit_operator', 'ops', true)")
                self.conn.execute(
                    f"UPDATE hpc_account_bindings SET {column} = {value} WHERE id = %s",
                    (verified.id,),
                )

    def test_replacement_is_a_new_version_and_history_is_kept(self):
        first = attest(self.access, self.alice, self.nano4.id, "alice01")
        second = attest(self.access, self.alice, self.nano4.id, "alice02", evidence_ref="T-2")
        self.assertEqual((second.version, second.status), (2, "verified"))
        history = self.access.bindings(user_id=self.alice, platform="nano4")
        self.assertEqual(
            [(b.version, b.unix_user, b.status) for b in history],
            [(1, "alice01", "replaced"), (2, "alice02", "verified")],
        )
        self.assertEqual(history[0].replaced_by_binding_id, second.id)
        self.assertEqual(history[0].verification_evidence_ref, "NCHC-ATTEST-1")
        self.assertEqual(self.access.current_binding(self.alice, self.nano4.id), second)
        # A claim for a different name than the verified one is replaced too.
        self.access.revoke(second.id, reason="account_closed", operator="ops")
        self.access.claim(self.alice, self.nano4.id, "typo")
        third = attest(self.access, self.alice, self.nano4.id, "alice03")
        self.assertEqual(
            [(b.version, b.status) for b in self.access.bindings(user_id=self.alice)],
            [(1, "replaced"), (2, "revoked"), (3, "replaced"), (4, "verified")],
        )
        self.assertEqual(third.version, 4)
        # Replacement must point to a later version of the same User and Platform.
        bob = attest(self.access, self.bob, self.nano4.id, "bob01")
        with (
            self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            self.conn.transaction(),
        ):
            self.conn.execute("SELECT set_config('nest.audit_operator', 'ops', true)")
            self.conn.execute(
                "UPDATE hpc_account_bindings SET status = 'replaced', "
                "replaced_by_binding_id = %s, ended_at = now() WHERE id = %s",
                (bob.id, third.id),
            )
        # Final versions never change, and nothing is deleted.
        with (
            self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            self.conn.transaction(),
        ):
            self.conn.execute("SELECT set_config('nest.audit_operator', 'ops', true)")
            self.conn.execute(
                "UPDATE hpc_account_bindings SET status = 'verified' WHERE id = %s",
                (first.id,),
            )
        for statement in ("DELETE FROM hpc_account_bindings", "TRUNCATE hpc_account_bindings"):
            with self.subTest(statement=statement), self.assertRaises(psycopg.Error):
                self.conn.execute(statement)

    def test_a_unix_account_is_verified_for_at_most_one_user_per_platform(self):
        attest(self.access, self.alice, self.nano4.id, "shared01")
        with self.assertRaises(BindingConflictError):
            attest(self.access, self.bob, self.nano4.id, "shared01")
        self.assertIsNone(self.access.current_binding(self.bob, self.nano4.id))
        # The same name on another Platform is another UNIX account.
        on_f1 = attest(self.access, self.bob, self.f1.id, "shared01")
        self.assertEqual(on_f1.platform_id, self.f1.id)
        # A claim is no evidence, so it cannot block the real owner.
        self.access.claim(self.bob, self.nano4.id, "shared01")

    def test_a_known_uid_is_verified_for_at_most_one_user_per_platform(self):
        attest(self.access, self.alice, self.nano4.id, "alice01", unix_uid=40123)
        # Another name for the same UID is the same POSIX principal.
        with self.assertRaises(BindingConflictError):
            attest(self.access, self.bob, self.nano4.id, "alias01", unix_uid=40123)
        self.assertIsNone(self.access.current_binding(self.bob, self.nano4.id))
        # The same UID on another Platform is another account.
        attest(self.access, self.bob, self.f1.id, "bob01", unix_uid=40123)
        # Bindings without an attested UID are checked by name only.
        attest(self.access, self.bob, self.nano4.id, "bob01")
        carol = IdentityStore(self.db).create_user(display_name="carol").id
        attest(self.access, carol, self.nano4.id, "carol01")
        # The owner may replace their own binding and keep the UID.
        mine = attest(self.access, self.alice, self.nano4.id, "alice02", unix_uid=40123)
        self.assertEqual((mine.version, mine.unix_uid), (2, 40123))
        # A claim verified in place is checked too.
        carol_first = self.access.current_binding(carol, self.nano4.id)
        assert carol_first is not None
        self.access.revoke(carol_first.id, reason="withdrawn", operator="ops")
        self.access.claim(carol, self.nano4.id, "carol02")
        with self.assertRaises(BindingConflictError):
            attest(self.access, carol, self.nano4.id, "carol02", unix_uid=40123)
        # Once revoked, the UID can be verified for its next owner.
        self.access.revoke(mine.id, reason="account_reassigned", operator="ops")
        moved = attest(self.access, carol, self.nano4.id, "carol02", unix_uid=40123)
        self.assertEqual(moved.status, "verified")

    def test_revocation_and_withdrawal(self):
        claim = self.access.claim(self.alice, self.nano4.id, "alice01")
        # Another User cannot withdraw it.
        with self.assertRaises(BindingNotFoundError):
            self.access.revoke(claim.id, reason="withdrawn", user_id=self.bob)
        withdrawn = self.access.revoke(claim.id, reason="withdrawn", user_id=self.alice)
        self.assertEqual((withdrawn.status, withdrawn.revoked_reason), ("revoked", "withdrawn"))
        verified = attest(self.access, self.alice, self.nano4.id, "alice01")
        with self.assertRaises(BindingStateError):
            self.access.revoke(verified.id, reason="withdrawn", user_id=self.alice)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            self.conn.execute(
                "UPDATE hpc_account_bindings SET status = 'revoked', revoked_reason = 'x', "
                "ended_at = now() WHERE id = %s",
                (verified.id,),
            )
        revoked = self.access.revoke(verified.id, reason="left_project", operator="ops")
        self.assertEqual(revoked.status, "revoked")
        self.assertIsNone(self.access.current_binding(self.alice, self.nano4.id))
        with self.assertRaises(BindingNotFoundError):
            self.access.revoke(verified.id, reason="again", operator="ops")
        events = [e.event for e in self.audit.events(limit=50) if e.resource_type == "hpc_binding"]
        self.assertEqual(
            list(reversed(events)),
            ["hpc_binding.claimed", "hpc_binding.revoked", "hpc_binding.verified",
             "hpc_binding.revoked"],
        )  # fmt: skip

    def test_login_node_changes_never_change_a_binding(self):
        binding = attest(self.access, self.alice, self.nano4.id, "alice01")
        second = self.platforms.add_login_node(
            self.nano4.id, "login2.nano4.example", transport_modes=["A"], **OPS
        )
        self.platforms.update_login_node(self.node.id, status="disabled", **OPS)
        self.platforms.remove_login_node(second.id, **OPS)
        self.assertEqual(self.access.current_binding(self.alice, self.nano4.id), binding)
        columns = {
            r[0]
            for r in self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'hpc_account_bindings'"
            ).fetchall()
        }
        self.assertFalse({c for c in columns if "node" in c or "host" in c})

    def test_no_credential_can_be_stored(self):
        columns = {
            r[0]
            for r in self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name IN ('hpc_account_bindings', 'hpc_platform_authority_policies', "
                "'project_platform_authority_policies')"
            ).fetchall()
        }
        for word in ("password", "secret", "otp", "mfa", "key", "token", "session"):
            self.assertFalse([c for c in columns if word in c], word)
        key = "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n"
        for evidence in (key, "pass word", "x" * 201, ""):
            with self.subTest(evidence=evidence[:12]), self.assertRaises(ValueError):
                attest(self.access, self.alice, self.nano4.id, "alice01", evidence_ref=evidence)
        with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
            self.conn.execute("SELECT set_config('nest.audit_operator', 'ops', true)")
            self.conn.execute(
                "INSERT INTO hpc_account_bindings (user_id, platform_id, version, unix_user, "
                "claim_source, status, verification_method, verification_evidence_ref, "
                "verified_by, verified_at) VALUES (%s, %s, 1, 'alice01', 'operator', 'verified', "
                "'operator_attestation', %s, 'ops', now())",
                (self.alice, self.nano4.id, key),
            )
        for name in ("al ice", "-rf", "", "a" * 65, "alice;id"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.access.claim(self.alice, self.nano4.id, name)


class ResolverTests(_Database):
    def setUp(self):
        super().setUp()
        self.resolver = AuthorityResolver(self.db, BOTH, self.audit)
        self.principal = Principal(self.alice, "cli", session_id=uuid.uuid4())

    def refused(self, *args, resolver=None, principal=None) -> AuthorityRefusedError:
        with self.assertRaises(AuthorityRefusedError) as caught:
            (resolver or self.resolver).resolve(
                principal or self.principal, self.project, self.nano4.id, *args
            )
        return caught.exception

    def enable_user_owned(self, project_authorities=BOTH):
        self.access.set_platform_policy(self.nano4.id, BOTH, **OPS)
        self.access.set_project_policy(self.project, self.nano4.id, project_authorities, **OPS)

    def test_the_default_is_controller_owned_and_user_owned_is_refused(self):
        decision = self.resolver.resolve(self.principal, self.project, self.nano4.id)
        self.assertEqual(
            (decision.authority, decision.origin), ("controller_owned", "policy_default")
        )
        self.assertEqual(decision.pin(), AuthorityPin("controller_owned", 0, 0))
        self.assertIsNone(decision.binding)
        error = self.refused("user_owned")
        self.assertEqual((error.reason, error.authority), ("authority_not_permitted", "user_owned"))
        [denied] = self.audit.events(event="execution.authority", limit=5)
        self.assertEqual((denied.outcome, denied.reason), ("denied", "authority_not_permitted"))
        self.assertEqual(denied.project_id, self.project)
        self.assertEqual(self.refused("root").reason, "invalid_authority")

    def test_the_deployment_layer_applies_too(self):
        self.enable_user_owned()
        attest(self.access, self.alice, self.nano4.id, "alice01")
        controller_only = AuthorityResolver(self.db, ["controller_owned"], self.audit)
        self.assertEqual(
            self.refused("user_owned", resolver=controller_only).reason, "authority_not_permitted"
        )
        decision = controller_only.resolve(self.principal, self.project, self.nano4.id)
        self.assertEqual(decision.authority, "controller_owned")
        user_only = AuthorityResolver(self.db, ["user_owned"], self.audit)
        self.assertEqual(
            self.refused("controller_owned", resolver=user_only).reason, "authority_not_permitted"
        )

    def test_user_owned_needs_a_verified_binding_of_a_human_user(self):
        self.enable_user_owned(["user_owned"])
        # Unrequested, user_owned is the only permitted authority: no binding means
        # refusal, never the Controller's identity.
        self.assertEqual(self.refused().reason, "hpc_binding_missing")
        self.assertEqual(self.refused("controller_owned").reason, "authority_not_permitted")
        claim = self.access.claim(self.alice, self.nano4.id, "alice01")
        self.assertEqual(self.refused("user_owned").reason, "hpc_binding_unverified")
        attest(self.access, self.alice, self.nano4.id, "alice01", unix_uid=1001)
        decision = self.resolver.resolve(self.principal, self.project, self.nano4.id)
        self.assertEqual(decision.authority, "user_owned")
        assert decision.binding is not None
        self.assertEqual(decision.binding.id, claim.id)
        identity, principal = decision.user_identity()
        self.assertEqual((identity.kind, identity.user_id), ("user", self.alice))
        self.assertEqual(
            principal, HPCPrincipal("alice01", 1001, binding_id=str(claim.id), binding_version=1)
        )
        self.assertEqual(decision.pin(), AuthorityPin("user_owned", 1, 1))
        # Another User's binding is never used, and a non-human caller has none.
        bob = Principal(self.bob, "cli")
        self.assertEqual(self.refused(principal=bob).reason, "hpc_binding_missing")
        service = Principal(self.alice, "service")
        self.assertEqual(self.refused(principal=service).reason, "hpc_binding_not_allowed")
        # Revocation applies to the next resolution.
        self.access.revoke(claim.id, reason="left_project", operator="ops")
        self.assertEqual(self.refused().reason, "hpc_binding_missing")

    def test_unrequested_prefers_the_users_own_identity(self):
        self.enable_user_owned()
        self.assertEqual(self.refused().reason, "hpc_binding_missing")
        decision = self.resolver.resolve(
            self.principal, self.project, self.nano4.id, "controller_owned"
        )
        self.assertEqual((decision.authority, decision.origin), ("controller_owned", "explicit"))
        self.access.set_project_policy(self.project, self.nano4.id, [], **OPS)
        self.assertEqual(self.refused().reason, "no_authority_permitted")


class RegistryPinTests(_Database):
    """`begin_attempt` and the database pin and re-check the authority."""

    def setUp(self):
        super().setUp()
        accounts = SlurmAccountStore(self.db)
        self.account = accounts.register_account(self.nano4.id, "proj", **OPS)
        accounts.link_project(self.project, self.nano4.id, "proj", modes=["A"], **OPS)
        self.endpoint = endpoint_for(self.nano4, self.node, "A")
        self.registry = ExecutionRegistry(self.db)
        self.access.set_platform_policy(self.nano4.id, BOTH, **OPS)
        self.access.set_project_policy(self.project, self.nano4.id, BOTH, **OPS)
        self.binding = attest(self.access, self.alice, self.nano4.id, "alice01", unix_uid=1001)

    def principal(self, binding=None):
        binding = binding or self.binding
        return HPCPrincipal(binding.unix_user, binding.unix_uid, str(binding.id), binding.version)

    def begin(self, authority, principal, owner=None):
        execution, _ = self.registry.register(
            self.project, owner or self.alice, "batch", {"n": secrets.token_hex(4)}
        )
        return self.registry.begin_attempt(
            execution.execution_id,
            endpoint=self.endpoint,
            principal=principal,
            account="proj",
            slurm_account_id=self.account.id,
            slurm_account_origin="explicit",
            options={"partition": ResolvedOption.defaulted("dev", "platform")},
            authority=authority,
        )

    def test_attempts_pin_their_authority_and_binding(self):
        user = self.begin(AuthorityPin("user_owned", 1, 1), self.principal())
        controller = self.begin(AuthorityPin("controller_owned", 1, 1), HPCPrincipal("nestop"))
        self.assertEqual(user.authority, AuthorityPin("user_owned", 1, 1))
        self.assertEqual(user.principal.binding_id, str(self.binding.id))
        self.assertEqual(controller.authority, AuthorityPin("controller_owned", 1, 1))
        for column in (
            "execution_authority",
            "platform_authority_policy_version",
            "project_authority_policy_version",
            "hpc_binding_id",
        ):
            with (
                self.subTest(column=column),
                self.assertRaises(psycopg.errors.IntegrityConstraintViolation),
            ):
                self.conn.execute(
                    f"UPDATE execution_attempts SET {column} = NULL WHERE id = %s", (user.id,)
                )

    def test_a_stale_policy_or_a_revoked_binding_is_refused_in_the_attempt_transaction(self):
        # Resolved under version 1; the Project's policy moved to version 2.
        self.access.set_project_policy(self.project, self.nano4.id, ["user_owned"], **OPS)
        with self.assertRaises(AuthorityNotPermittedError):
            self.begin(AuthorityPin("user_owned", 1, 1), self.principal())
        with self.assertRaises(AuthorityNotPermittedError):
            self.begin(AuthorityPin("controller_owned", 1, 2), HPCPrincipal("nestop"))
        self.begin(AuthorityPin("user_owned", 1, 2), self.principal())
        self.access.revoke(self.binding.id, reason="left_project", operator="ops")
        with self.assertRaises(BindingNotVerifiedError):
            self.begin(AuthorityPin("user_owned", 1, 2), self.principal())

    def test_a_binding_is_never_used_for_another_user_or_as_the_controller(self):
        bob = attest(self.access, self.bob, self.nano4.id, "bob01")
        with self.assertRaises(BindingNotVerifiedError):
            self.begin(AuthorityPin("user_owned", 1, 1), self.principal(bob))
        with self.assertRaises(BindingNotVerifiedError):
            self.begin(AuthorityPin("user_owned", 1, 1), HPCPrincipal("alice01"))
        with self.assertRaises(ValueError):
            self.begin(AuthorityPin("controller_owned", 1, 1), self.principal())
        forged = HPCPrincipal("root", 0, str(self.binding.id), 1)
        with self.assertRaises(BindingNotVerifiedError):
            self.begin(AuthorityPin("user_owned", 1, 1), forged)

    def test_the_database_refuses_unpinned_or_inconsistent_attempts_for_every_writer(self):
        execution, _ = self.registry.register(self.project, self.alice, "batch", {})
        insert = (
            "INSERT INTO execution_attempts (execution_id, attempt_number, platform_id, "
            "login_node_id, login_node_host, transport_mode, unix_user, hpc_binding_id, "
            "hpc_binding_version, slurm_account_id, slurm_account, slurm_account_origin, "
            "execution_authority, platform_authority_policy_version, "
            "project_authority_policy_version) "
            "VALUES (%s, 1, %s, %s, 'h', 'A', %s, %s, %s, %s, 'proj', 'explicit', %s, %s, %s)"
        )
        claim = self.access.claim(self.bob, self.nano4.id, "bob01")
        base = (execution.execution_id, self.nano4.id, self.node.id)
        for label, values in (
            ("no authority", ("alice01", None, None, None, None, None)),
            ("stale policy", ("nestop", None, None, "controller_owned", 0, 0)),
            ("not permitted", ("alice01", None, None, "controller_owned", 1, 1)),
            ("unverified", ("bob01", str(claim.id), 1, "user_owned", 1, 1)),
            ("wrong uid name", ("root", str(self.binding.id), 1, "user_owned", 1, 1)),
            ("controller binding", ("alice01", str(self.binding.id), 1, "controller_owned", 1, 1)),
        ):
            if label == "not permitted":
                self.access.set_project_policy(self.project, self.nano4.id, ["user_owned"], **OPS)
                values = ("alice01", None, None, "controller_owned", 1, 2)
            if label in ("unverified", "wrong uid name"):
                values = (*values[:4], 1, 2)
            if label == "controller binding":
                self.access.set_project_policy(self.project, self.nano4.id, BOTH, **OPS)
                values = (*values[:4], 1, 3)
            unix_user, binding_id, version, authority, platform_v, project_v = values
            with self.subTest(label), self.assertRaises(psycopg.errors.CheckViolation):
                self.conn.execute(
                    insert,
                    (
                        *base,
                        unix_user,
                        binding_id,
                        version,
                        self.account.id,
                        authority,
                        platform_v,
                        project_v,
                    ),
                )
        self.assertEqual(
            self.registry.attempts(execution.execution_id, project_id=self.project), []
        )

    def test_history_keeps_the_original_principal_after_replacement(self):
        attempt = self.begin(AuthorityPin("user_owned", 1, 1), self.principal())
        self.registry.finish_attempt(attempt.id, "failed")
        replacement = attest(self.access, self.alice, self.nano4.id, "alice02", evidence_ref="T-2")
        self.access.set_project_policy(self.project, self.nano4.id, ["controller_owned"], **OPS)
        [kept] = self.registry.attempts(attempt.execution_id, project_id=self.project)
        self.assertEqual(kept.principal, self.principal())
        self.assertEqual(kept.authority, AuthorityPin("user_owned", 1, 1))
        self.assertNotEqual(kept.principal.binding_id, str(replacement.id))
        # The replaced version no longer authorizes anything.
        with self.assertRaises(AuthorityNotPermittedError):
            self.begin(AuthorityPin("user_owned", 1, 1), self.principal())


class AdmissionTests(_AdmissionBase):
    """Submissions through gRPC to the served Mode A Platform `nano` (Project `alpha`)."""

    def setUp(self):
        super().setUp()
        self.access_store = HPCAccessStore(self.db)

    def deployment(self, authorities=BOTH):
        """A Controller whose deployment permits `authorities`."""
        admission = RegistryAdmission(
            self.db,
            self.backend,
            self.profile,
            self.platform_id,
            audit=self.audit,
            authorities=authorities,
        )
        return self.serve(admission)

    def enable_user_owned(self, project_authorities=BOTH):
        self.access_store.set_platform_policy(self.platform_id, BOTH, **OPS)
        self.access_store.set_project_policy(
            self.project, self.platform_id, project_authorities, **OPS
        )

    def denied(self, reason):
        return [
            e
            for e in self.audit.events(event="execution.admit", limit=20)
            if e.outcome == "denied" and e.reason == reason
        ]

    def test_private_mode_a_regression_pins_controller_owned(self):
        response = self.submit()
        [attempt] = self.registry.list_for_project(self.project)
        [recorded] = self.registry.attempts(attempt.execution_id, project_id=self.project)
        self.assertEqual(recorded.slurm_job_id, response.job_id)
        self.assertEqual(recorded.authority, AuthorityPin("controller_owned", 0, 0))
        self.assertEqual(recorded.principal, HPCPrincipal("nestop"))
        [admitted] = [
            e
            for e in self.audit.events(event="execution.admit", limit=5)
            if e.outcome == "succeeded"
        ]
        self.assertEqual(admitted.details["execution_authority"], "controller_owned")
        self.assertEqual(admitted.details["authority_origin"], "policy_default")
        self.assertEqual(admitted.details["platform_authority_policy_version"], 0)

    def test_requesting_user_owned_without_policy_is_refused_not_downgraded(self):
        stub = self.deployment()
        error = self.refused(lambda: self.submit(stub, x_nest_execution_authority="user_owned"))
        self.assertEqual(error.code(), grpc.StatusCode.PERMISSION_DENIED)
        self.assertIn("authority_not_permitted", error.details())
        self.assert_nothing_sent()
        self.assertEqual(self.attempts(), [])
        self.assertEqual(len(self.denied("authority_not_permitted")), 1)
        # The default Controller does not permit it either, whatever the database says.
        self.enable_user_owned()
        error = self.refused(lambda: self.submit(x_nest_execution_authority="user_owned"))
        self.assertIn("authority_not_permitted", error.details())
        self.assert_nothing_sent()

    def test_user_owned_never_falls_back_to_the_controllers_credentials(self):
        # The Controller's Mode A credentials are configured and work (see the
        # regression test); user_owned still never uses them.
        self.enable_user_owned()
        stub = self.deployment()
        for outcome, prepare in (
            ("hpc_binding_missing", lambda: None),
            (
                "hpc_binding_unverified",
                lambda: self.access_store.claim(self.member, self.platform_id, "member01"),
            ),
            (
                "user_owned_transport_unavailable",
                lambda: attest(self.access_store, self.member, self.platform_id, "member01"),
            ),
        ):
            prepare()
            for extra in ({}, {"x_nest_execution_authority": "user_owned"}):
                with self.subTest(outcome=outcome, extra=extra):
                    error = self.refused(lambda extra=extra: self.submit(stub, **extra))
                    self.assertIn(outcome, error.details())
                    self.assertNotEqual(error.code(), grpc.StatusCode.OK)
        self.assert_nothing_sent()
        self.assertEqual(self.attempts(), [])
        [verified] = self.denied("user_owned_transport_unavailable")[:1]
        self.assertEqual(verified.details["execution_authority"], "user_owned")
        self.assertIn("hpc_binding_id", verified.details)
        # An explicit controller_owned request is a separate, permitted choice.
        self.submit(stub, x_nest_execution_authority="controller_owned")
        [attempt] = self.attempts()
        self.assertEqual(attempt["execution_authority"], "controller_owned")
        self.assertIsNone(attempt["hpc_binding_id"])
        self.assertEqual(attempt["unix_user"], "nestop")

    def test_default_submissions_are_refused_once_every_layer_permits_user_owned(self):
        # No shipped client sends x-nest-execution-authority. With both authorities
        # permitted on all three layers, such a submission resolves to user_owned and
        # is refused even with a verified binding: never controller_owned.
        self.enable_user_owned(BOTH)
        attest(self.access_store, self.member, self.platform_id, "member01")
        stub = self.deployment(BOTH)
        error = self.refused(lambda: self.submit(stub))
        self.assertEqual(error.code(), grpc.StatusCode.FAILED_PRECONDITION)
        self.assertIn("user_owned_transport_unavailable", error.details())
        error = self.refused(
            lambda: stub.Execute(
                execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((lambda: 1, (), {}))),
                timeout=60,
                metadata=self.metadata(),
            )
        )
        self.assertIn("user_owned_transport_unavailable", error.details())
        self.assert_nothing_sent()
        self.assertEqual(self.attempts(), [])
        # The same Project still runs Mode A on a deployment that does not permit it.
        self.submit()
        [attempt] = self.attempts()
        self.assertEqual(attempt["execution_authority"], "controller_owned")

    def test_a_user_owned_only_project_cannot_choose_controller_owned(self):
        self.enable_user_owned(["user_owned"])
        stub = self.deployment()
        error = self.refused(
            lambda: self.submit(stub, x_nest_execution_authority="controller_owned")
        )
        self.assertIn("authority_not_permitted", error.details())
        error = self.refused(lambda: self.submit(stub))
        self.assertIn("hpc_binding_missing", error.details())
        self.assert_nothing_sent()

    def test_policy_revocation_applies_to_the_next_submission_only(self):
        self.submit()
        self.access_store.set_project_policy(self.project, self.platform_id, [], **OPS)
        error = self.refused(self.submit)
        self.assertEqual(error.code(), grpc.StatusCode.PERMISSION_DENIED)
        self.assertIn("no_authority_permitted", error.details())
        # The earlier attempt keeps what it was admitted under.
        [attempt] = self.attempts()
        self.assertEqual(
            (
                attempt["execution_authority"],
                attempt["platform_authority_policy_version"],
                attempt["project_authority_policy_version"],
            ),
            ("controller_owned", 0, 0),
        )
        self.assertEqual(len(self.sbatch_calls()), 1)

    def test_invalid_authority_metadata_is_refused(self):
        for value, reason in (("root", "invalid_authority"), ("User-Owned!", None)):
            with self.subTest(value=value):
                error = self.refused(
                    lambda value=value: self.submit(x_nest_execution_authority=value)
                )
                self.assertEqual(error.code(), grpc.StatusCode.INVALID_ARGUMENT)
                if reason:
                    self.assertIn(reason, error.details())
        self.assert_nothing_sent()

    def test_project_and_platform_denials_come_first(self):
        self.enable_user_owned()
        attest(self.access_store, self.outsider, self.platform_id, "out01")
        stub = self.deployment()
        error = self.refused(
            lambda: self.submit(
                stub, token=self.token_outsider, x_nest_execution_authority="user_owned"
            )
        )
        # Not a member: the Project is not even visible to the caller.
        self.assertIn(error.code(), (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED))
        self.assertNotIn("user_owned_transport_unavailable", error.details())
        self.assertEqual(self.audit.events(event="execution.authority", limit=5), [])
        self.assert_nothing_sent()

    def test_the_idempotency_key_covers_the_requested_authority(self):
        self.submit(x_nest_idempotency_key="k", x_nest_execution_authority="controller_owned")
        # The same key with a different request (here: no authority asked for).
        error = self.refused(lambda: self.submit(x_nest_idempotency_key="k"))
        self.assertIn("idempotency_conflict", error.details())


class LegacyPathTests(_AdmissionBase):
    """The legacy Slurm path and the local executor run only as the Controller."""

    def setUp(self):
        super().setUp()
        self.access_store = HPCAccessStore(self.db)
        self.legacy = self.serve(None)

    def legacy_execute(self, **extra):
        return self.legacy.Execute(
            execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((lambda: 7, (), {}))),
            timeout=60,
            metadata=self.metadata(**extra),
        )

    def jobs_rows(self):
        return self.sql("SELECT execution_id FROM jobs")

    def test_ordinary_legacy_calls_are_unchanged(self):
        self.submit(self.legacy)
        self.submit(self.legacy, x_nest_execution_authority="controller_owned")
        self.assertEqual(cloudpickle.loads(self.legacy_execute().result), 7)
        self.assertEqual(len(self.sbatch_calls()), 3)
        self.assertEqual(self.attempts(), [])

    def test_a_user_owned_request_is_refused_before_anything_runs(self):
        for call in (
            lambda **kw: self.submit(self.legacy, **kw),
            lambda **kw: self.legacy_execute(**kw),
        ):
            error = self.refused(lambda call=call: call(x_nest_execution_authority="user_owned"))
            self.assertEqual(error.code(), grpc.StatusCode.PERMISSION_DENIED)
            self.assertIn("authority_not_permitted", error.details())
            error = self.refused(lambda call=call: call(x_nest_execution_authority="root"))
            self.assertEqual(error.code(), grpc.StatusCode.INVALID_ARGUMENT)
        duplicate = self.metadata() + [
            ("x-nest-execution-authority", "controller_owned"),
            ("x-nest-execution-authority", "user_owned"),
        ]
        error = self.refused(
            lambda: self.legacy.SubmitJob(
                execution_pb2.SubmitJobRequest(workdir=self.batch_dir(), script="hello.sbatch"),
                timeout=60,
                metadata=duplicate,
            )
        )
        self.assertEqual(error.code(), grpc.StatusCode.INVALID_ARGUMENT)
        self.assert_nothing_sent()
        self.assertEqual(self.jobs_rows(), [])
        denied = [
            e
            for e in self.audit.events(event="execution.authority", limit=20)
            if e.outcome == "denied"
        ]
        self.assertEqual(len(denied), 4)

    def test_restrictive_stored_policies_are_not_bypassed(self):
        for authorities in ([], ["user_owned"]):
            with self.subTest(authorities=authorities):
                self.access_store.set_project_policy(
                    self.project, self.platform_id, authorities, **OPS
                )
                for call in (lambda: self.submit(self.legacy), self.legacy_execute):
                    error = self.refused(call)
                    self.assertEqual(error.code(), grpc.StatusCode.PERMISSION_DENIED)
                    self.assertIn("authority_not_permitted", error.details())
        # The Platform layer applies too.
        self.access_store.set_project_policy(self.project, self.platform_id, BOTH, **OPS)
        self.access_store.set_platform_policy(self.platform_id, ["user_owned"], **OPS)
        error = self.refused(lambda: self.submit(self.legacy))
        self.assertIn("authority_not_permitted", error.details())
        self.assert_nothing_sent()
        self.assertEqual(self.jobs_rows(), [])
        # Restoring controller_owned restores the legacy path.
        self.access_store.set_platform_policy(self.platform_id, BOTH, **OPS)
        self.submit(self.legacy)
        self.assertEqual(len(self.sbatch_calls()), 1)

    def test_the_local_executor_refuses_user_owned_too(self):
        service = ExecutionService(None, executor="local")
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, None)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        request = execution_pb2.ExecuteRequest(payload=cloudpickle.dumps((lambda: 5, (), {})))
        with self.assertRaises(grpc.RpcError) as caught:
            stub.Execute(
                request, timeout=30, metadata=[("x-nest-execution-authority", "user_owned")]
            )
        self.assertEqual(
            cast(grpc.Call, caught.exception).code(), grpc.StatusCode.PERMISSION_DENIED
        )
        response = stub.Execute(request, timeout=30)
        self.assertEqual(cloudpickle.loads(response.result), 5)


class CommandLineTests(_Database):
    def nest_db(self, *args):
        out, err = StringIO(), StringIO()
        with (
            mock.patch.dict(os.environ, {"NEST_DATABASE_URL": self.url}),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = nest_db(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_operator_commands(self):
        ops = ["--operator", "ops", "--reason", "NCHC ticket 7"]
        code, out, _ = self.nest_db(
            "hpc-access", "set-platform-policy", "nano4", "--authorities", "user_owned", *ops
        )
        self.assertEqual(
            (code, out.strip()), (0, "Platform nano4 authority policy is now version 1")
        )
        code, out, _ = self.nest_db(
            "hpc-access",
            "set-project-policy",
            "nano4",
            "--project-id",
            str(self.project),
            "--authorities",
            "controller_owned,user_owned",
            *ops,
        )
        self.assertEqual(code, 0, out)
        code, out, _ = self.nest_db(
            "hpc-access", "show", "nano4", "--project-id", str(self.project)
        )
        self.assertEqual(code, 0)
        self.assertIn("in force for project", out)
        self.assertIn(": user_owned (platform v1, project v1", out)
        code, out, err = self.nest_db(
            "hpc-access",
            "attest",
            "nano4",
            "--user-id",
            str(self.alice),
            "--unix-user",
            "alice01",
            "--unix-uid",
            "1001",
            "--method",
            "nchc_attestation",
            "--evidence-ref",
            "NCHC-7",
            *ops,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("status=verified", out)
        binding = self.access.current_binding(self.alice, self.nano4.id)
        assert binding is not None
        code, out, _ = self.nest_db("hpc-access", "bindings", "nano4")
        self.assertIn(f"binding {binding.id}", out)
        code, out, err = self.nest_db(
            "hpc-access", "revoke", str(binding.id), "--revoked-reason", "left_project", *ops
        )
        self.assertEqual(code, 0, err)
        self.assertIn("status=revoked", out)
        [revoked] = self.audit.events(event="hpc_binding.revoked", limit=5)
        self.assertEqual((revoked.actor_operator, revoked.note), ("ops", "NCHC ticket 7"))
        code, _, err = self.nest_db(
            "hpc-access", "set-platform-policy", "nano4", "--authorities", "mode_b", *ops
        )
        self.assertEqual(code, 2, err)
        code, _, err = self.nest_db(
            "hpc-access", "revoke", str(binding.id), "--revoked-reason", "again", *ops
        )
        self.assertEqual(code, 1)


class MigrationTests(PostgresTestCase):
    def test_upgrade_keeps_earlier_attempts_unpinned_and_refuses_unpinned_writers(self):
        url = self.database()
        migrations = load_migrations()
        at = [m.label for m in migrations].index("0019_hpc_access_policy")
        with connect(url) as conn:
            upgrade(conn, migrations[:at])
        db = Database(load_database_config({"NEST_DATABASE_URL": url}))
        db.open()
        self.addCleanup(db.close)
        owner = IdentityStore(db).create_user(display_name="o").id
        project = ProjectStore(db).create_project("p", created_by_user_id=owner).id
        platforms = PlatformStore(db)
        platform = platforms.create_platform("nano4", "Nano 4", **OPS)
        node = platforms.add_login_node(platform.id, "login.example", transport_modes=["A"], **OPS)
        platforms.grant_project(platform.id, project, **OPS)
        account = SlurmAccountStore(db).register_account(platform.id, "proj", **OPS)
        SlurmAccountStore(db).link_project(project, platform.id, "proj", modes=["A"], **OPS)
        registry = ExecutionRegistry(db)

        def begin(**kw):
            execution, _ = registry.register(project, owner, "batch", {"n": secrets.token_hex(4)})
            return registry.begin_attempt(
                execution.execution_id,
                endpoint=endpoint_for(platform, node, "A"),
                principal=HPCPrincipal("nestop"),
                account="proj",
                slurm_account_id=account.id,
                slurm_account_origin="explicit",
                options={},
                **kw,
            )

        # Written by the 0018 code path: no authority columns exist yet.
        before = begin()
        self.assertIsNone(before.authority)
        with connect(url) as conn:
            self.assertEqual(
                [m.label for m in upgrade(conn, migrations[: at + 1])], ["0019_hpc_access_policy"]
            )
        [kept] = registry.attempts(before.execution_id, project_id=project)
        self.assertIsNone(kept.authority)  # not reinterpreted
        # Its lifecycle continues.
        registry.record_submission(before.id, 7)
        registry.finish_attempt(before.id, "succeeded")
        # A writer that does not pin an authority is refused after the upgrade.
        with self.assertRaises(DatabaseError):
            begin()
        after = begin(authority=AuthorityPin("controller_owned", 0, 0))
        self.assertEqual(after.authority, AuthorityPin("controller_owned", 0, 0))


class ConfigurationTests(unittest.TestCase):
    BASE: ClassVar[dict[str, str]] = {
        "NEST_EXECUTOR": "slurm",
        "NEST_SLURM_HOST": "login",
        "NEST_SLURM_ACCOUNT": "acct",
        "NEST_SLURM_WORKDIR": "/w",
        "NEST_SLURM_SSH_USER": "op",
        "NEST_DATABASE_URL": "postgresql://nest@db.example.internal:5432/nest",
        "NEST_CONTROLLER_BIND": "127.0.0.1:50051",
    }

    def load(self, **settings):
        return load_controller_config({**self.BASE, **settings})

    def problems(self, **settings) -> str:
        with self.assertRaises(ConfigError) as caught:
            self.load(**settings)
        return str(caught.exception)

    def test_execution_authorities(self):
        config = self.load()
        self.assertEqual(config.execution_authorities, ("controller_owned",))
        self.assertEqual(config.summary()["execution_authorities"], ["controller_owned"])
        both = self.load(NEST_EXECUTION_AUTHORITIES="user_owned, controller_owned")
        self.assertEqual(both.execution_authorities, EXECUTION_AUTHORITIES)
        with (
            mock.patch("nest.controller.environment_platform_id"),
            mock.patch("nest.controller.RegistryAdmission") as admission,
        ):
            build_admission(both, mock.Mock(), mock.Mock(), mock.Mock())
        self.assertEqual(admission.call_args.kwargs["authorities"], EXECUTION_AUTHORITIES)
        for bad in ("mode_b", "user_owned,user_owned", "A,B", ","):
            with self.subTest(bad=bad):
                self.assertIn(
                    "NEST_EXECUTION_AUTHORITIES", self.problems(NEST_EXECUTION_AUTHORITIES=bad)
                )
        legacy = self.problems(
            NEST_EXECUTION_REGISTRY="legacy", NEST_EXECUTION_AUTHORITIES="user_owned"
        )
        self.assertIn("needs NEST_EXECUTION_REGISTRY=required", legacy)
        self.assertEqual(
            self.load(
                NEST_EXECUTION_REGISTRY="legacy", NEST_EXECUTION_AUTHORITIES="controller_owned"
            ).execution_authorities,
            ("controller_owned",),
        )


if __name__ == "__main__":
    unittest.main()
