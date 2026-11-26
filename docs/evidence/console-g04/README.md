# Console redesign evidence (G04, G02c)

Screenshots of the Management Console before (`main` at 272308f) and after this
change, at desktop (1280 px) and mobile (390 px) widths, taken with
`tests/console_screenshots.py` in a real Chromium against the test suite's fake Google sign-in
and a throwaway PostgreSQL database. All data is fixture data: the user
"Test Researcher <researcher@example.org>", Projects named `shared` (twice, to show
duplicate names) and `Protein folding`, the Platform `nano`, and random UUIDs.

| Screen | Before | After |
| --- | --- | --- |
| Sign in | `before/01-*` | `after/01-*` |
| Overview / Project switching | `before/02-*` (Projects list) | `after/02-*` |
| Connect to a Project (onboarding) | none | `after/03-*` |
| Project members | `before/04-*` | `after/04-*` |
| Project invitations | part of `before/04-*` | `after/04b-*` |
| Your invitations | `before/05-*` | `after/05-*` |
| Administration | `before/06-*` | `after/06-*`, `after/06b-*` |
| Project access (admin) | `before/07-*` | `after/07-*` |
| CLI approval | `before/08-*` | `after/08-*` |
| Invitation review | `before/09-*` | `after/09-*` |
| States: empty, denied, not available, error, loading | none | `after/10-*` to `after/14-*` |

To reproduce, with `NEST_TEST_DATABASE_URL` set (for the "before" set, run the
same file from a checkout of `main` with `SHOTS_ROUTES=old`):

```bash
cd tests && SHOTS_DIR=/tmp/after SHOTS_ROUTES=new PYTHONPATH=../src:. python -m unittest console_screenshots
```

This directory is review evidence only; it can be removed before or after merging.
