"""TLS handshakes through the edge with one SNI name, for
tests/smoke_two_environments.sh (#118).

    python tls_sni_probe.py HOST:PORT NAME --ca-file CA [--count N]

Each handshake asks for NAME with ALPN h2, verifies the certificate against CA for
NAME, and records the certificate's common name or the failure. Prints one JSON
line: {"ok": n, "failed": n, "names": [...], "errors": [...]} and exits 0 whatever
the outcome: the smoke test compares it. Sends no application data.
"""

import argparse
import json
import socket
import ssl


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("address")
    parser.add_argument("name")
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()
    host, _, port = args.address.rpartition(":")
    context = ssl.create_default_context(cafile=args.ca_file)
    context.set_alpn_protocols(["h2"])
    ok, failed, names, errors = 0, 0, set(), set()
    for _ in range(args.count):
        try:
            with (
                socket.create_connection((host, int(port)), timeout=args.timeout) as raw,
                context.wrap_socket(raw, server_hostname=args.name) as tls,
            ):
                certificate = tls.getpeercert() or {}
                subject = {
                    key: value for item in certificate.get("subject", ()) for key, value in item
                }
                if tls.selected_alpn_protocol() != "h2":
                    raise ssl.SSLError("no h2")
                names.add(subject.get("commonName", "?"))
                ok += 1
        except (OSError, ssl.SSLError) as error:
            failed += 1
            errors.add(type(error).__name__)
    print(
        json.dumps({"ok": ok, "failed": failed, "names": sorted(names), "errors": sorted(errors)})
    )


if __name__ == "__main__":
    main()
