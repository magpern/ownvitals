"""Heart rules C1, C1x and C2 (ADR 0010 §3 and §7; PLAN §7.2). Pure.

Same contract as ``body``: a ``Finding`` proposal, a ``NoFinding`` or ``Insufficient`` for one
evaluation ``day``. C1 and C1x are runs of consecutive calendar days (a day without data ends the
run); both end on their first non-firing evaluation. C2 is a trend-test rule (persistence applies).
C1x is experimental: it has its own ``rule_id``, so the two tiers are separate findings. "Resting
HR" is heart pulse (11)'s daily value as defined in §3, not a clinical resting heart rate.
"""

from collections.abc import Sequence
from datetime import date, timedelta

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.methods.baseline import Window
from ownvitals.analysis.methods.trend import Direction
from ownvitals.analysis.policy import InsufficientData
from ownvitals.analysis.rules.core import (
    Band,
    NoFinding,
    ProvenanceMap,
    RuleResult,
    confidence_data,
    confidence_run,
    display_band,
    groups_on,
    insufficient_from,
    lowest,
    make_finding,
    require_value,
    trend_rule,
)

KEY = "heart_pulse"
C1_MARGIN_BPM = 10.0  # "10 bpm or more above your usual level" (a product parameter)
MIN_RUN_DAYS = 3
C1X_LOW_BPM = 50.0  # below (sinus bradycardia, Kusumoto et al. 2019)
C1X_HIGH_BPM = 100.0  # above (sinus tachycardia, Page et al. 2016)
C2_DAYS = 90
DAYS_PER_YEAR = 365.25


def run_ending_at(daily: Sequence[DailyValue], day: date) -> list[DailyValue]:
    """The consecutive calendar days with a daily value that end at ``day``, ascending."""
    by_day = {v.day: v for v in daily}
    run = [by_day[day]]
    while (previous := run[0].day - timedelta(days=1)) in by_day:
        run.insert(0, by_day[previous])
    return run


def c1(
    daily: Sequence[DailyValue], day: date, *, provenance: ProvenanceMap | None = None
) -> RuleResult:
    """C1: a run of at least 3 consecutive days ending at ``day``, each value at least the median
    plus 10 bpm and above the display high, with median and band (k 1.5, BASELINE policy) over the
    90 days before the run's first day. The longest run that satisfies this is taken, so a growing
    run keeps its first day and band. Window: the run; reference day: its first day."""
    require_value(daily, day, KEY)
    chain = run_ending_at(daily, day)
    if len(chain) < MIN_RUN_DAYS:
        return NoFinding("C1", KEY, day, "fewer than 3 consecutive days")
    shortest = len(chain) - MIN_RUN_DAYS
    for index in range(shortest + 1):
        run = chain[index:]
        band = display_band(KEY, daily, run[0].day, window=Window.PRECEDING)
        if isinstance(band, InsufficientData):
            if index == shortest:
                return insufficient_from("C1", KEY, day, band, "heart_pulse baseline policy")
            continue
        if all(
            float(v.value) >= band.median + C1_MARGIN_BPM and float(v.value) > band.high
            for v in run
        ):
            return _c1_finding(run, band, day, provenance)
    return NoFinding("C1", KEY, day, "no run above the usual level")


def _c1_finding(
    run: list[DailyValue], band: Band, day: date, provenance: ProvenanceMap | None
) -> RuleResult:
    return make_finding(
        "C1",
        KEY,
        day,
        window_start=run[0].day,
        window_end=day,
        reference_day=run[0].day,
        confidence=lowest(confidence_data(band.verdict), confidence_run(len(run), MIN_RUN_DAYS)),
        evidence={
            "run_days": len(run),
            "values": [float(v.value) for v in run],
            "margin_bpm": C1_MARGIN_BPM,
            "median": band.median,
            "display_band": band.as_dict(),
        },
        data_quality=band.verdict.data_quality,
        trigger_groups=groups_on(provenance, KEY, (v.day for v in run)),
    )


def c1x(
    daily: Sequence[DailyValue], day: date, *, provenance: ProvenanceMap | None = None
) -> RuleResult:
    """C1x (experimental): a run of at least 3 consecutive days ending at ``day``, every value below
    50 bpm or every value above 100 bpm (one direction per episode). No band, so no policy."""
    value = float(require_value(daily, day, KEY).value)
    if value < C1X_LOW_BPM:
        direction = "below"

        def met(x: float) -> bool:
            return x < C1X_LOW_BPM
    elif value > C1X_HIGH_BPM:
        direction = "above"

        def met(x: float) -> bool:
            return x > C1X_HIGH_BPM
    else:
        return NoFinding("C1x", KEY, day, "within 50 to 100 bpm")
    run: list[DailyValue] = []
    for v in reversed(run_ending_at(daily, day)):
        if not met(float(v.value)):
            break
        run.insert(0, v)
    if len(run) < MIN_RUN_DAYS:
        return NoFinding("C1x", KEY, day, "fewer than 3 consecutive days")
    return make_finding(
        "C1x",
        KEY,
        day,
        window_start=run[0].day,
        window_end=day,
        reference_day=run[0].day,
        confidence=confidence_run(len(run), MIN_RUN_DAYS),
        evidence={
            "direction": direction,
            "threshold_bpm": C1X_LOW_BPM if direction == "below" else C1X_HIGH_BPM,
            "run_days": len(run),
            "values": [float(v.value) for v in run],
        },
        data_quality=None,
        trigger_groups=groups_on(provenance, KEY, (v.day for v in run)),
    )


def c2(
    vascular_age: Sequence[DailyValue], day: date, *, provenance: ProvenanceMap | None = None
) -> RuleResult:
    """C2: vascular age "rising" relative to the calendar: a significant increasing trend of
    ``VA - (day - 1970-01-01) / 365.25`` over the 90 days ending at ``day``. The birth year only
    shifts that by a constant, so none is needed. The score-card part is not in v1."""
    return trend_rule(
        "C2",
        "vascular_age",
        vascular_age,
        day,
        provenance,
        window_days=C2_DAYS,
        direction=Direction.INCREASING,
        transform=lambda t, y: y - t / DAYS_PER_YEAR,
    )
