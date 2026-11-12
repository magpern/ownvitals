"""The unsubscribe endpoint's two pages, in English and Swedish (ADR 0017; #459). Pure: no I/O.

``confirm`` (the answer to ``GET``) asks before anything changes and posts the link's token back
to the same URL; ``done`` (the answer to every ``POST``) says the same whether the token was
valid, expired, forged or already used, so the page is no oracle. Neither names the user, the
address or the kind. Plain HTML with no script, no style and no external resource, so the
app's CSP (``ownvitals.headers``) applies unchanged; the form posts to the same origin
(``form-action 'self'``). The token is echoed only when it has the token's own alphabet and
length (``ownvitals.notify.unsubscribe.TOKEN_PATTERN``), and HTML-escaped all the same.
"""

from collections.abc import Mapping
from html import escape
from types import MappingProxyType
from typing import Final

from ownvitals.notify.unsubscribe import QUERY, TOKEN_PATTERN

TEXTS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        "en": MappingProxyType(
            {
                "title": "Email from ownvitals",
                "confirm.heading": "Stop these emails?",
                "confirm.body": "This switches off the kind of email this link came in. Account "
                "and security emails, and an export you asked for, always arrive.",
                "confirm.button": "Switch them off",
                "done.heading": "Thank you",
                "done.body": "If the link was still valid, that kind of email is now switched "
                "off. You can change every email choice in ownvitals under Settings, "
                "Notifications.",
                "limited.heading": "Too many requests",
                "limited.body": "Please try again later.",
            }
        ),
        "sv": MappingProxyType(
            {
                "title": "E-post från ownvitals",
                "confirm.heading": "Sluta få de här mejlen?",
                "confirm.body": "Det här stänger av den sortens mejl som länken kom i. Mejl om "
                "ditt konto och din säkerhet, och en export du har bett om, kommer alltid.",
                "confirm.button": "Stäng av dem",
                "done.heading": "Tack",
                "done.body": "Om länken fortfarande gällde är den sortens mejl nu avstängd. Du "
                "kan ändra alla val för e-post i ownvitals under Inställningar, Aviseringar.",
                "limited.heading": "För många förfrågningar",
                "limited.body": "Försök igen senare.",
            }
        ),
    }
)


def _page(locale: str, heading: str, body: str, form: str = "") -> str:
    texts = TEXTS[locale]
    return (
        "<!doctype html>\n"
        f'<html lang="{locale}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="robots" content="noindex">\n'
        f"<title>{escape(texts['title'])}</title>\n</head>\n<body>\n<main>\n"
        f"<h1>{escape(texts[heading])}</h1>\n<p>{escape(texts[body])}</p>\n{form}"
        "</main>\n</body>\n</html>\n"
    )


def confirm(locale: str, token: object) -> str:
    """The ``GET`` page: a question and a button that posts the token back. A value that is not
    token-shaped is not echoed (the form then posts an empty one)."""
    shown = token if isinstance(token, str) and TOKEN_PATTERN.fullmatch(token) else ""
    form = (
        f'<form method="post" action="?{QUERY}={escape(shown)}">\n'
        '<input type="hidden" name="List-Unsubscribe" value="One-Click">\n'
        f'<button type="submit">{escape(TEXTS[locale]["confirm.button"])}</button>\n</form>\n'
    )
    return _page(locale, "confirm.heading", "confirm.body", form)


def done(locale: str) -> str:
    """The ``POST`` page, the same for every token."""
    return _page(locale, "done.heading", "done.body")


def limited(locale: str) -> str:
    """The page of a refused request (the rate limit)."""
    return _page(locale, "limited.heading", "limited.body")
