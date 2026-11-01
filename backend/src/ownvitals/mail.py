"""Sending mail over SMTP (ADR 0003 §5, roadmap Amendment 1): web only.

Settings choose the host, port, sender and TLS mode (``starttls``, ``tls`` or ``none``; ``none``
is refused for any host but loopback or the compose ``mailpit`` service, see ``Settings``). The
password comes from the ``smtp_password`` secret file. Locally the host is mailpit; tests use the
in-process SMTP fake (``tests/smtpfake.py``).

This is the one module the outbound guard lets open a connection outside ``ownvitals.egress``
(``tests/egress_classified.py``: ``smtplib.SMTP``, ``SMTP_SSL`` and smtplib's exception classes,
only here): the SMTP host is a setting, not an allowlist entry. The
account mails run in the web process; ``refuse_in_worker`` refuses a send in the worker before
any connection. TLS uses ``egress.tls_client_context()``: verifying, and never a key log.

**A total deadline per delivery** (``SMTP_TOTAL_SECONDS``, 20 s). A socket timeout bounds one
blocking operation only, so a server trickling its replies a byte at a time could hold a web
request for as long as it likes. Every socket operation here gets the time that is left as its
timeout: the connection, each ``recv`` of a reply (``_DeadlineReader``), each send, and the TLS
handshake (CPython bounds a TLS handshake, read or write and a ``sendall`` as a whole by the
socket timeout). Once the deadline has passed nothing more is sent or awaited. The one wait it
cannot cut is a DNS lookup in progress; and a host name with several addresses gets the time
left for each connection attempt. ``EHLO`` names the public site's host (``ehlo_name``), never
the machine's: smtplib would otherwise call ``socket.getfqdn()``, a reverse lookup outside the
deadline that the egress hook refuses anyway.

**The egress hook** (ADR 0009) lets a socket connect only inside a connection scope: the blocking
send opens ``smtp_session()`` itself, in the thread that connects. Its lookup of ``smtp_host`` is
allowed in web and the CLI, never in the worker.

``smtplib`` is blocking, so a send runs in a worker thread; the deadline is taken before the
thread is scheduled. Mail is sent after the response (``send_logged`` is a background task). A
failed send is a ``MailError`` naming the exception class only, raised without cause or context:
never the recipient, the subject, the body or the exception text (an SMTP server may echo the
address back).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import smtplib
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from ownvitals.config import Settings
from ownvitals.egress import (
    check_total_seconds,
    refuse_in_worker,
    smtp_session,
    tls_client_context,
)

if TYPE_CHECKING:  # annotations only: the guard lets mail import smtplib alone at run time
    import socket
    import ssl

logger = logging.getLogger(__name__)

# The total deadline of one delivery. A module constant until ``config.py`` can take a setting
# (the Architect moves it; the shared-file lock is held elsewhere).
SMTP_TOTAL_SECONDS: Final = 20.0
_RECV_BYTES: Final = 4096
# ``EHLO``'s name when the public site has no DNS name of its own (an address or ``localhost``).
FALLBACK_EHLO_NAME: Final = "localhost"


def ehlo_name(public_base_url: str) -> str:
    """The fixed name mail gives in ``EHLO``: the public site's host name, or
    ``FALLBACK_EHLO_NAME`` for an address or a name without a dot. Never a lookup."""
    host = (urlsplit(public_base_url).hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host if "." in host else FALLBACK_EHLO_NAME
    return FALLBACK_EHLO_NAME


class MailError(Exception):
    """A mail could not be delivered. The message never contains the recipient or the body."""


@dataclass(frozen=True, slots=True, repr=False)
class MailMessage:
    to: str
    subject: str
    body: str

    def __repr__(self) -> str:
        return "MailMessage(...)"


class _Deadline:
    """The total deadline of one delivery, a ``time.monotonic()`` instant."""

    def __init__(self, at: float) -> None:
        self.at = at

    def remaining(self) -> float:
        """The time left; past the deadline ``SMTPServerDisconnected``, so nothing more is sent
        or awaited (inside ``QUIT`` too, which ``smtplib`` then treats as a closed server)."""
        left = self.at - time.monotonic()
        if left <= 0:
            raise smtplib.SMTPServerDisconnected("deadline passed")
        return left

    def arm(self, sock: socket.socket) -> None:
        """The socket's next blocking operation waits at most the time left."""
        sock.settimeout(self.remaining())


class _DeadlineReader:
    """What ``smtplib`` reads replies from (``SMTP.file``, normally ``sock.makefile``): each
    ``recv`` waits at most the time left, so a reply trickling in a byte at a time ends at the
    deadline instead of after one socket timeout per byte."""

    def __init__(self, sock: socket.socket, deadline: _Deadline) -> None:
        self._sock = sock
        self._deadline = deadline
        self._buffer = b""

    def readline(self, limit: int = -1) -> bytes:
        while b"\n" not in self._buffer and (limit < 0 or len(self._buffer) < limit):
            self._deadline.arm(self._sock)
            chunk = self._sock.recv(_RECV_BYTES)
            if not chunk:
                break
            self._buffer += chunk
        newline = self._buffer.find(b"\n")
        end = len(self._buffer) if newline < 0 else newline + 1
        if limit >= 0:
            end = min(end, limit)
        line, self._buffer = self._buffer[:end], self._buffer[end:]
        self._deadline.arm(self._sock)  # what follows (a TLS handshake) gets only the rest
        return line

    def close(self) -> None:
        self._buffer = b""


class _DeadlineSMTP(smtplib.SMTP):
    """``smtplib.SMTP`` whose every socket operation waits at most the time left."""

    _deadline: _Deadline

    def __init__(self, host: str, port: int, *, deadline: _Deadline, **options: Any) -> None:
        # ``options`` reach ``SMTP_SSL`` (its ``context``) when this is ``_DeadlineSMTPSSL``.
        self._deadline = deadline
        super().__init__(host, port, timeout=deadline.remaining(), **options)

    # ``_get_socket`` is smtplib's own hook for SMTP_SSL (the type stubs leave it out).
    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        sock = super()._get_socket(host, port, self._deadline.remaining())  # pyright: ignore[reportAttributeAccessIssue]
        self._deadline.arm(sock)
        return sock

    def send(self, s: Any) -> None:
        # A failed STARTTLS handshake leaves ``sock`` closed: smtplib's own send then refuses
        # (``SMTPServerDisconnected``), so the QUIT on the way out keeps the original error.
        if self.sock is not None and self.sock.fileno() != -1:
            self._deadline.arm(self.sock)
        super().send(s)

    def getreply(self) -> tuple[int, bytes]:
        if self.file is None and self.sock is not None:
            # Replaced on every new socket: ``starttls`` drops the plaintext reader with its
            # buffer, as ``smtplib`` drops its own file.
            self.file = _DeadlineReader(self.sock, self._deadline)
        return super().getreply()


class _DeadlineSMTPSSL(_DeadlineSMTP, smtplib.SMTP_SSL):
    """``smtplib.SMTP_SSL`` with the same deadline; the handshake gets only the time left."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        deadline: _Deadline,
        context: ssl.SSLContext,
        local_hostname: str | None = None,
    ) -> None:
        super().__init__(
            host, port, deadline=deadline, context=context, local_hostname=local_hostname
        )

    def _get_socket(self, host: str, port: int, timeout: float) -> socket.socket:
        # Plain ``SMTP._get_socket`` (the TCP connection), then the handshake with the time left.
        sock = smtplib.SMTP._get_socket(self, host, port, self._deadline.remaining())  # pyright: ignore[reportAttributeAccessIssue]
        self._deadline.arm(sock)
        wrapped = self.context.wrap_socket(sock, server_hostname=host)
        self._deadline.arm(wrapped)
        return wrapped


class Mailer:
    def __init__(self, settings: Settings, *, total_seconds: float = SMTP_TOTAL_SECONDS) -> None:
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._sender = settings.smtp_sender
        self._tls = settings.smtp_tls
        self._username = settings.smtp_username
        self._password = (
            settings.smtp_password.get_secret_value() if settings.smtp_password else None
        )
        self._total_seconds = check_total_seconds(total_seconds)
        self._ehlo_name = ehlo_name(settings.public_base_url)

    def __repr__(self) -> str:
        return "Mailer(...)"

    @property
    def configured(self) -> bool:
        return self._host is not None

    def _build(self, message: MailMessage) -> EmailMessage:
        built = EmailMessage()
        built["From"] = self._sender
        built["To"] = message.to
        built["Subject"] = message.subject
        built.set_content(message.body)
        return built

    def _send_blocking(self, message: MailMessage, at: float | None = None) -> None:
        """Deliver within the deadline ``at`` (``time.monotonic()``; default: from now)."""
        if self._host is None:
            raise MailError("smtp_host is not configured")
        refuse_in_worker(self._host)
        deadline = _Deadline(time.monotonic() + self._total_seconds if at is None else at)
        deadline.remaining()  # nothing starts after the deadline
        built = self._build(message)
        context = tls_client_context()  # verifying, and never a TLS key log (SSLKEYLOGFILE)
        client: smtplib.SMTP
        names = {"local_hostname": self._ehlo_name}  # smtplib's default calls socket.getfqdn()
        with smtp_session():  # set here, in the thread that connects (ADR 0009)
            if self._tls == "tls":
                client = _DeadlineSMTPSSL(
                    self._host, self._port, deadline=deadline, context=context, **names
                )
            else:
                client = _DeadlineSMTP(self._host, self._port, deadline=deadline, **names)
            with client:
                if self._tls == "starttls":
                    client.starttls(context=context)
                if self._username and self._password:
                    client.login(self._username, self._password)
                client.send_message(built)

    async def send(self, message: MailMessage) -> None:
        """Deliver one message within ``total_seconds``; raises ``MailError`` (without details,
        cause or context) when that fails."""
        if not self.configured:
            raise MailError("smtp_host is not configured")
        refuse_in_worker(self._host)  # web only: the worker never sends mail
        at = time.monotonic() + self._total_seconds  # before the thread is scheduled
        failure: str | None = None
        try:
            await asyncio.to_thread(self._send_blocking, message, at)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            # The text of these can hold the recipient; only the class is kept.
            failure = "deadline passed" if time.monotonic() >= at else type(exc).__name__
        if failure is not None:
            # Raised after the except block: no __cause__ and no __context__ to carry the text.
            raise MailError(f"delivery failed ({failure})")


async def send_logged(mailer: Mailer, message: MailMessage) -> None:
    """A background task: send, and on failure log the error class only."""
    try:
        await mailer.send(message)
    except MailError as exc:
        logger.warning("mail not sent: %s", exc)
    except Exception as exc:  # a background task must never raise into the server
        logger.warning("mail not sent (%s)", type(exc).__name__)
