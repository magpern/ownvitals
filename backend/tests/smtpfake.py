"""An in-process SMTP server for tests on loopback: accepts every message and keeps it.

Just enough SMTP for ``smtplib`` without TLS: EHLO, MAIL, RCPT, DATA, RSET, NOOP, QUIT. The
messages tests send it are synthetic (``example.com`` addresses, made-up text).
"""

import asyncio
import re
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message
from typing import Self


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
    port: int = 0
    messages: list[Received] = field(default_factory=lambda: list[Received]())
    _server: asyncio.Server | None = None

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def to(self, address: str) -> list[Received]:
        return [m for m in self.messages if m.to == address]

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def say(line: str) -> None:
            writer.write(f"{line}\r\n".encode())
            await writer.drain()

        sender, recipients = "", []
        try:
            await say("220 fake ESMTP")
            while line := (await reader.readline()).decode().strip():
                verb = line.split(" ", 1)[0].upper()
                if verb in ("EHLO", "HELO"):
                    await say("250 fake")
                elif verb == "MAIL":
                    sender, recipients = line.split(":", 1)[1].strip(), []
                    await say("250 ok")
                elif verb == "RCPT":
                    recipients.append(line.split(":", 1)[1].strip())
                    await say("250 ok")
                elif verb == "DATA":
                    await say("354 go")
                    raw = await reader.readuntil(b"\r\n.\r\n")
                    body = raw[: -len(b"\r\n.\r\n")].replace(b"\r\n..", b"\r\n.")
                    self.messages.append(Received(sender, recipients, message_from_bytes(body)))
                    await say("250 queued")
                elif verb == "QUIT":
                    await say("221 bye")
                    break
                else:
                    await say("250 ok")
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
