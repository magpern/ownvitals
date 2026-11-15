"""One reason code per HIS area that has no score (ADR 0012 §1 data states; G4: codes only). Pure.

A criterion's own reason (``his._one``) is mapped to the area-level vocabulary the UI words:
``birth_year_unset``, ``sex_unset``, ``no_height``, ``implausible_height``, ``insufficient_data``,
``no_band_row``, ``not_supported``, ``not_available``. When several criteria of an area lack a
score, the one the user can act on first wins (the order of ``PRIORITY``).
"""

from collections.abc import Mapping
from typing import Any, Final

PRIORITY: Final = (
    "birth_year_unset",
    "sex_unset",
    "no_height",
    "implausible_height",
    "insufficient_data",
    "no_band_row",
    "not_supported",
    "not_available",
)
_MAP: Final = {
    "ecg_not_supported": "not_supported",
    "no_data": "not_available",
    "no_input": "not_available",
    "input_not_wired": "not_available",
}


def area_reason(criterion_reasons: list[str | None]) -> str:
    """The one code for an area from its criteria's reasons (``None`` = no reason given)."""
    codes = {_MAP.get(r, r) for r in criterion_reasons if r}
    for code in PRIORITY:
        if code in codes:
            return code
    return "not_available"


def vertical_reasons(
    verticals_of: Mapping[str, str],
    criteria: Mapping[str, Any],
    scores: Mapping[str, float | None],
) -> dict[str, str | None]:
    """Area -> reason code, ``None`` where the area has a score. ``verticals_of`` is
    criterion -> area, ``criteria`` the per-criterion outcomes of ``his._one``."""
    out: dict[str, str | None] = {}
    for area, score in scores.items():
        if score is not None:
            out[area] = None
            continue
        reasons = [criteria[c].get("reason") for c, v in verticals_of.items() if v == area]
        out[area] = area_reason(reasons)
    return out
