"""Shared fixtures for the hosted-profile, Project-selection and gateway tests.

A throwaway CA and server certificates, a recording Controller (with or without
TLS) that answers `ListEligibleProjects` and `Execute`, stored CLI sessions, and a
patch that points the hosted connection profile at a local server. Nothing here
contacts a real host.
"""

from __future__ import annotations

import datetime
import os
import secrets
import tempfile
import unittest
import uuid
from concurrent import futures
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from unittest import mock

import cloudpickle
import grpc
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from nest import config as nest_config
from nest.login import CredentialStore, NestSession
from nest.proto import execution_pb2, execution_pb2_grpc


def session_for(auth_url: str, **overrides) -> NestSession:
    now = datetime.datetime.now(UTC)
    values = {
        "auth_url": auth_url,
        "user_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "scope": "jobs:run projects:read",
        "access_token": "access-" + secrets.token_urlsafe(24),
        "refresh_token": "refresh-" + secrets.token_urlsafe(24),
        "access_expires_at": now + timedelta(minutes=15),
        "refresh_expires_at": now + timedelta(hours=24),
        "session_expires_at": now + timedelta(days=7),
    }
    values.update(overrides)
    return NestSession(**values)


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _pem_key(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


class CertificateAuthority:
    """A throwaway CA valid for one day; issues server certificates for DNS names."""

    def __init__(self, name: str = "Nest test CA"):
        self.key = _key()
        now = datetime.datetime.now(UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        self.certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            # Python 3.13+ verifies with VERIFY_X509_STRICT, which needs these.
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False
            )
            .sign(self.key, hashes.SHA256())
        )

    @property
    def pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)

    def issue(self, *names: str) -> tuple[bytes, bytes]:
        """A server certificate chain and key for `names`, as PEM."""
        key = _key()
        now = datetime.datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(self.certificate.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False
            )
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
            )
            .sign(self.key, hashes.SHA256())
        )
        chain = certificate.public_bytes(serialization.Encoding.PEM) + self.pem
        return chain, _pem_key(key)


class RecordingController(execution_pb2_grpc.ExecutionServiceServicer):
    """
    Records each call's method and metadata. Lists `projects` as eligible, requires
    a session (and a Project) when `require_session`, and runs `Execute` payloads.
    """

    def __init__(self, *, require_session: bool = True, projects=()):
        self.require_session = require_session
        self.projects = list(projects)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def _record(self, method, context) -> dict[str, str]:
        metadata = {k: v for k, v in context.invocation_metadata() if isinstance(v, str)}
        self.calls.append((method, metadata))
        if self.require_session and "authorization" not in metadata:
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "Authentication required")
        return metadata

    def ListEligibleProjects(self, request, context):
        self._record("ListEligibleProjects", context)
        return execution_pb2.ListEligibleProjectsResponse(
            projects=[
                # (id, name) or (id, name, role); the role defaults to member.
                execution_pb2.EligibleProject(id=p[0], name=p[1], role=(p[2:] or ("member",))[0])
                for p in self.projects
            ],
            projects_required=self.require_session,
        )

    def Execute(self, request, context):
        metadata = self._record("Execute", context)
        if self.require_session and "x-nest-project-id" not in metadata:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Name the Project")
        func, args, kwargs = cloudpickle.loads(request.payload)
        return execution_pb2.ExecuteResponse(result=cloudpickle.dumps(func(*args, **kwargs)))

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def serve(
    case: unittest.TestCase,
    servicer: execution_pb2_grpc.ExecutionServiceServicer,
    *,
    tls: tuple[bytes, bytes] | None = None,
) -> int:
    """Serves `servicer` on loopback (TLS with `(chain, key)`); returns its port."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    execution_pb2_grpc.add_ExecutionServiceServicer_to_server(servicer, server)
    if tls is None:
        port = server.add_insecure_port("127.0.0.1:0")
    else:
        chain, key = tls
        port = server.add_secure_port("127.0.0.1:0", grpc.ssl_server_credentials([(key, chain)]))
    server.start()
    case.addCleanup(server.stop, None)
    return port


class HostedCase(unittest.TestCase):
    """Private credentials and preferences files, and an environment without Nest settings."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        os.chmod(directory.name, 0o700)
        self.home = Path(directory.name)
        self.credentials = str(self.home / "nest" / "credentials.json")
        self.preferences = str(self.home / "nest" / "preferences.json")
        self.store = CredentialStore(self.credentials)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("NEST_")}
        clean["XDG_CONFIG_HOME"] = str(self.home)
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def hosted_at(self, port: int, roots: bytes | None, *, host: str = "localhost"):
        """Points the hosted profile at `host:port`, trusting `roots` (tests only)."""
        profile = replace(
            nest_config.HOSTED_PROFILE, address=f"{host}:{port}", root_certificates=roots
        )
        patcher = mock.patch.object(nest_config, "HOSTED_PROFILE", profile)
        patcher.start()
        self.addCleanup(patcher.stop)
        return profile
