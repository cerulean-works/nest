# Choose the Project your calls run in

Every call to a Controller with session authentication runs in a **Nest Project**:
the server-side group whose membership, Provider approval and Platform grants
decide who may run what. A Nest Project is unrelated to the local directory whose
`pyproject.toml` and `uv.lock` a call sends (`remote(project=...)`).

You no longer have to set `NEST_PROJECT_ID`. When it is unset, the SDK asks the
Controller which of your Projects it would accept a call from now, and chooses:

| Order | Source | When |
| --- | --- | --- |
| 1 | `NEST_PROJECT_ID` | Always wins when set. It is sent as it is; the Controller authorizes it. |
| 2 | Your saved choice | It is still eligible on this Controller. A saved Project that is no longer eligible is forgotten, with a message, and the SDK chooses again. |
| 3 | The only eligible Project | You may run in exactly one Project here. It is used, but not saved. |
| 4 | A prompt | Several are eligible and standard input and standard error are terminals. Your answer is saved. |
| - | An error, nothing runs | Several are eligible and there is no terminal (a batch job, CI, a notebook kernel): `ProjectSelectionError` (`ambiguous`) lists them. None is eligible: `ProjectSelectionError` (`none_eligible`) says to ask for an invitation. |

```text
You can run in several Nest Projects on this Controller:
  1. 3f0c...e1a2  Protein folding (member)
  2. 9b47...0c11  Course: HPC 101 (admin)
Run in which Project? [1-2] 2
nest: saved 9b47...0c11 as your Project here; change it with `nest projects use <ID>` or override it with NEST_PROJECT_ID.
```

## What "eligible" means

A Project is eligible when, at the moment the SDK asks, your role in it allows
submitting (Owner, Admin or Member; not Viewer), the Controller's Provider approves
it, the Controller's HPC Platform is active and granted to it, and a submission
that names no Slurm account would get one: the Project's default account, or the
Platform's Mode A operator default, linked and active. A Project that admission
would refuse with `no_account` is not offered. The Controller reads all of this
from its database each time; nothing is cached, and the answer never names an
account. `NEST_PROJECT_ID` still selects any Project, and admission decides.

The list is advice. **Every execution is authorized again** by the Controller, from
scratch, before anything is staged or submitted. If a membership, approval or
grant is revoked after the SDK chose the Project, the call is refused
(`RemoteAccessDeniedError`), nothing runs, and a saved choice that was refused is
forgotten so the next call chooses again. A revoked session is refused before any
Project is listed.

## Saved choices

Choices are kept in `NEST_PREFERENCES_FILE` (default
`~/.config/nest/preferences.json`), mode `0600` in a `0700` directory. Each holds
only the sign-in origin, the Controller address, your Nest User ID and the
Project ID, with no token. A choice applies only to the same account on the same
Nest and Controller, so signing in as someone else, or pointing the SDK at another
Controller, never reuses it. An unreadable file is ignored and replaced.

## Manage it from the command line

`nest projects` uses the same settings as the SDK (`NEST_PROFILE`,
`NEST_CONTROLLER_ADDRESS`, `NEST_AUTH_URL`) and signs in when needed:

```bash
nest projects list            # the Projects you may run in here; * marks the saved one
nest projects current         # what the next call would use, and why (never prompts)
nest projects select          # choose one from a list, by name, and save it
nest projects use <ID>        # save one (it must be eligible)
nest projects forget          # forget the saved choice
```

`--json` prints machine-readable output for `list` and `current`. In scripts and
CI, set `NEST_PROJECT_ID` instead: it is the only choice that never depends on what
is stored on the machine. The [Management Console](console#connect-to-a-project)
shows the `nest projects use` command with your Project's ID filled in.

### Choose interactively

`nest projects select` lists the Projects you may run in on this Controller by name
and role, in the Controller's order, and marks the saved one:

```text
Nest Projects you may run in on nest-controller.tail8e5ba1.ts.net:50051:
  1. Protein folding (member)
  2. Shared lab (admin)  id 3f0c1a2b
  3. Shared lab (member)  id 9b470c11  (saved)
Select a Project [1-3, Enter to cancel] 1
The SDK now runs in Protein folding (member) [5a92...] on nest-controller.tail8e5ba1.ts.net:50051.
```

- Answer with a number, a full Project ID, or a name exactly one listed Project has
  (ignoring case). Projects that share a name are told apart by the shortest ID
  prefix (at least 8 characters) that separates them, and a shared name is never
  guessed: the prompt asks for the number.
- Enter, `q`, end of input or Ctrl-C cancel without changing anything (exit status
  1, or 130 for Ctrl-C).
- The choice is checked against a fresh listing before it is saved, so a Project
  revoked while you were choosing is not saved. It is saved exactly like
  `nest projects use`: for this sign-in origin, Controller and account only.
- With `NEST_PROJECT_ID` set, it says that the variable keeps overriding what you
  save, and `nest projects current` keeps reporting the variable.
- Without a terminal it changes nothing and exits 1, listing the Projects and
  pointing to `nest projects use <ID>`. With no eligible Project it exits 1 with
  onboarding guidance.

## Compatibility

A Controller from before Project selection does not offer the listing. The SDK
then sends no Project, as before, and that Controller answers as it always did
(it asks for `NEST_PROJECT_ID` when it needs one). A Controller without session
authentication needs no Project; nothing is listed or sent. `nest projects`
reports either case.

## Acceptance checklist (E07a)

- [x] `nest projects select` uses `ListEligibleProjects` and the same preference
  store as the implicit prompt and `nest projects use`.
- [x] Choice by name and role; duplicate names disambiguated by ID, never guessed.
- [x] Cancellation, non-interactive use and no eligible Project change nothing.
- [x] An active `NEST_PROJECT_ID` is explained and still reported as effective.
- [x] The choice is re-authorized before it is saved; `list`, `current`, `use`,
  `forget` and their JSON output are unchanged
  (`tests/test_project_selection.py::ProjectsCommandTests`).

## Acceptance checklist (E07)

- [x] `NEST_PROJECT_ID` keeps the highest precedence and is never second-guessed.
- [x] A valid saved Project is reused; one no longer eligible is forgotten.
- [x] The only eligible Project is used automatically.
- [x] Several eligible Projects prompt on a terminal and fail clearly without one;
  none eligible fails with onboarding guidance; nothing runs in either case.
- [x] Saved choices are scoped by account, sign-in origin and Controller, and hold
  no secret.
- [x] The Controller revalidates membership, approval and grant on every execution;
  a refused saved choice is forgotten.
- [x] Tested against a recording Controller and against the real Controller with
  PostgreSQL (`tests/test_project_selection.py`), including custom endpoints and
  noninteractive use. Live pilot validation is part of the
  [H08 checklist](public-grpc-gateway#evidence).
