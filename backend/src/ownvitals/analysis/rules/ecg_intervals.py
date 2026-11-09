"""ECG interval rules E4 and E6 (ADR 0006 section 11; ADR 0010 section 7). Pure.

Split from ``ecg`` (which re-exports both and describes every E rule): E4 and E6 read the reported
intervals QTc (138), PR (136) and QRS (135) as readings and daily values, not the recordings.
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.policy import InsufficientData
from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules.core import (
    Confidence,
    NoFinding,
    ProvenanceMap,
    RuleResult,
    confidence_data,
    finding_band,
    groups_on,
    insufficient_from,
    make_finding,
    require_value,
)

# E4's bands (ADR 0006 section 11), in ms. ``>`` except QRS, which is ``>=``.
QTC_KEY: Final = "qtc_interval"
PR_KEY: Final = "pr_interval"
QRS_KEY: Final = "qrs_duration"
QTC_MALE_MS: Final = Decimal(450)
QTC_FEMALE_MS: Final = Decimal(460)
QTC_ANY_MS: Final = Decimal(500)
PR_MS: Final = Decimal(200)
QRS_MS: Final = Decimal(120)
E4_KEYS: Final = (QRS_KEY, PR_KEY, QTC_KEY)
E6_KEYS: Final = (QRS_KEY, QTC_KEY)

Sex = Literal["female", "male"]


def _e4_band(metric_key: str, value: Decimal, sex: Sex | None) -> tuple[str, Decimal] | None:
    """The band ``value`` is above (name, threshold), or ``None``."""
    if metric_key == QTC_KEY:
        if value > QTC_ANY_MS:
            return "qtc_any", QTC_ANY_MS
        if sex == "male" and value > QTC_MALE_MS:
            return "qtc_male", QTC_MALE_MS
        if sex == "female" and value > QTC_FEMALE_MS:
            return "qtc_female", QTC_FEMALE_MS
        return None
    if metric_key == PR_KEY:
        return ("pr", PR_MS) if value > PR_MS else None
    if metric_key == QRS_KEY:
        return ("qrs", QRS_MS) if value >= QRS_MS else None
    raise ValueError("E4 reads only QTc, PR and QRS")


def e4(readings: Sequence[Reading], day: date, *, metric_key: str, sex: Sex | None) -> RuleResult:
    """E4 (experimental): a reported ``metric_key`` reading of ``day`` above its band. Every
    reading of the day above it is a trigger; the evidence names the highest one's band."""
    found = sorted((r for r in readings if r.local_date == day), key=lambda r: r.sort_key)
    if not found:
        raise ValueError(f"{metric_key} has no reading on {day}: evaluate only days with data")
    above = [(r, band) for r in found if (band := _e4_band(metric_key, r.value, sex)) is not None]
    if not above:
        return NoFinding("E4", metric_key, day, "within the band")
    top, (name, threshold) = max(above, key=lambda pair: pair[0].value)
    return make_finding(
        "E4",
        metric_key,
        day,
        window_start=day,
        window_end=day,
        reference_day=day,
        confidence=Confidence.HIGH,
        evidence={
            "band": name,
            "threshold_ms": float(threshold),
            "value_ms": float(top.value),
            "sex_known": sex is not None,
            "readings_above": len(above),
        },
        data_quality=None,
        trigger_groups=(r.grpid for r, _ in above),
    )


def e6(
    daily: Sequence[DailyValue],
    day: date,
    *,
    metric_key: str,
    provenance: ProvenanceMap | None = None,
) -> RuleResult:
    """E6: the daily value of ``day`` above the finding threshold at ``day`` (k 2.5, the 90 days
    before it, BASELINE policy); equal does not fire. Below the policy: insufficient data."""
    if metric_key not in E6_KEYS:
        raise ValueError("E6 reads only QTc and QRS")
    value = float(require_value(daily, day, metric_key).value)
    band = finding_band(metric_key, daily, day)
    if isinstance(band, InsufficientData):
        return insufficient_from("E6", metric_key, day, band, f"{metric_key} baseline policy")
    if not value > band.high:
        return NoFinding("E6", metric_key, day, "not above the finding threshold")
    return make_finding(
        "E6",
        metric_key,
        day,
        window_start=day,
        window_end=day,
        reference_day=day,
        confidence=confidence_data(band.verdict),
        evidence={"value": value, "finding_band": band.as_dict()},
        data_quality=band.verdict.data_quality,
        trigger_groups=groups_on(provenance, metric_key, (day,)),
    )


__all__ = ["E4_KEYS", "E6_KEYS", "PR_KEY", "QRS_KEY", "QTC_KEY", "Sex", "e4", "e6"]
