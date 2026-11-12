"""Metric keys: which ``observations.metric_key`` a Withings measure type becomes (PLAN §7.1
step 1, ADR 0010 §3 and §7).

The keys of the quality table (``analysis.quality.METRICS``) are the ones the analysis uses, so
that table is the source for them; this module adds the types the analysis does not judge yet
(``EXTRA``) and the segmental types 173 to 175, whose key carries the body position
(``segmental_muscle_mass:left_arm``). A measure type with no entry is **kept** under
``meastype_<n>`` (withings-findings §2: "store unknown meastypes too"), never dropped.

Every key matches ``derived.tables.METRIC_KEY_PATTERN``; ``metric_key`` checks that.
``unit`` is a token for ``observations.conversion`` (its check allows ``[a-z0-9_.:/-]``), the
physical unit of the value ``value_raw x 10^unit`` or ``unknown`` where the findings document
none. No value is converted to another physical unit: Withings' own units are the ones the
quality table uses.
"""

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from ownvitals.analysis.quality import METRICS
from ownvitals.derived.tables import METRIC_KEY_PATTERN

UNKNOWN_UNIT: Final = "unknown"
_UNIT_TOKENS: Final = {"%": "pct", "mmHg": "mmhg", "µS": "us"}
_PATTERN: Final = re.compile(METRIC_KEY_PATTERN)


@dataclass(frozen=True, slots=True)
class MetricKey:
    key: str
    unit: str
    analysed: bool  # has a row in ``analysis.quality.METRICS`` (quality gates apply)


def _token(unit: str) -> str:
    return _UNIT_TOKENS.get(unit, unit)


# Types the quality table has no row for (height has one since #492). Temperatures, VO2 max, BMR
# and the AFib enums are ``unknown`` until a source says otherwise (withings-findings §2).
EXTRA: Final = MappingProxyType(
    {
        12: ("temperature", UNKNOWN_UNIT),
        71: ("body_temperature", UNKNOWN_UNIT),
        73: ("skin_temperature", UNKNOWN_UNIT),
        123: ("vo2_max", UNKNOWN_UNIT),
        130: ("afib_ecg_result", UNKNOWN_UNIT),
        139: ("afib_ppg_result", UNKNOWN_UNIT),
        226: ("basal_metabolic_rate", UNKNOWN_UNIT),
    }
)

# Segmental fat-free (173), fat (174) and muscle (175) mass, in kg, one value per body position.
SEGMENTAL: Final = MappingProxyType(
    {
        173: "segmental_fat_free_mass",
        174: "segmental_fat_mass",
        175: "segmental_muscle_mass",
    }
)
SEGMENTAL_UNIT: Final = "kg"

# withings-findings §2, "Values and enums": 2 right arm, 3 left arm, 4 right foot, 5 left foot,
# 8 left body, 9 right body, 10 left leg, 11 right leg, 12 torso. 13 and 14 are "hands" with no
# side given, so they stay ``position_13`` and ``position_14``.
POSITIONS: Final = MappingProxyType(
    {
        2: "right_arm",
        3: "left_arm",
        4: "right_foot",
        5: "left_foot",
        8: "left_body",
        9: "right_body",
        10: "left_leg",
        11: "right_leg",
        12: "torso",
    }
)
UNPOSITIONED: Final = "unpositioned"

_ANALYSED: Final = MappingProxyType(
    {spec.meastype: MetricKey(spec.key, _token(spec.unit), True) for spec in METRICS.values()}
)
_EXTRA: Final = MappingProxyType(
    {meastype: MetricKey(key, unit, False) for meastype, (key, unit) in EXTRA.items()}
)


def position_name(position: int | None) -> str:
    if position is None:
        return UNPOSITIONED
    named = POSITIONS.get(position)
    if named is not None:
        return named
    return f"position_{position}" if 0 <= position <= 9999 else "position_other"


def metric_key(meastype: int, position: int | None = None) -> MetricKey:
    """The key (and unit token) of a measure type, with its position for the segmental types."""
    found = _ANALYSED.get(meastype) or _EXTRA.get(meastype)
    if found is None:
        segment = SEGMENTAL.get(meastype)
        if segment is not None:
            found = MetricKey(f"{segment}:{position_name(position)}", SEGMENTAL_UNIT, False)
        elif meastype >= 0:
            found = MetricKey(f"meastype_{meastype}", UNKNOWN_UNIT, False)
        else:
            found = MetricKey(f"meastype_neg{-meastype}", UNKNOWN_UNIT, False)
    if _PATTERN.fullmatch(found.key) is None:
        raise ValueError("not a metric key")
    return found


def conversion(unit: int, token: str) -> str:
    """``observations.conversion``: ``e<unit>:<physical unit>``, for example ``e-3:kg``: the value
    is ``value_raw x 10^unit`` in that physical unit, with no other conversion applied."""
    return f"e{unit}:{token}"
