FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
WORKDIR /opt/nest
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-editable --compile-bytecode

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/cerulean-works/nest"
LABEL org.opencontainers.image.description="Nest gRPC Controller for Slurm clusters"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu openssh-client \
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
    CMD ["python", "-c", "import grpc; channel = grpc.insecure_channel('127.0.0.1:50051'); grpc.channel_ready_future(channel).result(timeout=3); channel.close()"]

ENTRYPOINT ["/usr/local/bin/nest-controller-entrypoint"]
CMD ["python", "-m", "nest.controller"]
