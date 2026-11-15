"""Outbox kinds whose Swedish template is still the English copy (ADR 0015 section 8).

A PR that adds or changes an English template lists its kind here and copies the English text to
``sv.py``; the translation PR translates it and removes it from the list (G6 emptied it). A test
fails on a Swedish template equal to the English one that is not listed, and on a listed one that
differs or no longer exists (``tests/test_notify_templates_locales.py``).
"""

from typing import Final

# export_ready (#333): the English text until the translation PR.
UNTRANSLATED_SV: Final[frozenset[str]] = frozenset({"export_ready"})
