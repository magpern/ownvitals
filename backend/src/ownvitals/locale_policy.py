"""Which language a request, an account or a sign-up uses (ADR 0015 sections 1 and 2). Pure.

A language is *offered* when the manifest supports it (``locales.SUPPORTED_LOCALES``) and the
operator enables it (``OWNVITALS_LOCALES``; ``en`` is always on). A locale is an allowlisted code
everywhere: input is compared with the offered set and otherwise dropped, never echoed, and never
builds a path, header or query.
"""

from collections.abc import Collection, Iterable

DEFAULT_LOCALE = "en"
# Accept-Language is read from its first 256 bytes and at most 8 entries (ADR 0015 section 2), so
# an 8 KiB header with a thousand entries costs the same as a short one. Starlette decodes header
# bytes as Latin-1, one character per byte, so the slice below counts bytes.
ACCEPT_LANGUAGE_BYTES = 256
ACCEPT_LANGUAGE_ENTRIES = 8
LOCALE_FIELD_MAX = 16  # a request's ``locale`` field: longer is certainly not a code


def offered_locales(supported: Iterable[str], enabled: Collection[str]) -> tuple[str, ...]:
    """Supported and enabled, in the manifest's order. ``enabled`` always holds ``en``."""
    return tuple(code for code in supported if code in enabled or code == DEFAULT_LOCALE)


def offered_value(offered: Collection[str], value: object) -> str | None:
    """``value`` itself when it is an offered code, else ``None``."""
    if isinstance(value, str) and len(value) <= LOCALE_FIELD_MAX and value in offered:
        return value
    return None


def parse_accept_language(header: str | None, offered: Collection[str]) -> str | None:
    """The offered language the header ranks first, or ``None``. Never raises.

    Bounded: only the first 256 bytes are split, into at most 8 entries. An entry matches by its
    primary subtag (``sv-SE`` is ``sv``); ``q=0`` or a malformed ``q`` excludes it, and ties keep
    the header's order.
    """
    if not header:
        return None
    best: tuple[float, str] | None = None
    entries = header[:ACCEPT_LANGUAGE_BYTES].split(",", ACCEPT_LANGUAGE_ENTRIES)
    for entry in entries[:ACCEPT_LANGUAGE_ENTRIES]:
        tag, _, params = entry.partition(";")
        code = tag.strip().lower().split("-", 1)[0]
        if code not in offered:
            continue
        quality = 1.0
        for param in params.split(";")[:2]:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 0.0
        if not 0.0 < quality <= 1.0:  # also drops nan
            continue
        if best is None or quality > best[0]:
            best = (quality, code)
    return None if best is None else best[1]


def request_locale(offered: Collection[str], field: object, accept_language: str | None) -> str:
    """The language of a request: the body's ``locale`` field if offered, else the
    ``Accept-Language`` header, else English. Always an offered code (or ``en``)."""
    return (
        offered_value(offered, field)
        or parse_accept_language(accept_language, offered)
        or DEFAULT_LOCALE
    )


def storable_locale(offered: Collection[str], locale: str | None) -> str | None:
    """What a new account (or its pending row or OAuth flow) stores: ``locale`` when offered.
    With a single offered language every account would be pinned to it once more arrive, so
    ``NULL`` (follow the browser) is stored until a second one is offered (classbeat #664)."""
    if len(offered) <= 1:
        return None
    return offered_value(offered, locale)
