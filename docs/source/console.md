# Manage Projects in the browser

The Management Console is a browser page on the Controller's auth listener, at
`https://<your Nest>/console`. With it, a signed-in User can see their Projects,
connect their computer to one, and manage members, invitations and roles without
the CLI. Platform administrators also
get the privileged pages, which use the privileged management API (G03).

The Console is a client of the same JSON routes as the CLI. It makes no permission
decisions: the Controller checks every request against the
[Project role matrix](projects#roles). The Console hides controls that a role can't
use, but hiding a control changes nothing on the server.

## What it supports

| Page | Route | Who | What |
| --- | --- | --- | --- |
| Sign in, account menu, sign out | `/console` | Everyone | Google sign-in; the account menu shows your name, email and Nest User ID |
| Overview | `/console#/overview` | Any signed-in User | Your current Project, whether it can run on this Controller and why not, pending invitations, next steps |
| Connect to a Project | `/console#/connect` | Any signed-in User | Copyable install, connection, sign-in and `nest projects use` commands for the chosen Project (G02c) |
| Your Projects | `/console#/projects` | Any signed-in User | Searchable, filterable, paged list with role and eligibility; *Make current* |
| Project: members | `/console#/projects/<id>` | Every role | Members by name; Owner and Admin add, change roles and remove; non-Owners leave |
| Project: invitations | `/console#/projects/<id>/invitations` | Owner, Admin | Email invitations (invite, resend, revoke) and Invite Links (create, revoke) |
| Project: activity | `/console#/projects/<id>/activity` | Owner, Admin | Recent membership changes (Project audit) |
| Your invitations | `/console#/invitations` | Any signed-in User | Accept or decline; join with an invitation link or Invite Link |
| Administration: Projects, Create a Project, Users | `/console#/admin`, `#/admin/new`, `#/admin/users` | `platform_admin` | All Projects (searchable), creation with an explicit Owner found by email or in the directory, the paged user directory (G03 API) |
| Project access | `/console#/admin/projects/<id>` | `platform_admin` | What running work still needs, Platform grants from the catalog, Slurm account links, Provider approvals and the Provider policy (G03 API) |

The **Current Project** switcher in the top bar lists your Projects by name and
role. Names several of your Projects share are told apart by the start of their ID
(`Shared lab (3f0c1a2b)`), never by guessing. The browser remembers only the ID of
the Project you last chose, per User, in its local storage; it is checked against
the server's list on every page, and a Project you lost access to is replaced, with
a notice. Changing it never changes the SDK's saved choice: that is done only by
the commands you copy and run yourself.

These are left out on purpose: renaming, transferring or deleting a Project (use
the CLI or the API), and Organizations. **Executions, usage, results, budgets and
HPC account binding** (G01, G02b, Mode B) are later milestones: the Console has no
navigation entry, count or placeholder data for them.

## Connect to a Project

The *Connect* page is the onboarding path from the browser to a first remote call.
It shows, for the Project chosen on the page:

1. **Install Nest** (once per uv project): `uv add 'nest @ git+…'`.
2. **Connect and sign in** (each new terminal): the `export` lines of the chosen
   connection profile, `uv run nest login` and `uv run nest auth status --check`.
3. **Select the Project**: `uv run nest projects use <id>` with the Project's ID
   already filled in, and `uv run nest projects current`. It is offered only for a
   Project that can run here; with exactly one such Project the SDK selects it by
   itself, which the page says. `uv run nest projects select` chooses from a list
   instead.
4. **Run a remote function**: a minimal `main.py` and `uv run python main.py`.

Every block has a **Copy** button (with a screen-reader announcement; where the
browser grants no clipboard access, the command is selected instead). Nothing runs
from the browser and the browser never changes local settings.

**Eligibility comes from the server.** `GET /api/connect` asks the Controller's own
execution access check ([`authz.ExecutionAccess.project_eligibility`]) for each of
your Projects, the same rules as the SDK's `ListEligibleProjects`, and reports every
unmet one: `role` (Viewers cannot run), `provider` (no effective Provider approval),
`platform` (the Controller's Platform is not granted), `account` (no default Slurm
account) or `controller` (the Controller serves no Platform). The page explains each
with the person to ask; it never names an account. Management access and compute
eligibility are separate: a Viewer can read a Project the Controller would refuse to
run in. Like the SDK's listing, the answer is advice; every call is authorized again.

**Connection profiles come from the operator** (`NEST_CONSOLE_*`, see
[Environment variables](configuration#console-connection-profiles)):

| Profile | Shown when | Settings |
| --- | --- | --- |
| Private network (or *Direct (TLS)*) | `NEST_CONSOLE_CONTROLLER_ADDRESS` is set; always first | `NEST_AUTH_URL` (this Nest's origin), `NEST_CONTROLLER_ADDRESS`, `NEST_CONTROLLER_TLS`, and `NEST_HOSTED_SESSION_ADDRESS` where the SDK requires it |
| Hosted gateway, *Pilot* | `NEST_CONSOLE_HOSTED_PROFILE=pilot` on the hosted sign-in origin | `NEST_PROFILE=hosted` |
| Hosted gateway, default | Only after H09 switches `HOSTED_PROFILE_DEFAULT` on | None |

With no setting, the page shows only how to install and sign in, says that the
operator has not published connection settings, and offers no Project selection or
run commands, which could not reach the Controller. A deployment whose users should
get working commands from the page sets `NEST_CONSOLE_CONTROLLER_ADDRESS` and
`NEST_CONSOLE_CONTROLLER_TLS`. The server builds every command,
quoting each value for a POSIX shell, from settings that were validated at startup
(a plain `host:port`, nothing else free-form); the browser adds only a Project ID it
checked is a UUID. No Project name, token, key or password is ever part of a
command. The tests load each profile's settings through the SDK's own
`load_client_config` to prove they are accepted as copied.

## Privileged pages use the G03 API

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
separate checks, and the Console shows and changes each one separately. Running
work needs all of them, plus a Project role that may submit.

### Recorded and effective Provider approvals

A Provider approval in the database is a record. Whether it lets submissions
through depends on where the serving Controller takes approvals from
(`NEST_PROVIDER_APPROVALS`, see [Onboard a Project](project-onboarding)). The
admin API reports this as `policy: {source, provider, approvals_effective}`, and
the Console shows it on every Project access page as *Approval source* and
*Provider checked*. It labels each record with its effect on this Controller:

| Source | Recorded approval for the Provider checked | Recorded revocation for the Provider checked |
| --- | --- | --- |
| `database` | Recorded and effective for new submissions | Recorded and effective: new submissions are refused |
| `environment` | Recorded only, not effective: the Controller's environment allow-list decides until the cutover | Recorded and effective: new submissions are refused, even for a listed Project |
| `unknown` (not reported) | Recorded only, not effective | Recorded only |

A record for a Provider other than the one the Controller checks is always
*recorded only*. When recorded approvals are not effective, the button reads
**Record approval (not effective here)**, a notice says so, and the Console asks
for confirmation before it records one. The message after a change comes from the
policy in the server's answer, never from the click alone.

## Architecture

```text
browser ──HTTPS──▶ TLS reverse proxy ──HTTP──▶ Controller auth listener (NEST_AUTH_HTTP_BIND)
                                                ├─ GET /console, /console/console.{js,css}, nest-mark.svg
                                                ├─ /auth/google/login, callback, /auth/session*
                                                └─ /api/projects, /api/invitations, /api/connect, /api/admin (G03)
```

- **Same origin, no BFF.** The Console page, the sign-in routes and the API are
  all served by the auth listener, on the origin of `NEST_GOOGLE_REDIRECT_URL`.
  The Console needs no extra origin, no extra service and no new setting. It is
  served wherever sign-in already is.
- **No build step.** The Console is one HTML page, one script, one stylesheet and
  one SVG mark, shipped in the `nest` package (`nest/auth/console_assets`) and so in
  the Controller image. It loads no third-party code, font or CDN. The script
  inserts content as text only, never as HTML. G04 kept this on purpose: the pages
  are forms and tables over a small JSON API, a framework would add a build
  service, pinned toolchain and supply chain for no capability the browser lacks,
  and the restrictive CSP stays as it is.
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
- **CSP.** The Console's responses carry
  `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self';
  img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`,
  along with `Referrer-Policy: same-origin`, `Cross-Origin-Opener-Policy:
  same-origin`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and
  `Cache-Control: no-store`. Every other route keeps its own policy:
  `default-src 'none'; frame-ancestors 'none'` for the JSON API. The sign-in
  result, CLI approval and invitation pages load the Console's stylesheet and mark
  under `default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none';
  form-action 'self'; frame-ancestors 'none'` (the invitation page adds the hash of
  its one script); none of them runs a same-origin script.
  `Referrer-Policy: same-origin` is needed because a same-origin `fetch` POST from
  a `no-referrer` page sends `Origin: null`, which the CSRF check refuses.
- **No secrets in the browser.** The Console holds no client secret, database URL,
  SSH key or HPC credential. The API never returns one (see
  [Security](security)). Invitation and Invite Link URLs are shown once, when
  they are created, as the API returns them, and they aren't stored. When you paste
  an invitation link into the Console, its token stays in page memory only.

## Design system

`console.css` holds the tokens and components that every browser page uses, so
the sign-in, CLI approval and invitation pages look and behave like the Console.

- **Tokens.** Cream `#fcfbf6` background, ink `#172a50` text, Nest blue `#8db8e4`
  accents and NCHC blue `#145a9a` for actions and links, plus status colors, an
  8-point spacing scale and one radius. Every text pair meets WCAG 2.2 AA contrast;
  a dark scheme follows `prefers-color-scheme`.
- **Components.** `.btn` (primary, `secondary`, `danger`, `ghost`, `sm`), `.card`,
  `.badge` (`ok`, `warn`, `bad`, `info`), `.callout`, `.field` and `.form-grid`,
  `.table-wrap` with `table.responsive` (rows become labelled cards below 640 px),
  `.toolbar` (search and filters), `.pager`, `.tabs`, `.steps`, `.code` (a command
  block with its Copy button), `.facts`, `.empty`, `.skeleton` and `dialog.confirm`.
  Identifiers use `.mono` and stay visible in details, but no common task needs one
  copied by hand.
- **Patterns.** One `h1` per page, focused after each navigation; a polite live
  region for outcomes; loading skeletons, empty, denied, not-available, failure and
  success states on every page; destructive changes go through a confirmation
  dialog that says what changes (Escape and Cancel decline, focus returns); a
  refused change keeps the form's input and moves focus to the message. Every
  control has a label, focus is always visible, and the layout works from 360 px.

When adding a page, build it from these components in `console.js` (`card`,
`table`/`row`, `field`, `badge`, `callout`, `codeBlock`, `confirmChange`) rather than
new styles, and never insert markup from data.

## Deploy

Nothing beyond [Sign in with Google](authentication) is needed:

1. Configure Google sign-in (`NEST_GOOGLE_*`, `NEST_AUTH_HTTP_BIND`) and the
   database, as for the CLI.
2. Have the TLS-terminating reverse proxy forward `/console` and `/console/*` to
   the auth listener unchanged, alongside `/auth/*`, `/api/*` and `/invitations/*`.
   A proxy that already forwards every path needs no change.
3. Optionally, publish connection settings for the *Connect* page with
   `NEST_CONSOLE_CONTROLLER_ADDRESS` and `NEST_CONSOLE_CONTROLLER_TLS` (and, only
   for the approved hosted pilot, `NEST_CONSOLE_HOSTED_PROFILE=pilot`). They change
   what the page shows, nothing else.
4. Open `https://<your Nest>/console`.

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
session returns to sign-in and that an expired access token is refreshed. As a
platform admin, they create a Project for a named Owner from the directory, grant
a Platform, and record and revoke Provider approvals in `database`, `environment`
and unreported policy modes. They check each label the page shows against the
admission authority the Controller would use (`DatabaseProviderApprovals` or
`AllowedProjects`). They also revoke the admin role and then the sessions while
the page is open. The Connect tests (`ConnectAPITests`, `ConnectBrowserTests`) check the eligibility
reasons as each grant is added and revoked, that no account name is returned,
profile gating (none, private, hosted pilot only on the hosted origin, the H09
default), duplicate names, a Project named with markup and shell metacharacters,
the exact clipboard contents, the remembered Project and its revocation, paging and
search, and the layout at 360 px with keyboard use. They fail on any script error
or CSP violation, and on any `POST` without the page's own `Origin`.
`NEST_TEST_EVIDENCE_DIR` names a directory for the screenshots they take.

To try the Console by hand, start a Controller with a database and Google sign-in
on `http://localhost` (see [Sign in with Google](authentication)) and open
`http://localhost:<port>/console`.
