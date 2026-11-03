"""Where a request comes from, as display text (ADR 0011 section 6).

The setting ``geo_source`` picks the source; the default is ``none``:

- ``none``: no place ("Place unknown"). Nothing is read and nothing is called.
- ``cloudflare``: Cloudflare's visitor-location headers ``CF-IPCity`` and ``CF-IPCountry``,
  only on a request whose peer is in both ``trusted_proxies`` and ``cf_connecting_ip_proxies``
  (the rule of ``CF-Connecting-IP``, ADR 0003 section 8) and only when each header is present
  exactly once; otherwise both are ignored. SWAG must strip them from requests that did not come
  from Cloudflare (open item O17).
- ``geoip_file``: refused by the settings until the owner answers open question 2 and a reviewed
  PR adds the reader; this module has no code for it.

The place is display text only: NFKC, control and format characters (bidirectional overrides
among them) removed, whitespace collapsed, at most 64 characters, "City, CC" or "CC". It never
takes part in a security decision, a rate limit or an audit key. No source makes a network call.
"""

import re
import unicodedata
from typing import Final

from fastapi import Request

from ownvitals.auth.clientip import from_cloudflare_proxy
from ownvitals.config import Settings

UNKNOWN: Final = "Place unknown"
PLACE_MAX: Final = 64
CITY_HEADER: Final = "cf-ipcity"
COUNTRY_HEADER: Final = "cf-ipcountry"
_COUNTRY = re.compile(r"[A-Z0-9]{2}")  # ISO 3166-1 alpha-2, and Cloudflare's XX and T1


def clean(text: str, limit: int = PLACE_MAX) -> str:
    """NFKC, control and format characters removed, whitespace collapsed, cut to ``limit``."""
    text = unicodedata.normalize("NFKC", text[: limit * 4])
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co"))
    return " ".join(text.split())[:limit].strip()


def _single(request: Request, name: str) -> str | None:
    """The header's value when it is present exactly once. Header bytes arrive as Latin-1;
    UTF-8 bytes (a city such as "Malmö") are read as UTF-8, anything else stays as it came."""
    values = request.headers.getlist(name)
    if len(values) != 1:
        return None
    try:
        return values[0].encode("latin-1").decode("utf-8")
    except UnicodeError:
        return values[0]


def from_headers(city: str | None, country: str | None) -> str | None:
    """The place from the two header values: "City, CC", "CC" or ``None`` (unknown)."""
    code = clean(country or "").upper()
    if not _COUNTRY.fullmatch(code):
        return None
    name = clean(city or "", PLACE_MAX - 4)
    return clean(f"{name}, {code}") if name else code


def place(request: Request, settings: Settings) -> str | None:
    """The request's place, or ``None`` for "Place unknown"."""
    if settings.geo_source != "cloudflare":
        return None
    peer = request.client.host if request.client else None
    if not from_cloudflare_proxy(
        peer, settings.trusted_proxy_networks, settings.cf_connecting_ip_networks
    ):
        return None
    if len(request.headers.getlist(CITY_HEADER)) > 1:
        return None  # a duplicate is never believed
    return from_headers(_single(request, CITY_HEADER), _single(request, COUNTRY_HEADER))


def shown(value: str | None) -> str:
    return value or UNKNOWN
