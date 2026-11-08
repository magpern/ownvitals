"""The account mails in the recipient's language (ADR 0015 section 7; #253). No database.

- every locale's table has the same keys with the same placeholders, and every mail has a golden
  file per locale (``test_golden_*``, ``test_every_table_*``);
- the Swedish table is the English copy on the untranslated list until G6 translates it;
- a locale outside the allowlist raises, for a mail and for ``MailMessage`` itself, and the code
  becomes the ``Content-Language`` header and nothing else (``test_*locale*``);
- no mail module formats month or day words or touches the process locale
  (``test_no_month_words_or_process_locale``).

Regenerate the golden files after an intended text change with
``WRITE_MAIL_GOLDEN=1 uv run pytest tests/test_auth_mail_locales.py``.
"""

import os
import re
import string
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ownvitals.auth import mails
from ownvitals.auth.mail_texts import TABLES, en
from ownvitals.auth.mail_texts.untranslated import UNTRANSLATED_SV
from ownvitals.config import Settings
from ownvitals.locales import SUPPORTED_LOCALES
from ownvitals.mail import Mailer, MailMessage

GOLDEN = Path(__file__).resolve().parent / "mail_golden" / "auth"
SRC = Path(__file__).resolve().parent.parent / "src" / "ownvitals"
SETTINGS = Settings(public_base_url="https://ownvitals.example.test")  # pyright: ignore[reportCallIssue]
TO = "synthetic@example.test"
TOKEN = "synthetic-token"
CREATED = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)

MAILS: dict[str, Callable[[str], MailMessage]] = {
    "signup_confirmation": lambda loc: mails.signup_confirmation(SETTINGS, TO, TOKEN, locale=loc),
    "account_exists": lambda loc: mails.account_exists(SETTINGS, TO, locale=loc),
    "password_reset": lambda loc: mails.password_reset(SETTINGS, TO, TOKEN, locale=loc),
    "password_reset_passkeys": lambda loc: mails.password_reset(
        SETTINGS, TO, TOKEN, passkeys=[("Synthetic key", CREATED)], locale=loc
    ),
    "password_reset_after_mfa_reset": lambda loc: mails.password_reset(
        SETTINGS,
        TO,
        TOKEN,
        after_mfa_reset=mails.ResetRemoved(passkeys=2, identities=1),
        locale=loc,
    ),
    "password_reset_after_mfa_reset_one_passkey": lambda loc: mails.password_reset(
        SETTINGS, TO, TOKEN, after_mfa_reset=mails.ResetRemoved(passkeys=1), locale=loc
    ),
    "email_change_confirmation": lambda loc: mails.email_change_confirmation(
        SETTINGS, TO, TOKEN, locale=loc
    ),
    "email_change_taken": lambda loc: mails.email_change_taken(SETTINGS, TO, locale=loc),
    "email_changed_notice": lambda loc: mails.email_changed_notice(TO, locale=loc),
    "totp_enrolled_notice": lambda loc: mails.totp_enrolled_notice(TO, locale=loc),
    "mfa_failures_notice": lambda loc: mails.mfa_failures_notice(TO, locale=loc),
    "provider_signup_confirmation": lambda loc: mails.provider_signup_confirmation(
        SETTINGS, TO, "google", TOKEN, locale=loc
    ),
    "identity_linked_notice": lambda loc: mails.identity_linked_notice(TO, "google", locale=loc),
    "passkey_added_notice": lambda loc: mails.passkey_added_notice(TO, "Synthetic key", locale=loc),
    "passkey_suspended_notice": lambda loc: mails.passkey_suspended_notice(
        TO, "Synthetic key", locale=loc
    ),
    "qr_login_approved_notice": lambda loc: mails.qr_login_approved_notice(
        SETTINGS, TO, "Firefox", "Linux", "Synthetic place", locale=loc
    ),
}
CASES = [(locale, name) for locale in sorted(TABLES) for name in sorted(MAILS)]


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


@pytest.mark.parametrize(("locale", "name"), CASES)
def test_golden_file_per_mail_and_locale(locale: str, name: str) -> None:
    mail = MAILS[name](locale)
    assert mail.locale == locale
    rendered = f"Content-Language: {mail.locale}\nSubject: {mail.subject}\n\n{mail.body}"
    path = GOLDEN / locale / f"{name}.txt"
    if os.environ.get("WRITE_MAIL_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == rendered, f"{name} ({locale}) differs"


def test_every_golden_file_belongs_to_a_mail_and_locale() -> None:
    assert {(p.parent.name, p.stem) for p in GOLDEN.glob("*/*.txt")} == set(CASES)


def test_every_table_has_the_english_keys_and_placeholders() -> None:
    assert set(TABLES) == set(SUPPORTED_LOCALES)
    for locale, table in TABLES.items():
        assert set(table) == set(en.TEXTS), locale
        for key, text in table.items():
            assert _fields(text) == _fields(en.TEXTS[key]), (locale, key)
            assert all(name.isidentifier() for name in _fields(text)), (locale, key)


def test_the_untranslated_list_holds_exactly_the_english_copies() -> None:
    copies = {key for key, text in TABLES["sv"].items() if text == en.TEXTS[key]}
    assert copies - {"list.separator"} == UNTRANSLATED_SV
    assert set(en.TEXTS) >= UNTRANSLATED_SV


def test_english_is_the_text_the_mails_had_before() -> None:
    """The catalogue moved the texts, it did not change them (spot checks of #37 and #129)."""
    assert MAILS["signup_confirmation"]("en").body.startswith(
        "Someone asked to create an ownvitals account with this address.\n\n"
    )
    removed = MAILS["password_reset_after_mfa_reset"]("en").body
    assert (
        "your authenticator app and recovery codes were removed, your passkeys were removed, "
        "Google sign-in was unlinked, your password was cleared and you were signed out "
        "everywhere." in removed
    )
    assert "- Synthetic key (added 2026-01-02)\n" in MAILS["password_reset_passkeys"]("en").body


@pytest.mark.parametrize("locale", ["de", "EN", "en-GB", "", "../en", "{link}", None])
def test_a_mail_refuses_a_locale_outside_the_allowlist(locale: object) -> None:
    for build in MAILS.values():
        with pytest.raises(ValueError, match="locale"):
            build(locale)  # type: ignore[arg-type]


@pytest.mark.parametrize("locale", ["de", "en\r\nBcc: x@example.test", "", None])
def test_a_message_refuses_a_locale_outside_the_manifest(locale: object) -> None:
    with pytest.raises(ValueError, match="locale"):
        MailMessage(TO, "s", "b", locale)  # type: ignore[arg-type]


@pytest.mark.parametrize("locale", sorted(SUPPORTED_LOCALES))
def test_the_code_is_the_content_language_header(locale: str) -> None:
    built = Mailer(SETTINGS)._build(MailMessage(TO, "s", "b\n", locale))  # pyright: ignore[reportPrivateUsage]
    assert built["Content-Language"] == locale
    assert MailMessage(TO, "s", "b").locale == "en"


def test_no_month_words_or_process_locale() -> None:
    """Month and day words come from the catalogue, never ``strftime("%B")`` or ``setlocale``
    (ADR 0015 section 7): no mail module formats a named month or day, or imports ``locale``."""
    modules = [
        SRC / "mail.py",
        SRC / "auth" / "mails.py",
        SRC / "auth" / "notices.py",
        SRC / "auth" / "notifier.py",
        *(SRC / "auth" / "mail_texts").glob("*.py"),
        *(SRC / "notify").rglob("*.py"),
    ]
    named = re.compile(r"%[aAbBch]|setlocale|^\s*(import locale|from locale )", re.MULTILINE)
    for path in modules:
        assert named.search(path.read_text(encoding="utf-8")) is None, path.name
