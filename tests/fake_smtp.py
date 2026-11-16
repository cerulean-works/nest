"""An in-process SMTP server for the email tests. Never a real mail provider.

It speaks enough of RFC 5321 for `smtplib`: EHLO, STARTTLS, AUTH PLAIN, MAIL, RCPT,
DATA, RSET, NOOP and QUIT, with real TLS from throwaway certificates. Tests choose
the TLS mode, credentials and injected failures, and read what the server received.
"""

from __future__ import annotations

import base64
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self, cast


@dataclass
class Certificates:
    """Test CA, a server certificate valid only for DNS:localhost, and an unrelated CA."""

    directory: Path
    ca: str
    other_ca: str
    cert: str
    key: str

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def make_certificates() -> Certificates:
    """Creates throwaway TLS material with `openssl`."""
    directory = Path(tempfile.mkdtemp(prefix="nest-smtp-tls-"))
    ca_ext = directory / "ca.ext"
    ca_ext.write_text(
        "basicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n"
    )
    server_ext = directory / "server.ext"
    server_ext.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:localhost\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid\n"
    )

    def run(*args: str) -> None:
        subprocess.run(["openssl", *args], cwd=directory, check=True, capture_output=True)

    for name, subject in (("ca", "Nest SMTP test CA"), ("other-ca", "Unrelated test CA")):
        run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", f"/CN={subject}",
            "-keyout", f"{name}.key", "-out", f"{name}.csr")  # fmt: skip
        run("x509", "-req", "-in", f"{name}.csr", "-key", f"{name}.key", "-days", "2",
            "-extfile", "ca.ext", "-out", f"{name}.crt")  # fmt: skip
    run("req", "-new", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
        "-keyout", "server.key", "-out", "server.csr")  # fmt: skip
    run("x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
        "-CAcreateserial", "-days", "2", "-extfile", "server.ext", "-out", "server.crt")  # fmt: skip
    return Certificates(
        directory=directory,
        ca=str(directory / "ca.crt"),
        other_ca=str(directory / "other-ca.crt"),
        cert=str(directory / "server.crt"),
        key=str(directory / "server.key"),
    )


@dataclass
class Received:
    """One message the server accepted."""

    mail_from: str
    recipients: list[str]
    data: str
    tls: bool
    authenticated_as: str | None


@dataclass
class ServerState:
    mode: str  # "starttls", "implicit", or "plain" (offers no STARTTLS)
    username: str | None = None
    password: str | None = None
    # Replies injected for the next commands, keyed by verb: {"RCPT": [450, 550]}.
    failures: dict[str, list[int]] = field(default_factory=dict)
    # Close connections right after accepting them, this many times.
    drop_connections: int = 0
    messages: list[Received] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    connections: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Handler(socketserver.StreamRequestHandler):
    @property
    def fake(self) -> _Server:
        return cast(_Server, self.server)

    def setup(self):
        super().setup()
        self.tls = False

    def reply(self, text: str) -> None:
        self.wfile.write(text.encode() + b"\r\n")
        self.wfile.flush()

    def injected(self, verb: str) -> int | None:
        state = self.fake.state
        with state.lock:
            codes = state.failures.get(verb)
            return codes.pop(0) if codes else None

    def start_tls(self) -> None:
        self.request = self.fake.tls_context.wrap_socket(self.request, server_side=True)
        self.rfile = self.request.makefile("rb")
        self.wfile = self.request.makefile("wb")
        self.tls = True

    def handle(self):
        state = self.fake.state
        with state.lock:
            state.connections += 1
            if state.drop_connections:
                state.drop_connections -= 1
                return
        if state.mode == "implicit":
            try:
                self.start_tls()
            except ssl.SSLError, OSError:
                return
        self.reply("220 localhost Nest fake SMTP")
        mail_from, recipients, user = None, [], None
        while True:
            try:
                raw = self.rfile.readline(4096)
            except ssl.SSLError, OSError:
                return
            if not raw:
                return
            line = raw.decode(errors="replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            with state.lock:
                state.commands.append(verb if verb == "AUTH" else line)
            code = self.injected(verb)
            if code is not None:
                self.reply(f"{code} injected failure")
                if code == 421:
                    return
                continue
            if verb in ("EHLO", "HELO"):
                features = ["localhost"]
                if state.mode == "starttls" and not self.tls:
                    features.append("STARTTLS")
                if state.username and self.tls:
                    features.append("AUTH PLAIN")
                for feature in features[:-1]:
                    self.reply(f"250-{feature}")
                self.reply(f"250 {features[-1]}")
            elif verb == "STARTTLS":
                if state.mode != "starttls" or self.tls:
                    self.reply("502 not available")
                    continue
                self.reply("220 ready to start TLS")
                try:
                    self.start_tls()
                except ssl.SSLError, OSError:
                    return
            elif verb == "AUTH":
                parts = line.split(" ")
                if not self.tls or len(parts) != 3 or parts[1].upper() != "PLAIN":
                    self.reply("538 encryption required")
                    continue
                _, name, secret = base64.b64decode(parts[2]).decode().split("\0")
                if name == state.username and secret == state.password:
                    user = name
                    self.reply("235 authenticated")
                else:
                    self.reply("535 authentication failed")
            elif verb == "MAIL":
                if state.username and user is None:
                    self.reply("530 authentication required")
                    continue
                mail_from = line.split(":", 1)[1].strip().strip("<>").split(">")[0]
                recipients = []
                self.reply("250 ok")
            elif verb == "RCPT":
                recipients.append(line.split(":", 1)[1].strip().strip("<>"))
                self.reply("250 ok")
            elif verb == "DATA":
                self.reply("354 end with .")
                lines = []
                while True:
                    chunk = self.rfile.readline()
                    if chunk in (b".\r\n", b""):
                        break
                    lines.append(chunk.decode(errors="replace"))
                with state.lock:
                    state.messages.append(
                        Received(mail_from or "", recipients, "".join(lines), self.tls, user)
                    )
                self.reply("250 queued")
            elif verb == "QUIT":
                self.reply("221 bye")
                return
            elif verb in ("RSET", "NOOP"):
                self.reply("250 ok")
            else:
                self.reply("500 unknown command")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state: ServerState, certificates: Certificates):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.state = state
        self.tls_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.tls_context.load_cert_chain(certificates.cert, certificates.key)


class FakeSMTPServer:
    """
    Runs the fake server on a free port of 127.0.0.1 in a background thread.

    Connect to `localhost` (the certificate's only name) at `port`.
    """

    def __init__(self, certificates: Certificates, mode: str = "starttls", **state):
        self.state = ServerState(mode=mode, **state)
        self._server = _Server(self.state, certificates)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def messages(self) -> list[Received]:
        with self.state.lock:
            return list(self.state.messages)

    def fail(self, verb: str, *codes: int) -> None:
        """Replies with each code, in turn, to the next `verb` commands."""
        with self.state.lock:
            self.state.failures.setdefault(verb.upper(), []).extend(codes)


def free_port() -> int:
    """Returns a port nothing listens on, for connection-refused tests."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
