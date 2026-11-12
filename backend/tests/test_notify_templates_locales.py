"""The outbox mail in the recipient's language (ADR 0015 section 7, ADR 0007 sections 1-2, T59;
#253). No database.

- one fixed table per locale of the manifest with the same kinds, ``{link}`` exactly once and no
  other placeholder, and a golden file per kind and locale (``test_golden_*``); a suppressible
  kind's golden file ends with the locale's unsubscribe footer and a synthetic token, and every
  locale has that footer with ``{unsubscribe}`` exactly once (ADR 0018, #459);
- the Swedish table is the English copy on the untranslated list until G6 translates it
  (``test_the_untranslated_list_*``);
- the decision is pure: a user's stored code while usable, else ``en``; owner notices ``en``;
  request mail the stored code, else the request's (``test_policy_*``);
- the outbox row is unchanged: its SQL and its definer digests are pinned
  (``test_the_outbox_sql_and_its_digests_are_unchanged``; the column list is pinned in
  ``test_notify_outbox.py::test_the_columns_are_exactly_these``).

Regenerate the golden files after an intended text change with
``WRITE_MAIL_GOLDEN=1 uv run pytest tests/test_notify_templates_locales.py``.
"""

import hashlib
import os
import string
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ownvitals.db.outbox_digests import OUTBOX_DIGESTS
from ownvitals.locales import SUPPORTED_LOCALES
from ownvitals.notify import locale_policy, unsubscribe
from ownvitals.notify.email_policy import suppressible
from ownvitals.notify.outbox import OWNER_NOTICE_ROUTES, USER_ROUTES
from ownvitals.notify.templates import FOOTERS, TABLES, render, with_unsubscribe
from ownvitals.notify.templates import en as en_table
from ownvitals.notify.templates.untranslated import UNTRANSLATED_SV

GOLDEN = Path(__file__).resolve().parent / "mail_golden" / "outbox"
REPO = Path(__file__).resolve().parents[2]
BASE = "https://ownvitals.example.test"
CASES = [(locale, kind) for locale in sorted(TABLES) for kind in sorted(TABLES[locale])]
# A synthetic token for the golden files: a zero key, a made-up owner, a fixed instant.
SYNTHETIC_OWNER = uuid.UUID("00000000-0000-4000-8000-000000000459")
SYNTHETIC_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _bare(text: str) -> bool:
    """True when every field is a plain ``{name}``: no format spec or ``!r`` (ADR 0015 s9)."""
    return all(
        spec == "" and conversion is None
        for _, name, spec, conversion in string.Formatter().parse(text)
        if name is not None
    )


def _fields(text: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(text) if name is not None]


def _golden(path: Path, actual: str) -> None:
    if os.environ.get("WRITE_MAIL_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == actual, f"{path.name} differs from its golden file"


@pytest.mark.parametrize(("locale", "kind"), CASES)
def test_golden_file_per_kind_and_locale(locale: str, kind: str) -> None:
    route = OWNER_NOTICE_ROUTES.get(kind) or USER_ROUTES[0]
    mail = render(kind, route, BASE, locale)
    headers = ""
    if suppressible(kind):
        token = unsubscribe.issue(bytes(32), SYNTHETIC_OWNER, kind, 0, SYNTHETIC_NOW)
        mail = with_unsubscribe(mail, kind, BASE, token)
        headers = (
            f"List-Unsubscribe: <{mail.unsubscribe_url}>\n"
            "List-Unsubscribe-Post: List-Unsubscribe=One-Click\n"
        )
    rendered = f"Content-Language: {mail.locale}\n{headers}Subject: {mail.subject}\n\n{mail.body}"
    _golden(GOLDEN / locale / f"{kind}.txt", rendered)


def test_every_locale_has_the_unsubscribe_footer_with_only_its_link() -> None:
    assert set(FOOTERS) == set(TABLES)
    for locale, footer in FOOTERS.items():
        assert _fields(footer) == ["unsubscribe"], locale
        assert _bare(footer), locale


def test_every_golden_file_belongs_to_a_kind_and_locale() -> None:
    files = {(p.parent.name, p.stem) for p in GOLDEN.glob("*/*.txt")}
    assert files == set(CASES)


def test_every_locale_has_every_kind_and_only_the_link_placeholder() -> None:
    assert set(TABLES) == set(SUPPORTED_LOCALES)
    kinds = set(en_table.TABLE)
    for locale, table in TABLES.items():
        assert set(table) == kinds, locale
        for kind, template in table.items():
            assert _fields(template.subject) == [], (locale, kind)
            assert _fields(template.body) == ["link"], (locale, kind)
            assert _bare(template.subject), (locale, kind)
            assert _bare(template.body), (locale, kind)


@pytest.mark.parametrize("text", ["{link!r}", "{link:>9}"])
def test_the_placeholder_check_rejects_a_conversion_or_format_spec(text: str) -> None:
    assert not _bare(text)


def test_the_untranslated_list_holds_exactly_the_english_copies() -> None:
    copies = {kind for kind, t in TABLES["sv"].items() if t == en_table.TABLE[kind]}
    assert copies == UNTRANSLATED_SV
    assert set(en_table.TABLE) >= UNTRANSLATED_SV


USABLE = frozenset({"en", "sv"})


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("sv", "sv"),
        ("en", "en"),
        (None, "en"),
        ("de", "en"),
        ("SV", "en"),
        ("sv ", "en"),
        (3, "en"),
    ],
)
def test_policy_outbox_mail_uses_the_stored_code_or_english(stored: object, expected: str) -> None:
    assert locale_policy.outbox_locale(USABLE, stored) == expected


def test_policy_a_code_switched_off_is_english() -> None:
    assert locale_policy.outbox_locale(frozenset({"en"}), "sv") == "en"


def test_policy_owner_notices_are_english() -> None:
    assert locale_policy.OWNER_NOTICE_LOCALE == "en"


@pytest.mark.parametrize(
    ("stored", "requested", "expected"),
    [
        ("sv", "en", "sv"),  # the account's own language wins
        ("en", "sv", "en"),
        (None, "sv", "sv"),  # no stored language (or no account): the request's
        ("de", "sv", "sv"),
        (None, "de", "en"),
        (None, None, "en"),
    ],
)
def test_policy_request_mail(stored: object, requested: object, expected: str) -> None:
    assert locale_policy.request_mail_locale(USABLE, stored, requested) == expected


def test_the_outbox_sql_and_its_digests_are_unchanged() -> None:
    """G5 adds no column and changes no definer function (ADR 0015 section 7, T59): the script
    and the committed digests are those of #194, with #459's two added functions (ADR 0018); the
    three of #194 keep their digests."""
    raw = (REPO / "docker" / "postgres" / "notify" / "50-outbox.sql").read_bytes()
    sql = raw.replace(b"\r\n", b"\n")  # the same on a Windows checkout
    assert b"locale" not in sql
    assert hashlib.sha256(sql).hexdigest() == OUTBOX_SQL_SHA256
    assert OUTBOX_DIGESTS == PINNED_DIGESTS


OUTBOX_SQL_SHA256 = "31cf411d09985d064f1c830f762c264a9889f3b5e1e46d67baefda2ad1723cf6"
PINNED_DIGESTS = {
    "mail_outbox_claim": "d9aa404f02b705056ea3779649d8c9d6",
    "mail_outbox_mark": "f75b98f2c485898329855d8151fabbad",
    "owner_notice_queue": "6335e9f45dbcdd2ec26720b096cb96ab",
    "mail_outbox_email_pref": "fb1c04e5cc3cbaa0a30a72b7b7c11a1f",
    "email_unsubscribe": "60dd83702d7409672762116d0a484839",
}
