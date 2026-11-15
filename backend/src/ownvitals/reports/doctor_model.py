"""The doctor PDF's content as plain data, and the monthly figures (ADR 0013 section 6). Pure.

``build_section`` turns a metric's stored daily rows (the 90 days before the period too, for the
typical range) into what the page prints: the daily points of the period, the personal typical
range (ADR 0010 section 2's display band at the period's last day) and one row per calendar
month. There is no field for an ownvitals estimate, a score, an experimental or trend finding,
an AI text or an unconfirmed (``attrib`` 1) reading, so none can be printed; the reader leaves
unconfirmed readings out before they get here.
"""

import statistics
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.policy import InsufficientData
from ownvitals.analysis.quality import METRICS
from ownvitals.analysis.rules.band import display_band

FOOTER: Final = (
    "Data from the user's Withings account, retrieved through the Withings public API; readings "
    "marked 'entered manually' were typed in, not measured by a device. Classifications are "
    "Withings'. ownvitals is not a medical device and this report is not a diagnosis."
)
RANGE_LABEL: Final = (
    "Your typical range, from your own last 90 days; not a clinical reference range"
)
FINDING_TEXT: Final = {
    "E1": "Withings classified an ECG as positive for atrial fibrillation.",
    "E2": "Withings classified several recent ECGs as inconclusive or noisy.",
    "E3": "Withings reported a low or high heart rate on an ECG.",
}
LABELS: Final = {
    "weight": "Weight",
    "fat_free_mass": "Fat-free mass",
    "fat_ratio": "Fat ratio",
    "fat_mass": "Fat mass",
    "diastolic_bp": "Diastolic blood pressure",
    "systolic_bp": "Systolic blood pressure",
    "heart_pulse": "Heart pulse",
    "spo2": "Oxygen saturation (SpO2)",
    "muscle_mass": "Muscle mass",
    "hydration": "Hydration",
    "bone_mass": "Bone mass",
    "pulse_wave_velocity": "Pulse wave velocity",
    "qrs_duration": "QRS duration",
    "pr_interval": "PR interval",
    "qt_interval": "QT interval",
    "qtc_interval": "QTc interval",
    "vascular_age": "Vascular age",
    "nerve_health_score": "Nerve health score",
    "extracellular_water": "Extracellular water",
    "intracellular_water": "Intracellular water",
    "visceral_fat": "Visceral fat",
    "nerve_response_score": "Nerve response score",
    "metabolic_age": "Metabolic age",
    "esc": "Electrochemical skin conductance",
}
BAND_DAYS: Final = 90


@dataclass(frozen=True)
class DailyRow:
    """One stored daily value; ``manual`` when a reading of the day was typed in (attrib 2, 4)."""

    day: date
    value: Decimal
    method: str
    n: int
    minimum: Decimal
    maximum: Decimal
    mad: Decimal
    algo_version: str
    manual: bool


@dataclass(frozen=True)
class Point:
    day: date
    value: float
    manual: bool


@dataclass(frozen=True)
class Range:
    low: float
    median: float
    high: float


@dataclass(frozen=True)
class MonthRow:
    month: str  # YYYY-MM
    median: float
    minimum: float
    maximum: float
    days: int
    manual_days: int


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    unit: str
    points: tuple[Point, ...]
    typical: Range | None
    months: tuple[MonthRow, ...]


@dataclass(frozen=True)
class FindingLine:
    rule_id: str
    text: str
    first: date
    last: date


@dataclass(frozen=True)
class EcgNote:
    """An ECG the user chose that is not printed as a strip, and why."""

    recorded: str
    reason: str


@dataclass(frozen=True)
class Report:
    start: date
    end: date
    generated: date
    birth_year: int | None
    sections: tuple[Section, ...]
    findings: tuple[FindingLine, ...]
    ecg_notes: tuple[EcgNote, ...]

    def __repr__(self) -> str:
        return "Report redacted"


def shown(value: float) -> str:
    """A figure as printed: at most two decimals, no trailing zeros."""
    return f"{round(value, 2):g}"


def _month_rows(rows: list[DailyRow]) -> tuple[MonthRow, ...]:
    by_month: dict[str, list[DailyRow]] = {}
    for row in rows:
        by_month.setdefault(f"{row.day:%Y-%m}", []).append(row)
    return tuple(
        MonthRow(
            month=month,
            median=float(statistics.median(r.value for r in month_rows)),
            minimum=float(min(r.minimum for r in month_rows)),
            maximum=float(max(r.maximum for r in month_rows)),
            days=len(month_rows),
            manual_days=sum(1 for r in month_rows if r.manual),
        )
        for month, month_rows in sorted(by_month.items())
    )


def _typical(key: str, rows: list[DailyRow], end: date) -> Range | None:
    daily = [
        DailyValue(r.day, r.value, DailyMethod(r.method), r.n, r.minimum, r.maximum, r.mad)
        for r in rows
        if end - timedelta(days=BAND_DAYS) < r.day <= end
    ]
    try:
        band = display_band(key, daily, end)
    except KeyError:  # a metric with no minimum half-width has no displayed range anywhere
        return None
    if isinstance(band, InsufficientData):
        return None
    return Range(low=band.low, median=band.median, high=band.high)


def build_section(key: str, rows: list[DailyRow], start: date, end: date) -> Section:
    """``rows``: the metric's daily rows from 90 days before ``start`` to ``end``."""
    in_period = sorted((r for r in rows if start <= r.day <= end), key=lambda r: r.day)
    return Section(
        key=key,
        label=LABELS.get(key, key),
        unit=METRICS[key].unit,
        points=tuple(Point(r.day, float(r.value), r.manual) for r in in_period),
        typical=_typical(key, rows, end),
        months=_month_rows(in_period),
    )
