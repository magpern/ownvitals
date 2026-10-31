"""SMTP delivery and its settings (ADR 0003 §5; the web-only mail path of roadmap Amendment 1).

Against the in-process SMTP fake on loopback; synthetic addresses and text only."""

import asyncio
import logging
import smtplib
import socket
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ownvitals import mail
from ownvitals.config import Settings
from ownvitals.egress import OutboundBlocked, core, install_worker_allowlist, tls_client_context
from ownvitals.mail import SMTP_TOTAL_SECONDS, Mailer, MailError, MailMessage, send_logged
from smtpfake import SmtpFake, loopback_certificate


@pytest.fixture(autouse=True)
def _fresh_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that makes this process the worker hands neither the opener nor the flag on."""
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


def settings(**values: object) -> Settings:
    return Settings(**values)  # pyright: ignore[reportArgumentType]


async def test_a_message_goes_through_smtp() -> None:
    async with SmtpFake() as smtp:
        mailer = Mailer(
            settings(
                smtp_host="127.0.0.1",
                smtp_port=smtp.port,
                smtp_tls="none",
                smtp_sender="ownvitals@example.test",
            )
        )
        await mailer.send(MailMessage("to@example.com", "Hello", "Body line\n"))
    [received] = smtp.messages
    assert (received.to, received.subject) == ("to@example.com", "Hello")
    assert received.message["From"] == "ownvitals@example.test"
    assert received.recipients == ["<to@example.com>"]
    assert received.text.strip() == "Body line"


async def test_the_worker_never_sends_mail_even_to_the_configured_host() -> None:
    """Roadmap Amendment 1: mail is a web-only path. The same send as above, in the worker, is
    refused before any connection: the fake sees nothing."""
    async with SmtpFake() as smtp:
        mailer = Mailer(settings(smtp_host="127.0.0.1", smtp_port=smtp.port, smtp_tls="none"))
        install_worker_allowlist()
        with pytest.raises(OutboundBlocked) as raised:
            await mailer.send(MailMessage("to@example.com", "Hello", "Body\n"))
    assert raised.value.host == "127.0.0.1"
    assert smtp.messages == []


async def test_without_a_host_nothing_is_sent_and_it_says_so() -> None:
    mailer = Mailer(settings())
    assert not mailer.configured
    with pytest.raises(MailError):
        await mailer.send(MailMessage("to@example.com", "s", "b"))


async def test_a_refused_connection_is_a_mail_error_without_the_recipient() -> None:
    mailer = Mailer(settings(smtp_host="127.0.0.1", smtp_port=1, smtp_tls="none"))
    with pytest.raises(MailError) as excinfo:
        await mailer.send(MailMessage("secret.person@example.com", "s", "b"))
    assert "secret.person" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None


async def test_a_failed_background_send_logs_the_class_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    mailer = Mailer(settings(smtp_host="127.0.0.1", smtp_port=1, smtp_tls="none"))
    await send_logged(
        mailer, MailMessage("secret.person@example.com", "Secret subject", "Secret body")
    )
    assert "mail not sent" in caplog.text
    for hidden in ("secret.person", "Secret subject", "Secret body"):
        assert hidden not in caplog.text


async def test_a_background_send_in_the_worker_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    install_worker_allowlist()
    mailer = Mailer(settings(smtp_host="mailpit", smtp_port=1025, smtp_tls="none"))
    await send_logged(mailer, MailMessage("secret.person@example.com", "s", "b"))
    assert "mail not sent (OutboundBlocked)" in caplog.text
    assert "secret.person" not in caplog.text


def test_neither_a_message_nor_the_mailer_shows_its_content_in_repr(secrets_dir: Path) -> None:
    assert "person@example.com" not in repr(MailMessage("person@example.com", "s", "b"))
    (secrets_dir / "smtp_password").write_text("planted-mail-secret\n", encoding="utf-8")
    mailer = Mailer(Settings(smtp_host="smtp.example.test", smtp_username="sender"))
    assert "planted-mail-secret" not in repr(mailer)


def test_defaults_use_starttls_and_no_host() -> None:
    s = settings()
    assert (s.smtp_host, s.smtp_port, s.smtp_tls) == (None, 587, "starttls")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "mailpit", "::1", "[::1]"])
def test_plain_smtp_is_allowed_for_loopback_and_mailpit(host: str) -> None:
    assert settings(smtp_host=host, smtp_tls="none").smtp_tls == "none"


@pytest.mark.parametrize("host", ["smtp.example.com", "10.0.0.5", "mailpit.evil.example"])
def test_plain_smtp_is_refused_for_other_hosts(host: str) -> None:
    with pytest.raises(ValidationError):
        settings(smtp_host=host, smtp_tls="none")
    assert settings(smtp_host=host, smtp_tls="starttls").smtp_host == host
    assert settings(smtp_host=host, smtp_tls="tls").smtp_host == host


def test_the_smtp_password_comes_only_from_its_secret_file(
    monkeypatch: pytest.MonkeyPatch, secrets_dir: Path
) -> None:
    (secrets_dir / "smtp_password").write_text("mail-secret\n", encoding="utf-8")
    monkeypatch.setenv("OWNVITALS_SMTP_PASSWORD", "from-the-env")  # ignored
    loaded = Settings()
    assert loaded.smtp_password is not None
    assert loaded.smtp_password.get_secret_value() == "mail-secret"
    assert "mail-secret" not in repr(loaded)
    assert "mail-secret" not in str(loaded.model_dump())


def test_the_smtp_port_is_range_checked() -> None:
    with pytest.raises(ValidationError):
        settings(smtp_port=0)


class RecordingSmtp:
    """Stands in for ``smtplib.SMTP`` and ``SMTP_SSL``: records the TLS contexts and the login,
    never connects."""

    contexts: list[ssl.SSLContext]
    logins: list[tuple[str, str]]

    def __init__(self, *args: Any, context: ssl.SSLContext | None = None, **kwargs: Any):
        if context is not None:
            RecordingSmtp.contexts.append(context)

    def __enter__(self) -> "RecordingSmtp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, *, context: ssl.SSLContext) -> None:
        RecordingSmtp.contexts.append(context)

    def login(self, user: str, password: str) -> None:
        RecordingSmtp.logins.append((user, password))

    def send_message(self, message: Any) -> None:
        return None


@pytest.fixture
def recording_smtp(monkeypatch: pytest.MonkeyPatch) -> type[RecordingSmtp]:
    RecordingSmtp.contexts, RecordingSmtp.logins = [], []
    monkeypatch.setattr(mail, "_DeadlineSMTP", RecordingSmtp)
    monkeypatch.setattr(mail, "_DeadlineSMTPSSL", RecordingSmtp)
    return RecordingSmtp


@pytest.mark.parametrize("mode", ["starttls", "tls"])
async def test_the_smtp_tls_context_ignores_sslkeylogfile(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recording_smtp: type[RecordingSmtp],
) -> None:
    """The stdlib's default context would append every session's secrets to the file that
    ``SSLKEYLOGFILE`` names; the mailer's context is ``egress.tls_client_context()``."""
    key_log = tmp_path / "keys.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(key_log))
    assert ssl.create_default_context().keylog_filename == str(key_log)  # the risk is real
    key_log.unlink()
    mailer = Mailer(settings(smtp_host="smtp.example.test", smtp_tls=mode))
    await mailer.send(MailMessage("to@example.com", "Hello", "Body\n"))
    [context] = recording_smtp.contexts
    assert context.keylog_filename is None
    assert (context.verify_mode, context.check_hostname) == (ssl.CERT_REQUIRED, True)
    assert not key_log.exists()


async def test_plain_mode_never_starts_tls(recording_smtp: type[RecordingSmtp]) -> None:
    mailer = Mailer(settings(smtp_host="mailpit", smtp_port=1025, smtp_tls="none"))
    await mailer.send(MailMessage("to@example.com", "Hello", "Body\n"))
    assert recording_smtp.contexts == []


async def test_a_login_needs_both_the_username_and_the_secret_file(
    secrets_dir: Path, recording_smtp: type[RecordingSmtp]
) -> None:
    message = MailMessage("to@example.com", "Hello", "Body\n")
    await Mailer(settings(smtp_host="smtp.example.test", smtp_username="sender")).send(message)
    assert recording_smtp.logins == []
    (secrets_dir / "smtp_password").write_text("synthetic-pass\n", encoding="utf-8")
    await Mailer(Settings(smtp_host="smtp.example.test", smtp_username="sender")).send(message)
    assert recording_smtp.logins == [("sender", "synthetic-pass")]


# --- the total deadline of a delivery ----------------------------------------------------------


def _local(port: int, **values: object) -> Settings:
    return settings(smtp_host="127.0.0.1", smtp_port=port, smtp_tls="none", **values)


async def _fails_within(mailer: Mailer, low: float, high: float) -> MailError:
    started = time.monotonic()
    with pytest.raises(MailError) as raised:
        await mailer.send(MailMessage("secret.person@example.com", "Hello", "Body\n"))
    elapsed = time.monotonic() - started
    assert low <= elapsed < high, elapsed
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "secret.person" not in str(raised.value)
    return raised.value


# Sent a byte every 50 ms, the greeting alone takes over 3 s.
LONG_GREETING = "fake ESMTP, a synthetic banner long enough to trickle for seconds"


async def test_a_trickling_server_is_cut_at_the_total_deadline() -> None:
    """Every byte of every reply comes 50 ms apart, far inside any per-operation timeout; a
    timeout recomputed only per reply line would still wait out the whole 3 s greeting. Only a
    deadline checked before every ``recv`` ends the delivery in time."""
    async with SmtpFake(reply_delay=0.05, greeting=LONG_GREETING) as smtp:
        error = await _fails_within(Mailer(_local(smtp.port), total_seconds=0.5), 0.45, 1.5)
    assert str(error) == "delivery failed (deadline passed)"
    assert smtp.messages == []


async def test_a_server_that_stalls_after_the_greeting_is_cut_at_the_total_deadline() -> None:
    async with SmtpFake(stall_after_greeting=True) as smtp:
        error = await _fails_within(Mailer(_local(smtp.port), total_seconds=0.5), 0.45, 1.5)
    assert str(error) == "delivery failed (deadline passed)"
    assert (smtp.connections, smtp.messages) == (1, [])


async def test_a_send_the_server_never_reads_is_cut_at_the_total_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N6: the server stops reading after ``354`` and both ends have 4 KiB buffers, so the
    client's ``sendall`` of a 4 MB message blocks; the time left bounds it like every receive.

    Every send runs with the time left as its timeout (on any platform). On Linux, where CI runs
    and the kernel keeps to the small buffers, the failure must come from the message's send
    itself; Windows' loopback takes the whole message into its buffers, so there the deadline
    ends the wait for the reply instead."""
    connect = socket.create_connection
    send = smtplib.SMTP.send
    sends: list[tuple[int, float | None]] = []
    failed_in_send: list[int] = []

    def small_buffer(*args: Any, **kwargs: Any) -> socket.socket:
        sock = connect(*args, **kwargs)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        return sock

    def recording(self: smtplib.SMTP, s: Any) -> None:
        if self.sock is None:  # the QUIT after a failure: smtplib refuses it itself
            return send(self, s)
        sends.append((len(s), self.sock.gettimeout()))
        try:
            send(self, s)
        except smtplib.SMTPServerDisconnected:
            failed_in_send.append(len(s))
            raise

    monkeypatch.setattr(socket, "create_connection", small_buffer)
    monkeypatch.setattr(smtplib.SMTP, "send", recording)
    body = "a synthetic line of an oversized message\n" * 100_000
    async with SmtpFake(stall_in_data=True) as smtp:
        mailer = Mailer(_local(smtp.port), total_seconds=1.5)
        started = time.monotonic()
        with pytest.raises(MailError) as raised:
            await mailer.send(MailMessage("to@example.com", "Big", body))
        assert 1.4 <= time.monotonic() - started < 4
    assert str(raised.value) == "delivery failed (deadline passed)"
    assert (smtp.connections, smtp.messages) == (1, [])
    message_sends = [timeout for size, timeout in sends if size > len(body)]
    assert len(message_sends) == 1
    assert all(t is not None and 0 < t <= 1.5 for _, t in sends), sends
    if sys.platform.startswith("linux"):
        assert len(failed_in_send) == 1
        assert failed_in_send[0] > len(body)  # the message's send, not the wait for the reply


async def test_mutation_a_socket_timeout_alone_never_ends_a_trickle() -> None:
    """Plain ``smtplib`` with a 0.3 s timeout per operation: the 0.8 s greeting still arrives,
    because each byte comes within the timeout. The deadline above is what bounds it."""
    async with SmtpFake(reply_delay=0.05) as smtp:

        def connect() -> float:
            started = time.monotonic()
            client = smtplib.SMTP("127.0.0.1", smtp.port, timeout=0.3)
            client.close()
            return time.monotonic() - started

        assert await asyncio.to_thread(connect) > 0.7


async def test_a_slow_but_timely_server_is_untouched() -> None:
    async with SmtpFake(reply_delay=0.002) as smtp:
        await Mailer(_local(smtp.port), total_seconds=5).send(
            MailMessage("to@example.com", "Hello", "Body\n")
        )
    assert [m.subject for m in smtp.messages] == ["Hello"]


async def test_nothing_connects_after_the_deadline() -> None:
    """The deadline is taken before the thread is scheduled: a send whose time is already used
    up opens no connection."""
    async with SmtpFake() as smtp:
        mailer = Mailer(_local(smtp.port))
        with pytest.raises(smtplib.SMTPServerDisconnected):
            await asyncio.to_thread(
                mailer._send_blocking,  # pyright: ignore[reportPrivateUsage]
                MailMessage("to@example.com", "s", "b"),
                time.monotonic() - 1,
            )
    assert smtp.connections == 0


def test_the_total_deadline_defaults_to_20_seconds() -> None:
    assert SMTP_TOTAL_SECONDS == 20.0
    assert Mailer(settings())._total_seconds == 20.0  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("seconds", [0, -1.0, float("nan"), float("inf"), True, "20"])
def test_the_total_deadline_is_positive_and_finite(seconds: Any) -> None:
    with pytest.raises(ValueError, match="positive, finite"):
        Mailer(settings(), total_seconds=seconds)


# --- TLS: STARTTLS (the default) and implicit TLS, with the deadline ------------------------


@pytest.fixture
def loopback_tls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ssl.SSLContext:
    """The fake's server context; the mailer trusts its throwaway certificate (and still
    verifies it: hostname ``127.0.0.1``, ``CERT_REQUIRED``)."""
    cert, key = loopback_certificate(tmp_path)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    monkeypatch.setattr(mail, "tls_client_context", lambda: tls_client_context(cafile=cert))
    return server


async def test_starttls_delivers_over_tls(loopback_tls: ssl.SSLContext) -> None:
    async with SmtpFake(tls=loopback_tls) as smtp:
        mailer = Mailer(settings(smtp_host="127.0.0.1", smtp_port=smtp.port))
        await mailer.send(MailMessage("to@example.com", "Hello", "Body\n"))
    assert smtp.tls_upgrades == 1
    assert [m.subject for m in smtp.messages] == ["Hello"]


async def test_implicit_tls_delivers(loopback_tls: ssl.SSLContext) -> None:
    async with SmtpFake(tls=loopback_tls, implicit_tls=True) as smtp:
        mailer = Mailer(settings(smtp_host="127.0.0.1", smtp_port=smtp.port, smtp_tls="tls"))
        await mailer.send(MailMessage("to@example.com", "Hello", "Body\n"))
    assert [m.subject for m in smtp.messages] == ["Hello"]


@pytest.mark.parametrize("implicit", [False, True])
async def test_a_trickling_tls_server_is_cut_at_the_total_deadline(
    loopback_tls: ssl.SSLContext, implicit: bool
) -> None:
    tls = "tls" if implicit else "starttls"
    async with SmtpFake(
        tls=loopback_tls, implicit_tls=implicit, reply_delay=0.05, greeting=LONG_GREETING
    ) as smtp:
        mailer = Mailer(
            settings(smtp_host="127.0.0.1", smtp_port=smtp.port, smtp_tls=tls), total_seconds=0.5
        )
        error = await _fails_within(mailer, 0.45, 1.5)
    assert str(error) == "delivery failed (deadline passed)"
    assert smtp.messages == []


async def test_a_tls_handshake_that_never_ends_is_cut_at_the_total_deadline(
    loopback_tls: ssl.SSLContext,
) -> None:
    """A server that accepts the connection and never answers the ClientHello."""

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while await reader.read(4096):
                pass
        except ConnectionError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        mailer = Mailer(
            settings(smtp_host="127.0.0.1", smtp_port=port, smtp_tls="tls"), total_seconds=0.5
        )
        error = await _fails_within(mailer, 0.45, 1.5)
    assert str(error) == "delivery failed (deadline passed)"
