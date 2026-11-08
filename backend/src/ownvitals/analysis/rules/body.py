"""Body composition rules B1 to B6 (ADR 0010 §3 and §7; PLAN §7.2). Pure.

Each function evaluates one rule on one evaluation ``day`` and returns a ``Finding`` proposal, a
``NoFinding`` or ``Insufficient``. B2, B4 and B5 are trend-test rules: their ``Finding`` is the
evaluation's condition, and an episode opens only through ``core.advance_persistent``. B1 and B3
are experimental: they carry the tier, and who may see them is not decided here.
"""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal, localcontext
from fractions import Fraction
from statistics import median
from typing import Literal

from ownvitals.analysis.daily import DailyValue, is_morning
from ownvitals.analysis.methods.trend import Direction
from ownvitals.analysis.policy import InsufficientData
from ownvitals.analysis.quality import EXACT, Reading
from ownvitals.analysis.rules.body_symmetry import (
    ARMS as ARMS,
)
from ownvitals.analysis.rules.body_symmetry import (
    B6_DAYS as B6_DAYS,
)
from ownvitals.analysis.rules.body_symmetry import (
    B6_SI as B6_SI,
)
from ownvitals.analysis.rules.body_symmetry import (
    LEGS as LEGS,
)
from ownvitals.analysis.rules.body_symmetry import (
    PAIRS as PAIRS,
)
from ownvitals.analysis.rules.body_symmetry import (
    PairName as PairName,
)
from ownvitals.analysis.rules.body_symmetry import (
    Segment as Segment,
)
from ownvitals.analysis.rules.body_symmetry import (
    b6 as b6,
)
from ownvitals.analysis.rules.body_symmetry import (
    pair_indices as pair_indices,
)
from ownvitals.analysis.rules.body_symmetry import (
    symmetry_index as symmetry_index,
)
from ownvitals.analysis.rules.core import (
    Confidence,
    Insufficient,
    NoFinding,
    ProvenanceMap,
    RuleResult,
    confidence_data,
    confidence_jump,
    finding_band,
    groups_on,
    insufficient_from,
    lowest,
    make_finding,
    provenance_of,
    require_value,
    value_on,
)
from ownvitals.analysis.rules.trendrule import trend_rule

B1_GAIN_KG = Decimal("2.0")  # "more than 2 kg in 3 days" (ESC 2021, Table 12)
B1_DAYS = 3
B2_DAYS = 182
B2_RATIO = -0.05
B4_DAYS = 90
B4_RATIO = -0.02
B3_RATIO = Fraction(2, 5)  # "more than 0.40" (InBody; device-specific, not a validated cut)


def b1(
    weight: Sequence[DailyValue],
    ecw: Sequence[DailyValue],
    day: date,
    *,
    provenance: ProvenanceMap | None = None,
) -> RuleResult:
    """B1 (experimental): weight up by more than 2.0 kg since a day in ``day - 3 … day - 1`` (the
    largest gain, the earliest such day on a tie) and the ECW daily value on ``day`` above the ECW
    finding threshold at that day. Window and reference day: that day. The 7-day arm is not in
    v1."""
    today = require_value(weight, day, "weight")
    best: tuple[Decimal, date] | None = None
    with localcontext(EXACT):
        for back in range(B1_DAYS, 0, -1):  # earliest first: a tie keeps it
            start = day - timedelta(days=back)
            earlier = value_on(weight, start)
            if earlier is None:
                continue
            gain = today.value - earlier.value
            if gain > B1_GAIN_KG and (best is None or gain > best[0]):
                best = (gain, start)
    if best is None:
        return NoFinding("B1", "weight", day, "no gain of more than 2 kg in 3 days")
    gain, start = best
    water = value_on(ecw, day)
    if water is None:
        return Insufficient("B1", "weight", day, "no extracellular water daily value on the day")
    band = finding_band("extracellular_water", ecw, start)
    if isinstance(band, InsufficientData):
        return insufficient_from("B1", "weight", day, band, "extracellular_water baseline policy")
    if not float(water.value) > band.high:
        return NoFinding("B1", "weight", day, "extracellular water within its range")
    jumped = (
        provenance_of(provenance, "weight", day).jump
        or provenance_of(provenance, "weight", start).jump
    )
    groups = [
        *groups_on(provenance, "weight", (start, day)),
        *groups_on(provenance, "extracellular_water", (day,)),
    ]
    return make_finding(
        "B1",
        "weight",
        day,
        window_start=start,
        window_end=day,
        reference_day=start,
        confidence=lowest(confidence_data(band.verdict), confidence_jump(jumped)),
        evidence={
            "gain_kg": str(gain),
            "from_day": start.isoformat(),
            "gain_threshold_kg": str(B1_GAIN_KG),
            "extracellular_water": float(water.value),
            "extracellular_water_band": band.as_dict(),
        },
        data_quality=band.verdict.data_quality,
        trigger_groups=groups,
    )


def b2(
    weight: Sequence[DailyValue], day: date, *, provenance: ProvenanceMap | None = None
) -> RuleResult:
    """B2: weight falling over the 182 days ending at ``day``, with the Theil-Sen change relative
    to the fitted level at most -5 % (``slope · 182 / intercept``). Persistence applies."""
    return trend_rule(
        "B2",
        "weight",
        weight,
        day,
        provenance,
        window_days=B2_DAYS,
        direction=Direction.DECREASING,
        ratio_at_most=B2_RATIO,
        jump_applies=True,
    )


def b4(
    muscle: Sequence[DailyValue], day: date, *, provenance: ProvenanceMap | None = None
) -> RuleResult:
    """B4: muscle mass falling over the 90 days ending at ``day``, change at most -2 %."""
    return trend_rule(
        "B4",
        "muscle_mass",
        muscle,
        day,
        provenance,
        window_days=B4_DAYS,
        direction=Direction.DECREASING,
        ratio_at_most=B4_RATIO,
    )


def b5(
    daily: Sequence[DailyValue],
    day: date,
    *,
    metric_key: Literal["visceral_fat", "metabolic_age"],
    provenance: ProvenanceMap | None = None,
) -> RuleResult:
    """B5: visceral fat index or metabolic age "rising" (a significant increasing trend over the 90
    days ending at ``day``); each metric is its own subject."""
    if metric_key not in ("visceral_fat", "metabolic_age"):
        raise ValueError(f"B5 reads visceral_fat or metabolic_age, not {metric_key}")
    return trend_rule(
        "B5",
        metric_key,
        daily,
        day,
        provenance,
        window_days=90,
        direction=Direction.INCREASING,
    )


def b3(ecw: Sequence[Reading], icw: Sequence[Reading], day: date) -> RuleResult:
    """B3 (experimental): ECW / (ECW + ICW) of one measure group more than 0.40 on ``day``.

    ECW (168) and ICW (169) are paired by ``grpid`` only. The day's value is the ratio of the
    earliest morning group that holds both, otherwise the median ratio of the day's groups that do.
    A user with no ICW reading at all is not available; a day without a pair is insufficient. The
    ratio is exact (``Fraction``), so 0.4 exactly does not fire. No policy window: confidence is
    ``high``."""
    subject = "water_ratio"
    if not icw:
        return Insufficient("B3", subject, day, "not available: no intracellular water (169)")
    waters = {r.grpid: r for r in icw}
    pairs = sorted(
        (e for e in ecw if e.local_date == day and e.grpid in waters), key=lambda r: r.sort_key
    )
    if not pairs:
        return Insufficient("B3", subject, day, "no measure group holds both ECW and ICW")
    chosen = next((e for e in pairs if is_morning(e)), None)
    used = [chosen] if chosen is not None else pairs
    ratios = [
        Fraction(e.value) / (Fraction(e.value) + Fraction(waters[e.grpid].value)) for e in used
    ]
    ratio = median(ratios)
    if not ratio > B3_RATIO:
        return NoFinding("B3", subject, day, "ratio not above 0.40")
    return make_finding(
        "B3",
        subject,
        day,
        window_start=day,
        window_end=day,
        reference_day=day,
        confidence=Confidence.HIGH,
        evidence={
            "ratio": float(ratio),
            "threshold": float(B3_RATIO),
            "method": "morning" if chosen is not None else "median",
            "groups_used": len(used),
        },
        data_quality=None,
        trigger_groups=(e.grpid for e in used),
    )
