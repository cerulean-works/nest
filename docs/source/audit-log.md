# Review the security audit log

With a database (see [Store Controller records in PostgreSQL](database)), Nest
records every security-relevant event in one append-only table,
`security_audit_events`, from every interface. It complements the Project history
in `project_audit_events` (see [Audit history](projects#audit-history)) and the
platform role history in `platform_audit_events` (see
[Bootstrap and recover platform administration](platform-admin)).

## What is recorded

| Event | Written by | When |
| --- | --- | --- |
| `run.submit` | Controller | Every `Execute` and `SubmitJob` call by an authenticated caller, allowed or refused. An allowed one is recorded and sealed into the hash chain before anything runs, and refused if its record cannot be written or sealed. |
| `run.cancel` | Controller | Every `CancelJob` call, allowed or refused; an allowed one is recorded and sealed before Slurm is asked. |
| `run.read` | Controller | A refused `GetJobStatus` call. Allowed reads are not recorded. |
| `project.*`, `member.*` | Controller | A refused Projects API action, such as `project.update` or `member.invite`. Allowed changes are in `project_audit_events`. |
| `controller.started` | Controller | Each start: which protections are on (`auth`, `tls`, `allow_plaintext`, the number of approved Projects), the executor, the default Provider, and whether an inline SSH key is used. Never a value. |
| `identity.linked` | Trigger | A sign-in identity is linked to a User (the issuer only). |
| `session.created`, `session.revoked` | Trigger | A sign-in (browser or CLI, with its scope) and every revocation with its reason, such as `logout`, `refresh_reuse`, `user_disabled` or `admin_revoked`. Routine refreshes are not recorded. |
| `user.disabled`, `user.enabled` | Trigger | A User's status changes. |
| `cli_login.approved`, `cli_login.denied` | Trigger | A CLI sign-in is decided in the browser, with the approving session. |
| `invitation.created`, `.token_issued`, `.accepted`, `.declined`, `.revoked`, `.expired` | Trigger | Each step of an email invitation, with its role and actor. |
| `invitation.email_queued`, `invitation.email_requeued` | Trigger | An invitation email is queued, sent again, or a failed one queued again. |
| `invitation.email_failed` | Trigger | The outbox gave up on an invitation email (outcome `failed`), with the error class and SMTP code and whether it was permanent. |
| `invite_link.created`, `.redeemed`, `.revoked`, `.exhausted` | Trigger | Each step of an Invite Link, with its role and use limit; `exhausted` when its last use is taken. |
| `invitation.accept`, `.decline`, `.manage`, `.list`, `.preview`, `invite_link.redeem` | Controller | A refused invitation action (outcome `denied`), with the reason, such as `email_mismatch`, `expired` or `not_found`. |
| `provider.profile_changed` | Trigger | A Provider Profile is created or a setting changes (the names of the changed columns). |
| `platform.changed` | Trigger | An HPC Platform is registered or its name, operator metadata, status or default changes (the names of the changed columns). |
| `platform.login_node.changed` | Trigger | A login node is approved, changed or removed, with its endpoint, modes, priority, status and host-key pin reference. Health observations are not recorded. |
| `platform.grant.added`, `platform.grant.removed` | Trigger | A Project is granted a Platform or loses the grant. |
| `project.default_platform.set`, `project.default_platform.cleared` | Trigger | A Project Owner or Admin chooses or clears the Project's default Platform, or the grant it pointed to is revoked. |
| `organization.created`, `organization.renamed`, `organization.deleted` | Trigger | An Organization is created, renamed or deleted. |
| `organization.member.added`, `organization.member.role_changed`, `organization.member.removed` | Trigger | An Organization membership changes, with the old and new roles. |
| `project.platform.default.set`, `organization.*` | Controller | A refused default-Platform choice or Organization action (outcome `denied`, with the reason). |
| `slurm_account.changed` | Trigger | A Slurm account is registered, or its status, description or Mode A default changes (the names of the changed columns). |
| `slurm_account.link.added`, `slurm_account.link.changed`, `slurm_account.link.removed` | Trigger | A Project is linked to an account on a Platform, its modes or default change, or the link is removed (also when the Platform grant is revoked). |
| `slurm_account.policy.changed` | Trigger | A Project's native Slurm default permission on a Platform changes. |
| `slurm_account.resolve` | Controller | No account may be charged for a request (outcome `denied`, with a machine-readable reason). See [Map Slurm accounts to Projects](slurm-accounts). |
| `platform.list`, `platform.resolve`, `platform.endpoint.select` | Controller | A refused Platform discovery, an unauthorized Platform, an unavailable Project default, or an unapproved login node (outcome `denied`). See [Register HPC Platforms and login nodes](platforms). |

Calls with no accepted Nest session (`auth.failed`) are logged only, never written
to the database, so an unauthenticated caller cannot fill it.

Each record has the event, its `outcome` (`succeeded`, `denied` or `failed`), the
`interface` (`grpc`, `http`, `controller` or `database`), the acting User and
session, the operator name and PostgreSQL role, the Project and target User, the
resource (such as `job:<execution ID>` or `slurm_job:<job ID>`), the request ID
from the Controller log, a machine-readable `reason` such as `not_a_member`, an
operator's free-text note, and a few scalar `details`. Operators set the actor
and note for changes made with plain SQL as for
[platform administration](platform-admin).

## What is never recorded

No record contains an access token, refresh secret, invitation or Invite Link
token, any of their hashes, a device or user code, the CLI's self-reported client
name, an email address, a message body, an SMTP setting, an SSH key, a database
URL or any other HPC credential. Triggers name rows by ID only. Details written by
the Controller pass through [`audit.redact_details`], which keeps only short
scalar values and replaces anything that looks like a token, key, password, email
address or registered secret with `***`. Every record is also logged as one
`Audit {...}` JSON line on the `nest.audit` logger, with the same content.

## Tamper evidence

Records cannot be updated, deleted or truncated through Nest or plain SQL. After
each write, the Controller seals new records into a SHA-256 hash chain: each
sealed record's hash covers its content and the previous record's hash. Someone
with the table owner's credentials can disable the triggers, but a changed,
removed or reordered sealed record then breaks the chain:

```bash
nest-db audit verify
```

```text
Audit chain intact: 1042 sealed records
```

It seals any records written since, then checks every link; it exits 1 and names
the first broken `chain_seq` otherwise. Records written by triggers are sealed by
the next Controller write or `nest-db audit verify`. For stronger guarantees, copy
the `nest.audit` log lines or regular database backups to storage the database
operator cannot change, and compare.

## Read the log

```bash
nest-db audit list --limit 20
nest-db audit list --event run.submit --project-id 6f1c...   # one Project's submissions
nest-db audit list --user-id 0b7e...                          # one User's actions
```

```text
2026-11-20T10:02:11.481+00:00 run.submit outcome=denied interface=grpc actor=0b7e... session=91aa... project=6f1c... reason=provider_denied request_id=5d2f... db_role=nest details={"cpu": 0, "gpu": 1, "provider": "nano4", "rpc": "Execute", "session_kind": "cli"}
```

Use the `request_id` to find the matching Controller log lines. From Python, see
[`audit.AuditLog.events`].
