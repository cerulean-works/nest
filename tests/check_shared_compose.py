"""Check the rendered deployment's isolation and Dokploy path resolution.

Needs Compose, but no Docker daemon, credentials, or Tailnet connection.
Set NEST_COMPOSE_COMMAND to a standalone Compose binary if needed.
"""

import json
import os
import re
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
    "NEST_CONTROLLER_AUTH",
    "NEST_CONTROLLER_ALLOW_PLAINTEXT",
    "NEST_PROVIDER_ALLOWED_PROJECTS",
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
    "NEST_EMAIL_ENABLED",
    "NEST_EMAIL_PROVIDER",
    "NEST_EMAIL_FROM",
    "NEST_EMAIL_MAX_ATTEMPTS",
    "NEST_EMAIL_RETRY_DELAY",
    "NEST_EMAIL_RETRY_MAX_DELAY",
    "NEST_EMAIL_POLL_INTERVAL",
    "NEST_SMTP_HOST",
    "NEST_SMTP_PORT",
    "NEST_SMTP_STARTTLS",
    "NEST_SMTP_IMPLICIT_TLS",
    "NEST_SMTP_USERNAME",
    "NEST_SMTP_PASSWORD",
    "NEST_SMTP_PASSWORD_FILE",
    "NEST_SMTP_CA_FILE",
    "NEST_SMTP_TIMEOUT",
    "NEST_BOOTSTRAP_ADMIN_GOOGLE_EMAIL",
}
COMMAND = shlex.split(os.environ.get("NEST_COMPOSE_COMMAND", "docker compose"))


def render(filename, directory=ROOT, *, overrides=(), profiles=(), **settings):
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
            *(arg for profile in profiles for arg in ("--profile", profile)),
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
    # HPC Platform Profiles reach every Controller definition from one optional env
    # file, unchanged, so native and Docker deployments read the same settings. No
    # `environment` entry may shadow them with an empty value.
    with tempfile.TemporaryDirectory() as temporary:
        platforms_file = Path(temporary) / "platforms.env"
        platforms = {
            "NEST_PLATFORMS": "campus,lab",
            "NEST_DEFAULT_PLATFORM": "lab",
            "NEST_PLATFORM_CAMPUS_LOGIN_NODES": "login1.example.edu",
            "NEST_PLATFORM_CAMPUS_ACCOUNT": "physics",
            "NEST_PLATFORM_LAB_LOGIN_NODES": "lab-login:2222",
            "NEST_PLATFORM_LAB_ACCOUNT": "lab",
        }
        platforms_file.write_text("".join(f"{k}={v}\n" for k, v in platforms.items()))
        for name in (*controllers, "compose.managed-ssh.yaml"):
            overrides = ("compose.managed-ssh.yaml",) if name == "compose.managed-ssh.yaml" else ()
            service = render(
                "compose.yaml" if overrides else name,
                overrides=overrides,
                NEST_PLATFORMS_ENV_FILE=str(platforms_file),
            )["services"]["controller"]
            assert set(service["environment"]) == CONTROLLER_SETTINGS | set(platforms), name
            for key, value in platforms.items():
                assert service["environment"][key] == value, (name, key)
        for service in controllers.values():
            assert not any(k.startswith("NEST_PLATFORM") for k in service["environment"])
    # Mode B renders without .env (render uses --env-file /dev/null), without an SSH
    # directory, and without any Tailscale setting reaching the Controller.
    managed = render(
        "compose.yaml", overrides=("compose.managed-ssh.yaml",), NEST_SSH_DIR="/nonexistent"
    )["services"]["controller"]
    assert not managed.get("volumes"), "Mode B must not need a host mount"
    assert set(managed["environment"]) == CONTROLLER_SETTINGS
    assert managed["environment"]["NEST_SLURM_SSH_MODE"] == "B"
    # The default deployment starts no database: the Controller only receives an
    # external NEST_DATABASE_URL, and nothing waits for or links to PostgreSQL.
    default = render("compose.yaml")
    for name, rendered in (("compose.yaml", default), ("shared", shared), ("outbound", outbound)):
        assert set(rendered["services"]) <= {"controller", "tailscale"}, name
        assert "postgres-data" not in rendered.get("volumes", {}), name
        assert not rendered["services"]["controller"].get("depends_on", {}).get("postgres")
    external = render("compose.yaml", NEST_DATABASE_URL="postgresql://nest@db.example/nest")
    assert (
        external["services"]["controller"]["environment"]["NEST_DATABASE_URL"]
        == "postgresql://nest@db.example/nest"
    )
    # The local-db profile adds only PostgreSQL. The Controller is unchanged, so
    # either service can be started, stopped, or replaced without the other.
    local = render("compose.yaml", profiles=("local-db",))
    assert set(local["services"]) == {"controller", "postgres"}
    assert local["services"]["controller"] == default["services"]["controller"]
    postgres = local["services"]["postgres"]
    assert postgres["profiles"] == ["local-db"]
    assert re.fullmatch(r"postgres:\d+\.\d+(-[a-z]+)?", postgres["image"]), postgres["image"]
    assert postgres["restart"] == "unless-stopped"
    assert postgres["security_opt"] == ["no-new-privileges:true"]
    assert postgres["healthcheck"]["test"][:2] == ["CMD", "pg_isready"]
    # Data lives in a named volume, which `docker compose down` keeps.
    [data] = postgres["volumes"]
    assert data["type"] == "volume", "Database data must be in a named volume"
    assert data["source"] == "postgres-data"
    assert data["target"] == "/var/lib/postgresql/data"
    assert local["volumes"]["postgres-data"]["name"] == "nest-shared-check_postgres-data"
    assert not local["volumes"]["postgres-data"].get("external")
    # The password comes from a file, never from Compose or the environment.
    environment = postgres["environment"]
    assert "POSTGRES_PASSWORD" not in environment
    assert "POSTGRES_HOST_AUTH_METHOD" not in environment
    assert environment["POSTGRES_PASSWORD_FILE"] == "/run/secrets/nest_postgres_password"
    assert [secret["source"] for secret in postgres["secrets"]] == ["nest_postgres_password"]
    assert local["secrets"]["nest_postgres_password"]["file"] == str(
        ROOT / "ssh/nest-postgres-password"
    ), "Default password file is inside NEST_SSH_DIR, shared with the Controller"
    moved = render("compose.yaml", profiles=("local-db",), NEST_POSTGRES_PASSWORD_FILE="/run/pw")
    assert moved["secrets"]["nest_postgres_password"]["file"] == "/run/pw"
    # Published on loopback only, for a native Controller, nest-db, or pg_dump. The
    # server has no TLS, so no variable can widen the interface.
    [port] = postgres["ports"]
    assert (port["host_ip"], port["published"], port["target"]) == ("127.0.0.1", "5432", 5432)
    widened = render("compose.yaml", profiles=("local-db",), NEST_POSTGRES_HOST_IP="0.0.0.0")
    assert widened["services"]["postgres"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert "NEST_POSTGRES_HOST_IP" not in (ROOT / "compose.yaml").read_text()
    # A major-version upgrade can move the image's data directory without edits.
    upgraded = render(
        "compose.yaml",
        profiles=("local-db",),
        NEST_POSTGRES_IMAGE="postgres:18.1",
        NEST_POSTGRES_DATA_DIR="/var/lib/postgresql",
    )["services"]["postgres"]
    assert upgraded["image"] == "postgres:18.1"
    assert upgraded["volumes"][0]["target"] == "/var/lib/postgresql"
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
        "PASS: rendered Compose isolation, Controller and Platform settings, default and local-db "
        "databases, state reuse, and Dokploy paths"
    )


if __name__ == "__main__":
    main()
