"""Container healthcheck: `python -m nest.healthcheck` exits 0 when the listener is up.

Reads the same `NEST_CONTROLLER_BIND` and `NEST_TLS_*` settings as the Controller and
connects to the listener's port on `127.0.0.1`. A plaintext listener must accept a
gRPC channel; a TLS listener must complete a TLS handshake. The certificate is not
verified because the check runs inside the Controller's own container.
"""

import os
import socket
import ssl
import sys

import grpc

from .config import DEFAULT_CONTROLLER_BIND


def main() -> int:
    port = int(os.environ.get("NEST_CONTROLLER_BIND", DEFAULT_CONTROLLER_BIND).rpartition(":")[2])
    if os.environ.get("NEST_TLS_CERT_FILE", "").strip():
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.set_alpn_protocols(["h2"])
        with (
            socket.create_connection(("127.0.0.1", port), timeout=3) as raw,
            context.wrap_socket(raw),
        ):
            return 0
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        grpc.channel_ready_future(channel).result(timeout=3)
    finally:
        channel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
