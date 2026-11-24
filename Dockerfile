FROM python:3.14-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /opt/nest
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable --compile-bytecode

# HPC runtime image, converted to `images/uv-python314.sif` on the cluster. It runs
# `uv sync` for user environments and `uv run` for the deployed Nest runtime, so it
# carries the same Python and uv as the Controller, plus Git (uv's Git dependency
# sources), CA certificates (verified HTTPS) and the OpenSSH client (ssh:// Git
# URLs). Build it with `docker build --target hpc-runtime`; see
# docs/source/runtime-image.md. It is not the default target.
FROM python:3.14-slim-bookworm AS hpc-runtime

LABEL org.opencontainers.image.source="https://github.com/cerulean-works/nest"
LABEL org.opencontainers.image.description="Nest HPC runtime (Python, uv and Git) for Apptainer"

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /usr/local/bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/*

CMD ["python"]

FROM python:3.14-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/cerulean-works/nest"
LABEL org.opencontainers.image.description="Nest gRPC Controller for Slurm clusters"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu netcat-openbsd openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin nest

WORKDIR /opt/nest
COPY --from=builder /opt/nest/.venv/ /opt/nest/.venv/
COPY --chmod=755 docker/controller-entrypoint.sh /usr/local/bin/nest-controller-entrypoint

ENV PATH="/opt/nest/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NEST_CONTROLLER_BIND="0.0.0.0:50051"

EXPOSE 50051
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "nest.healthcheck"]

ENTRYPOINT ["/usr/local/bin/nest-controller-entrypoint"]
CMD ["python", "-m", "nest.controller"]
