"""Nerve-health rules N1, N2, N3 and N5 (ADR 0010 §3 and §7; PLAN §7.2). Pure.

Same contract as ``body`` and ``heart``: a ``Finding`` proposal, a ``NoFinding`` or ``Insufficient``
for one evaluation ``day``, which must hold the rule's trigger (else ``ValueError``: the caller
schedules evaluations, §1).

* **N1** is not a rule and never a finding: ``n1`` is the nerve TREND policy (``NERVE_TREND``: at
  least 4 days with data spanning at least 28 days) that gates N2, N3 and N5. Below it the metric
  is "insufficient data".
* **N2** has two parts with their own rule ids, so a guided and a routine finding of one nerve key
  are separate episodes: ``N2:guided`` and ``N2:routine``. Both read the finding threshold (k 2.5,
  the 90 days before the reference day, ``NERVE_BASELINE``) over the daily values of all usable
  readings, guided and routine alike. They open on the first firing and end on the first
  non-firing evaluation (the run is the persistence).
* **N3** is a trend-test rule (``advance_persistent``): ``DECREASING`` at alpha 0.025 per window on
  the 90 or the 180 days ending at ``day`` (Bonferroni over the two), each window passing N1.
* **N5**: the symmetric index of the two foot pairs, per measure group.

167, 196 and 229 are never mixed: every rule takes one metric's readings. A guided reading is
``attrib`` 15. N4 is not in v1.
"""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
from statistics import median
from typing import Any, Literal

from ownvitals.analysis.daily import DailyMethod, DailyValue, daily_values
from ownvitals.analysis.methods.trend import Direction
from ownvitals.analysis.policy import (
    NERVE_TREND,
    InsufficientData,
    Purpose,
    Sufficient,
    evaluate,
    evaluate_metric,
)
from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules.body import symmetry_index
from ownvitals.analysis.rules.core import (
    Insufficient,
    NoFinding,
    ProvenanceMap,
    RuleResult,
    confidence_data,
    confidence_run,
    confidence_trend,
    finding_band,
    groups_on,
    insufficient_from,
    lowest,
    make_finding,
    require_value,
)
from ownvitals.analysis.rules.trendrule import trend_of

NERVE_KEYS = ("nerve_health_score", "nerve_response_score", "esc")
GUIDED_ATTRIB = 15  # Withings' attrib for a guided measurement
N1_DAYS = 90  # N2, N5 and N3's short window read the 90 days ending at the evaluation day
N2_MIN_RUN = 2  # routine: the last 2 consecutive days with data (measure days, not calendar days)
N3_WINDOWS = (180, 90)  # the longer one is preferred when both fire
N3_ALPHA = 0.025  # per window: Bonferroni over the two (Dunn 1961)
N5_SI = Fraction(1, 5)  # "more than 0.20"
N5_DAYS = 90

type FootPair = Literal["158_159", "197_198"]


def _check_key(metric_key: str) -> None:
    if metric_key not in NERVE_KEYS:
        raise ValueError(f"{metric_key} is not a nerve metric: use one of {NERVE_KEYS}")


def n1(
    metric_key: str, daily: Sequence[DailyValue], day: date, *, window_days: int = N1_DAYS
) -> Sufficient | InsufficientData:
    """N1, the nerve TREND policy on the ``window_days`` days ending at ``day``. Not a finding."""
    _check_key(metric_key)
    return evaluate_metric(
        metric_key, Purpose.TREND, daily, window_end=day, window_days=window_days
    )


def n2_guided(readings: Sequence[Reading], day: date, *, metric_key: str) -> RuleResult:
    """N2, guided: a guided reading (``attrib`` 15) on ``day`` that is itself below the finding
    threshold at ``day``, even when a routine reading the same morning is the day's value. Equal
    to the threshold does not fire. ``readings`` are the metric's usable readings, guided and
    routine. Window and reference day: ``day``. ``ValueError`` without a guided reading on
    ``day``."""
    _check_key(metric_key)
    guided = sorted(
        (r for r in readings if r.attrib == GUIDED_ATTRIB and r.local_date == day),
        key=lambda r: r.sort_key,
    )
    if not guided:
        raise ValueError(f"no guided {metric_key} reading on {day}: evaluate only days with data")
    daily = daily_values(readings)
    gate = n1(metric_key, daily, day)
    if isinstance(gate, InsufficientData):
        return insufficient_from("N2:guided", metric_key, day, gate, "nerve trend policy (N1)")
    band = finding_band(metric_key, daily, day)
    if isinstance(band, InsufficientData):
        return insufficient_from("N2:guided", metric_key, day, band, "nerve baseline policy")
    below = next((r for r in guided if float(r.value) < band.low), None)
    if below is None:
        return NoFinding("N2:guided", metric_key, day, "no guided reading below the threshold")
    return make_finding(
        "N2:guided",
        metric_key,
        day,
        window_start=day,
        window_end=day,
        reference_day=day,
        confidence=confidence_data(gate, band.verdict),
        evidence={
            "value": float(below.value),
            "threshold": band.low,
            "finding_band": band.as_dict(),
        },
        data_quality=band.verdict.data_quality,
        trigger_groups=(below.grpid,),
    )


def n2_routine(
    readings: Sequence[Reading],
    day: date,
    *,
    metric_key: str,
    provenance: ProvenanceMap | None = None,
) -> RuleResult:
    """N2, routine: the daily values of the routine readings (``attrib`` other than 15) on the last
    2 or more consecutive days with data (sparse data: measure days, not calendar days) ending at
    ``day`` are all below the finding threshold at the first of them. The longest such run wins,
    so a growing run keeps its first day and band. The band and N1 read the daily values of all
    usable readings. Window: the run; reference day: its first day. ``ValueError`` without a
    routine reading on ``day``."""
    _check_key(metric_key)
    routine = daily_values(r for r in readings if r.attrib != GUIDED_ATTRIB)
    require_value(routine, day, metric_key)
    chain = [v for v in routine if v.day <= day]
    if len(chain) < N2_MIN_RUN:
        return NoFinding("N2:routine", metric_key, day, "fewer than 2 measure days")
    daily = daily_values(readings)
    gate = n1(metric_key, daily, day)
    if isinstance(gate, InsufficientData):
        return insufficient_from("N2:routine", metric_key, day, gate, "nerve trend policy (N1)")
    shortest = len(chain) - N2_MIN_RUN
    for index in range(shortest + 1):
        run = chain[index:]
        band = finding_band(metric_key, daily, run[0].day)
        if isinstance(band, InsufficientData):
            if index == shortest:
                return insufficient_from(
                    "N2:routine", metric_key, day, band, "nerve baseline policy"
                )
            continue
        if all(float(v.value) < band.low for v in run):
            return make_finding(
                "N2:routine",
                metric_key,
                day,
                window_start=run[0].day,
                window_end=day,
                reference_day=run[0].day,
                confidence=lowest(
                    confidence_data(gate, band.verdict), confidence_run(len(run), N2_MIN_RUN)
                ),
                evidence={
                    "run_measure_days": len(run),
                    "values": [float(v.value) for v in run],
                    "threshold": band.low,
                    "finding_band": band.as_dict(),
                },
                data_quality=band.verdict.data_quality,
                trigger_groups=groups_on(provenance, metric_key, (v.day for v in run)),
            )
    return NoFinding("N2:routine", metric_key, day, "no run below the threshold")


def n3(
    daily: Sequence[DailyValue],
    day: date,
    *,
    metric_key: str,
    provenance: ProvenanceMap | None = None,
) -> RuleResult:
    """N3: a significant decline over the 90 or the 180 days ending at ``day``: ``trend`` (AR(1))
    gives ``DECREASING`` at alpha 0.025 on a window that passes N1. Window: the 180 days if that
    window fired, else the 90. A window below N1 is not tested; when neither is testable the result
    is insufficient data. ``daily`` are the daily values of all usable readings of the metric."""
    _check_key(metric_key)
    require_value(daily, day, metric_key)
    tested: list[dict[str, Any]] = []
    fired: tuple[int, Sufficient, Any] | None = None
    first_gap: InsufficientData | None = None
    for days in N3_WINDOWS:
        gate = n1(metric_key, daily, day, window_days=days)
        if isinstance(gate, InsufficientData):
            first_gap = gate
            tested.append({"window_days": days, "insufficient": True})
            continue
        result = trend_of(daily, day, days, alpha=N3_ALPHA)
        tested.append(
            {
                "window_days": days,
                "direction": result.direction.value,
                "p_value": result.p_value,
                "n": result.inputs.n,
            }
        )
        if fired is None and result.direction is Direction.DECREASING:
            fired = (days, gate, result)
    if fired is None:
        if first_gap is not None and all(t.get("insufficient") for t in tested):
            return insufficient_from("N3", metric_key, day, first_gap, "nerve trend policy (N1)")
        return NoFinding("N3", metric_key, day, "no significant decline")
    days, gate, result = fired
    return make_finding(
        "N3",
        metric_key,
        day,
        window_start=day - timedelta(days=days - 1),
        window_end=day,
        reference_day=day,
        confidence=lowest(confidence_data(gate), confidence_trend(result)),
        evidence={
            "window_days": days,
            "alpha": N3_ALPHA,
            "trend": {
                "direction": result.direction.value,
                "p_value": result.p_value,
                "slope_per_day": result.slope,
                "intercept": result.intercept,
                "n": result.inputs.n,
                "ar1_phi": result.ar1_phi,
            },
            "windows_tested": tested,
        },
        data_quality=gate.data_quality,
        trigger_groups=groups_on(provenance, metric_key, [day]),
    )


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
