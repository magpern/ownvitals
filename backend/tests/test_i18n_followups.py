"""Machine-checkable i18n follow-ups of the G3 to G7 reviews (#283, ADR 0015).

Synthetic: reads source text only. No database, no network.
"""

import json
import re
from pathlib import Path

import pytest

from ownvitals.response_codes import CONNECTIONS
from ownvitals.withings.oauth import (
    _REASONS as CONNECT_REASONS,  # pyright: ignore[reportPrivateUsage]
)

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend/src/ownvitals"
I18N = ROOT / "frontend/src/i18n"


def test_every_connect_refusal_is_a_listed_connections_code_with_a_text() -> None:
    assert CONNECT_REASONS <= CONNECTIONS
    codes = json.loads((ROOT / "frontend/src/api/codes.json").read_text())
    assert set(codes) >= CONNECT_REASONS
    for locale in ("en", "sv"):
        words = (I18N / locale / "codes.ts").read_text(encoding="utf-8")
        words += (I18N / locale / "errors.ts").read_text(encoding="utf-8")
        for reason in CONNECT_REASONS:
            assert f"'code.{reason}'" in words or f"'reason.{reason}'" in words, (locale, reason)


def _oauth_outcome_codes() -> set[str]:
    found: set[str] = set()
    for path in (SRC / "auth").glob("*.py"):
        found |= set(re.findall(r'(?:_fail|Outcome)\("([a-z_]+)"', path.read_text("utf-8")))
    return found - {"ok"}


@pytest.mark.parametrize("locale", ["en", "sv"])
def test_every_oauth_outcome_word_has_a_text_in_each_language(locale: str) -> None:
    words = (I18N / locale / "errors.ts").read_text(encoding="utf-8")
    outcomes = _oauth_outcome_codes()
    assert {"denied", "invalid", "identity_taken"} <= outcomes  # the scan finds what it should
    for code in outcomes:
        assert f"'oauth.{code}'" in words, (locale, code)


def test_no_response_falls_back_to_an_english_framework_sentence() -> None:
    """The 404 of the SPA fallback and the 500 handler answer with a code (ADR 0015 s6)."""
    for rel in ("web.py", "auth/setup.py"):
        text = (SRC / rel).read_text(encoding="utf-8")
        assert "Internal Server Error" not in text, rel
        assert "HTTPException(status_code=404)" not in text, rel
