# Email delivery

`nest.mail` holds the optional email transport: the `EmailSender` interface, its
SMTP implementation, message templates, and the Controller's outbox delivery worker.
See [Send email with SMTP](../email) for configuration, TLS, retries, and operations.

Queue a message in the same transaction as the record it is about, so it is sent
only if that record commits:

```python
from nest.mail import INVITATION_KIND, render_invitation

if email.enabled:  # the Controller's EmailService
    message = render_invitation(email.config, token=token, project_name="Physics", role="Member")
    with database.connection() as conn:
        invitation_id = create_pending_invitation(conn, ...)  # business code
        email.queue(
            message_key=f"invitation:{invitation_id}:send:1",
            kind=INVITATION_KIND,
            recipient=address,
            subject=message.subject,
            body_text=message.body_text,
            subject_ref=f"invitation:{invitation_id}",
            conn=conn,
        )
else:
    ...  # show the recipient-bound link for manual sharing
```

## Service

[[autodoc]] mail.start_email_service

[[autodoc]] mail.EmailService

[[autodoc]] mail.OutboxWorker

## Transport

[[autodoc]] mail.EmailSender

[[autodoc]] mail.SMTPEmailSender

[[autodoc]] mail.OutgoingEmail

[[autodoc]] mail.DeliveryError

[[autodoc]] mail.classify

[[autodoc]] mail.tls_context

## Templates

[[autodoc]] mail.render_invitation

[[autodoc]] mail.RenderedEmail

[[autodoc]] mail.invitation_link

[[autodoc]] mail.public_link

## Outbox

[[autodoc]] db.EmailOutbox

[[autodoc]] db.DeliveryStatus

[[autodoc]] db.QueuedEmail
