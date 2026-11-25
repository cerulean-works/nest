# Public gRPC transport PoC (H06)

Staging-only feasibility PoC for exposing Nest's gRPC on :443. The findings,
recommendation, test matrix, measured results, rollback and pending live checks
are in [docs/source/public-grpc-transport.md](../../docs/source/public-grpc-transport.md).

Nothing here touches production DNS, routes, firewalls or the hosted Controller.
The origin (`origin.py`) is synthetic: it serves the real `ExecutionService`
handler with a synthetic token check and **no Slurm backend**, plus an echo/sleep
`TransportProbe` service. The probe only sends requests that are rejected before
deserialization, so no code is ever executed and no job is submitted.

```bash
cd poc/public-grpc
uv run python make_certs.py .secrets/certs
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > .secrets/token
sudo chown 10001 .secrets/token              # the origin container runs as UID 10001
docker compose --profile envoy up -d --build  # or --profile traefik
NEST_POC_TOKEN_FILE=.secrets/token uv run python probe.py 127.0.0.1:8443 \
  --server-name grpc.staging.nest.invalid --ca-file .secrets/certs/ca.crt --silent 30
docker compose --profile envoy --profile traefik down
```

`probe.py --help` lists the long-lived options (`--silent`, `--long-chat`,
`--long-stream`, `--keepalive`) and `--origin` for the bypass check. Output is
JSON lines; the token and payloads are never printed. `.secrets/` is ignored by Git.
