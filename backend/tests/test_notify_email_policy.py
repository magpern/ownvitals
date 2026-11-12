"""Security and account mail ignores the email preference (ADR 0019; #459). No database.

One test per kind: every account and security mail of ``ownvitals.auth.mails`` (and the
operator's reconnect mail) is built without an unsubscribe link and goes out without the RFC
8058 headers, ``export_ready`` and the owner notice are not suppressible, and nothing on the
account-mail path reads a preference. The suppressible list is exactly the outbox's other user
kinds, and a mail with a link carries both headers unencoded (``test_a_mail_with_a_link_*``).
Synthetic addresses and tokens only.
"""

import ast
import inspect
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace

import pytest

from ownvitals.auth import mails
from ownvitals.config import Settings
from ownvitals.keys_cli import _mail as reconnect_mail  # pyright: ignore[reportPrivateUsage]
from ownvitals.mail import Mailer, MailMessage
from ownvitals.notify import email_policy
from ownvitals.notify.email_policy import ALWAYS_SENT_KINDS, SUPPRESSIBLE_KINDS, suppressible
from ownvitals.notify.outbox import OWNER_NOTICE_KINDS, USER_KINDS

SRC = Path(__file__).resolve().parent.parent / "src" / "ownvitals"
BASE = "https://ownvitals.example.test"
TO = "synthetic-policy@example.test"
SETTINGS = Settings(public_base_url=BASE, smtp_sender="ownvitals@example.test")  # pyright: ignore[reportCallIssue]
WHEN = datetime(2026, 1, 1, tzinfo=UTC)

# Every account and security mail, with synthetic arguments.
ACCOUNT_MAILS: dict[str, Callable[[], MailMessage]] = {
    "signup_confirmation": lambda: mails.signup_confirmation(SETTINGS, TO, "synthetic-token"),
    "account_exists": lambda: mails.account_exists(SETTINGS, TO),
    "password_reset": lambda: mails.password_reset(SETTINGS, TO, "synthetic-token"),
    "password_reset_after_mfa_reset": lambda: mails.password_reset(
        SETTINGS, TO, "synthetic-token", after_mfa_reset=mails.ResetRemoved(passkeys=1)
    ),
    "email_change_confirmation": lambda: mails.email_change_confirmation(SETTINGS, TO, "t"),
    "email_change_taken": lambda: mails.email_change_taken(SETTINGS, TO),
    "email_changed_notice": lambda: mails.email_changed_notice(TO),
    "totp_enrolled_notice": lambda: mails.totp_enrolled_notice(TO),
    "mfa_failures_notice": lambda: mails.mfa_failures_notice(TO),
    "provider_signup_confirmation": lambda: mails.provider_signup_confirmation(
        SETTINGS, TO, "google", "synthetic-token"
    ),
    "identity_linked_notice": lambda: mails.identity_linked_notice(TO, "google"),
    "passkey_added_notice": lambda: mails.passkey_added_notice(TO, "synthetic key"),
    "passkey_suspended_notice": lambda: mails.passkey_suspended_notice(TO, "synthetic key"),
    "qr_login_approved_notice": lambda: mails.qr_login_approved_notice(
        SETTINGS, TO, "Firefox", "Linux", "Synthetic Town"
    ),
    "operator_reconnect": lambda: reconnect_mail(
        SETTINGS,
        SimpleNamespace(email=TO, totp_reset=True, google_unlinked=True),  # type: ignore[arg-type]
    ),
}


def _built(message: MailMessage) -> EmailMessage:
    return Mailer(SETTINGS)._build(message)  # pyright: ignore[reportPrivateUsage]


def test_every_account_mail_builder_is_listed() -> None:
    """A new account mail joins ``ACCOUNT_MAILS`` (and so these tests) in its own PR."""
    builders = {
        name
        for name, value in vars(mails).items()
        if inspect.isfunction(value)
        and not name.startswith("_")
        and inspect.signature(value).return_annotation in (MailMessage, "MailMessage")
    }
    listed = {name.removesuffix("_after_mfa_reset") for name in ACCOUNT_MAILS} - {
        "operator_reconnect"
    }
    assert builders == listed


@pytest.mark.parametrize("name", sorted(ACCOUNT_MAILS))
def test_an_account_or_security_mail_has_no_unsubscribe_link(name: str) -> None:
    message = ACCOUNT_MAILS[name]()
    assert message.unsubscribe_url is None
    assert "/api/unsubscribe" not in message.body
    built = _built(message)
    assert built["List-Unsubscribe"] is None
    assert built["List-Unsubscribe-Post"] is None


@pytest.mark.parametrize("kind", [*ALWAYS_SENT_KINDS, *OWNER_NOTICE_KINDS])
def test_an_outbox_kind_that_is_always_sent_is_not_suppressible(kind: str) -> None:
    assert not suppressible(kind)


def test_the_suppressible_kinds_are_the_other_user_kinds() -> None:
    assert set(SUPPRESSIBLE_KINDS) == set(USER_KINDS) - set(ALWAYS_SENT_KINDS)
    assert ALWAYS_SENT_KINDS == ("export_ready",)
    assert not suppressible("a_kind_added_later")  # sent until listed on purpose


def test_the_account_mail_path_reads_no_preference() -> None:
    """No module of ``ownvitals.auth`` and not ``ownvitals.keys_cli`` or ``ownvitals.mail``
    imports the policy, the token or the preference store, or names the table."""
    banned = {
        "ownvitals.notify.email_policy",
        "ownvitals.notify.email_prefs_store",
        "ownvitals.notify.unsubscribe",
    }
    paths = [*sorted((SRC / "auth").rglob("*.py")), SRC / "keys_cli.py", SRC / "mail.py"]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "email_prefs" not in source, path
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module, *(f"{node.module}.{a.name}" for a in node.names)}
                assert not names & banned, path
            elif isinstance(node, ast.Import):
                assert not {a.name for a in node.names} & banned, path
    assert email_policy.__name__ in banned


def test_a_mail_with_a_link_carries_both_headers_unencoded() -> None:
    url = f"{BASE}/api/unsubscribe?t=" + "A" * 67  # longer than 78 with the header name
    built = _built(MailMessage(TO, "ownvitals: något väntar", "synthetic", "sv", url))
    raw = built.as_bytes()
    assert f"List-Unsubscribe: <{url}>".encode() in raw
    assert b"List-Unsubscribe-Post: List-Unsubscribe=One-Click" in raw
    assert b"=?utf-8?q?=3Chttps" not in raw


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", f"{BASE}/a b", f"{BASE}/a\r\nBcc: x@example.test", f"{BASE}/<x>", ""],
)
def test_a_link_that_is_not_a_plain_url_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match="not a plain URL"):
        MailMessage(TO, "subject", "body", "en", url)
