"""Body rule B6, segmental-muscle symmetry (ADR 0010 §3; PLAN §7.2). Pure.

The symmetry index and rule B6, split out of ``ownvitals.analysis.rules.body`` (which re-exports
every name here); same contract: a ``Finding`` proposal, a ``NoFinding`` or ``Insufficient`` for
one evaluation ``day``.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from statistics import median
from typing import Literal

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.policy import InsufficientData, Purpose, evaluate_metric
from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules.core import (
    NoFinding,
    RuleResult,
    confidence_data,
    insufficient_from,
    make_finding,
)

B6_SI = Fraction(1, 10)  # "more than 0.10"
B6_DAYS = 90

# Segmental muscle (175) positions (findings §2): (left, right)
ARMS = (3, 2)
LEGS = (10, 11)
type PairName = Literal["arms", "legs"]
PAIRS: dict[PairName, tuple[int, int]] = {"arms": ARMS, "legs": LEGS}


@dataclass(frozen=True, slots=True)
class Segment:
    """One segmental muscle (175) reading at a body ``position``; duplicates of a measure group are
    keyed on ``(grpid, position)``."""

    position: int
    reading: Reading


def symmetry_index(left: Decimal, right: Decimal) -> Fraction | None:
    """``|L - R| / ((L + R) / 2)``, exactly; ``None`` when ``L + R`` is zero."""
    total = Fraction(left) + Fraction(right)
    if total == 0:
        return None
    return abs(Fraction(left) - Fraction(right)) / (total / 2)


def pair_indices(segments: Sequence[Segment], pair: PairName) -> dict[int, tuple[date, Fraction]]:
    """``grpid -> (day, SI)`` of the groups holding both positions of ``pair``."""
    left, right = PAIRS[pair]
    keyed: dict[tuple[int, int], Reading] = {}
    for segment in sorted(segments, key=lambda s: s.reading.sort_key):
        keyed.setdefault((segment.reading.grpid, segment.position), segment.reading)
    found: dict[int, tuple[date, Fraction]] = {}
    for (grpid, position), reading in keyed.items():
        other = keyed.get((grpid, right)) if position == left else None
        if other is None:
            continue
        index = symmetry_index(reading.value, other.value)
        if index is not None:
            found[grpid] = (reading.local_date, index)
    return found


def b6(segments: Sequence[Segment], day: date, *, pair: PairName) -> RuleResult:
    """B6: the median symmetry index of the groups holding both of ``pair`` ("arms" or "legs") in
    the 90 days ending at ``day`` is more than 0.10. The days with a pair must pass the BASELINE
    policy. ``day`` must hold a pair. Subject: ``pair``."""
    found = pair_indices(segments, pair)
    window_start = day - timedelta(days=B6_DAYS - 1)
    inside = {g: (d, si) for g, (d, si) in found.items() if window_start <= d <= day}
    if not any(d == day for d, _ in inside.values()):
        raise ValueError(f"no {pair} pair on {day}: evaluate only days with data")
    by_day: dict[date, list[Fraction]] = {}
    for d, si in inside.values():
        by_day.setdefault(d, []).append(si)
    stand_ins = [_stand_in(d, values) for d, values in sorted(by_day.items())]
    verdict = evaluate_metric(
        f"segmental_muscle_mass:{pair}",
        Purpose.BASELINE,
        stand_ins,
        window_end=day,
        window_days=B6_DAYS,
    )
    if isinstance(verdict, InsufficientData):
        return insufficient_from("B6", pair, day, verdict, f"{pair} baseline policy")
    ratio = median(si for _, si in inside.values())
    if not ratio > B6_SI:
        return NoFinding("B6", pair, day, "symmetry index not above 0.10")
    return make_finding(
        "B6",
        pair,
        day,
        window_start=window_start,
        window_end=day,
        reference_day=day,
        confidence=confidence_data(verdict),
        evidence={
            "median_symmetry_index": float(ratio),
            "threshold": float(B6_SI),
            "groups": len(inside),
        },
        data_quality=verdict.data_quality,
        trigger_groups=(g for g, (d, _) in inside.items() if d == day),
    )


def _stand_in(day: date, indices: list[Fraction]) -> DailyValue:
    """A daily value that only carries the day and the number of groups, for the policy check."""
    values = [Decimal(float(si)) for si in indices]
    return DailyValue(
        day=day,
        value=Decimal(float(median(indices))),
        method=DailyMethod.MEDIAN,
        n=len(indices),
        minimum=min(values),
        maximum=max(values),
        mad=Decimal(0),
    )
