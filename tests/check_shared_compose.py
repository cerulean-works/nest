"""Check the rendered deployment's isolation and Dokploy path resolution.

Needs Compose, but no Docker daemon, credentials, or Tailnet connection.
Set NEST_COMPOSE_COMMAND to a standalone Compose binary if needed.
"""

import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_SETTINGS = {
    "NEST_CONTROLLER_BIND",
    "NEST_EXECUTOR",
    "NEST_SLURM_HOST",
    "NEST_SLURM_ACCOUNT",
    "NEST_SLURM_WORKDIR",
    "NEST_LOG_LEVEL",
    "NEST_SSH_TIMEOUT",
    "NEST_RESULT_TIMEOUT",
    "NEST_ENVIRONMENT_TIMEOUT",
    "NEST_POLL_INTERVAL",
    "NEST_SLURM_LOGIN_NODES",
    "NEST_SLURM_SSH_MODE",
    "NEST_SLURM_SSH_USER",
    "NEST_SLURM_PARTITION",
    "NEST_SLURM_TIME",
    "NEST_SSH_MODES_ALLOWED",
    "NEST_SSH_KNOWN_HOSTS",
    "NEST_SSH_KNOWN_HOSTS_DATA",
    "NEST_SSH_IDENTITY_FILE",
    "NEST_SSH_PRIVATE_KEY",
    "NEST_TLS_CERT_FILE",
    "NEST_TLS_KEY_FILE",
    "NEST_PUBLIC_URL",
    "NEST_DATABASE_URL",
    "NEST_DATABASE_REQUIRED",
    "NEST_DATABASE_PASSWORD_FILE",
    "NEST_DATABASE_SSLMODE",
    "NEST_DATABASE_SSLROOTCERT",
    "NEST_DATABASE_SSLCERT",
    "NEST_DATABASE_SSLKEY",
    "NEST_DATABASE_POOL_MIN_SIZE",
    "NEST_DATABASE_POOL_MAX_SIZE",
    "NEST_DATABASE_CONNECT_TIMEOUT",
    "NEST_DATABASE_POOL_TIMEOUT",
}
COMMAND = shlex.split(os.environ.get("NEST_COMPOSE_COMMAND", "docker compose"))


def render(filename, directory=ROOT, *, overrides=(), **settings):
    env = os.environ.copy()
    env.update(
        COMPOSE_PROJECT_NAME="nest-shared-check",
        NEST_SSH_DIR="./ssh",
        NEST_TAILSCALE_AUTHKEY_FILE="./secrets/tailscale-authkey",
        NEST_TAILSCALE_SERVE_DIR="./docker/tailscale-serve",
        NEST_CONTROLLER_HOST_IP="127.0.0.1",
        NEST_CONTROLLER_PORT="50051",
    )
    env.update(settings)
    result = subprocess.run(
        COMMAND
        + [
            "--env-file",
            "/dev/null",
            "--project-directory",
            str(directory),
            *(arg for name in (filename, *overrides) for arg in ("-f", str(ROOT / name))),
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def main():
    shared = render("compose.tailscale-shared.yaml")
    outbound = render("compose.tailscale.yaml")
    controller, sidecar = (shared["services"][key] for key in ("controller", "tailscale"))
    for service in (controller, sidecar):
        assert not service.get("ports"), "Shared deployment must not publish host ports"
        assert not service.get("privileged")
        assert not service.get("cap_add")
        assert not service.get("devices")
        assert not service.get("network_mode"), "Keep independent bridge namespaces"
        assert not service.get("extends"), "Dokploy must receive self-contained services"
        assert service["labels"]["traefik.enable"] == "false"
        assert set(service["networks"]) == {"tailnet"}
    assert controller["environment"]["NEST_CONTROLLER_BIND"] == "0.0.0.0:50051"
    assert controller["networks"]["tailnet"]["aliases"] == ["controller"]
    assert sidecar["networks"]["tailnet"]["aliases"] == ["tailscale"]
    assert sidecar["environment"]["TS_USERSPACE"] == "true"
    assert sidecar["environment"]["TS_AUTH_ONCE"] == "true"
    assert sidecar["environment"]["TS_AUTHKEY"] == "file:/run/secrets/tailscale_authkey"
    assert sidecar["environment"]["TS_SOCKS5_SERVER"] == ":1055"
    assert "--shields-up=false" in sidecar["environment"]["TS_EXTRA_ARGS"].split()
    assert "--ssh=false" in sidecar["environment"]["TS_EXTRA_ARGS"].split()
    assert sidecar["cap_drop"] == ["ALL"]
    assert controller["read_only"]
    assert controller["volumes"][0]["read_only"]
    assert not controller.get("secrets"), "Controller must not receive the Auth Key"
    assert sidecar["volumes"][0] == outbound["services"]["tailscale"]["volumes"][0]
    assert (
        "--shields-up" in outbound["services"]["tailscale"]["environment"]["TS_EXTRA_ARGS"].split()
    )
    assert outbound["services"]["controller"]["ports"], "Keep original deployment behavior"
    # The four Controller definitions are copies; keep their settings in step.
    controllers = {
        name: render(name)["services"]["controller"]
        for name in ("compose.yaml", "compose-nop.yaml", "compose.tailscale.yaml")
    }
    controllers["compose.tailscale-shared.yaml"] = controller
    for name, service in controllers.items():
        assert set(service["environment"]) == CONTROLLER_SETTINGS, (name, service["environment"])
        assert service["environment"]["NEST_CONTROLLER_BIND"] == "0.0.0.0:50051", name
        assert not service.get("secrets"), f"{name}: Controller must not receive the Auth Key"
        assert service["read_only"], name
        assert service["volumes"][0]["target"] == "/run/nest-ssh", name
        assert service["volumes"][0]["read_only"], name
    assert not controllers["compose-nop.yaml"].get("ports")
    # Mode B renders without .env (render uses --env-file /dev/null), without an SSH
    # directory, and without any Tailscale setting reaching the Controller.
    managed = render(
        "compose.yaml", overrides=("compose.managed-ssh.yaml",), NEST_SSH_DIR="/nonexistent"
    )["services"]["controller"]
    assert not managed.get("volumes"), "Mode B must not need a host mount"
    assert set(managed["environment"]) == CONTROLLER_SETTINGS
    assert managed["environment"]["NEST_SLURM_SSH_MODE"] == "B"
    # Without the development override no database service exists, and an external
    # NEST_DATABASE_URL passes through unchanged.
    for name, rendered in (("compose.yaml", render("compose.yaml")), ("shared", shared)):
        assert set(rendered["services"]) <= {"controller", "tailscale"}, name
    external = render("compose.yaml", NEST_DATABASE_URL="postgresql://nest@db.example/nest")
    assert (
        external["services"]["controller"]["environment"]["NEST_DATABASE_URL"]
        == "postgresql://nest@db.example/nest"
    )
    dev = render("compose.yaml", overrides=("compose.postgres-dev.yaml",))
    services = dev["services"]
    assert set(services) == {"controller", "postgres", "migrate"}
    assert set(services["controller"]["environment"]) == CONTROLLER_SETTINGS
    assert services["controller"]["environment"]["NEST_DATABASE_URL"].endswith(
        "@postgres:5432/nest"
    )
    assert services["controller"]["environment"]["NEST_DATABASE_REQUIRED"] == "true"
    assert services["controller"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["migrate"]["entrypoint"] == ["nest-db"]
    assert services["migrate"]["command"] == ["upgrade"]
    assert not services["postgres"].get("ports"), "Development database must not be published"
    for deployment in (shared, outbound):
        secrets = deployment["services"]["tailscale"]["secrets"]
        assert [secret["source"] for secret in secrets] == ["tailscale_authkey"]
    serve = json.loads((ROOT / "docker/tailscale-serve/serve.json").read_text())
    assert serve == {"TCP": {"50051": {"TCPForward": "controller:50051"}}, "AllowFunnel": {}}

    with tempfile.TemporaryDirectory() as temporary:
        checkout = Path(temporary) / "code"
        checkout.mkdir()
        deployed = render(
            "compose.tailscale-shared.yaml",
            directory=checkout,
            NEST_SSH_DIR="../files/nest-ssh",
            NEST_TAILSCALE_AUTHKEY_FILE="../files/tailscale/authkey",
            NEST_TAILSCALE_SERVE_DIR="../files/tailscale/serve",
        )
        files = checkout.parent / "files"
        assert deployed["services"]["controller"]["volumes"][0]["source"] == str(files / "nest-ssh")
        assert deployed["secrets"]["tailscale_authkey"]["file"] == str(files / "tailscale/authkey")
        assert deployed["services"]["tailscale"]["volumes"][1]["source"] == str(
            files / "tailscale/serve"
        )
    print(
        "PASS: rendered Compose isolation, Controller settings, development database, state "
        "reuse, and Dokploy paths"
    )


if __name__ == "__main__":
    main()
