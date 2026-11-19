# Sign in with Google

Nest Users sign in with their Google account. The Controller runs the OAuth 2.0
authorization code flow with PKCE itself, verifies the OpenID Connect ID token
Google returns, and links the sign-in to a Nest User by Google's stable subject ID
(`sub`), never by email address. The flow follows Flock's server-run Google
sign-in, with the ID token's signature checked against Google's published keys and
the pending sign-in stored in PostgreSQL rather than a signed cookie.

Google sign-in is opt-in: without `NEST_GOOGLE_CLIENT_ID` the Controller opens no
HTTP listener and reads no other Google setting.

## Set up

1. In the Google Cloud console, create an OAuth client of type **Web application**.
   Add the callback URL Nest will serve as an **Authorized redirect URI**, such as
   `https://nest.example.com/auth/google/callback`. It must match
   `NEST_GOOGLE_REDIRECT_URL` exactly.
2. Store the client secret in your secret manager and mount it as a file.
3. Set the Controller's environment, with the database already migrated
   (`nest-db upgrade`, see [Store Controller records in PostgreSQL](database)):

   ```bash
   NEST_DATABASE_URL=postgresql://nest@db.example.internal:5432/nest
   NEST_GOOGLE_CLIENT_ID=1234-example.apps.googleusercontent.com
   NEST_GOOGLE_CLIENT_SECRET_FILE=/run/secrets/google_client_secret
   NEST_GOOGLE_REDIRECT_URL=https://nest.example.com/auth/google/callback
   NEST_AUTH_HTTP_BIND=127.0.0.1:8080
   ```

4. Serve `https://nest.example.com` from a TLS-terminating reverse proxy that
   forwards `/auth/google/login`, the redirect URL's path, `/auth/session`
   (with `/auth/session/refresh` and `/auth/session/logout`) and `/auth/cli/`
   (see [Sign in from the command line](cli-login)), unchanged, to
   `NEST_AUTH_HTTP_BIND`.

At startup the Controller logs
`Google sign-in enabled listening=127.0.0.1:8080 login_path=/auth/google/login ...`.
It never contacts Google at startup, so a Google outage cannot stop it. Users sign
in by opening `https://nest.example.com/auth/google/login`.

See [Environment variables and defaults](configuration#google-sign-in) for every
setting. A secret or redirect URL without `NEST_GOOGLE_CLIENT_ID` is a
configuration error, so a half-configured deployment never starts silently without
sign-in.

### Local development

For a Controller on your own machine, an `http://localhost` or `http://127.0.0.1`
redirect URL is accepted, such as `http://localhost:8080/auth/google/callback`
with `NEST_AUTH_HTTP_BIND=127.0.0.1:8080`. Register the same URL with the OAuth
client. Every other host must use `https://`, because the callback carries the
authorization code.

## How a sign-in works

```text
Browser                    Nest auth listener                 Google
   |  GET /auth/google/login  |                                  |
   |------------------------->| store pending sign-in            |
   |  302 + sign-in cookie    |   (hash of state, hash of cookie)|
   |<-------------------------|                                  |
   |  authorization request: client_id, redirect_uri, state,      |
   |  nonce, code_challenge (S256), scope=openid email profile    |
   |------------------------------------------------------------>|
   |  302 to redirect URL with code and state                    |
   |<------------------------------------------------------------|
   |  GET <redirect path>?code&state + sign-in cookie             |
   |------------------------->| consume pending sign-in          |
   |                          | POST token endpoint: code,       |
   |                          |   code_verifier, client secret   |
   |                          |--------------------------------->|
   |                          |        ID token (signed JWT)     |
   |                          |<---------------------------------|
   |                          | verify ID token with Google JWKS |
   |                          | sign in User by (issuer, sub)    |
   |  200 + session cookies   | create browser session           |
   |<-------------------------|                                  |
```

1. **Start.** `GET /auth/google/login` creates a random `state` and a random
   sign-in cookie secret. The PKCE verifier and the OIDC nonce are derived from the
   cookie secret with HMAC-SHA256 under separate labels, so the database stores
   neither. A row in `oidc_login_flows` holds only the SHA-256 of `state` and of
   the cookie secret, and expires after 10 minutes. The browser receives the
   `HttpOnly`, `SameSite=Lax` cookie `nest_google_signin`, scoped to the callback
   path (and `Secure` for an `https://` redirect URL), and is redirected to Google
   with the `S256` code challenge.
2. **Callback.** Google redirects to `NEST_GOOGLE_REDIRECT_URL`. Nest deletes the
   pending sign-in for that `state` first, so a `state` works at most once, even
   when a later check fails. The callback is refused unless the browser presents
   the cookie that started this sign-in, within its 10 minutes.
3. **Code exchange.** Nest posts the code, the PKCE verifier, the configured
   redirect URL and the client credentials to Google's token endpoint.
4. **ID-token verification.** Nest verifies the ID token itself, rather than
   trusting the TLS channel alone:
   - the RS256 signature, with a key from Google's JWKS
     (`https://www.googleapis.com/oauth2/v3/certs`), cached for an hour and
     refetched when a token names an unknown key, so Google's key rotation needs no
     restart;
   - `iss` is `https://accounts.google.com` or `accounts.google.com`;
   - `aud` contains `NEST_GOOGLE_CLIENT_ID`, and `azp`, when present or when there
     are several audiences, equals it;
   - `exp` is in the future and `iat` is not, with 60 seconds of clock skew allowed;
   - `nonce` equals the one this browser's sign-in sent;
   - `sub` is present;
   - `email_verified` is the JSON value `true` and `email` is present.
5. **Sign-in.** Nest signs the User in by `(https://accounts.google.com, sub)`,
   creating the User on first sign-in. Both issuer forms map to the same stored
   issuer, so they link the same User. A different Google account with the same
   email address is a different User: Nest never links accounts by email. The
   identity's email address and `email_verified` claim are refreshed on every
   sign-in, for invitation matching.
6. **Session.** In the same transaction Nest creates a refresh session and a
   short-lived access token for it, and sets both as cookies (see
   [Browser sessions](#browser-sessions)). Only SHA-256 digests of the secrets are
   stored. A disabled User gets no session.

The command line signs in by having a signed-in browser approve it; see
[Sign in from the command line](cli-login). This page covers the Google sign-in and
the browser sessions it creates.

## Browser sessions

A sign-in starts a *session family*: a refresh session plus an access token tied to
it. Each has its own secret and its own cookie.

| | Access token | Refresh session |
| --- | --- | --- |
| Cookie | `__Host-nest_session` (`nest_session` on `http://localhost`) | `__Secure-nest_refresh` (`nest_refresh` on `http://localhost`) |
| Attributes | `HttpOnly`, `SameSite=Lax`, `Path=/`, `Secure` on `https://` | `HttpOnly`, `SameSite=Strict`, `Path=/auth/session`, `Secure` on `https://` |
| Lifetime | `NEST_AUTH_ACCESS_TOKEN_TTL`, default 15 minutes | `NEST_AUTH_REFRESH_TTL`, default 24 hours, started again by each refresh |
| Sent | On every request to Nest | Only to `/auth/session/refresh` and `/auth/session/logout` |

The whole family ends `NEST_AUTH_SESSION_MAX_LIFETIME` after sign-in (default 7
days), however often it is refreshed; the User then signs in again. No access token
outlives its refresh session, and no refresh session outlives its family. See
[Environment variables and defaults](configuration#google-sign-in) for the limits.

### Routes

| Route | Does |
| --- | --- |
| `GET /auth/session` | `200` with the User ID and the expiry of the access token, the refresh session and the family, for a valid access token; otherwise `401 {"error": "unauthenticated"}`. |
| `POST /auth/session/refresh` | Rotates the refresh session: the presented refresh secret is revoked as `rotated`, a new refresh session in the same family and a new access token are issued, and both cookies are replaced. `200` with the same fields as `GET /auth/session`. A refused refresh answers `401` and clears both cookies. |
| `POST /auth/session/logout` | Revokes the whole family named by the refresh cookie, or failing that by the access cookie, and clears both cookies. Always `200 {"signed_out": true}`. |

The session routes answer JSON. A page refreshes before the access token expires,
or when a request is refused with `401`:

```javascript
await fetch("/auth/session/refresh", { method: "POST", credentials: "same-origin" });
```

### Rotation and reuse detection

A refresh secret works once. Refreshing revokes it and issues a new one, so a copy
of an old secret is worthless after its owner has refreshed. If a rotated secret is
presented again, either the browser or someone holding a copy is replaying it, and
Nest cannot tell which: it revokes every session of the family (reason
`refresh_reuse`), logs `Refresh secret reused; session family revoked`, and the User
signs in again. Concurrent refreshes with the same secret are serialized in the
database; only the first succeeds, so a page should not send several refreshes at
once.

### What is refused

An access token is accepted only while all of these hold: the token itself has not
expired, its refresh session is neither revoked nor expired, and its User is active.
A refresh is refused when its secret is unknown, expired, revoked or already
rotated, or its User is disabled. Because every check is made in PostgreSQL on each
request, logout and revocation take effect immediately: there is no window in which
a revoked session's access token still works. A logout or revocation that races a refresh
waits for it and then revokes the session the refresh created as well, so a refresh
in flight cannot leave a revoked sign-in with a working pair of secrets.

### Revoke sessions

- **Logout** revokes the browser's own session family.
- **Disabling a User** (for example with [`db.IdentityStore.set_user_status`])
  revokes all their sessions.
- **Administrative revocation** with `nest-db`, which authenticates with the
  database credentials:

  ```bash
  nest-db sessions list   --user-id 0b6c…   # active sessions, with their family IDs
  nest-db sessions revoke --session-id 7f1e… # that session's whole family
  nest-db sessions revoke --user-id 0b6c…    # every session of the User
  ```

  Revocations are recorded with reason `admin_revoked`. Both commands exit with
  status 1 when there was nothing to revoke, 2 for invalid arguments and 3 when the
  database is unreachable. From Python, use [`db.IdentityStore.revoke_session`]
  and [`db.IdentityStore.revoke_user_sessions`].

A revoked session can never be restored: the database refuses any change to a
revoked session's revocation time or reason, and to a session's User, secret or
family.

## Refused sign-ins

The callback answers with a short page and never creates or changes a User,
identity or session when a check fails. Messages name the cause, never a token or
claim value.

| Status | Cause | Message |
| --- | --- | --- |
| 400 | Google returned an error, such as the user cancelling | Google sign-in was cancelled or refused. |
| 400 | Unknown, reused or missing `state`, or the callback opened in another browser | This sign-in was not started in this browser. Start again. |
| 400 | More than 10 minutes between start and callback | The sign-in took too long. Start again. |
| 401 | Bad signature, issuer, audience, `azp`, time, subject or nonce | Google's answer could not be verified. |
| 403 | `email_verified` is not `true`, or no email | Your Google account's email address is not verified. |
| 403 | The User is disabled | Your Nest account is disabled. |
| 502 | Google refused the code exchange, such as a wrong client secret or redirect URI | Google did not confirm the sign-in. Start again. |
| 503 | Google or the database is unreachable, or 10,000 sign-ins are pending | (varies) |

The Controller logs each refusal with its `reason`, such as
`Sign-in refused reason=invalid_state`, and an ID-token failure with the check
that failed, such as `check=ExpiredSignatureError`.

## Security properties

- **Login CSRF and code injection.** A callback needs both the `state` and the
  cookie from the browser that started the sign-in, so an attacker cannot make a
  victim's browser finish the attacker's sign-in, or replay a victim's callback
  URL in another browser.
- **Code interception.** The PKCE verifier never appears in a URL; a stolen code is
  useless without the sign-in cookie, from which the verifier is derived.
- **Replay.** Each `state` is consumed once; Google's codes are single-use, and the
  nonce binds the ID token to this sign-in.
- **Storage.** The database holds only SHA-256 digests of the `state`, the sign-in
  cookie secret, the refresh secrets and the access tokens. Pending sign-ins are capped at 10,000 and
  expired rows are deleted as new sign-ins start.
- **Logging.** The listener never logs request URLs' query strings, so codes and
  states stay out of logs, nor cookies or request headers, so session secrets stay
  out too; session log lines name session, family and User IDs only. The client secret is masked in every log line, and log
  lines name User IDs, not email addresses.
- **Session theft.** Access tokens are short-lived, and refresh secrets are
  rotated on every use with reuse detection. The refresh cookie is `SameSite=Strict`
  and scoped to `/auth/session`, so it is not sent on ordinary requests.
- **Cross-site requests.** `POST /auth/session/refresh` and `/auth/session/logout`
  are refused with `403 {"error": "cross_origin"}` when the browser's `Origin` is
  not the redirect URL's origin or `Sec-Fetch-Site` is not `same-origin`.
- **Headers.** Every response carries `Cache-Control: no-store`,
  `Referrer-Policy: no-referrer` (the callback URL holds the code),
  `X-Frame-Options: DENY`, and a `default-src 'none'` content security policy.

## Rotate the client secret

`NEST_GOOGLE_CLIENT_SECRET_FILE` is re-read for every code exchange. Add a new
secret to the OAuth client in the Google Cloud console, write it to the mounted
file, then delete the old secret in the console. No restart is needed. The inline
`NEST_GOOGLE_CLIENT_SECRET` needs a restart, and is visible to anyone who can
inspect the process environment, so prefer the file.

## Disable Google sign-in

Unset `NEST_GOOGLE_CLIENT_ID` and the other `NEST_GOOGLE_*` settings, and restart.
The auth listener is not started. Users, identities and sessions stay in the
database.

## Test Google sign-in

The tests never contact Google. `tests/fake_oidc.py` runs a local fake provider
with its own throwaway RSA keys: its authorization endpoint consents at once, its
token endpoint enforces the client secret, the registered redirect URI and the
`S256` verifier, and its JWKS endpoint publishes the fake's keys.
`tests/test_google_auth.py` drives the Controller's auth listener through it over
real HTTP against a real PostgreSQL, including refusals for a wrong issuer,
audience, nonce or state, an expired token, a bad signature, an unverified email,
and a disabled User:

```bash
NEST_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
    uv run pytest tests/test_google_auth.py
```
