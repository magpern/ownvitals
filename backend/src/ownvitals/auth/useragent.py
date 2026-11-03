"""Browser and OS names from a ``User-Agent`` header (ADR 0011 section 7).

A small parser of our own, for the QR approval screen: it names one of a fixed set of browser
families and operating systems, or ``other``. The header is the desktop's own claim, which the
phone shows "as reported by that browser"; it takes part in no security decision. The raw header
is never stored, only the two names this returns.
"""

from typing import Final

UA_MAX: Final = 512  # only this much of the header is looked at

BROWSERS: Final = ("Firefox", "Chrome", "Edge", "Safari", "Opera", "Samsung Internet", "other")
SYSTEMS: Final = ("Windows", "macOS", "iOS", "iPadOS", "Android", "Linux", "ChromeOS", "other")

# (marker, family), first match wins: the browsers built on Chromium name it too, and Chrome
# names Safari, so the more specific markers come first.
_BROWSER_MARKERS: Final = (
    ("SamsungBrowser/", "Samsung Internet"),
    ("EdgA/", "Edge"),
    ("EdgiOS/", "Edge"),
    ("Edg/", "Edge"),
    ("Edge/", "Edge"),
    ("OPR/", "Opera"),
    ("OPT/", "Opera"),
    ("Opera", "Opera"),
    ("FxiOS/", "Firefox"),
    ("Firefox/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chromium/", "Chrome"),
    ("Chrome/", "Chrome"),
)
_SYSTEM_MARKERS: Final = (
    ("iPad", "iPadOS"),
    ("iPhone", "iOS"),
    ("iPod", "iOS"),
    ("Android", "Android"),
    ("CrOS", "ChromeOS"),
    ("Windows", "Windows"),
    ("Macintosh", "macOS"),
    ("Mac OS X", "macOS"),
    ("Linux", "Linux"),
)


def browser_family(user_agent: str | None) -> str:
    text = (user_agent or "")[:UA_MAX]
    for marker, family in _BROWSER_MARKERS:
        if marker in text:
            return family
    if "Safari/" in text and "Version/" in text:
        return "Safari"
    return "other"


def os_family(user_agent: str | None) -> str:
    text = (user_agent or "")[:UA_MAX]
    for marker, family in _SYSTEM_MARKERS:
        if marker in text:
            return family
    return "other"


def describe(user_agent: str | None) -> tuple[str, str]:
    """``(browser, os)``: each one of ``BROWSERS`` and ``SYSTEMS``."""
    return browser_family(user_agent), os_family(user_agent)
