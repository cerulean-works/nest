# Public SSH egress feasibility and relay contract PoC (D03)

No-job feasibility tooling and a synthetic relay contract for D03. The approval
record, operator steps, contract, evidence classes and open decisions are in
[docs/source/ssh-relay-feasibility.md](../../docs/source/ssh-relay-feasibility.md).

Nothing here is a production relay or is wired into the Controller. Tests and CI
use loopback fixtures only. Run the probe against a real login node only from the
Controller host, and only after the Platform operator has approved it.

| File | What it is |
| --- | --- |
| `egress_probe.py` | No-job smoke: resolve once and validate the addresses, connect over TCP, read the SSH identification line (it sends zero bytes), and run `ssh-keyscan` against the validated address, then compare the keys it observed there with independent pins. Prints JSON lines |
| `egress_policy.py` | Shared checks: public, non-Tailnet addresses, the port allowlist, approved addresses, OpenSSH SHA256 fingerprints |
| `relay_contract.py` | Synthetic reference model of the relay: request parsing, authorization order, allowlist, rebinding defense, limits, opaque pump with backpressure and half-close, and audit |
| `relay.proto` | Draft `nest.relay.v1alpha.SshRelay` contract (not compiled) |
| `fake_sshd.py` | Loopback-only fixture that sends an SSH banner and echoes bytes. It is not an SSH server |

```bash
cd poc/ssh-egress
python3 fake_sshd.py --port 2222 &
python3 egress_probe.py --host 127.0.0.1 --port 2222 --allowed-port 2222 --fixture
kill %1
```

Fixture tests: `uv run pytest tests/test_ssh_egress_poc.py`.
