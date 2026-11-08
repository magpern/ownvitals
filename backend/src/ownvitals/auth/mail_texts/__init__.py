"""The account mails' texts, one fixed table per locale (ADR 0015 section 7; #253).

``table(locale)`` picks the table of an allowlisted code (``ValueError`` for any other), so the
locale selects text and never becomes part of it. Every table has the same keys
with the same placeholders (``tests/test_auth_mail_locales.py``). Dates are ISO (``YYYY-MM-DD``):
no month or day words, so nothing here asks ``strftime`` for a name or reads the process
locale.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from ownvitals.auth.mail_texts import en, sv

TABLES: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType({"en": en.TEXTS, "sv": sv.TEXTS})


def table(locale: str) -> Mapping[str, str]:
    """``locale``'s table; ``ValueError`` for a code without one."""
    found = TABLES.get(locale) if isinstance(locale, str) else None
    if found is None:
        raise ValueError("no mail texts for this locale")
    return found
