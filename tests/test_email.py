"""Optional SMTP email delivery and the persistent outbox (A07).

Configuration, template and transport tests need no database: the transport tests
use the in-process fake SMTP server in `fake_smtp` with real TLS, never a real mail
provider. Outbox and Controller tests also need the PostgreSQL server named by
`NEST_TEST_DATABASE_URL` (see `postgres_fixture`).
"""

import atexit
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fake_smtp import FakeSMTPServer, make_certificates
from postgres_fixture import PostgresTestCase, connect
from test_controller import controller_environment

from nest._logging import _RedactingFormatter
from nest.config import ConfigError, DatabaseConfig, EmailConfig, load_controller_config
from nest.db import Database, DatabaseError, DeliveryStatus, EmailOutbox, upgrade
from nest.db.outbox import message_id_for, normalize_recipient
from nest.mail import (
    DeliveryError,
    EmailService,
    OutboxWorker,
    OutgoingEmail,
    SMTPEmailSender,
    invitation_link,
    render_invitation,
    start_email_service,
)

PASSWORD = "smtp-secret-Pa55word"
TOKEN = "inviteTOKEN-9f2c1d7e5b"
RECIPIENT = "invitee.private@example.org"
DATABASE_URL = "postgresql://nest@db.example.internal/nest"
CERTS = make_certificates()
# Not unittest.addModuleCleanup: its list is shared by every module, so under plain
# `unittest` (as in the Controller image) it would run after the first module.
atexit.register(CERTS.cleanup)


def private_file(directory: Path, name: str, text: str) -> str:
    path = directory / name
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def email_settings(**overrides: str) -> dict[str, str]:
    """Enabled email settings that pass validation."""
    return {
        "NEST_EMAIL_ENABLED": "true",
        "NEST_EMAIL_PROVIDER": "smtp",
        "NEST_EMAIL_FROM": "Nest <nest@example.com>",
        "NEST_PUBLIC_URL": "https://nest.example.com",
        "NEST_SMTP_HOST": "localhost",
        "NEST_SMTP_USERNAME": "nest@example.com",
        "NEST_SMTP_PASSWORD": PASSWORD,
        "NEST_SMTP_CA_FILE": CERTS.ca,
        "NEST_DATABASE_URL": DATABASE_URL,
        **overrides,
    }


def enabled(settings: dict[str, str]) -> EmailConfig:
    """Loads settings that must enable email."""
    config = load_controller_config(settings).email
    assert config is not None
    return config


def email_config(**overrides: str) -> EmailConfig:
    return enabled(email_settings(**overrides))


def database_config(url: str, **settings: str) -> DatabaseConfig:
    config = load_controller_config({"NEST_DATABASE_URL": url, **settings}).database
    assert config is not None
    return config


class CapturedLogs:
    """Collects every `nest` record, formatted by Nest's redacting formatter."""

    def __init__(self, case: unittest.TestCase):
        logger = logging.getLogger("nest")
        self.records: list[str] = []
        capture = logging.Handler()
        capture.setFormatter(_RedactingFormatter("%(levelname)s %(name)s %(message)s"))
        capture.emit = lambda record: self.records.append(capture.format(record))
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(capture)
        case.addCleanup(logger.removeHandler, capture)
        case.addCleanup(logger.setLevel, previous)

    @property
    def text(self) -> str:
        return "\n".join(self.records)


class EmailConfigTests(unittest.TestCase):
    def setUp(self):
        self.files = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.files)

    def problems(self, settings) -> str:
        with self.assertRaises(ConfigError) as caught:
            load_controller_config(settings)
        text = str(caught.exception)
        self.assertNotIn(PASSWORD, text)
        return text

    def test_email_is_disabled_by_default(self):
        config = load_controller_config({})
        self.assertIsNone(config.email)
        self.assertIsNone(config.summary()["email"])

    def test_smtp_settings_are_ignored_while_disabled(self):
        # Even invalid leftovers cannot stop a Controller that does not send mail.
        for enabled in ("", "false", "0"):
            with self.subTest(enabled=enabled):
                config = load_controller_config(
                    {
                        "NEST_EMAIL_ENABLED": enabled,
                        "NEST_SMTP_HOST": "bad host!",
                        "NEST_SMTP_PORT": "nope",
                        "NEST_SMTP_STARTTLS": "false",
                        "NEST_SMTP_PASSWORD": PASSWORD,
                    }
                )
                self.assertIsNone(config.email)

    def test_enabled_email_reports_every_missing_setting(self):
        text = self.problems({"NEST_EMAIL_ENABLED": "true"})
        for name in ("NEST_EMAIL_FROM", "NEST_PUBLIC_URL", "NEST_SMTP_HOST", "NEST_DATABASE_URL"):
            self.assertIn(name, text)

    def test_valid_settings_default_to_starttls_on_587(self):
        config = email_config()
        self.assertEqual(config.provider, "smtp")
        self.assertEqual(config.sender, "Nest <nest@example.com>")
        self.assertEqual(config.sender_address, "nest@example.com")
        self.assertEqual((config.smtp.port, config.smtp.tls_mode), (587, "starttls"))
        self.assertEqual(config.max_attempts, 5)

    def test_provider_must_be_smtp(self):
        self.assertIn(
            "NEST_EMAIL_PROVIDER", self.problems(email_settings(NEST_EMAIL_PROVIDER="ses"))
        )

    def test_sender_must_be_one_valid_address(self):
        for sender in ("nest", "a@b.co, c@d.co", "Nest <nest@localhost>", "<>"):
            with self.subTest(sender=sender):
                text = self.problems(email_settings(NEST_EMAIL_FROM=sender))
                self.assertIn("NEST_EMAIL_FROM", text)

    def test_public_url_must_be_https_for_links(self):
        for url in ("http://nest.example.com", "grpcs://nest.example.com", "https://x.com/#a"):
            with self.subTest(url=url):
                self.assertIn("NEST_PUBLIC_URL", self.problems(email_settings(NEST_PUBLIC_URL=url)))
        local = email_config(NEST_PUBLIC_URL="http://localhost:8080/")
        self.assertEqual(local.public_url, "http://localhost:8080")

    def test_plaintext_transport_is_refused(self):
        text = self.problems(email_settings(NEST_SMTP_STARTTLS="false"))
        self.assertIn("never sends mail over an unencrypted connection", text)

    def test_implicit_tls_is_an_explicit_alternative(self):
        config = email_config(NEST_SMTP_IMPLICIT_TLS="true")
        self.assertEqual((config.smtp.tls_mode, config.smtp.port), ("implicit", 465))
        config = email_config(
            NEST_SMTP_IMPLICIT_TLS="true", NEST_SMTP_STARTTLS="false", NEST_SMTP_PORT="2465"
        )
        self.assertEqual((config.smtp.tls_mode, config.smtp.port), ("implicit", 2465))
        text = self.problems(email_settings(NEST_SMTP_IMPLICIT_TLS="true", NEST_SMTP_STARTTLS="1"))
        self.assertIn("Set only one of", text)

    def test_port_timeout_and_retry_bounds_are_validated(self):
        text = self.problems(
            email_settings(
                NEST_SMTP_PORT="70000",
                NEST_SMTP_TIMEOUT="0",
                NEST_EMAIL_MAX_ATTEMPTS="0",
                NEST_EMAIL_RETRY_DELAY="600",
                NEST_EMAIL_RETRY_MAX_DELAY="60",
            )
        )
        for name in (
            "NEST_SMTP_PORT",
            "NEST_SMTP_TIMEOUT",
            "NEST_EMAIL_MAX_ATTEMPTS",
            "NEST_EMAIL_RETRY_MAX_DELAY",
        ):
            self.assertIn(name, text)
        self.assertIn(
            "NEST_EMAIL_MAX_ATTEMPTS", self.problems(email_settings(NEST_EMAIL_MAX_ATTEMPTS="101"))
        )

    def test_credentials_come_in_pairs_and_from_one_source(self):
        no_password = email_settings()
        del no_password["NEST_SMTP_PASSWORD"]
        self.assertIn("NEST_SMTP_USERNAME needs a password", self.problems(no_password))
        no_user = email_settings()
        del no_user["NEST_SMTP_USERNAME"]
        self.assertIn("needs NEST_SMTP_USERNAME", self.problems(no_user))
        secret = private_file(self.files, "smtp-password", PASSWORD + "\n")
        both = email_settings(NEST_SMTP_PASSWORD_FILE=secret)
        self.assertIn("Set only one of NEST_SMTP_PASSWORD", self.problems(both))
        anonymous = email_settings()
        del anonymous["NEST_SMTP_USERNAME"], anonymous["NEST_SMTP_PASSWORD"]
        self.assertIsNone(enabled(anonymous).smtp.username)

    def test_password_file_is_read_on_each_use_so_rotation_applies(self):
        secret = private_file(self.files, "smtp-password", PASSWORD + "\n")
        settings = email_settings(NEST_SMTP_PASSWORD_FILE=secret)
        del settings["NEST_SMTP_PASSWORD"]
        config = enabled(settings)
        self.assertIsNone(config.smtp.password)
        self.assertEqual(config.smtp.read_password(), PASSWORD)
        Path(secret).write_text("rotated-password-2\n")
        self.assertEqual(config.smtp.read_password(), "rotated-password-2")

    def test_password_file_must_exist_and_be_non_empty(self):
        settings = email_settings(NEST_SMTP_PASSWORD_FILE=str(self.files / "missing"))
        del settings["NEST_SMTP_PASSWORD"]
        self.assertIn("NEST_SMTP_PASSWORD_FILE", self.problems(settings))
        settings["NEST_SMTP_PASSWORD_FILE"] = private_file(self.files, "empty", "\n")
        self.assertIn("is empty", self.problems(settings))
        self.assertIn(
            "NEST_SMTP_PASSWORD_FILE must be an absolute path",
            self.problems({**settings, "NEST_SMTP_PASSWORD_FILE": "relative"}),
        )

    def test_password_never_appears_in_repr_or_summary(self):
        config = load_controller_config(email_settings())
        self.assertNotIn(PASSWORD, repr(config))
        self.assertNotIn(PASSWORD, str(config.summary()))
        summary = enabled(email_settings()).summary()
        self.assertEqual(summary["smtp_password"], "***")
        self.assertEqual(summary["from"], "nest@example.com")


class TemplateTests(unittest.TestCase):
    def test_invitation_uses_the_public_url_and_keeps_the_token_out_of_the_path(self):
        config = email_config(NEST_PUBLIC_URL="https://nest.example.com/app/")
        link = invitation_link(config.public_url, "a/b c")
        self.assertEqual(link, "https://nest.example.com/app/invitations/accept#token=a%2Fb%20c")

    def test_rendered_invitation_cannot_inject_headers(self):
        config = email_config()
        rendered = render_invitation(
            config,
            token=TOKEN,
            project_name="Physics\r\nBcc: attacker@example.net",
            role="Member",
            inviter_name="Ada\nLovelace",
            expires="2026-12-01",
        )
        self.assertNotIn("\n", rendered.subject)
        self.assertIn("Physics Bcc: attacker@example.net", rendered.subject)
        self.assertIn(
            f"https://nest.example.com/invitations/accept#token={TOKEN}", rendered.body_text
        )
        self.assertIn("Ada Lovelace invited you", rendered.body_text)
        self.assertIn("nest@example.com", rendered.body_text)
        self.assertNotIn(TOKEN, repr(rendered))

    def test_built_message_comes_from_the_configured_sender(self):
        sender = SMTPEmailSender(email_config())
        mime = sender.build(OutgoingEmail(RECIPIENT, "Hello", "Body\n", "<id@example.com>"))
        self.assertEqual(mime["From"], "Nest <nest@example.com>")
        self.assertEqual(mime["To"], RECIPIENT)
        self.assertEqual(mime["Message-ID"], "<id@example.com>")
        self.assertNotIn(RECIPIENT, repr(OutgoingEmail(RECIPIENT, "s", "b", "<id@x.co>")))

    def test_recipients_are_validated_without_echoing_them(self):
        self.assertEqual(normalize_recipient(" Ada@Example.ORG "), "Ada@example.org")
        for value in ("not-an-address", f"{RECIPIENT}\r\nBcc: x@y.co", "a@b.co, c@d.co"):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                normalize_recipient(value)
            self.assertNotIn(value, str(caught.exception))

    def test_message_ids_are_stable_per_key(self):
        first = message_id_for("invitation:1:send:1", "example.com")
        self.assertEqual(first, message_id_for("invitation:1:send:1", "example.com"))
        self.assertNotEqual(first, message_id_for("invitation:1:send:2", "example.com"))
        self.assertTrue(first.endswith("@example.com>"))


class SMTPTransportTests(unittest.TestCase):
    """`SMTPEmailSender` against the fake server with real TLS."""

    def server(self, mode="starttls", **state) -> FakeSMTPServer:
        state.setdefault("username", "nest@example.com")
        state.setdefault("password", PASSWORD)
        server = FakeSMTPServer(CERTS, mode, **state)
        server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        return server

    def sender(self, server: FakeSMTPServer, **overrides: str) -> SMTPEmailSender:
        return SMTPEmailSender(
            email_config(NEST_SMTP_PORT=str(server.port), NEST_SMTP_TIMEOUT="5", **overrides)
        )

    def send(self, sender: SMTPEmailSender) -> None:
        sender.send(OutgoingEmail(RECIPIENT, "Invitation", f"Link {TOKEN}\n", "<m1@example.com>"))

    def failure(self, sender: SMTPEmailSender) -> DeliveryError:
        with self.assertRaises(DeliveryError) as caught:
            self.send(sender)
        error = caught.exception
        for private in (RECIPIENT, PASSWORD, TOKEN):
            self.assertNotIn(private, error.description)
        self.assertIsNone(error.__cause__)
        return error

    def test_starttls_delivery_authenticates_only_after_tls(self):
        server = self.server()
        self.send(self.sender(server))
        [message] = server.messages
        self.assertTrue(message.tls)
        self.assertEqual(message.authenticated_as, "nest@example.com")
        self.assertEqual(message.mail_from, "nest@example.com")
        self.assertEqual(message.recipients, [RECIPIENT])
        self.assertIn("From: Nest <nest@example.com>", message.data)
        self.assertIn("Message-ID: <m1@example.com>", message.data)
        commands = server.state.commands
        self.assertLess(commands.index("STARTTLS"), commands.index("AUTH"))

    def test_implicit_tls_delivery(self):
        server = self.server("implicit")
        self.send(self.sender(server, NEST_SMTP_IMPLICIT_TLS="true"))
        [message] = server.messages
        self.assertTrue(message.tls)
        self.assertNotIn("STARTTLS", server.state.commands)

    def test_password_file_is_used(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        server = self.server()
        settings = email_settings(
            NEST_SMTP_PORT=str(server.port),
            NEST_SMTP_PASSWORD_FILE=private_file(directory, "pw", PASSWORD + "\n"),
        )
        del settings["NEST_SMTP_PASSWORD"]
        self.send(SMTPEmailSender(enabled(settings)))
        self.assertEqual(len(server.messages), 1)

    def test_missing_starttls_is_never_downgraded(self):
        server = self.server("plain")
        error = self.failure(self.sender(server))
        self.assertEqual(error.description, "STARTTLSNotOffered")
        self.assertFalse(error.permanent)
        self.assertNotIn("AUTH", server.state.commands)
        self.assertFalse(any(c.startswith("MAIL") for c in server.state.commands))

    def test_untrusted_certificate_is_rejected(self):
        server = self.server()
        error = self.failure(self.sender(server, NEST_SMTP_CA_FILE=CERTS.other_ca))
        self.assertIn("SSLCertVerificationError", error.description)
        self.assertEqual(server.messages, [])
        self.assertNotIn("AUTH", server.state.commands)

    def test_system_roots_do_not_trust_the_test_ca(self):
        server = self.server()
        settings = email_settings(NEST_SMTP_PORT=str(server.port))
        del settings["NEST_SMTP_CA_FILE"]
        error = self.failure(SMTPEmailSender(enabled(settings)))
        self.assertIn("SSLCertVerificationError", error.description)

    def test_host_name_mismatch_is_rejected(self):
        server = self.server()
        error = self.failure(self.sender(server, NEST_SMTP_HOST="127.0.0.1"))
        self.assertIn("mismatch", error.description)
        self.assertEqual(server.messages, [])

    def test_implicit_tls_also_verifies_the_certificate(self):
        server = self.server("implicit")
        sender = self.sender(
            server, NEST_SMTP_IMPLICIT_TLS="true", NEST_SMTP_CA_FILE=CERTS.other_ca
        )
        self.assertIn("SSLCertVerificationError", self.failure(sender).description)

    def test_wrong_password_is_transient_and_not_echoed(self):
        server = self.server(password="something-else-entirely")
        error = self.failure(self.sender(server))
        self.assertEqual(error.description, "SMTPAuthenticationError code=535")
        self.assertFalse(error.permanent)

    def test_recipient_rejections_are_classified_by_code(self):
        server = self.server()
        sender = self.sender(server)
        server.fail("RCPT", 550, 450)
        permanent = self.failure(sender)
        self.assertEqual(permanent.description, "SMTPRecipientsRefused code=550")
        self.assertTrue(permanent.permanent)
        transient = self.failure(sender)
        self.assertFalse(transient.permanent)
        self.send(sender)
        self.assertEqual(len(server.messages), 1)

    def test_data_and_session_failures(self):
        server = self.server()
        sender = self.sender(server)
        server.fail("DATA", 554)
        self.assertTrue(self.failure(sender).permanent)
        server.fail("MAIL", 451)
        self.assertEqual(self.failure(sender).description, "SMTPSenderRefused code=451")
        server.fail("EHLO", 421)
        self.assertFalse(self.failure(sender).permanent)

    def test_connection_failures_are_transient(self):
        server = self.server(drop_connections=1)
        self.assertFalse(self.failure(self.sender(server)).permanent)
        refused = SMTPEmailSender(email_config(NEST_SMTP_PORT="1", NEST_SMTP_TIMEOUT="2"))
        self.assertFalse(self.failure(refused).permanent)

    def test_transport_logs_no_secrets(self):
        logs = CapturedLogs(self)
        server = self.server(password="other-password-value")
        self.failure(self.sender(server))
        for private in (PASSWORD, RECIPIENT, TOKEN):
            self.assertNotIn(private, logs.text)


class DisabledServiceTests(unittest.TestCase):
    def test_disabled_service_never_queues_or_connects(self):
        service = start_email_service(None, None)
        self.assertFalse(service.enabled)
        self.assertIsNone(service.worker)
        with self.assertRaises(RuntimeError):
            service.queue(message_key="k", kind="test", recipient=RECIPIENT, subject="s",
                          body_text="b")  # fmt: skip

    def test_enabled_service_needs_a_database(self):
        with self.assertRaises(RuntimeError):
            start_email_service(email_config(), None)


INVITATIONS_TABLE = """
CREATE TABLE test_invitations (
    id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status text NOT NULL DEFAULT 'PENDING'
)
"""
"""Stand-in for B08's invitations, to show delivery never changes them."""


class OutboxTests(PostgresTestCase):
    def setUp(self):
        super().setUp()
        self.url = self.database()
        with connect(self.url) as conn:
            upgrade(conn)
            conn.execute(INVITATIONS_TABLE)
        self.db = Database(database_config(self.url, NEST_DATABASE_POOL_MIN_SIZE="0"))
        self.db.open()
        self.addCleanup(self.db.close)
        self.outbox = EmailOutbox(self.db, "example.com", max_attempts=3, retry_delay=0.05,
                                  retry_max_delay=0.2)  # fmt: skip
        self.server = FakeSMTPServer(CERTS, username="nest@example.com", password=PASSWORD)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.sender = SMTPEmailSender(
            email_config(NEST_SMTP_PORT=str(self.server.port), NEST_SMTP_TIMEOUT="5")
        )
        self.worker = OutboxWorker(self.outbox, self.sender, poll_interval=0.05)

    def invite(self, key: str = "invitation:1:send:1", **fields) -> int:
        """Creates a PENDING stand-in invitation and queues its email in one transaction."""
        with self.db.connection() as conn:
            row = conn.execute(
                "INSERT INTO test_invitations DEFAULT VALUES RETURNING id"
            ).fetchone()
            assert row is not None
            invitation_id = row[0]
            self.outbox.enqueue(
                message_key=key,
                kind="project_invitation",
                recipient=RECIPIENT,
                subject="Invitation to join Physics on Nest",
                body_text=f"https://nest.example.com/invitations/accept#token={TOKEN}\n",
                subject_ref=f"invitation:{invitation_id}",
                conn=conn,
                **fields,
            )
        return invitation_id

    def status(self, key: str = "invitation:1:send:1") -> DeliveryStatus:
        status = self.outbox.status(key)
        assert status is not None
        return status

    def invitation_status(self, invitation_id: int) -> str:
        with connect(self.url) as conn:
            return conn.execute(
                "SELECT status FROM test_invitations WHERE id = %s", (invitation_id,)
            ).fetchone()[0]

    def row(self, key: str = "invitation:1:send:1") -> dict:
        with connect(self.url) as conn, conn.cursor() as cursor:
            cursor.execute("SELECT * FROM email_outbox WHERE message_key = %s", (key,))
            names = [column.name for column in cursor.description]
            return dict(zip(names, cursor.fetchone(), strict=True))

    def make_due(self) -> None:
        with connect(self.url) as conn:
            conn.execute("UPDATE email_outbox SET next_attempt_at = now()")

    def test_queued_message_is_delivered_and_its_body_cleared(self):
        invitation = self.invite()
        self.assertEqual(self.worker.deliver_once(), {"sent": 1, "retrying": 0, "failed": 0})
        [message] = self.server.messages
        self.assertEqual(message.recipients, [RECIPIENT])
        status = self.status()
        self.assertEqual((status.status, status.attempts), ("sent", 1))
        self.assertIsNotNone(status.sent_at)
        row = self.row()
        self.assertIsNone(row["body_text"])
        self.assertIn(row["message_id"], message.data)
        self.assertEqual(self.invitation_status(invitation), "PENDING")
        self.assertEqual(self.worker.deliver_once(), {"sent": 0, "retrying": 0, "failed": 0})

    def test_enqueue_is_idempotent(self):
        self.invite()
        again = self.outbox.enqueue(
            message_key="invitation:1:send:1",
            kind="project_invitation",
            recipient=RECIPIENT,
            subject="Different",
            body_text="Different",
        )
        self.assertEqual(again.status, "pending")
        self.assertEqual(self.outbox.counts()["pending"], 1)
        self.worker.deliver_once()
        self.assertEqual(len(self.server.messages), 1)

    def test_rolled_back_transaction_queues_nothing(self):
        with self.assertRaises(RuntimeError), self.db.connection() as conn:
            self.outbox.enqueue(
                message_key="invitation:9:send:1",
                kind="project_invitation",
                recipient=RECIPIENT,
                subject="s",
                body_text="b",
                conn=conn,
            )
            raise RuntimeError("invitation insert failed")
        self.assertIsNone(self.outbox.status("invitation:9:send:1"))

    def test_status_never_exposes_the_recipient_or_body(self):
        invitation = self.invite()
        [status] = self.outbox.statuses(f"invitation:{invitation}")
        text = repr(status)
        for private in (RECIPIENT, TOKEN, "Physics"):
            self.assertNotIn(private, text)
        self.assertFalse(hasattr(status, "recipient"))
        self.assertTrue(status.retriable)

    def test_transient_failures_retry_with_backoff_then_fail(self):
        invitation = self.invite()
        self.server.fail("RCPT", 450, 450, 450)
        self.assertEqual(self.worker.deliver_once()["retrying"], 1)
        status = self.status()
        self.assertEqual((status.status, status.attempts), ("pending", 1))
        self.assertEqual(status.last_error, "SMTPRecipientsRefused code=450")
        self.assertEqual(status.last_error_kind, "transient")
        self.assertGreater(status.next_attempt_at, status.updated_at)
        self.assertEqual(self.outbox.claim(), [], "not due until the backoff passes")
        # Backoff doubles per attempt, bounded by retry_max_delay.
        self.assertEqual([self.outbox.retry_wait(n) for n in (1, 2, 3, 9)], [0.05, 0.1, 0.2, 0.2])

        self.make_due()
        self.worker.deliver_once()
        self.make_due()
        self.assertEqual(self.worker.deliver_once()["failed"], 1)
        status = self.status()
        self.assertEqual((status.status, status.attempts), ("failed", 3))
        self.assertTrue(status.retriable)
        self.make_due()
        self.assertEqual(self.outbox.claim(), [], "a failed message is not retried by itself")
        self.assertEqual(self.server.messages, [])
        # The invitation stays pending throughout; delivery never touches it.
        self.assertEqual(self.invitation_status(invitation), "PENDING")
        self.assertIsNotNone(self.row()["body_text"], "kept so it can be retried")

    def test_permanent_failure_stops_at_once_and_can_be_retriggered_once(self):
        invitation = self.invite()
        self.server.fail("RCPT", 550)
        self.assertEqual(self.worker.deliver_once()["failed"], 1)
        status = self.status()
        self.assertEqual((status.status, status.attempts), ("failed", 1))
        self.assertEqual(status.last_error_kind, "permanent")

        self.assertTrue(self.outbox.retry("invitation:1:send:1"))
        self.assertEqual(self.status().attempts, 0)
        self.assertEqual(self.worker.deliver_once()["sent"], 1)
        self.assertFalse(self.outbox.retry("invitation:1:send:1"), "sent messages stay sent")
        self.worker.deliver_once()
        self.assertEqual(len(self.server.messages), 1)
        self.assertEqual(self.invitation_status(invitation), "PENDING")
        self.assertFalse(self.outbox.retry("unknown:key"))

    def test_concurrent_workers_never_claim_the_same_message(self):
        for n in range(6):
            self.invite(f"invitation:{n}:send:1")
        claims: list[list[int]] = []

        def claim():
            claims.append([m.id for m in self.outbox.claim(limit=3)])

        threads = [threading.Thread(target=claim) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        ids = [i for batch in claims for i in batch]
        self.assertEqual(len(ids), 6)
        self.assertEqual(len(set(ids)), 6)

    def test_an_interrupted_claim_is_retried_until_out_of_attempts(self):
        self.invite(max_attempts=2)
        self.outbox.claim()
        expired = "UPDATE email_outbox SET locked_until = now() - interval '1 second'"
        with connect(self.url) as conn:
            conn.execute(expired)
        [again] = self.outbox.claim()
        self.assertEqual(again.attempts, 2)
        with connect(self.url) as conn:
            conn.execute(expired)
        self.assertEqual(self.outbox.claim(), [])
        status = self.status()
        self.assertEqual((status.status, status.last_error), ("failed", "DeliveryInterrupted"))

    def test_background_worker_delivers_after_wake(self):
        service = EmailService(email_config(), self.outbox, self.worker)
        self.worker.start()
        self.addCleanup(self.worker.stop, 5)
        service.queue(
            message_key="invitation:5:send:1",
            kind="project_invitation",
            recipient=RECIPIENT,
            subject="Invitation",
            body_text=f"Token {TOKEN}\n",
        )
        deadline = time.monotonic() + 10
        while not self.server.messages and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.server.messages), 1)

    def test_database_outage_does_not_stop_the_worker(self):
        logs = CapturedLogs(self)
        self.db.close()
        with self.assertRaises(DatabaseError):
            self.worker.deliver_once()
        self.worker.start()
        time.sleep(0.2)
        self.worker.stop(5)
        self.assertIn("Email outbox scan failed", logs.text)

    def test_delivery_logs_are_observable_but_private(self):
        logs = CapturedLogs(self)
        self.invite()
        self.server.fail("RCPT", 450)
        self.worker.deliver_once()
        self.make_due()
        self.worker.deliver_once()
        text = logs.text
        self.assertIn("Email queued outbox_id=", text)
        self.assertIn("Email delivery attempt failed", text)
        self.assertIn("error=SMTPRecipientsRefused code=450", text)
        self.assertIn("Email sent outbox_id=", text)
        for private in (RECIPIENT, TOKEN, PASSWORD):
            self.assertNotIn(private, text)


class ControllerProcess(unittest.TestCase):
    """Runs `nest.controller` and checks its output for private values."""

    def run_until_listening(self, env, *, until="Controller listening", timeout=30) -> str:
        process = subprocess.Popen(
            [sys.executable, "-m", "nest.controller"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines = []
        deadline = time.monotonic() + timeout
        try:
            assert process.stdout is not None
            for line in process.stdout:
                lines.append(line)
                if until in line or time.monotonic() > deadline:
                    break
        finally:
            process.terminate()
            rest, _ = process.communicate(timeout=15)
        output = "".join(lines) + rest
        self.assertIn(until, output)
        for private in (PASSWORD, RECIPIENT, TOKEN):
            self.assertNotIn(private, output)
        return output

    def environment(self, **settings):
        env = controller_environment(NEST_CONTROLLER_BIND="127.0.0.1:0", **settings)
        for name in [n for n in env if n.startswith(("NEST_EMAIL_", "NEST_SMTP_"))]:
            if name not in settings:
                env.pop(name)
        return env


class ControllerEmailTests(ControllerProcess):
    """The Controller with email disabled or invalid: no database needed."""

    def test_disabled_email_starts_without_smtp_or_database(self):
        output = self.run_until_listening(
            self.environment(NEST_SMTP_HOST="unused.invalid", NEST_SMTP_PASSWORD=PASSWORD)
        )
        self.assertIn("Email delivery disabled", output)
        self.assertIn("email=disabled", output)

    def test_invalid_enabled_email_is_a_config_error(self):
        result = subprocess.run(
            [sys.executable, "-m", "nest.controller"],
            env=self.environment(
                NEST_EMAIL_ENABLED="true",
                NEST_SMTP_STARTTLS="false",
                NEST_SMTP_PASSWORD=PASSWORD,
            ),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("NEST_EMAIL_FROM", result.stderr)
        self.assertIn("unencrypted", result.stderr)
        self.assertNotIn(PASSWORD, result.stderr)


class ControllerDeliveryTests(PostgresTestCase, ControllerProcess):
    """The Controller with email enabled and a migrated database."""

    def test_unreachable_smtp_does_not_block_startup(self):
        url = self.database()
        with connect(url) as conn:
            upgrade(conn)
        output = self.run_until_listening(
            self.environment(
                **email_settings(
                    NEST_DATABASE_URL=url, NEST_SMTP_HOST="localhost", NEST_SMTP_PORT="1"
                )
            )
        )
        self.assertIn("Email delivery enabled provider=smtp host=localhost port=1", output)
        self.assertIn("email=enabled", output)

    def test_controller_delivers_a_queued_invitation(self):
        url = self.database()
        with connect(url) as conn:
            upgrade(conn)
        database = Database(database_config(url))
        with database:
            EmailOutbox(database, "example.com").enqueue(
                message_key="invitation:7:send:1",
                kind="project_invitation",
                recipient=RECIPIENT,
                subject="Invitation",
                body_text=f"Accept: https://nest.example.com/invitations/accept#token={TOKEN}\n",
            )
        with FakeSMTPServer(CERTS, username="nest@example.com", password=PASSWORD) as server:
            directory = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, directory)
            settings = email_settings(
                NEST_DATABASE_URL=url,
                NEST_SMTP_PORT=str(server.port),
                NEST_SMTP_PASSWORD_FILE=private_file(directory, "pw", PASSWORD),
                NEST_EMAIL_POLL_INTERVAL="0.2",
            )
            del settings["NEST_SMTP_PASSWORD"]
            output = self.run_until_listening(self.environment(**settings), until="Email sent")
            [message] = server.messages
        self.assertIn(TOKEN, message.data)
        self.assertIn("Email sent outbox_id=1 kind=project_invitation attempt=1/5", output)
        with connect(url) as conn:
            self.assertEqual(
                conn.execute("SELECT status, body_text FROM email_outbox").fetchone(),
                ("sent", None),
            )


if __name__ == "__main__":
    os.environ.setdefault("NEST_LOG_LEVEL", "INFO")
    unittest.main()
