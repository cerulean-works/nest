# Develop Nest and its docs

Install development dependencies:

```bash
uv sync
```

Run the local examples:

```bash
uv run python examples/hello.py
uv run python examples/serialization.py
```

Run the Controller integration tests:

```bash
uv run python -m unittest discover -s tests -v
```

The PostgreSQL tests (migrations, `nest-db`, Controller restart persistence) need a
real server and are skipped without one. Point them at any PostgreSQL 14 or newer
whose user may create databases; each test creates and drops its own database:

```bash
docker run -d --name nest-test-postgres -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:5432:5432 postgres:17
export NEST_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres
uv run pytest tests/test_database.py
```

Two groups need more than one server:

- **TLS tests** need a second, TLS-enabled server whose certificate comes from
  `tests/make_postgres_tls_certs.sh`. Set `NEST_TEST_TLS_DATABASE_URL` (host
  `localhost`), `NEST_TEST_TLS_CA_FILE` and `NEST_TEST_TLS_OTHER_CA_FILE`:

  ```bash
  bash tests/make_postgres_tls_certs.sh /tmp/nest-tls
  docker run -d --name nest-test-postgres-tls -p 127.0.0.1:5433:5432 -e POSTGRES_PASSWORD=postgres \
      -v /tmp/nest-tls:/certs:ro postgres:17 bash -c 'cp /certs/server.* /tmp/ && chown postgres /tmp/server.* && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key'
  export NEST_TEST_TLS_DATABASE_URL=postgresql://postgres:postgres@localhost:5433/postgres
  export NEST_TEST_TLS_CA_FILE=/tmp/nest-tls/ca.crt NEST_TEST_TLS_OTHER_CA_FILE=/tmp/nest-tls/other-ca.crt
  ```

- **The backup and restore test** needs `pg_dump` and `pg_restore` at least as new
  as the server, on `PATH` or in `NEST_TEST_PG_BIN`.

- **The email tests** (`tests/test_email.py`) need `openssl` on `PATH` to create
  throwaway certificates for the in-process fake SMTP server in
  `tests/fake_smtp.py`; they never contact a real mail provider. Their outbox and
  Controller delivery cases use `NEST_TEST_DATABASE_URL`. See
  [Send email with SMTP](email#test-email-delivery).

CI sets `NEST_TEST_DATABASE_REQUIRED=1`, which turns all of these skips into
failures.

The real-browser tests (`tests/test_cli_login_browser.py`) also need
`NEST_TEST_DATABASE_URL`, and a Chromium for Playwright; without one they are
skipped. CI installs it and sets `NEST_TEST_BROWSER_REQUIRED=1`:

```bash
uv run playwright install chromium
uv run pytest tests/test_cli_login_browser.py
```

### Run the tests in parallel

The development dependencies include `pytest-xdist`. Without options pytest runs the
suite serially; `--numprocesses` (`-n`) spreads tests over worker processes:

```bash
uv run pytest                          # serial
uv run pytest --numprocesses 8         # 8 workers, as in CI
uv run pytest --numprocesses 8 --cov=src/nest --durations 20
```

Coverage from every worker is combined into one report. The suite is worker-safe:
each PostgreSQL test creates its own randomly named database (and any login role it
needs), servers listen on free ports, and temporary files go to per-test directories.
Checks that read server-wide views, such as `pg_locks`, filter to the test's own
database (`ADVISORY_LOCK_COUNT` in `tests/postgres_fixture.py`), so other workers'
databases never leak in. Keep new tests that way; a test that genuinely cannot share
the server belongs in its own group rather than behind retries or longer timeouts.

Most tests wait on subprocesses, servers and PostgreSQL rather than the CPU, so more
workers than cores still help. Measured on a 4-vCPU machine, the same commit and test
set (852 tests) with coverage:

| Workers | Wall time |
| ------- | --------- |
| serial  | 409 s     |
| 2       | 226 s     |
| 4       | 136 s     |
| 6       | 96 s      |
| 8       | 88 s      |
| 12      | 84 s      |

CI uses eight workers: beyond that, wall time stops improving.

### CI topology

Each pull request runs four workflows concurrently. A new commit cancels the pull
request's superseded runs; runs for pushes to `main`, tags and manual dispatches are
never cancelled.

- **Tests** (`test (3.14)`): one job with PostgreSQL 17, a TLS PostgreSQL, the
  PostgreSQL 17 client tools and Chromium, running the full suite with coverage on
  eight workers.
- **Code Quality**: ruff lint, ruff format and ty.
- **Controller container**: five parallel `smoke (...)` jobs, each on its own
  runner with its own Controller image build: `controller` (Compose file validation,
  the in-image unittest suite, `smoke_compose.sh`), `postgres`, `env-only`,
  `postgres-upgrade` and `tailnet-ssh`. The `test` job keeps the workflow's single
  check and fails unless every smoke job succeeded.
- **HPC runtime image**: builds the `hpc-runtime` target once as an OCI archive,
  runs `smoke_hpc_runtime.sh` against that exact image and records its manifest.
  Only pushes to `main` and release tags publish to GHCR, from separate jobs; see
  [Published images](runtime-image#published-images).

To build and test the Compose deployment, with Docker Engine running:

```bash
docker build -t nest-controller:test .
bash tests/smoke_compose.sh
```

The smoke test uses local execution and temporary SSH fixtures. It checks the
health check, non-root execution, credential copying, remote calls and errors,
and clean container shutdown. It does not submit real Slurm jobs.

`bash tests/smoke_compose_postgres.sh` checks the database deployments: the
default Compose deployment starts no database, the `local-db` profile starts
PostgreSQL with a file-based password, data survives `down` and `up`, the
Controller and database run independently, and the documented backup and restore
commands work. `bash tests/smoke_compose_postgres_upgrade.sh` runs the documented
PostgreSQL 17 to 18 upgrade and its rollback, and checks that records and the
schema history survive both.

## Regenerate Protobuf Code

```bash
uv run python -m grpc_tools.protoc \
    -I src \
    --python_out=src \
    --pyi_out=src \
    --grpc_python_out=src \
    src/nest/proto/execution.proto
```

## Documentation layout

Documentation lives in `docs/source/`. Put complete workflows in Guide and API
contracts or settings in Reference. Add every Markdown page exactly once to
`_toctree.yml`, using a path relative to that folder without the `.md` extension.

Guide sections separate audiences: *Get started* and *User guide* for people who
call `.remote()`, the *Operator guide* sections for hosts who deploy and onboard,
and *Contributing*. Keep deployment, recovery and security runbooks out of the
Quickstart. A page's URL depends only on its file name and on Guide versus
Reference, not on its section, so moving a page between Guide sections keeps its
links working; renaming it does not. `tests/test_docs_quickstart.py` runs the
Quickstart's `main.py` and checks the navigation.

API pages use `[[autodoc]]` markers to render Python docstrings. Keep each
documented object on one Reference page so cross-references have one target.

## Preview and validate docs

Install doc-builder into the development environment after `uv sync`:

```bash
uv pip install "git+https://github.com/cerulean-works/doc-builder"
.venv/bin/doc-builder preview nest docs/source
```

The preview requires Node.js 20 or newer and pnpm. It opens at
`http://localhost:5173/nest/main/guide/`.

Check links and build the full HTML site:

```bash
.venv/bin/doc-builder check-links docs/source --package_name nest
.venv/bin/doc-builder build nest docs/source --version main --build_dir build --html
```

Do not use `--not_python_module`: Reference pages import Nest to read its
docstrings. A later `uv sync` can remove doc-builder because it is installed as
an extra development tool; reinstall it before previewing again.

## Publishing

The `Docs` workflow builds and publishes on pushes to `main` or a `v*` tag.
It can also be run manually from GitHub Actions. The repository needs access to
`DOC_BUILDER_R2_ACCESS_KEY_ID`, `DOC_BUILDER_R2_SECRET_ACCESS_KEY`, and
`CLOUDFLARE_ACCOUNT_ID`.

The `main` site is served at
[docs.cerulean.works/nest/main/guide/](https://docs.cerulean.works/nest/main/guide/).
A tag publishes a separate version. See the
[doc-builder publishing guide](https://github.com/cerulean-works/doc-builder/blob/main/docs/source/publishing.md)
for reusable workflow details.
