"""An in-process SMTP server for tests on loopback: accepts every message and keeps it.

Just enough SMTP for ``smtplib``: EHLO, MAIL, RCPT, DATA, RSET, NOOP, QUIT, and with ``tls`` (a
server context, see ``loopback_certificate``) either STARTTLS or, with ``implicit_tls``, TLS from
the first byte. The messages tests send it are synthetic (``example.com`` addresses, made-up
text).
"""

import asyncio
import datetime
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message
from pathlib import Path
from typing import Self

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def loopback_certificate(directory: Path) -> tuple[str, str]:
    """A throwaway self-signed certificate for ``127.0.0.1``: ``(cert_path, key_path)``. The
    certificate is its own CA (``cafile=cert_path`` on the client)."""
    now = datetime.datetime.now(datetime.UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ownvitals test loopback")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


@dataclass
class Received:
    sender: str
    recipients: list[str]
    message: Message

    @property
    def to(self) -> str:
        return str(self.message["To"])

    @property
    def subject(self) -> str:
        return str(self.message["Subject"])

    @property
    def text(self) -> str:
        payload = self.message.get_payload(decode=True)
        assert isinstance(payload, bytes)
        return payload.decode()

    def token(self) -> str:
        """The token of the mail link (it travels in the URL fragment)."""
        match = re.search(r"#token=([\w-]+)", self.text)
        assert match, "the mail has no token link"
        return match.group(1)


@dataclass
class SmtpFake:
    """``reply_delay``: send every reply one byte at a time, this many seconds apart (a
    trickling server). ``greeting``: the banner after ``220``. ``stall_after_greeting``:
    greet, then never answer again. ``stall_in_data``: answer ``354`` to ``DATA``, then read
    nothing more until the fake closes (a 4 KiB receive buffer, so the client's send blocks).
    ``tls``: offer STARTTLS with this server context, or speak TLS at once with
    ``implicit_tls``."""

    port: int = 0
    messages: list[Received] = field(default_factory=lambda: list[Received]())
    reply_delay: float = 0.0
    greeting: str = "fake ESMTP"
    stall_after_greeting: bool = False
    stall_in_data: bool = False
    tls: ssl.SSLContext | None = None
    implicit_tls: bool = False
    connections: int = 0
    tls_upgrades: int = 0
    ehlo_names: list[str] = field(default_factory=lambda: list[str]())  # what EHLO/HELO named
    _server: asyncio.Server | None = None
    _release: asyncio.Event = field(default_factory=asyncio.Event)

    async def __aenter__(self) -> Self:
        context = self.tls if self.implicit_tls else None
        listener = socket.socket()
        if self.stall_in_data:  # accepted sockets inherit it
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        listener.bind(("127.0.0.1", 0))
        self._server = await asyncio.start_server(
            self._serve, sock=listener, ssl=context, limit=1024
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._server is not None
        self._release.set()
        self._server.close()
        await self._server.wait_closed()

    def to(self, address: str) -> list[Received]:
        return [m for m in self.messages if m.to == address]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def say(line: str) -> None:
            data = f"{line}\r\n".encode()
            if not self.reply_delay:
                writer.write(data)
                await writer.drain()
                return
            for i in range(len(data)):
                writer.write(data[i : i + 1])
                await writer.drain()
                await asyncio.sleep(self.reply_delay)

        self.connections += 1
        offer_starttls = self.tls is not None and not self.implicit_tls
        sender, recipients = "", []
        try:
            await say(f"220 {self.greeting}")
            if self.stall_after_greeting:
                while await reader.read(4096):  # read what comes, answer nothing, until EOF
                    pass
                return
            while line := (await reader.readline()).decode().strip():
                verb = line.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    self.ehlo_names.append(line.split(" ", 1)[1] if " " in line else "")
                    if offer_starttls:
                        await say("250-fake")
                        await say("250 STARTTLS")
                    else:
                        await say("250 fake")
                elif verb == "STARTTLS" and offer_starttls and self.tls is not None:
                    await say("220 go ahead")
                    await writer.start_tls(self.tls)
                    self.tls_upgrades += 1
                    offer_starttls = False
                elif verb == "MAIL":
                    sender, recipients = line.split(":", 1)[1].strip(), []
                    await say("250 ok")
                elif verb == "RCPT":
                    recipients.append(line.split(":", 1)[1].strip())
                    await say("250 ok")
                elif verb == "DATA":
                    await say("354 go")
                    if self.stall_in_data:
                        await asyncio.wait_for(self._release.wait(), 30)
                        return
                    raw = await reader.readuntil(b"\r\n.\r\n")
                    body = raw[: -len(b"\r\n.\r\n")].replace(b"\r\n..", b"\r\n.")
                    self.messages.append(Received(sender, recipients, message_from_bytes(body)))
                    await say("250 queued")
                elif verb == "QUIT":
                    await say("221 bye")
                    break
                else:
                    await say("250 ok")
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError, TimeoutError):
            pass
        finally:
            writer.close()
