"""The fixed templates of the outbox kinds, one table per locale (ADR 0007 section 2, ADR 0015
section 7, threat model T59 and O8; #194, #253).

Every mail says only that something is waiting and links to the app: no value, no finding text,
no ECG result, no name, no address, no token, no number. ``render`` takes the kind, the route key,
the public base URL and the locale, and nothing else, so nothing from a row or an account can
reach the text (``tests/test_notify_outbox_templates.py``). The locale is an allowlisted code
whose only use is to pick a table; every table has the same kinds, each with ``{link}`` exactly
once and no other placeholder (``tests/test_notify_templates_locales.py``). The link is
``public_base_url`` plus the route's fixed path (T12).

A suppressible kind's mail (``ownvitals.notify.email_policy``; ADR 0017, #459) then goes through
``with_unsubscribe``, which appends the locale's unsubscribe footer, whose only placeholder is the
signed one-click link: the endpoint's fixed path under ``public_base_url`` and an opaque token of
a fixed alphabet and length (``ownvitals.notify.unsubscribe``; anything else is refused).
``Rendered.unsubscribe_url`` carries the same link for the RFC 8058 headers. It refuses every
other kind, so that mail never has a link or the headers.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from ownvitals.notify import unsubscribe
from ownvitals.notify.email_policy import suppressible
from ownvitals.notify.outbox import OWNER_NOTICE_ROUTES, ROUTE_PATHS, USER_KINDS, USER_ROUTES
from ownvitals.notify.templates import en, sv
from ownvitals.notify.templates.base import LINK, UNSUBSCRIBE_LINK, Template

__all__ = [
    "FOOTERS",
    "LINK",
    "TABLES",
    "UNSUBSCRIBE_LINK",
    "Rendered",
    "Template",
    "render",
    "route_allowed",
    "with_unsubscribe",
]

# One table per code of the locale manifest (``ownvitals.locales``), the same kinds in each.
TABLES: Final[Mapping[str, Mapping[str, Template]]] = MappingProxyType(
    {"en": en.TABLE, "sv": sv.TABLE}
)
# The unsubscribe footer of each table's locale (ADR 0017).
FOOTERS: Final[Mapping[str, str]] = MappingProxyType({"en": en.UNSUBSCRIBE, "sv": sv.UNSUBSCRIBE})


@dataclass(frozen=True, slots=True, repr=False)
class Rendered:
    subject: str
    body: str
    locale: str  # the table's code: the mail's ``Content-Language``
    unsubscribe_url: str | None = None  # the RFC 8058 link, for a suppressible kind only

    def __repr__(self) -> str:
        return "<Rendered mail>"


def route_allowed(kind: str, route: str) -> bool:
    """Whether a row of ``kind`` may link to ``route``: an owner notice only to its kind's route,
    a user mail only to a user-facing route."""
    if kind in OWNER_NOTICE_ROUTES:
        return OWNER_NOTICE_ROUTES[kind] == route
    return kind in USER_KINDS and route in USER_ROUTES


def render(kind: str, route: str, public_base_url: str, locale: str) -> Rendered:
    """The mail of ``kind`` linking to ``route``, from ``locale``'s table. ``ValueError`` for a
    locale without a table, a kind without a template or a route the kind may not use."""
    table = TABLES.get(locale) if isinstance(locale, str) else None
    if table is None:
        raise ValueError("no template table for this locale")
    template = table.get(kind)
    if template is None or not route_allowed(kind, route) or route not in ROUTE_PATHS:
        raise ValueError("no template for this outbox row")
    link = public_base_url.rstrip("/") + ROUTE_PATHS[route]
    return Rendered(
        subject=template.subject, body=template.body.replace(LINK, link, 1), locale=locale
    )


def with_unsubscribe(mail: Rendered, kind: str, public_base_url: str, token: str) -> Rendered:
    """``mail`` (of ``kind``) with its locale's unsubscribe footer and ``unsubscribe_url`` set.
    ``ValueError`` for a kind that is not suppressible, a mail that has a link already and a
    token that is not token-shaped."""
    if not suppressible(kind) or mail.unsubscribe_url is not None:
        raise ValueError("an unsubscribe link belongs to a suppressible kind's mail, once")
    url = unsubscribe.link(public_base_url, token)  # refuses any other text
    footer = FOOTERS[mail.locale].replace(UNSUBSCRIBE_LINK, url, 1)
    return Rendered(
        subject=mail.subject, body=mail.body + footer, locale=mail.locale, unsubscribe_url=url
    )
