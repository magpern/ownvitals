"""The Health Improvement Score band table (ADR 0012 §2): parse and validate. Pure.

A table is a JSON document the owner approves; only a table whose SHA-256 a reviewed PR registers
in ``REGISTERED`` loads (``withings_card_v1``, owner decision 2026-11-12, provisional). Reading
the file is the wiring's job (``derived.scores_tables``); this module gets the bytes.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

CRITERIA: Final = (
    "bmi",
    "fat_ratio",
    "night_hr",
    "vascular_age_gap",
    "afib_ecg_count",
    "high_bp_ratio",
    "daily_steps",
    "weekly_active_minutes",
    "sleep_ahi",
)
VERTICALS: Final = ("body", "heart", "activity", "sleep")  # nutrition: no public-API input
SEXES: Final = ("any", "female", "male")
# table_id -> SHA-256 of its file (ADR 0012 owner question 1: the Withings card values).
REGISTERED: Final[Mapping[str, str]] = {
    "withings_card_v1": "9f20d86e398e9b9357cf8fd7b7acbcd7b2fa92c6459ad694a24369df441212cb",
}


class InvalidBandTable(ValueError):
    """The reason is a short code, never the file's content."""


@dataclass(frozen=True, slots=True)
class Side:
    perfect: float
    good: float
    average: float
    bad: float


@dataclass(frozen=True, slots=True)
class BandRow:
    criterion: str
    vertical: str
    sex: str
    age_min: int
    age_max: int
    unit: str
    low: Side | None
    high: Side | None
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class BandTable:
    table_id: str
    version: str
    source: str
    rows: tuple[BandRow, ...]


def _side(raw: Any) -> Side | None:
    if raw is None:
        return None
    try:
        return Side(*(float(raw[k]) for k in ("perfect", "good", "average", "bad")))
    except (KeyError, TypeError, ValueError):
        raise InvalidBandTable("bad_side") from None


def _row(raw: Any) -> BandRow:
    try:
        row = BandRow(
            criterion=str(raw["criterion"]),
            vertical=str(raw["vertical"]),
            sex=str(raw["sex"]),
            age_min=int(raw["age_min"]),
            age_max=int(raw["age_max"]),
            unit=str(raw["unit"]),
            low=_side(raw.get("low")),
            high=_side(raw.get("high")),
            weight=float(raw.get("weight", 1)),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise InvalidBandTable("bad_row") from None
    low, high = row.low, row.high
    if row.criterion not in CRITERIA or row.vertical not in VERTICALS or row.sex not in SEXES:
        raise InvalidBandTable("unknown_criterion")
    if (low is None and high is None) or row.age_min > row.age_max or not row.weight > 0:
        raise InvalidBandTable("bad_row")
    if low and not low.bad <= low.average <= low.good <= low.perfect:
        raise InvalidBandTable("low_not_monotone")
    if high and not high.perfect <= high.good <= high.average <= high.bad:
        raise InvalidBandTable("high_not_monotone")
    if low and high and low.perfect > high.perfect:
        raise InvalidBandTable("perfect_range_inverted")
    return row


def _overlap(a: BandRow, b: BandRow) -> bool:
    same_sex = a.sex == b.sex or "any" in (a.sex, b.sex)
    return same_sex and a.age_min <= b.age_max and b.age_min <= a.age_max


def parse_table(raw: bytes, *, registered: Mapping[str, str] = REGISTERED) -> BandTable:
    """The validated table, or ``InvalidBandTable``. The SHA-256 of ``raw`` must equal the
    registered one for its ``table_id`` (a table changes only through a reviewed PR)."""
    try:
        doc = json.loads(raw)
        table = BandTable(
            table_id=str(doc["table_id"]),
            version=str(doc["version"]),
            source=str(doc["source"]),
            rows=tuple(_row(r) for r in doc["rows"]),
        )
    except InvalidBandTable:
        raise
    except (KeyError, TypeError, ValueError):
        raise InvalidBandTable("bad_document") from None
    if registered.get(table.table_id) != hashlib.sha256(raw).hexdigest():
        raise InvalidBandTable("not_registered")
    for i, a in enumerate(table.rows):
        if any(a.criterion == b.criterion and _overlap(a, b) for b in table.rows[i + 1 :]):
            raise InvalidBandTable("overlapping_rows")
    return table
