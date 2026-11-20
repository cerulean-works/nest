# Sign in from the command line

The Nest command line signs in by asking you to approve it in a browser where you
are signed in to Nest with Google. No password, token or code is typed into the
terminal or passed as an argument. The flow follows the OAuth 2.0 device
authorization grant ([RFC 8628](https://www.rfc-editor.org/rfc/rfc8628)), so it
also works on a login node or over SSH, where no browser runs.

`nest login` is optional. A command that needs a Nest session, such as `nest run`,
signs you in the same way the first time it needs to, through
[`login.ensure_session`].

A Nest session identifies you to Nest only. It is **not** an NCHC SSH password,
one-time code (MFA) or key, Nest never sends it to an HPC login node, and the
approval page never asks for HPC credentials. Configure HPC access separately (see
[Deploy a Slurm Controller](deploy)).

## Sign in

Point the CLI at your Nest, then sign in:

```bash
export NEST_AUTH_URL=https://nest.example.com
nest login
```

```text
To sign in to Nest, open this link in a browser:

    https://nest.example.com/auth/cli/approve?code=BCDF-GHJK

and check that it shows the code BCDF-GHJK.
Nest will never ask for your NCHC password or one-time code there.
Waiting for approval...
```

Open the link on any device. If that browser is not signed in to Nest yet, it
signs in with Google first and comes back to the approval. Check that the page
shows the same code as your terminal and the client it names, then choose
**Approve**. The terminal prints `Signed in to Nest.`

On a desktop with a graphical browser, `nest login` opens the link for you;
`--no-browser` only prints it. On a server without `DISPLAY`, it never starts a
text-mode browser in your terminal.

Approve only a sign-in you started yourself, just now. Someone who sends you a
link to approve is asking for access to your Nest account.

## Sign in from Python

```python
from nest.login import ensure_session

session = ensure_session()  # stored session, refreshed, or a new browser approval
headers = {"Authorization": f"Bearer {session.access_token}"}
```

`ensure_session(interactive=False)` raises [`login.NotSignedInError`] instead of
starting a sign-in, for non-interactive jobs.

## Inspect and clear the session

```bash
nest auth status           # who is signed in, the scope and the expiries
nest auth status --check   # also ask Nest whether the session is still accepted
nest logout                # revoke the session at Nest and delete it here
nest logout --local        # only delete it here
```

`nest auth status` never prints a token. `nest logout` deletes the local session
even when Nest cannot be reached, and says so. Signing in again with `nest login`
revokes the previous session.

Exit status: 0 on success, 1 when not signed in or the sign-in was denied or
expired, 2 for invalid configuration, 3 when Nest is unreachable.

## Sessions

A CLI session is a session family like a [browser session](authentication#browser-sessions),
with the same lifetimes: a short-lived access token (`NEST_AUTH_ACCESS_TOKEN_TTL`,
15 minutes by default), a refresh secret rotated on every use, with reuse detection
(`NEST_AUTH_REFRESH_TTL`), and an end however often it is refreshed
(`NEST_AUTH_SESSION_MAX_LIFETIME`, 7 days). The CLI refreshes it automatically.

- **Scoped.** A CLI session carries the scopes it was approved for, shown on the
  approval page: `jobs:run` (run and manage jobs in your Projects) and
  `projects:read` (see your Projects and their members). A CLI token is accepted
  only by the CLI routes, the Projects API and the Controller's gRPC service (see
  [Authenticated execution](security#authenticated-execution)), never as a browser
  session cookie, and a browser session is never accepted as a CLI token.
- **Stored privately.** The session is kept in `NEST_CLI_CREDENTIALS_FILE`
  (default `~/.config/nest/credentials.json`), written atomically with mode `0600`
  in a `0700` directory. A file that other users can read is refused. Concurrent
  Nest commands refresh one at a time under a lock, so they never trip reuse
  detection.
- **Revocable.** `nest logout`, `nest-db sessions revoke`, and disabling the User
  end it at once (see [Revoke sessions](authentication#revoke-sessions)).

## How it works

```text
CLI                               Nest auth listener                   Browser
 | POST /auth/cli/start              |                                    |
 |---------------------------------->| store request: hash of device code |
 | device_code, user_code, link      |   and of user code, 10 minutes     |
 |<----------------------------------|                                    |
 |  shows link and user code ------------------------------------------->|
 |                                   |  GET /auth/cli/approve?code=...    |
 |                                   |<-----------------------------------|
 |                                   |  (signs in with Google if needed)  |
 |                                   |  page: code, client, scopes        |
 |                                   |----------------------------------->|
 |                                   |  POST approve (Origin, CSRF token) |
 |                                   |<-----------------------------------|
 | POST /auth/cli/token device_code  |                                    |
 |---------------------------------->| create CLI session, consume request|
 | access_token, refresh_token       |                                    |
 |<----------------------------------|                                    |
```

1. **Start.** `POST /auth/cli/start` with `{"scope": ..., "client_name": ...}`
   returns a secret device code, a user code such as `BCDF-GHJK` (8 letters, no
   vowels), the approval link, `expires_in` (600 seconds) and the polling
   `interval`. Only SHA-256 digests of both codes are stored, in
   `cli_login_requests`. At most 10,000 requests wait at once.
2. **Approve.** `GET /auth/cli/approve?code=...` needs a browser session; without
   one it sends the browser to Google sign-in, which returns to this approval. The
   page shows the code, the client name the CLI reported (escaped, and labelled as
   reported, never trusted) and the scopes. The form posts back with a CSRF token
   bound to the browser's access token and this request, and the post is refused
   without the browser's same-site `Origin`. The decision re-checks the browser
   session in its own transaction, under the same locks as logout and revocation,
   so a session that ended a moment earlier approves nothing. A request is approved
   or denied once.
3. **Redeem.** The CLI polls `POST /auth/cli/token` with `{"device_code": ...}`.
   The answer is `400` with the RFC 8628 `error` `authorization_pending`,
   `slow_down` (polled too fast), `access_denied`, `expired_token`, or
   `invalid_grant` (unknown or already used), until approval. Then, in one
   transaction, Nest creates the CLI session with the approved scope and consumes
   the request, so a device code yields at most one session. A User disabled after
   approving gets none.
4. **Use.** The CLI sends `Authorization: Bearer <access token>`.
   `GET /auth/cli/session` describes the session; `POST /auth/cli/refresh` with
   `{"refresh_token": ...}` rotates it; `POST /auth/cli/logout` revokes it.

The CLI's JSON routes take secrets only in the body or the `Authorization` header,
never in a URL, and refuse any request carrying `Origin` or `Sec-Fetch-Site`, so no
web page can drive them. Request bodies are limited to 4 KiB. Log lines name
request, session and User IDs only, never a code or token.

## Configure

The Controller serves CLI sign-in whenever [Google sign-in](authentication) is
enabled; there is nothing more to set. Forward `/auth/cli/` from the reverse proxy
to `NEST_AUTH_HTTP_BIND` together with the other `/auth/` routes. The approval link
is built from the origin of `NEST_GOOGLE_REDIRECT_URL`.

On the CLI side:

| Variable | Default | Behavior |
| --- | --- | --- |
| `NEST_AUTH_URL` | Unset | Origin of your Nest, such as `https://nest.example.com`: the origin of `NEST_GOOGLE_REDIRECT_URL`. `https://` only (`http://` for localhost). `--url` overrides it. |
| `NEST_CLI_CREDENTIALS_FILE` | `$XDG_CONFIG_HOME/nest/credentials.json` (`~/.config/nest/credentials.json`) | Absolute path of the stored session. |
