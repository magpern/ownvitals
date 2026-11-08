"""The English outbox templates, the source of every locale's table (ADR 0007 section 2, ADR 0015
section 7). Each says only that something is waiting and links to the app (T59, O8)."""

from types import MappingProxyType
from typing import Final

from ownvitals.notify.templates.base import LINK, Template

_FOOTER: Final = (
    "\n\nThis email says no more than that on purpose: what is waiting is shown only after you "
    "sign in.\n\n- ownvitals\n"
)

TABLE: Final = MappingProxyType(
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
