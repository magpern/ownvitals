"""The fixed template of each outbox kind (ADR 0007 section 2, threat model T59 and O8; #194).

Every mail says only that something is waiting and links to the app: no value, no finding text,
no ECG result, no name, no address, no token, no number. ``render`` takes the kind, the route key
and the public base URL and nothing else, so nothing from a row or an account can reach the text
(``tests/test_notify_outbox_templates.py``). The link is ``public_base_url`` plus the route's
fixed path (T12).
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from ownvitals.notify.outbox import OWNER_NOTICE_ROUTES, ROUTE_PATHS, USER_KINDS, USER_ROUTES

LINK: Final = "{link}"
_FOOTER: Final = (
    "\n\nThis email says no more than that on purpose: what is waiting is shown only after you "
    "sign in.\n\n- ownvitals\n"
)


@dataclass(frozen=True, slots=True)
class Template:
    subject: str
    body: str  # holds ``LINK`` exactly once


TEMPLATES: Final = MappingProxyType(
    {
        "findings_waiting": Template(
            subject="ownvitals: something is waiting for you",
            body="Something new is waiting for you in ownvitals.\n\nOpen it here: "
            + LINK
            + _FOOTER,
        ),
        "connection_attention": Template(
            subject="ownvitals: a connection needs your attention",
            body="One of your connections in ownvitals needs your attention.\n\nOpen it here: "
            + LINK
            + _FOOTER,
        ),
        "connection_needs_reconnect": Template(
            subject="ownvitals: something is waiting on the admin page",
            body="Something is waiting for an administrator in ownvitals.\n\nOpen the admin page "
            "here: " + LINK + _FOOTER,
        ),
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class Rendered:
    subject: str
    body: str

    def __repr__(self) -> str:
        return "<Rendered mail>"


def route_allowed(kind: str, route: str) -> bool:
    """Whether a row of ``kind`` may link to ``route``: an owner notice only to its kind's route,
    a user mail only to a user-facing route."""
    if kind in OWNER_NOTICE_ROUTES:
        return OWNER_NOTICE_ROUTES[kind] == route
    return kind in USER_KINDS and route in USER_ROUTES


def render(kind: str, route: str, public_base_url: str) -> Rendered:
    """The mail of ``kind`` linking to ``route``. ``ValueError`` for a kind without a template
    or a route the kind may not use."""
    template = TEMPLATES.get(kind)
    if template is None or not route_allowed(kind, route) or route not in ROUTE_PATHS:
        raise ValueError("no template for this outbox row")
    link = public_base_url.rstrip("/") + ROUTE_PATHS[route]
    return Rendered(subject=template.subject, body=template.body.replace(LINK, link, 1))
