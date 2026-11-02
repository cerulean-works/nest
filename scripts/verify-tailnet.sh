#!/usr/bin/env bash
# Read-only connectivity check. Never invokes Nest RPCs or submits Slurm jobs.
set -euo pipefail
cd "$(dirname "$0")/.."
compose=(docker compose -f compose.tailscale.yaml "$@")

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
