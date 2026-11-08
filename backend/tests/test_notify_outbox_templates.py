"""The outbox's templates and its worker boundary (ADR 0007 section 2, T59, O8; #194). No database.

- every email body, in every locale's table, says only that something is waiting and links to
  the app: no health value, finding text, token or number, and nothing from a row or an account
  can reach it; the locale is an allowlisted code that only picks a table (``test_*template*``,
  ``test_render_*``, ``test_the_locale_only_selects_a_table``; ADR 0015 section 7, #253);
- the worker cannot open SMTP for the outbox: no worker module imports the sender, the worker
  process never loads the mail module through the outbox, and the sender refuses in the worker
  (the extended guard of ``tests/egress_worker.py``; ``test_notify_outbox_sender.py`` runs the
  refusal against a database).
"""

import inspect
import re
from pathlib import Path

import pytest

from egress_scan import imported_modules, sources
from egress_worker import WORKER_ENTRYPOINTS, WORKER_PACKAGES, loaded_watched_modules
from ownvitals.locales import SUPPORTED_LOCALES
from ownvitals.notify import outbox, sender
from ownvitals.notify.outbox import (
    OWNER_NOTICE_KINDS,
    OWNER_NOTICE_ROUTES,
    ROUTE_PATHS,
    USER_KINDS,
    USER_ROUTES,
)
from ownvitals.notify.templates import LINK, TABLES, render, route_allowed

SRC = Path(__file__).resolve().parent.parent / "src"
BASE = "https://ownvitals.example.test"
# What a mail may never carry (T59): numbers (a value, a date, a count), units, finding or ECG
# words, credentials, and placeholders a later edit could fill from a row. Swedish words are
# listed beside the English ones so the translation of G6 is held to the same rule.
FORBIDDEN = re.compile(
    r"[0-9]|%|\{(?!link\})|\$|kg|bpm|mmhg|ecg|ekg|afib|fibrillation|flimmer|weight|vikt|pressure"
    r"|blodtryck|pulse|puls|heart rate|hjärt|sleep|sömn|finding|fynd|token|password|lösenord|secret"
    r"|hemlig|code|kod|\bkey\b|nyckel",
    re.IGNORECASE,
)
CASES = [(locale, kind) for locale in sorted(TABLES) for kind in sorted(TABLES[locale])]


def test_every_locale_has_exactly_one_template_per_kind() -> None:
    assert set(TABLES) == set(SUPPORTED_LOCALES)
    for table in TABLES.values():
        assert set(table) == {*USER_KINDS, *OWNER_NOTICE_KINDS}


@pytest.mark.parametrize(("locale", "kind"), CASES)
def test_a_template_says_only_that_something_is_waiting(locale: str, kind: str) -> None:
    template = TABLES[locale][kind]
    assert template.body.count(LINK) == 1
    assert LINK not in template.subject
    for text in (template.subject, template.body.replace(LINK, "")):
        assert FORBIDDEN.search(text) is None, (locale, kind, FORBIDDEN.search(text))
    if locale == "en":
        assert "waiting" in template.subject or "attention" in template.subject
        assert "sign in" in template.body


@pytest.mark.parametrize(("locale", "kind"), CASES)
def test_render_fills_in_the_link_and_nothing_else(locale: str, kind: str) -> None:
    route = OWNER_NOTICE_ROUTES.get(kind) or USER_ROUTES[0]
    mail = render(kind, route, BASE + "/", locale)
    link = BASE + ROUTE_PATHS[route]
    assert mail.subject == TABLES[locale][kind].subject
    assert mail.body == TABLES[locale][kind].body.replace(LINK, link)
    assert mail.locale == locale
    assert repr(mail) == "<Rendered mail>"  # never the text in a log line


def test_render_takes_no_data_from_a_row_or_an_account() -> None:
    """Its inputs are the kind, the route key, the public base URL and the locale code: no owner,
    no address, no value can reach the text."""
    assert list(inspect.signature(render).parameters) == [
        "kind",
        "route",
        "public_base_url",
        "locale",
    ]


@pytest.mark.parametrize(
    "locale", ["de", "EN", "en-GB", " sv", "", "../en", "{link}", None, 1, ("en",)]
)
def test_render_refuses_a_locale_outside_the_allowlist(locale: object) -> None:
    with pytest.raises(ValueError, match="no template table"):
        render("findings_waiting", "findings", BASE, locale)  # type: ignore[arg-type]


def test_the_locale_only_selects_a_table() -> None:
    """The code never reaches the text: every rendering equals its table's template, whichever
    locale, and a table's text does not change with the code passed."""
    for locale, kind in CASES:
        route = OWNER_NOTICE_ROUTES.get(kind) or USER_ROUTES[0]
        mail = render(kind, route, BASE, locale)
        assert locale not in {mail.subject, mail.body}
        assert mail.body == TABLES[locale][kind].body.replace(LINK, BASE + ROUTE_PATHS[route])


@pytest.mark.parametrize(
    ("kind", "route"),
    [
        ("findings_waiting", "admin"),
        ("connection_needs_reconnect", "findings"),
        ("unknown_kind", "findings"),
        ("findings_waiting", "https://evil.example/"),
        ("findings_waiting", "../admin"),
    ],
)
def test_render_refuses_a_kind_or_route_outside_the_lists(kind: str, route: str) -> None:
    assert not route_allowed(kind, route)
    for locale in TABLES:
        with pytest.raises(ValueError, match="no template"):
            render(kind, route, BASE, locale)


def test_every_user_route_and_owner_route_has_a_path() -> None:
    for route in (*USER_ROUTES, *OWNER_NOTICE_ROUTES.values()):
        assert ROUTE_PATHS[route].startswith("/")
        assert "//" not in ROUTE_PATHS[route]


# --- the worker boundary -----------------------------------------------------------------------


def test_the_outbox_module_counts_as_worker_code_and_the_sender_does_not() -> None:
    assert outbox.__name__ in WORKER_PACKAGES
    assert outbox.__name__ in WORKER_ENTRYPOINTS
    assert not sender.__name__.startswith(WORKER_PACKAGES)


def test_no_worker_module_imports_the_sender_or_mail() -> None:
    for module, source in sources(SRC):
        if not module.startswith(WORKER_PACKAGES):
            continue
        imported = imported_modules(source, module)
        assert not any(
            name.startswith(("ownvitals.notify.sender", "ownvitals.mail")) for name in imported
        ), module


def test_the_worker_process_does_not_load_mail_through_the_outbox() -> None:
    assert loaded_watched_modules(("ownvitals.notify.outbox", "ownvitals.notify")) == []


def test_the_reachability_check_sees_mail_through_the_sender() -> None:
    """The check above would fail if the outbox module imported the sender."""
    assert loaded_watched_modules(("ownvitals.notify.sender",)) != []
