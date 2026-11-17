"""
Nest sign-in.

Google sign-in uses the OAuth 2.0 authorization code flow with PKCE (`S256`) and
OpenID Connect ID-token verification, following Flock's server-run flow. Users are
linked by Google's stable `sub` under its issuer, never by email address. It is
enabled by `NEST_GOOGLE_CLIENT_ID` (see *Sign in with Google*).
"""

from .google import (
    CLOCK_SKEW,
    GOOGLE_ENDPOINTS,
    GOOGLE_ISSUER,
    GOOGLE_ISSUERS,
    SESSION_TTL,
    SIGN_IN_ERRORS,
    SIGN_IN_TTL,
    GoogleSignIn,
    IDTokenVerifier,
    OIDCEndpoints,
    SignInError,
    SignInResult,
    SignInStart,
    VerifiedIdentity,
    derive_flow_secrets,
    pkce_challenge,
)
from .web import (
    INSECURE_SESSION_COOKIE,
    LOGIN_PATH,
    SESSION_COOKIE,
    SIGN_IN_COOKIE,
    AuthHTTPServer,
    AuthService,
    start_auth_service,
)

__all__ = [
    "CLOCK_SKEW",
    "GOOGLE_ENDPOINTS",
    "GOOGLE_ISSUER",
    "GOOGLE_ISSUERS",
    "INSECURE_SESSION_COOKIE",
    "LOGIN_PATH",
    "SESSION_COOKIE",
    "SESSION_TTL",
    "SIGN_IN_COOKIE",
    "SIGN_IN_ERRORS",
    "SIGN_IN_TTL",
    "AuthHTTPServer",
    "AuthService",
    "GoogleSignIn",
    "IDTokenVerifier",
    "OIDCEndpoints",
    "SignInError",
    "SignInResult",
    "SignInStart",
    "VerifiedIdentity",
    "derive_flow_secrets",
    "pkce_challenge",
    "start_auth_service",
]
