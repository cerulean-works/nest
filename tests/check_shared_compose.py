"""Check the rendered deployment's isolation and Dokploy path resolution.

Needs Compose, but no Docker daemon, credentials, or Tailnet connection.
Set NEST_COMPOSE_COMMAND to a standalone Compose binary if needed.
"""

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
COMMAND = shlex.split(os.environ.get("NEST_COMPOSE_COMMAND", "docker compose"))


def render(filename, directory=ROOT, **settings):
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
        COMMAND + ["--env-file", "/dev/null", "--project-directory", str(directory),
                   "-f", str(ROOT / filename), "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True,
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
    assert "--shields-up" in outbound["services"]["tailscale"]["environment"]["TS_EXTRA_ARGS"].split()
    assert outbound["services"]["controller"]["ports"], "Keep original deployment behavior"
    serve = json.loads((ROOT / "docker/tailscale-serve/serve.json").read_text())
    assert serve == {"TCP": {"50051": {"TCPForward": "controller:50051"}}, "AllowFunnel": {}}

    with tempfile.TemporaryDirectory() as temporary:
        checkout = Path(temporary) / "code"
        checkout.mkdir()
        deployed = render(
            "compose.tailscale-shared.yaml", directory=checkout,
            NEST_SSH_DIR="../files/nest-ssh",
            NEST_TAILSCALE_AUTHKEY_FILE="../files/tailscale/authkey",
            NEST_TAILSCALE_SERVE_DIR="../files/tailscale/serve",
        )
        files = checkout.parent / "files"
        assert deployed["services"]["controller"]["volumes"][0]["source"] == str(files / "nest-ssh")
        assert deployed["secrets"]["tailscale_authkey"]["file"] == str(files / "tailscale/authkey")
        assert deployed["services"]["tailscale"]["volumes"][1]["source"] == str(files / "tailscale/serve")
    print("PASS: rendered shared Compose isolation, state reuse, and Dokploy ../files paths")


if __name__ == "__main__":
    main()
