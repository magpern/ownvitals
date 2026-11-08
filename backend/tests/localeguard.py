"""The half-added-language check (ADR 0015 section 9, G7): which pieces of a language are missing.

``missing_pieces(root)`` reads a checkout laid out like the repository (``frontend/src/i18n``,
``backend/src/ownvitals``, ``backend/migrations``, ``backend/tests/mail_golden``) and returns, for
each code of its ``locales.json``, the pieces the code still lacks. The pieces are ``PIECES``.
Text and file-name checks only; no import of the code under test, so a temporary copy works.
"""

import re
from pathlib import Path

from ownvitals.locales import parse_manifest

PIECES = ("catalogue", "mail", "checks", "picker", "golden")

I18N = Path("frontend/src/i18n")
SRC = Path("backend/src/ownvitals")
GOLDEN = Path("backend/tests/mail_golden")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _record_has(source: str, name: str, code: str) -> bool:
    block = re.search(rf"{name}\b[^=]*=\s*\{{([^}}]*)\}}", source)
    return block is not None and re.search(rf"\b{code}\s*:", block.group(1)) is not None


def _catalogue(root: Path, code: str) -> bool:
    loaders = re.search(rf"\b{code}\s*:\s*\(\)\s*=>", _text(root / I18N / "store.ts"))
    return (root / I18N / code / "index.ts").is_file() and loaders is not None


def _mail(root: Path, code: str) -> bool:
    tables = (SRC / "notify/templates", SRC / "auth/mail_texts")
    return all(
        (root / t / f"{code}.py").is_file() and f'"{code}":' in _text(root / t / "__init__.py")
        for t in tables
    )


def _checks(root: Path, code: str) -> bool:
    # The three ``locale`` columns share ``LOCALE_CHECK`` (``users``, the pending sign-up and the
    # OAuth flow); the outbox row has no language column (the language is looked up at send time).
    check = re.search(r'LOCALE_CHECK\s*=\s*"([^"]*)"', _text(root / SRC / "auth/tables.py"))
    widened = any(
        "locale" in text and (f'"{code}"' in text or f"'{code}'" in text)
        for text in (_text(p) for p in (root / "backend/migrations/versions").glob("*.py"))
    )
    return check is not None and f"'{code}'" in check.group(1) and widened


def _picker(root: Path, code: str) -> bool:
    source = _text(root / I18N / "locale.ts")
    return _record_has(source, "LOCALE_NAMES", code) and _record_has(source, "INTL_TAG", code)


def _golden(root: Path, code: str) -> bool:
    for family in ("outbox", "auth"):
        want = {p.name for p in (root / GOLDEN / family / "en").glob("*.txt")}
        have = {p.name for p in (root / GOLDEN / family / code).glob("*.txt")}
        if not want or have != want:
            return False
    return True


_CHECKS = {
    "catalogue": _catalogue,
    "mail": _mail,
    "checks": _checks,
    "picker": _picker,
    "golden": _golden,
}


def missing_pieces(root: Path) -> dict[str, tuple[str, ...]]:
    """code -> the pieces it lacks, for every code of ``root``'s manifest (empty when whole)."""
    codes = parse_manifest(_text(root / I18N / "locales.json"))
    return {
        code: tuple(piece for piece in PIECES if not _CHECKS[piece](root, code)) for code in codes
    }
