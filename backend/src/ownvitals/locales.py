"""The languages ownvitals supports (ADR 0015 section 1): ``SUPPORTED_LOCALES``.

The list is the checked-in manifest ``frontend/src/i18n/locales.json``, the same file the
frontend pins its catalogues to. The image carries a copy at ``/app/locales.json`` (the
``Dockerfile``); in a checkout the repository copy is read. The decisions on a locale (which are
offered, the request's language, what a new account stores) are pure and live in
``locale_policy``; this module only reads the file, once, at import.
"""

import json
import re
from pathlib import Path

_CODE = re.compile(r"[a-z]{2}")


def _manifest_path() -> Path:
    here = Path(__file__).resolve()
    candidates = (
        here.parents[3] / "frontend" / "src" / "i18n" / "locales.json",  # the repository
        here.parents[2] / "locales.json",  # the image (/app/locales.json)
    )
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError("the locale manifest locales.json was not found")


def parse_manifest(raw: str) -> tuple[str, ...]:
    """The codes of a manifest: a JSON list of two-letter lower-case codes, ``en`` among them."""
    codes = json.loads(raw)
    if not isinstance(codes, list) or not all(
        isinstance(code, str) and _CODE.fullmatch(code) for code in codes
    ):
        raise RuntimeError("locales.json must be a list of two-letter codes")
    if "en" not in codes:
        raise RuntimeError("locales.json must list en, the source language")
    return tuple(dict.fromkeys(codes))


SUPPORTED_LOCALES: tuple[str, ...] = parse_manifest(_manifest_path().read_text(encoding="utf-8"))
