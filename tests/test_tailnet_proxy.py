"""Offline checks for the container's OpenSSH/SOCKS5 path; no Tailnet credentials."""

import concurrent.futures
import shutil
import socket
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "docker/ssh_config.tailscale.example"


@unittest.skipUnless(shutil.which("ssh") and shutil.which("nc"), "requires OpenSSH and OpenBSD nc")
class TailnetProxyTests(unittest.TestCase):
    def test_ssh_template_requires_verified_host_keys(self):
        result = subprocess.run(
            ["ssh", "-F", str(TEMPLATE), "-G", "hpc"],
            capture_output=True,
            text=True,
            check=True,
        )
        config = dict(line.split(" ", 1) for line in result.stdout.splitlines())
        self.assertIn(config["stricthostkeychecking"], ("yes", "true"))
        self.assertEqual(config["batchmode"], "yes")
        self.assertEqual(config["identitiesonly"], "yes")
        self.assertEqual(config["userknownhostsfile"], str(Path.home() / ".ssh/known_hosts"))
        self.assertEqual(config["proxycommand"], "/usr/bin/nc -X 5 -x tailscale:1055 %h %p")

    def test_ssh_sends_destination_and_stream_through_socks5(self):
        # A deliberately unresolvable name proves DNS is delegated to SOCKS5.
        hostname = "nest-hpc-unresolvable.invalid"
        with socket.socket() as listener, tempfile.TemporaryDirectory() as directory:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5)
            port = listener.getsockname()[1]
            config = Path(directory) / "config"
            config.write_text(
                TEMPLATE.read_text()
                .replace("login.YOUR-TAILNET.ts.net", hostname)
                .replace("YOUR-HPC-USER", "fixture")
                .replace("tailscale:1055", f"127.0.0.1:{port}")
            )

            def receive_proxy_request():
                with listener.accept()[0] as connection:
                    connection.settimeout(5)
                    with connection.makefile("rb", buffering=0) as stream:

                        def read_exact(size):
                            data = bytearray()
                            while len(data) < size:
                                chunk = stream.read(size - len(data))
                                if not chunk:
                                    raise AssertionError("Unexpected EOF in SOCKS5 request")
                                data.extend(chunk)
                            return bytes(data)

                        self.assertEqual(read_exact(1), b"\x05")
                        methods = read_exact(read_exact(1)[0])
                        self.assertIn(0, methods)
                        connection.sendall(b"\x05\x00")
                        # CONNECT with a domain name, not a locally resolved IP.
                        self.assertEqual(read_exact(4), b"\x05\x01\x00\x03")
                        target = read_exact(read_exact(1)[0]).decode()
                        target_port = struct.unpack("!H", read_exact(2))[0]
                        connection.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x16")
                        banner = stream.readline(255)
                        # End the SSH exchange explicitly; nc may keep its stdout
                        # open after a peer half-close while SSH still owns stdin.
                        connection.sendall(b"SSH-1.0-offline-fixture\r\n")
                        return target, target_port, banner

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                request = executor.submit(receive_proxy_request)
                result = subprocess.run(
                    ["ssh", "-F", str(config), "hpc", "hostname"],
                    check=False,
                    capture_output=True,
                    timeout=8,
                )
                target, target_port, banner = request.result(timeout=6)

            self.assertEqual(target, hostname)
            self.assertEqual(target_port, 22)
            self.assertTrue(banner.startswith(b"SSH-2.0-"), banner)
            # The fixture rejects the protocol before key exchange/authentication.
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
