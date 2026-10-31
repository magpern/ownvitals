"""SMTP delivery and its settings (ADR 0003 §5; the web-only mail path of roadmap Amendment 1).

Against the in-process SMTP fake on loopback; synthetic addresses and text only."""

import logging
import smtplib
import ssl
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ownvitals.config import Settings
from ownvitals.egress import OutboundBlocked, core, install_worker_allowlist
from ownvitals.mail import Mailer, MailError, MailMessage, send_logged
from smtpfake import SmtpFake


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
    monkeypatch.setattr(smtplib, "SMTP", RecordingSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", RecordingSmtp)
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
