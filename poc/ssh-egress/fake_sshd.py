#!/usr/bin/env python3
"""
A disposable local stand-in for a Platform login node's SSH port (D03 fixture).

It is **not** an SSH server. It sends an RFC 4253 identification line, then echoes
whatever bytes it receives, so the relay contract and the egress probe can be
exercised against opaque bytes with no real host, account, password or Slurm.
When the peer half-closes, it half-closes too, so EOF propagation is observable.
It records how many bytes each connection sent it, never their content.

    python fake_sshd.py --port 2222          # listens on 127.0.0.1 only
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

BANNER = b"SSH-2.0-NestFixture_0.1 synthetic\r\n"


@dataclass
class FakeSshd:
    """
    A loopback-only echo server that greets with an SSH identification line.

    Attributes:
        banner (`bytes`): Sent first on every connection; empty sends nothing.
        echo (`bool`): Echo received bytes back to the peer.
        received (`list[int]`): Bytes received per finished connection.
        handler: Replaces the greet-and-echo behaviour, for misbehaving-peer tests.
    """

    banner: bytes = BANNER
    echo: bool = True
    received: list[int] = field(default_factory=list)
    connections: int = 0
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]] | None = None
    _server: asyncio.Server | None = None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError("The fixture listens on loopback only")
        self._server = await asyncio.start_server(self.handler or self._handle, host, port)
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        count = 0
        try:
            if self.banner:
                writer.write(self.banner)
                await writer.drain()
            while chunk := await reader.read(65536):
                count += len(chunk)
                if self.echo:
                    writer.write(chunk)
                    await writer.drain()
            if writer.can_write_eof():
                writer.write_eof()
        except ConnectionError:
            pass
        finally:
            self.received.append(count)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass


async def _serve(port: int) -> None:
    server = FakeSshd()
    bound = await server.start(port=port)
    print(f"fake sshd listening on 127.0.0.1:{bound}", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--port", type=int, default=2222)
    asyncio.run(_serve(parser.parse_args().port))


if __name__ == "__main__":
    main()
