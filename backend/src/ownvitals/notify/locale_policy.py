"""Which language a mail uses (ADR 0015 section 7). Pure: the caller passes the stored value, the
request's language and the codes it may use.

- outbox mail to a user: the stored ``users.locale``; ``NULL`` (or a code no longer offered) is
  ``en``, since there is no browser at send time;
- owner notices to the admins: ``en``, whatever the admins' own languages, like the admin area;
- auth mail inside a request: the account's stored language if it has one, otherwise the
  request's language; mail before an account exists always uses the request's language;
- anything else: ``en``.

Every result is a code from ``usable`` (or ``en``): the stored and the requested values are only
compared with it, never echoed.
"""

from collections.abc import Collection
from typing import Final

DEFAULT_LOCALE: Final = "en"
OWNER_NOTICE_LOCALE: Final = DEFAULT_LOCALE
LOCALE_MAX: Final = 16  # longer is certainly not a code


def usable_code(usable: Collection[str], value: object) -> str | None:
    """``value`` itself when it is one of ``usable``, else ``None``."""
    if isinstance(value, str) and len(value) <= LOCALE_MAX and value in usable:
        return value
    return None


def outbox_locale(usable: Collection[str], stored: object) -> str:
    """The language of outbox mail to a user."""
    return usable_code(usable, stored) or DEFAULT_LOCALE


def request_mail_locale(usable: Collection[str], stored: object, requested: object) -> str:
    """The language of auth mail sent inside a request: the account's stored language
    (``stored``; ``None`` when there is no account), else the request's, else ``en``."""
    return usable_code(usable, stored) or usable_code(usable, requested) or DEFAULT_LOCALE
