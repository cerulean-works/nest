# Security audit log

`nest.audit` writes and reads the append-only `security_audit_events` table. See
[Review the security audit log](../audit-log) for what is recorded, how records are
redacted and sealed, and `nest-db audit`.

Record an event from Controller code:

```python
from nest.audit import AuditLog

AuditLog(database).record(
    "run.submit",
    "succeeded",
    actor_user_id=principal.user_id,
    session_id=principal.session_id,
    project_id=project_id,
    details={"rpc": "Execute", "gpu": 1},
    required=True,  # refuse the action if its record cannot be written
)
```

[[autodoc]] audit.AuditLog

[[autodoc]] audit.SecurityAuditEvent

[[autodoc]] audit.ChainStatus

[[autodoc]] audit.redact_details

[[autodoc]] audit.audit_interface
