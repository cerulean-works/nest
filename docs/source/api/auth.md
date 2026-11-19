# Sign-in

`nest.auth` holds Google sign-in: the OAuth 2.0 authorization code flow with PKCE,
OpenID Connect ID-token verification, and the Controller's auth listener. See
[Sign in with Google](../authentication) for configuration, the flow, and the
security checks.

ID tokens are verified with [PyJWT](https://pyjwt.readthedocs.io/) and its
`cryptography` backend. PyJWT is widely used and actively maintained, does only
JWS/JWT, rejects `alg: none` and algorithm confusion when `algorithms` is fixed,
and ships `PyJWKClient`, which fetches and caches a JWKS and refetches on an
unknown `kid` with a cooldown. Nest needs no OAuth client framework on top, because
the flow itself is a few HTTP requests that Nest makes directly.

Run a sign-in programmatically, for example behind another HTTP framework:

```python
from nest.auth import GoogleSignIn, SignInError

sign_in = GoogleSignIn(config.google, database)
started = sign_in.start()
# Set started.binding as an HttpOnly cookie and redirect to started.authorization_url.

try:
    result = sign_in.finish(state=state, code=code, binding=cookie, error=error)
except SignInError as exc:
    respond(exc.status, str(exc))  # exc.reason is a stable code
else:
    # Only their hashes are stored. The access token is short-lived; the refresh
    # secret is presented only to refresh (see "Browser sessions").
    set_session_cookies(result.access_token, result.refresh_token)
```

Refresh and check browser sessions with [`db.SessionStore`]:

```python
from nest.db import SessionStore, SessionRefreshError, hash_token

sessions = SessionStore(database)
grant = sessions.authenticate(hash_token(access_cookie))  # None when refused
try:
    rotation = sessions.rotate(
        hash_token(refresh_cookie),
        new_refresh_hash=hash_token(new_refresh),
        new_access_hash=hash_token(new_access),
        refresh_ttl=config.google.sessions.refresh_ttl,
        access_ttl=config.google.sessions.access_token_ttl,
    )
except SessionRefreshError as exc:
    ...  # exc.reason: unknown, expired, revoked, reused or user_disabled
```

## Sign-in service

[[autodoc]] auth.GoogleSignIn

[[autodoc]] auth.SignInStart

[[autodoc]] auth.SignInResult

[[autodoc]] auth.SignInError

`auth.SIGN_IN_ERRORS` maps each `reason` to its HTTP status and message.

## ID tokens

[[autodoc]] auth.IDTokenVerifier

[[autodoc]] auth.VerifiedIdentity

[[autodoc]] auth.OIDCEndpoints

`auth.GOOGLE_ENDPOINTS` holds Google's endpoints. Only tests replace them, with a
local fake provider; no setting points Nest at another issuer.

[[autodoc]] auth.pkce_challenge

[[autodoc]] auth.derive_flow_secrets

## Auth listener

[[autodoc]] auth.start_auth_service

[[autodoc]] auth.AuthService

[[autodoc]] auth.AuthHTTPServer

## Browser sessions

See [Browser sessions](../authentication#browser-sessions).

[[autodoc]] db.SessionStore

[[autodoc]] db.AccessGrant

[[autodoc]] db.Rotation

[[autodoc]] db.SessionRefreshError

## CLI sign-in

See [Sign in from the command line](../cli-login). The client side is
[`login.ensure_session`].

[[autodoc]] auth.CLISignIn

[[autodoc]] auth.CLISignInStart

[[autodoc]] auth.CLITokens

[[autodoc]] auth.CLISignInError

[[autodoc]] auth.normalize_user_code

[[autodoc]] db.CLILoginStore

[[autodoc]] db.CLILoginRequest

[[autodoc]] db.CLIPoll

[[autodoc]] db.TooManyPendingCLILoginsError

## Pending sign-ins

[[autodoc]] db.LoginFlowStore

[[autodoc]] db.LoginFlow

[[autodoc]] db.TooManyPendingSignInsError
