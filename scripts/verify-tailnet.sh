#!/usr/bin/env bash
# Read-only deployment check. Runs only hostname/id over SSH; never submits jobs.
set -euo pipefail
cd "$(dirname "$0")/.."
compose_file=compose.tailscale.yaml
shared=false
if [[ ${1:-} == --shared ]]; then
    compose_file=compose.tailscale-shared.yaml
    shared=true
    shift
fi
compose=(docker compose -f "$compose_file" "$@")

"${compose[@]}" config --quiet
"${compose[@]}" ps
"${compose[@]}" exec -T tailscale tailscale status --json |
    "${compose[@]}" exec -T --user nest controller python -c '
import json, sys
status = json.load(sys.stdin)
if status.get("BackendState") != "Running" or not status.get("TailscaleIPs"):
    sys.exit("FAIL: Tailscale is not authenticated with a Tailnet IP")
if "tag:nest-controller" not in status.get("Self", {}).get("Tags", []):
    sys.exit("FAIL: expected tag:nest-controller; check Auth Key and Tailnet policy")
print("PASS: authenticated tagged Tailscale node:", status["Self"]["DNSName"])
'

if "$shared"; then
    "${compose[@]}" exec -T tailscale tailscale debug prefs |
        "${compose[@]}" exec -T --user nest controller python -c '
import json, sys
prefs = json.load(sys.stdin)
for key in ("ShieldsUp", "RunSSH", "RouteAll"):
    if prefs.get(key) is not False:
        sys.exit(f"FAIL: expected {key}=false; check persisted Tailscale preferences")
print("PASS: inbound allowed; Tailscale SSH and route acceptance disabled")
'
    "${compose[@]}" exec -T tailscale tailscale serve status --json |
        "${compose[@]}" exec -T --user nest controller python -c '
import json, sys
config = json.load(sys.stdin)
if config.get("TCP") != {"50051": {"TCPForward": "controller:50051"}}:
    sys.exit("FAIL: expected only raw TCP 50051 -> controller:50051")
if any(config.get(key) for key in ("Web", "Services", "Foreground")):
    sys.exit("FAIL: unexpected additional Serve handlers")
if any(config.get("AllowFunnel", {}).values()):
    sys.exit("FAIL: Funnel must remain disabled")
print("PASS: private gRPC Serve config active; Funnel disabled")
'
fi

# Use the same SSH alias, user, keys, ProxyCommand, and host-key database as Nest.
"${compose[@]}" exec -T --user nest controller sh -eu -c '
    : "${NEST_SLURM_HOST:?Set NEST_SLURM_HOST to the configured Tailnet SSH alias}"
    ssh -G "$NEST_SLURM_HOST" | python -c '\''
import sys
config = dict(line.rstrip().split(" ", 1) for line in sys.stdin if " " in line)
proxy = config.get("proxycommand", "")
if "-X 5 -x tailscale:1055" not in proxy:
    sys.exit("FAIL: SSH alias does not use the Tailscale SOCKS5 proxy")
if config.get("stricthostkeychecking") not in ("yes", "true"):
    sys.exit("FAIL: SSH must require verified host keys")
print("PASS: SSH proxy and strict host-key verification configured")
'\''
    ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 \
        "$NEST_SLURM_HOST" "hostname && id -un"
'
echo 'PASS: SSH connected to the Login Node; no Slurm job submitted'
