"""Minimal no-auth SOCKS5 CONNECT proxy standing in for the Tailscale sidecar.

Used only by `tests/smoke_tailnet_ssh.sh`. Like userspace `tailscaled`, it
resolves the destination name itself, so the Controller never needs to.
"""

import socket
import struct
import sys
import threading


def read_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Unexpected EOF in SOCKS5 request")
        data.extend(chunk)
    return bytes(data)


def pipe(source: socket.socket, target: socket.socket) -> None:
    try:
        while chunk := source.recv(65536):
            target.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(client: socket.socket) -> None:
    try:
        negotiate(client)
    except OSError:
        pass  # Health checks connect and close without a request.
    finally:
        client.close()


def negotiate(client: socket.socket) -> None:
    version, count = read_exact(client, 2)
    if version != 5 or 0 not in read_exact(client, count):
        client.sendall(b"\x05\xff")
        return
    client.sendall(b"\x05\x00")
    version, command, _, kind = read_exact(client, 4)
    if kind == 3:
        host = read_exact(client, read_exact(client, 1)[0]).decode()
    elif kind == 1:
        host = socket.inet_ntoa(read_exact(client, 4))
    else:
        client.sendall(b"\x05\x08\x00\x01" + bytes(6))
        return
    port = struct.unpack("!H", read_exact(client, 2))[0]
    if version != 5 or command != 1:
        client.sendall(b"\x05\x07\x00\x01" + bytes(6))
        return
    try:
        upstream = socket.create_connection((host, port), timeout=10)
    except OSError as error:
        print(f"CONNECT {host}:{port} failed: {error}", flush=True)
        client.sendall(b"\x05\x04\x00\x01" + bytes(6))
        return
    print(f"CONNECT {host}:{port}", flush=True)
    with upstream:
        upstream.settimeout(None)
        client.sendall(b"\x05\x00\x00\x01" + bytes(6))
        reverse = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
        reverse.start()
        pipe(client, upstream)
        reverse.join()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 1055
    with socket.create_server(("0.0.0.0", port)) as listener:
        print(f"SOCKS5 listening on :{port}", flush=True)
        while True:
            client, _ = listener.accept()
            threading.Thread(target=handle, args=(client,), daemon=True).start()


if __name__ == "__main__":
    main()
