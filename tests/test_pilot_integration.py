"""H07 + F02a together: the guarded listener, durable reservations, revocation and audit.

Serves the real `ExecutionService` on `controller.build_server` with its `RpcGuard`,
session authentication and a limit-enforcing `RegistryAdmission`, against a real
PostgreSQL and the `FakeHpc` login node (see `test_registry_admission._Base`).

Checks that the controls compose without double accounting or a fail-open path:
a Provider revocation refuses before anything is reserved, rate-limited and refused
calls reserve nothing, every reservation is released exactly once however many
times its final state is observed, and the audit log records each decision with
its actor, Project and reason but no credential.
"""

from datetime import timedelta
from typing import cast

import grpc
from test_admission_limits import LIMITS, _LimitsBase

from nest.admission import RegistryAdmission
from nest.config import ControllerConfig, RpcPolicy
from nest.controller import ExecutionService, build_server
from nest.db import ProviderApprovalStore
from nest.proto import execution_pb2, execution_pb2_grpc
from nest.rpc_guard import RpcGuard


class PilotIntegrationTests(_LimitsBase):
    def setUp(self):
        super().setUp()
        self.admission = RegistryAdmission(
            self.db,
            self.backend,
            self.profile,
            self.platform_id,
            audit=self.audit,
            limits=LIMITS,
            provider_revocations="nano",
        )
        policy = RpcPolicy(user_rate=60)
        guard = RpcGuard(policy)
        self.guard = guard
        service = ExecutionService(
            self.backend,
            executor="slurm",
            jobs=self.jobs,
            access=self.access,
            admission=self.admission,
            guard=guard,
            local_execution=False,
        )
        server = build_server(ControllerConfig(rpc=policy), guard)
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, None)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        self.stub = execution_pb2_grpc.ExecutionServiceStub(channel)

    def status(self, job_id):
        return self.stub.GetJobStatus(
            execution_pb2.JobStatusRequest(job_id=job_id), timeout=30, metadata=self.metadata()
        ).state

    def test_security_reservation_revocation_and_audit_compose(self):
        self.hpc.hold_jobs("RUNNING")
        first = self.batch(gpu=1)
        second = self.batch(gpu=1)
        self.assertEqual(len(self.held()), 2)

        # At the limit: refused, nothing sent, nothing reserved.
        error = self.refused(lambda: self.batch(gpu=1))
        self.assertEqual(error.code(), grpc.StatusCode.RESOURCE_EXHAUSTED)
        self.assertEqual(len(self.sbatch_calls()), 2)

        # The Provider revokes the Project: the next submission of another member
        # is refused in the reserving transaction, and existing reservations stand.
        ProviderApprovalStore(self.db).revoke("nano", self.project, operator="ops", reason="t")
        error = self.refused(lambda: self.batch(token=self.token_owner))
        self.assertEqual(error.code(), grpc.StatusCode.PERMISSION_DENIED)
        self.assertIn("provider_denied", error.details())
        self.assertEqual(len(self.held()), 2)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM execution_reservations"), [{"n": 2}])

        # A finished job is observed twice (a status query and the sweeper), and is
        # released once; the other stays held.
        self.finish(first.job_id)
        self.assertEqual(self.status(first.job_id), "COMPLETED")
        self.admission.settle(timedelta(0))
        self.assertEqual(self.status(first.job_id), "COMPLETED")
        [left] = self.held()
        self.assertEqual(left["gpu"], 1)
        self.assertEqual(
            self.registry.held_reservations(), {"active": 1, "cpu": 0, "gpu": 1, "unknown": 0}
        )
        # Cancelling one's own Run still works after the revocation.
        self.stub.CancelJob(
            execution_pb2.CancelJobRequest(job_id=second.job_id),
            timeout=30,
            metadata=self.metadata(),
        )
        self.admission.settle(timedelta(0))
        self.assertEqual(self.held(), [])

        # The audit log tells the story without a credential.
        admits = sorted(self.audit.events(event="execution.admit"), key=lambda e: e.id)
        self.assertEqual(
            [(e.outcome, e.reason) for e in admits],
            [
                ("succeeded", None),
                ("succeeded", None),
                ("denied", "limit_active_per_user"),
                ("denied", "provider_denied"),
            ],
        )
        self.assertTrue(all(e.project_id == self.project for e in admits))
        self.assertEqual(admits[0].details["gpu"], 1)
        text = repr([e.details for e in self.audit.events(limit=1000)])
        for token in (self.token_member, self.token_owner):
            self.assertNotIn(token, text)

    def test_rate_limited_and_unauthenticated_calls_reserve_nothing(self):
        self.guard.users.rate = 6
        self.guard.users.capacity = 5.0
        self.guard.users.per_second = 0.1
        codes = []
        for _ in range(8):
            try:
                self.batch()
            except grpc.RpcError as exc:
                codes.append(cast(grpc.Call, exc).code())
        self.assertIn(grpc.StatusCode.RESOURCE_EXHAUSTED, codes)
        sent = len(self.sbatch_calls())
        self.assertEqual(
            self.sql("SELECT count(*) AS n FROM execution_reservations"), [{"n": sent}]
        )
        self.refused(lambda: self.batch(token="nope"))
        self.assertEqual(len(self.sbatch_calls()), sent)

    def test_local_execution_stays_blocked_for_an_authenticated_listener(self):
        service = ExecutionService(executor="local", access=self.access, local_execution=False)
        server = build_server(ControllerConfig(), RpcGuard(RpcPolicy()))
        execution_pb2_grpc.add_ExecutionServiceServicer_to_server(service, server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        self.addCleanup(server.stop, None)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        self.addCleanup(channel.close)
        stub = execution_pb2_grpc.ExecutionServiceStub(channel)
        error = self.refused(
            lambda: stub.Execute(
                execution_pb2.ExecuteRequest(payload=b"not even a pickle"),
                timeout=30,
                metadata=self.metadata(),
            )
        )
        self.assertEqual(error.code(), grpc.StatusCode.FAILED_PRECONDITION)
