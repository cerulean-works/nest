# Get access to a Project

Every remote call runs in a **Project**. Before your first call you need two
things: a role in a Project that allows running, and the operator's approval of
that Project for HPC. This page explains how to get both. When
`uv run nest projects current` names a Project, you have them and can follow the
[Quickstart](quickstart).

## Roles

Your role in a Project decides what you may do:

| Role | Submit and cancel own Runs | Invite people, change roles |
| --- | --- | --- |
| Owner, Admin | yes | yes |
| Member | yes | no |
| Viewer | no | no |

A role never grants HPC access by itself: the operator must also approve the
Project (below). See [Share Projects and manage roles](projects).

## Join an existing Project

Ask its Owner or an Admin for an email invitation or an Invite Link (see
[Invite people to a Project](invitations)). Open the link, sign in with Google,
review the Project and role, and choose *Accept*. Pending invitations are also
listed in the [Management Console](console) under *Your invitations*.

## Ask for a new Project

You cannot create a Project yourself. Only a **platform administrator** creates
Projects, in the Console's *Administration → Create a Project* page or through the
[privileged management API](privileged-api), or an **operator** with
`nest-db project create`. Each time, they name an explicit Owner.

Send the administrator or operator:

- the Project name; and
- your Nest User ID, which `uv run nest auth status` prints as `user:` (it is not
  secret and contains no email address).

They create the Project with you as its Owner (see
[Create a Project for a User](project-onboarding#create-a-project-for-a-user)). As
Owner you then invite the rest of your group. Do not copy session cookies or
tokens into other tools to work around this: an ordinary session is refused at
every Project-creation entry point (`platform_admin_required`).

## The operator's approval

Before the first Run, the **operator** or a platform administrator must, for your
Project:

1. approve it for the Controller's Provider;
2. grant it an HPC Platform; and
3. link a Slurm account it may charge.

No Project role, including Owner, can do these. Send the operator your Project ID
(from the Console, or `uv run nest projects list` once approved) and the Slurm
account your group should be charged to; never send your Nest session file or
tokens. The operator's side is in [Onboard a Project](project-onboarding).

Check what is still missing:

- The Console's *Overview* and *Connect* pages say whether each of your Projects
  can run on this Controller and, if not, why.
- `uv run nest platforms default --project-id <project-id>` prints the granted
  Platform, such as `nano4 (only_eligible)`, or
  `No HPC Platform is granted to this Project.` while step 2 is pending.
- `uv run nest projects list` shows only Projects you may run in **now**: your
  role allows running and the Project is approved and has a Platform.

Run the CLI with the same [connection settings](quickstart#connection-settings)
as your program: Project eligibility is decided by the Controller you name.

## Find and choose your Projects

- In the browser: the [Management Console](console) at
  `https://auth.nest.cerulean.works/console` (Cerulean-hosted Nest) lists every
  Project you belong to under *Your Projects*, with its ID on its page.
- In the terminal: `uv run nest projects list`, `current`, `select`, `use <ID>`
  and `forget`. See [Choose the Project your calls run in](project-selection).

With several eligible Projects, scripts, CI and notebooks need
`NEST_PROJECT_ID="<project-id>"`, a UUID. It always wins, and the Controller still
authorizes it on every call.
