"""Account-mail keys whose Swedish text is still the English copy (ADR 0015 section 8).

A PR that adds or changes an English text lists its key here and copies the English text to
``sv.py``; the translation PR (G6 and later) translates it and removes it from the list. A test
fails on a Swedish text equal to the English one that is not listed, and on a listed one that
differs or no longer exists (``tests/test_auth_mail_locales.py``). Separators that read the same
in both languages are not listed, so ``list.separator`` stays off the list once translated too.
"""

from typing import Final

from ownvitals.auth.mail_texts import en

UNTRANSLATED_SV: Final = frozenset(en.TEXTS) - {"list.separator"}
