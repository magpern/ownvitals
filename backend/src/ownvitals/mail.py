"""Sending mail over SMTP (ADR 0003 §5, roadmap Amendment 1): web only.

Settings choose the host, port, sender and TLS mode (``starttls``, ``tls`` or ``none``; ``none``
is refused for any host but loopback or the compose ``mailpit`` service, see ``Settings``). The
password comes from the ``smtp_password`` secret file. Locally the host is mailpit; tests use the
in-process SMTP fake (``tests/smtpfake.py``).

This is the one module the outbound guard lets open a connection outside ``ownvitals.egress``
(``tests/egress_scan.py``, ``EXEMPT``): the SMTP host is a setting, not an allowlist entry. The
account mails run in the web process; ``refuse_in_worker`` refuses a send in the worker before
any connection. TLS uses ``egress.tls_client_context()``: verifying, and never a key log.

``smtplib`` is blocking, so a send runs in a worker thread. Mail is sent after the response
(``send_logged`` is a background task). A failed send is logged with the exception class only:
never the recipient, the subject, the body or the exception text (an SMTP server may echo the
address back).
"""

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage

from ownvitals.config import Settings
from ownvitals.egress import refuse_in_worker, tls_client_context

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_SECONDS = 15.0


class MailError(Exception):
    """A mail could not be delivered. The message never contains the recipient or the body."""


@dataclass(frozen=True, slots=True, repr=False)
class MailMessage:
    to: str
    subject: str
    body: str

    def __repr__(self) -> str:
        return "MailMessage(...)"


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._sender = settings.smtp_sender
        self._tls = settings.smtp_tls
        self._username = settings.smtp_username
        self._password = (
            settings.smtp_password.get_secret_value() if settings.smtp_password else None
        )

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

    def _send_blocking(self, message: MailMessage) -> None:
        if self._host is None:
            raise MailError("smtp_host is not configured")
        refuse_in_worker(self._host)
        built = self._build(message)
        context = tls_client_context()  # verifying, and never a TLS key log (SSLKEYLOGFILE)
        if self._tls == "tls":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                self._host, self._port, timeout=SMTP_TIMEOUT_SECONDS, context=context
            )
        else:
            client = smtplib.SMTP(self._host, self._port, timeout=SMTP_TIMEOUT_SECONDS)
        with client:
            if self._tls == "starttls":
                client.starttls(context=context)
            if self._username and self._password:
                client.login(self._username, self._password)
            client.send_message(built)

    async def send(self, message: MailMessage) -> None:
        """Deliver one message; raises ``MailError`` (without details) when that fails."""
        if not self.configured:
            raise MailError("smtp_host is not configured")
        refuse_in_worker(self._host)  # web only: the worker never sends mail
        failure: str | None = None
        try:
            await asyncio.to_thread(self._send_blocking, message)
        except (OSError, smtplib.SMTPException, ValueError) as exc:
            # The text of these can hold the recipient; only the class is kept.
            failure = type(exc).__name__
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
