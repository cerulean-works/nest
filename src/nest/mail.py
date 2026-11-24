"""
Optional outbound email: the `EmailSender` interface, its SMTP transport, message
templates, and the Controller's outbox delivery worker.

Email is never required. With `NEST_EMAIL_ENABLED` unset the Controller creates no
sender and no worker and never opens an SMTP connection; invitations and Invite
Links still work and are shared manually. When enabled, business code queues
messages in the persistent outbox ([`db.EmailOutbox`]) inside its own transaction,
and [`mail.OutboxWorker`] delivers them with bounded retries. A delivery failure only
ever updates the outbox row.

Nothing in this module logs a recipient address, a message body, an invitation
link or token, or an SMTP credential.
"""

from __future__ import annotations

import smtplib
import ssl
import threading
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate
from typing import Protocol
from urllib.parse import quote

from ._logging import get_logger, register_secret
from .config import INVITATION_PAGE_PATH, EmailConfig, SMTPConfig
from .db.connection import DatabaseError
from .db.outbox import EmailOutbox, QueuedEmail

logger = get_logger("nest.mail")

INVITATION_KIND = "project_invitation"
INVITATION_PATH = INVITATION_PAGE_PATH


class DeliveryError(Exception):
    """
    A message could not be delivered.

    Attributes:
        description (`str`): Error class and SMTP reply code, such as
            `SMTPRecipientsRefused code=550`. Never contains an address, the
            server's reply text, or a credential.
        permanent (`bool`): `True` when retrying cannot succeed (an SMTP `5xx`
            rejection of the sender, recipient or content).
    """

    def __init__(self, description: str, *, permanent: bool = False):
        super().__init__(description)
        self.description = description
        self.permanent = permanent


@dataclass(frozen=True)
class OutgoingEmail:
    """
    One plain-text message.

    Attributes:
        recipient (`str`): One address.
        subject (`str`): Subject line.
        body_text (`str`): Body.
        message_id (`str`): `Message-ID` header, stable across attempts.
    """

    recipient: str
    subject: str
    body_text: str
    message_id: str

    def __repr__(self) -> str:
        return f"OutgoingEmail(message_id={self.message_id!r})"


class EmailSender(Protocol):
    """
    Transport that hands one message to a mail service.

    A future provider (such as an HTTP API) implements this interface without
    changing the outbox or invitation logic.
    """

    def send(self, message: OutgoingEmail) -> None:
        """
        Delivers `message`, or raises [`mail.DeliveryError`].

        Returning means the service accepted the message.
        """
        ...


def tls_context(config: SMTPConfig) -> ssl.SSLContext:
    """
    Returns the client TLS context for SMTP: certificate and host name checks on,
    TLS 1.2 or newer, and the system roots or `NEST_SMTP_CA_FILE` / `NEST_SMTP_CA`.
    """
    context = ssl.create_default_context(cafile=config.ca_file, cadata=config.ca_data)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _code_error(name: str, code: int | None) -> DeliveryError:
    suffix = f" code={code}" if code is not None else ""
    return DeliveryError(name + suffix, permanent=code is not None and 500 <= code < 600)


def classify(exc: BaseException) -> DeliveryError:
    """
    Turns an `smtplib`, TLS or network exception into a [`mail.DeliveryError`]
    without copying its text, which can echo addresses or server replies.

    Authentication and TLS failures are transient: they are operator configuration
    problems that a fixed credential or certificate resolves, within the bounded
    attempt count.
    """
    name = type(exc).__name__
    if isinstance(exc, DeliveryError):
        return exc
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [code for code, _ in exc.recipients.values()]
        return _code_error(name, codes[0] if codes else None)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return DeliveryError(f"{name} code={exc.smtp_code}")
    if isinstance(exc, (smtplib.SMTPSenderRefused, smtplib.SMTPDataError)):
        return _code_error(name, exc.smtp_code)
    if isinstance(exc, smtplib.SMTPResponseException):
        # Other replies (such as 421 or 554 to the greeting) concern the session.
        return DeliveryError(f"{name} code={exc.smtp_code}")
    if isinstance(exc, ssl.SSLCertVerificationError):
        return DeliveryError(f"{name}: {exc.verify_message}")
    if isinstance(exc, ssl.SSLError):
        return DeliveryError(f"{name}: {exc.reason}")
    if isinstance(exc, (smtplib.SMTPException, OSError)):
        return DeliveryError(name)
    return DeliveryError(name)


class SMTPEmailSender:
    """
    Sends mail over SMTP with STARTTLS or implicit TLS; never in plaintext.

    With STARTTLS the connection fails, rather than continuing unencrypted, when
    the server does not offer the extension. Credentials are sent only after TLS
    is established and the certificate verified. The password is re-read from
    `NEST_SMTP_PASSWORD_FILE` on each connection and registered with Nest's log
    redaction.

    Args:
        config ([`config.EmailConfig`]): Validated, enabled email settings.
    """

    def __init__(self, config: EmailConfig):
        self.config = config
        self.smtp = config.smtp
        if self.smtp.password is not None:
            register_secret(self.smtp.password.reveal())

    def build(self, message: OutgoingEmail) -> EmailMessage:
        """Returns the MIME message, from the configured verified sender."""
        mime = EmailMessage()
        mime["From"] = self.config.sender
        mime["To"] = message.recipient
        mime["Subject"] = message.subject
        mime["Date"] = formatdate(localtime=False, usegmt=True)
        mime["Message-ID"] = message.message_id
        mime["Auto-Submitted"] = "auto-generated"
        body = message.body_text
        plain = body.isascii() and all(len(line) <= 998 for line in body.splitlines())
        # 7bit keeps links intact for readers that show the raw text.
        mime.set_content(body, cte="7bit" if plain else None)
        return mime

    def _connect(self) -> smtplib.SMTP:
        context = tls_context(self.smtp)
        implicit = self.smtp.tls_mode == "implicit"
        if implicit:
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self.smtp.host, self.smtp.port, timeout=self.smtp.timeout, context=context
            )
        else:
            client = smtplib.SMTP(self.smtp.host, self.smtp.port, timeout=self.smtp.timeout)
        try:
            client.ehlo()
            if not implicit:
                if not client.has_extn("starttls"):
                    raise DeliveryError("STARTTLSNotOffered")
                client.starttls(context=context)
                client.ehlo()
        except BaseException:
            client.close()
            raise
        return client

    def send(self, message: OutgoingEmail) -> None:
        """
        Delivers one message.

        Raises:
            [`mail.DeliveryError`]: When the connection, TLS handshake, login or
                transaction fails.
        """
        mime = self.build(message)
        try:
            client = self._connect()
            try:
                if self.smtp.username is not None:
                    password = self.smtp.read_password() or ""
                    register_secret(password)
                    client.login(self.smtp.username, password)
                refused = client.send_message(
                    mime, from_addr=self.config.sender_address, to_addrs=[message.recipient]
                )
                if refused:
                    codes = [code for code, _ in refused.values()]
                    raise _code_error("SMTPRecipientsRefused", codes[0])
            finally:
                try:
                    client.quit()
                except smtplib.SMTPException, OSError:
                    client.close()
        except (smtplib.SMTPException, OSError, DeliveryError) as exc:
            raise classify(exc) from None


def public_link(public_url: str, path: str, fragment: str | None = None) -> str:
    """
    Returns an absolute link under `NEST_PUBLIC_URL`.

    Args:
        public_url (`str`): Base URL, without a trailing slash.
        path (`str`): Absolute path, such as `/invitations/accept`.
        fragment (`str`, *optional*): URL fragment. Browsers never send it to a
            server, so a token placed here stays out of proxy and access logs.
    """
    if not path.startswith("/"):
        raise ValueError("path must start with /")
    link = public_url.rstrip("/") + quote(path)
    return f"{link}#{fragment}" if fragment else link


def invitation_link(public_url: str, token: str) -> str:
    """Returns the recipient-bound invitation link for an invite token."""
    return public_link(public_url, INVITATION_PATH, fragment="token=" + quote(token, safe=""))


@dataclass(frozen=True)
class RenderedEmail:
    """Subject and plain-text body produced by a template."""

    subject: str
    body_text: str

    def __repr__(self) -> str:
        return "RenderedEmail(***)"


def _one_line(value: str, limit: int = 200) -> str:
    return " ".join(value.split())[:limit]


def render_invitation(
    config: EmailConfig,
    *,
    token: str,
    project_name: str,
    role: str,
    inviter_name: str | None = None,
    expires: str | None = None,
) -> RenderedEmail:
    """
    Renders a Project invitation from the configured sender and public URL.

    Values are flattened to one line so a Project name cannot add headers or
    forge content. The link carries the token in its fragment.

    Args:
        config ([`config.EmailConfig`]): Enabled email settings.
        token (`str`): Raw invite token. It appears only in the body.
        project_name (`str`): Project display name.
        role (`str`): Role granted on acceptance, such as `Member`.
        inviter_name (`str`, *optional*): Who sent the invitation.
        expires (`str`, *optional*): Human-readable expiry, such as a date.

    Returns:
        [`mail.RenderedEmail`]: Subject and body.
    """
    project = _one_line(project_name) or "a Nest Project"
    inviter = _one_line(inviter_name) if inviter_name else "A Project administrator"
    lines = [
        f"{inviter} invited you to join the Nest Project {project!r} as {_one_line(role, 40)}.",
        "",
        "Accept the invitation by signing in with this email address:",
        invitation_link(config.public_url, token),
        "",
    ]
    if expires:
        lines += [f"The invitation expires {_one_line(expires, 80)}.", ""]
    lines += [
        "If you did not expect this invitation, ignore this message.",
        "",
        f"Sent by Nest ({config.public_url}) from {config.sender_address}.",
    ]
    return RenderedEmail(
        subject=f"Invitation to join {project} on Nest"[:200], body_text="\n".join(lines) + "\n"
    )


class OutboxWorker:
    """
    Delivers due outbox messages in a background thread.

    Each scan claims up to `batch_size` due messages and sends each once. Every
    attempt is logged with the outbox ID, kind, attempt number and error class,
    never the recipient or content. Database outages are logged and retried on the
    next scan; they never stop the Controller.

    Args:
        outbox ([`db.EmailOutbox`]): Outbox to deliver from.
        sender ([`mail.EmailSender`]): Transport.
        poll_interval (`float`, *optional*): Seconds between scans.
        batch_size (`int`, *optional*): Messages claimed per scan.
    """

    def __init__(
        self,
        outbox: EmailOutbox,
        sender: EmailSender,
        *,
        poll_interval: float = 15.0,
        batch_size: int = 10,
    ):
        self.outbox = outbox
        self.sender = sender
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def deliver_once(self) -> dict[str, int]:
        """
        Attempts every due message once.

        Returns:
            `dict[str, int]`: Counts of `sent`, `retrying` and `failed` messages, and
                `superseded` ones whose claim expired before the result was recorded
                (another worker owns them now, so nothing was written).
        """
        result = {"sent": 0, "retrying": 0, "failed": 0, "superseded": 0}
        for message in self.outbox.claim(self.batch_size):
            result[self._deliver(message)] += 1
        return result

    def _deliver(self, message: QueuedEmail) -> str:
        try:
            self.sender.send(
                OutgoingEmail(
                    recipient=message.recipient,
                    subject=message.subject,
                    body_text=message.body_text,
                    message_id=message.message_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - every failure is recorded, then retried
            error = classify(exc)
            status = self.outbox.mark_failed(message, error.description, permanent=error.permanent)
            if status is None:
                return "superseded"
            if status.status == "failed":
                logger.error(
                    "Email delivery failed outbox_id=%s kind=%s attempt=%s/%s error=%s "
                    "kind_of_failure=%s; queue it again to retry",
                    message.id,
                    message.kind,
                    message.attempts,
                    message.max_attempts,
                    error.description,
                    "permanent" if error.permanent else "transient",
                )
                return "failed"
            logger.warning(
                "Email delivery attempt failed outbox_id=%s kind=%s attempt=%s/%s error=%s "
                "retry_in_s=%.0f",
                message.id,
                message.kind,
                message.attempts,
                message.max_attempts,
                error.description,
                self.outbox.retry_wait(message.attempts),
            )
            return "retrying"
        if not self.outbox.mark_sent(message):
            # Another worker owns the message now; at-least-once delivery means it
            # may be sent again, with the same Message-ID.
            return "superseded"
        logger.info(
            "Email sent outbox_id=%s kind=%s attempt=%s/%s",
            message.id,
            message.kind,
            message.attempts,
            message.max_attempts,
        )
        return "sent"

    def wake(self) -> None:
        """Starts the next scan now, such as right after a message is queued."""
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.deliver_once()
            except DatabaseError as exc:
                logger.error("Email outbox scan failed: %s", exc)
            except Exception:
                logger.exception("Email outbox scan failed unexpectedly")
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def start(self) -> None:
        """Starts the delivery thread."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="nest-email-outbox", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        """Stops the delivery thread after its current message."""
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)


class EmailService:
    """
    Entry point for business code that may send mail.

    When email is disabled, [`mail.EmailService.enabled`] is `False`,
    [`mail.EmailService.queue`] refuses, and nothing contacts an SMTP server; callers
    such as invitation creation then offer the manually shared link instead.

    Args:
        config ([`config.EmailConfig`] or `None`): Enabled settings, or `None`.
        outbox ([`db.EmailOutbox`] or `None`): Outbox when enabled.
        worker ([`mail.OutboxWorker`] or `None`): Delivery worker when enabled.
    """

    def __init__(
        self,
        config: EmailConfig | None = None,
        outbox: EmailOutbox | None = None,
        worker: OutboxWorker | None = None,
    ):
        self.config = config
        self.outbox = outbox
        self.worker = worker

    @property
    def enabled(self) -> bool:
        """Whether messages can be queued for delivery."""
        return self.config is not None and self.outbox is not None

    def queue(self, **kwargs):
        """
        Queues a message with [`db.EmailOutbox.enqueue`] and wakes the worker.

        When `conn` is given the worker is woken now but only sees the message once
        that transaction commits.

        Raises:
            `RuntimeError`: If email delivery is disabled.
        """
        if self.outbox is None:
            raise RuntimeError("Email delivery is disabled; share the invitation link manually")
        status = self.outbox.enqueue(**kwargs)
        if self.worker is not None:
            self.worker.wake()
        return status


def start_email_service(config: EmailConfig | None, database) -> EmailService:
    """
    Creates the Controller's email service.

    Never connects to the SMTP server: a mail outage cannot stop startup, and the
    first connection happens when a message is due.

    Args:
        config ([`config.EmailConfig`] or `None`): Email settings, or `None` when
            disabled.
        database ([`db.Database`] or `None`): Open database; required when enabled.

    Returns:
        [`mail.EmailService`]: A disabled service, or one with a running worker.
    """
    if config is None:
        logger.info("Email delivery disabled; invitation links are shared manually")
        return EmailService()
    if database is None:
        raise RuntimeError("Email delivery needs the database outbox")
    outbox = EmailOutbox(
        database,
        config.sender_address.rpartition("@")[2],
        max_attempts=config.max_attempts,
        retry_delay=config.retry_delay,
        retry_max_delay=config.retry_max_delay,
        lease=max(300.0, 4 * config.smtp.timeout),
    )
    worker = OutboxWorker(outbox, SMTPEmailSender(config), poll_interval=config.poll_interval)
    worker.start()
    logger.info(
        "Email delivery enabled provider=%s host=%s port=%s tls=%s from=%s",
        config.provider,
        config.smtp.host,
        config.smtp.port,
        config.smtp.tls_mode,
        config.sender_address,
    )
    return EmailService(config, outbox, worker)
