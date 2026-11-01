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

To build and test the Compose deployment, with Docker Engine running:

```bash
docker build -t nest-controller:test .
bash tests/smoke_compose.sh
```

The smoke test uses local execution and temporary SSH fixtures. It checks the
health check, non-root execution, credential copying, remote calls and errors,
and clean container shutdown. It does not submit real Slurm jobs.

## Regenerate Protobuf Code

```bash
uv run python -m grpc_tools.protoc \
    -I src \
    --python_out=src \
    --grpc_python_out=src \
    src/nest/proto/execution.proto
```

## Documentation layout

Documentation lives in `docs/source/`. Put complete workflows in Guide and API
contracts or settings in Reference. Add every Markdown page exactly once to
`_toctree.yml`, using a path relative to that folder without the `.md` extension.

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
