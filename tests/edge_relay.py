"""Stand-in for Dokploy's Traefik TCP passthrough in front of the public gRPC gateway.

Used only by `tests/smoke_public_grpc_networks.sh`. Like Traefik's HostSNI router
with `proxyProtocol: {version: 2}`, it accepts TLS on `LISTEN_PORT`, connects to
`UPSTREAM_HOST:UPSTREAM_PORT` from its own edge-network address, prepends a PROXY
protocol v2 header naming the real client, and relays bytes without terminating TLS.

    python edge_relay.py LISTEN_PORT UPSTREAM_HOST UPSTREAM_PORT
"""

import socket
import struct
import sys
import threading

SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"


def proxy_v2_header(client: tuple[str, int], destination: tuple[str, int]) -> bytes:
    """A PROXY protocol v2 PROXY command for TCP over IPv4."""
    body = (
        socket.inet_aton(client[0])
        + socket.inet_aton(destination[0])
        + struct.pack("!HH", client[1], destination[1])
    )
    return SIGNATURE + bytes([0x21, 0x11]) + struct.pack("!H", len(body)) + body


def pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, sink):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def relay(downstream: socket.socket, client, upstream_host: str, upstream_port: int) -> None:
    try:
        upstream = socket.create_connection((upstream_host, upstream_port), timeout=10)
    except OSError:
        downstream.close()
        return
    upstream.settimeout(None)
    upstream.sendall(proxy_v2_header(client, upstream.getpeername()[:2]))
    threading.Thread(target=pump, args=(downstream, upstream), daemon=True).start()
    pump(upstream, downstream)


def main() -> None:
    listen_port, upstream_host, upstream_port = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    server = socket.create_server(("0.0.0.0", listen_port), reuse_port=False)
    print(f"edge relay :{listen_port} -> {upstream_host}:{upstream_port}", flush=True)
    while True:
        downstream, client = server.accept()
        threading.Thread(
            target=relay, args=(downstream, client, upstream_host, upstream_port), daemon=True
        ).start()


if __name__ == "__main__":
    main()
