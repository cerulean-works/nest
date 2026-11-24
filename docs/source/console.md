# Manage Projects in the browser

The Management Console is a browser page on the Controller's auth listener, at
`https://<your Nest>/console`. With it, a signed-in User can see their Projects and
manage members, invitations and roles without the CLI. Platform administrators also
get the privileged pages once the privileged management API (G03) is available.

The Console is a client of the same JSON routes as the CLI. It makes no permission
decisions: the Controller checks every request against the
[Project role matrix](projects#roles). The Console hides controls that a role can't
use, but hiding a control changes nothing on the server.

## What it supports

| Page | Route | Who | Status |
| --- | --- | --- | --- |
| Sign in, session status, sign out | `/console` | Everyone | Supported |
| Your Projects (list, empty state) | `/console#/projects` | Any signed-in User | Supported |
| Project detail and members | `/console#/projects/<id>` | Every role | Supported |
| Add an existing user, change a role, remove a member | Project detail | Owner, Admin | Supported |
| Leave a Project | Project detail | Admin, Member, Viewer | Supported |
| Email invitations: invite, resend, revoke | Project detail | Owner, Admin | Supported |
| Invite Links: create, revoke | Project detail | Owner, Admin | Supported |
| Recent membership changes (Project audit) | Project detail | Owner, Admin | Supported |
| Your pending invitations: accept, decline | `/console#/invitations` | Any signed-in User | Supported |
| Join with an invitation link or Invite Link | `/console#/invitations` | Any signed-in User | Supported |
| User directory (paged), create a Project with an explicit Owner | `/console#/admin` | `platform_admin` | Gated on G03 |
| Project access: Platform grants, Slurm account links, Provider approvals | `/console#/admin/projects/<id>` | `platform_admin` | Gated on G03 |

These are left out on purpose: renaming, transferring or deleting a Project (use
the CLI or the API), Organizations, and anything about usage, account binding,
execution or Runs (G01 and G02b).

### Privileged pages are gated on G03

Creating a Project, the user directory, Platform grants, Slurm account links and
Provider approvals all go through the privileged management API under `/api/admin`
([#79](https://github.com/cerulean-works/nest/issues/79)). The Console never uses
another route, a direct database write or a client-side shortcut for these actions:

- A server without `/api/admin` (no `AuthHTTPServer.admin`) renders the page with
  `data-privileged-api="unavailable"`. The Console then shows no administration
  page and no Project creation form to anyone.
- A server with `/api/admin` renders `available`. The Console then calls
  `GET /api/admin/users?limit=1` and shows the Administration page only if the
  server answers 200. The server re-checks the `platform_admin` role on every
  request, so revoking the role takes effect on the next request. Every change asks
  for a reason, which goes to the security audit log.

Being a platform admin grants no Project membership and no compute access. A
Project's Provider approval, its Platform grant and its Slurm account link are
separate checks, and the Console shows and changes each one separately.

## Architecture

```text
browser ──HTTPS──▶ TLS reverse proxy ──HTTP──▶ Controller auth listener (NEST_AUTH_HTTP_BIND)
                                                ├─ GET /console, /console/console.{js,css}
                                                ├─ /auth/google/login, callback, /auth/session*
                                                └─ /api/projects, /api/invitations, /api/admin (G03)
```

- **Same origin, no BFF.** The Console page, the sign-in routes and the API are
  all served by the auth listener, on the origin of `NEST_GOOGLE_REDIRECT_URL`.
  The Console needs no extra origin, no extra service and no new setting. It is
  served wherever sign-in already is.
- **No build step.** The Console is one HTML page, one script and one stylesheet,
  shipped in the `nest` package (`nest/auth/console_assets`). It loads no
  third-party code. The script inserts content as text only, never as HTML.
- **Sign-in.** **Sign in with Google** opens `GET /auth/google/login?next=console`.
  That route sets a short-lived `nest_console_return` cookie (HttpOnly, scoped to
  the callback path). After Google's callback, the listener answers
  `303 Location: /console`. A sign-in started anywhere else ends on the usual
  "Signed in" page.
- **Sessions.** The Console uses the existing browser session cookies and never
  reads them: they are `HttpOnly`. The access cookie is `__Host-nest_session`, or
  `nest_session` on `http://localhost`, with `Path=/` and `SameSite=Lax`. The
  refresh cookie is `__Secure-nest_refresh`, or `nest_refresh`, with
  `Path=/auth/session` and `SameSite=Strict`. With an `https://` redirect URL both
  cookies are `Secure`. On a `401`, the Console makes a single
  `POST /auth/session/refresh` and retries the request once. If the refresh also
  fails, because the session was revoked, the user was disabled or the session
  reached its maximum lifetime, the Console returns to its sign-in page. **Sign out**
  calls `POST /auth/session/logout`, which revokes the whole session family.
- **CSRF.** Every change is a `POST` with `Content-Type: application/json`. The
  listener refuses any other content type (`415`), so a cross-site HTML form,
  which can't send JSON, can't make a change. It also refuses a browser-session
  `POST` whose `Origin` is missing or isn't the listener's own origin, or whose
  `Sec-Fetch-Site` isn't `same-origin` (`403 cross_origin`). The refresh and logout
  routes check the origin too.
- **CORS.** None. The listener sends no `Access-Control-*` header, so another site
  can't read an answer, and a preflight gets no permission.
- **CSP.** The Console's three responses carry
  `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self';
  img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`,
  along with `Referrer-Policy: same-origin`, `Cross-Origin-Opener-Policy:
  same-origin`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and
  `Cache-Control: no-store`. Every other route keeps its own policy:
  `default-src 'none'; frame-ancestors 'none'` for the JSON API.
  `Referrer-Policy: same-origin` is needed because a same-origin `fetch` POST from
  a `no-referrer` page sends `Origin: null`, which the CSRF check refuses.
- **No secrets in the browser.** The Console holds no client secret, database URL,
  SSH key or HPC credential. The API never returns one (see
  [Security](security)). Invitation and Invite Link URLs are shown once, when
  they are created, as the API returns them, and they aren't stored. When you paste
  an invitation link into the Console, its token stays in page memory only.

## Deploy

Nothing beyond [Sign in with Google](authentication) is needed:

1. Configure Google sign-in (`NEST_GOOGLE_*`, `NEST_AUTH_HTTP_BIND`) and the
   database, as for the CLI.
2. Have the TLS-terminating reverse proxy forward `/console` and `/console/*` to
   the auth listener unchanged, alongside `/auth/*`, `/api/*` and `/invitations/*`.
   A proxy that already forwards every path needs no change.
3. Open `https://<your Nest>/console`.

`NEST_GOOGLE_REDIRECT_URL` can't use a `/console` path. Keep the Console private,
like the rest of the listener, until public exposure is approved separately. This
change doesn't publish anything.

## Test locally

Use the fake Google provider from the test suite. Run the Console's HTTP and
real-browser tests against a PostgreSQL server:

```bash
export NEST_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres
uv run playwright install chromium   # once
uv run pytest tests/test_console.py
```

The browser tests sign in through the Console's own button. They then manage
members, invitations and Invite Links as an Admin, join a Project from a second
account, and check that a Viewer sees no management controls and that the same
request sent from the page is refused (`403`). They also check that a revoked
session returns to sign-in and that an expired access token is refreshed. They
fail on any script error or CSP violation, and on any `POST` without the page's
own `Origin`. `NEST_TEST_EVIDENCE_DIR` names a directory for the screenshots they
take.

To try the Console by hand, start a Controller with a database and Google sign-in
on `http://localhost` (see [Sign in with Google](authentication)) and open
`http://localhost:<port>/console`.
