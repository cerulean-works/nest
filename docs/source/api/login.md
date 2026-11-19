# Command-line sign-in

`nest.login` signs the command line and SDK in to Nest through a browser approval
and keeps the session. See [Sign in from the command line](../cli-login).

```python
from nest.login import NotSignedInError, ensure_session

session = ensure_session()              # signs in inline when needed
try:
    session = ensure_session(interactive=False)
except NotSignedInError:
    ...                                 # ask the user to run nest login
```

A [`login.NestSession`] is a Nest identity only, never an HPC credential.

## Sessions

[[autodoc]] login.ensure_session

[[autodoc]] login.login

[[autodoc]] login.logout

[[autodoc]] login.status

[[autodoc]] login.NestSession

[[autodoc]] login.LogoutResult

## Storage

[[autodoc]] login.CredentialStore

[[autodoc]] config.CLIAuthConfig

[[autodoc]] config.load_cli_auth_config

## Errors

[[autodoc]] login.AuthError

[[autodoc]] login.NotSignedInError

[[autodoc]] login.LoginDeniedError

[[autodoc]] login.LoginExpiredError

[[autodoc]] login.NestUnavailableError

[[autodoc]] login.CredentialsFileError
