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
   forwards `/auth/google/login` and the redirect URL's path, unchanged, to
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
   |  200 + session cookie    | create browser session           |
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
6. **Session.** In the same transaction Nest creates a browser session valid for 12
   hours and sets it as the `HttpOnly`, `SameSite=Lax`, `Path=/` cookie
   `__Host-nest_session` (`nest_session` for an `http://localhost` redirect URL,
   where browsers refuse the `__Host-` prefix). Only the SHA-256 of the session
   secret is stored. A disabled User gets no session.

Browser session renewal, logout and revocation endpoints belong to browser session
management, as do CLI sign-in and the one-time admin bootstrap; this page covers
only the Google sign-in itself.

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
  cookie secret and the session secret. Pending sign-ins are capped at 10,000 and
  expired rows are deleted as new sign-ins start.
- **Logging.** The listener never logs request URLs' query strings, so codes and
  states stay out of logs. The client secret is masked in every log line, and log
  lines name User IDs, not email addresses.
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
