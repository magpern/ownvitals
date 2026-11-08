"""Nerve-health rule N5 (ADR 0010 §3 and §7; PLAN §7.2). Pure.

The symmetric index of the two foot pairs, per measure group; same contract as the rules of
``ownvitals.analysis.rules.nerve`` (which re-exports every name here): a ``Finding`` proposal, a
``NoFinding`` or ``Insufficient`` for one evaluation ``day``. The nerve TREND policy (N1) gates it.
"""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from statistics import median
from typing import Literal

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.policy import NERVE_TREND, InsufficientData, evaluate
from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules.body import symmetry_index
from ownvitals.analysis.rules.core import (
    Insufficient,
    NoFinding,
    RuleResult,
    confidence_data,
    insufficient_from,
    make_finding,
)

N5_SI = Fraction(1, 5)  # "more than 0.20"
N5_DAYS = 90

type FootPair = Literal["158_159", "197_198"]


def _foot_indices(
    first: Sequence[Reading], second: Sequence[Reading]
) -> dict[int, tuple[date, Fraction]]:
    """``grpid -> (day, SI)`` of the groups holding both members of the pair. A duplicate of one
    group and type is keyed on ``grpid``: the first in canonical order wins."""
    members: list[dict[int, Reading]] = []
    for readings in (first, second):
        keyed: dict[int, Reading] = {}
        for r in sorted(readings, key=lambda r: r.sort_key):
            keyed.setdefault(r.grpid, r)
        members.append(keyed)
    found: dict[int, tuple[date, Fraction]] = {}
    for grpid, a in members[0].items():
        b = members[1].get(grpid)
        if b is None:
            continue
        index = symmetry_index(a.value, b.value)
        if index is not None:
            found[grpid] = (a.local_date, index)
    return found


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


def n5(
    first: Sequence[Reading], second: Sequence[Reading], day: date, *, pair: FootPair
) -> RuleResult:
    """N5: the median symmetric index ``|L - R| / ((L + R) / 2)`` of the measure groups holding both
    members of the foot pair in the 90 days ending at ``day`` is more than 0.20, the days with a
    pair passing N1. ``first`` and ``second`` are the usable readings of 158 and 159, or of 197 and
    198; which member is left does not matter. No group with both members at all: not supported
    (``Insufficient``), never "no difference". Subject ``nerve_foot_<pair>``; ``ValueError`` when
    ``day`` has no pair although others do."""
    subject = f"nerve_foot_{pair}"
    found = _foot_indices(first, second)
    if not found:
        return Insufficient("N5", subject, day, "not supported: no group holds both of the pair")
    window_start = day - timedelta(days=N5_DAYS - 1)
    inside = {g: (d, si) for g, (d, si) in found.items() if window_start <= d <= day}
    if not any(d == day for d, _ in inside.values()):
        raise ValueError(f"no {pair} pair on {day}: evaluate only days with data")
    by_day: dict[date, list[Fraction]] = {}
    for d, si in inside.values():
        by_day.setdefault(d, []).append(si)
    stand_ins = [_stand_in(d, values) for d, values in sorted(by_day.items())]
    gate = evaluate(stand_ins, requirement=NERVE_TREND, window_end=day, window_days=N5_DAYS)
    if isinstance(gate, InsufficientData):
        return insufficient_from("N5", subject, day, gate, "nerve trend policy (N1)")
    ratio = median(si for _, si in inside.values())
    if not ratio > N5_SI:
        return NoFinding("N5", subject, day, "symmetry index not above 0.20")
    return make_finding(
        "N5",
        subject,
        day,
        window_start=window_start,
        window_end=day,
        reference_day=day,
        confidence=confidence_data(gate),
        evidence={
            "median_symmetry_index": float(ratio),
            "threshold": float(N5_SI),
            "groups": len(inside),
        },
        data_quality=gate.data_quality,
        trigger_groups=(g for g, (d, _) in inside.items() if d == day),
    )
