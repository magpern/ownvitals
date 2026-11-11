"""The arguments and output schemas of the ECG, sleep and activity tools (ADR 0014 section 4;
#401). Pure: no I/O. ``ai.tools_model`` registers them in ``TOOLS`` and ``OUTPUT_FIELDS``.

- ``ecg_summary``: the number of recordings in the period, the counts of Withings' own
  classification (``afib``, its documented meanings, withings-findings section 3), and per
  recording its day, that classification, Withings' heart rate and the intervals Withings reported
  with it (ADR 0006 A7). No sample, signal, signal id, device or time of day. ownvitals' own
  estimates (ADR 0006 section 9: experimental for AI) only in ``EcgSummaryExperimentalOut``, which
  the tool returns only when the tier filter lets an estimate through; otherwise the field is
  absent, not empty.
- ``sleep_summary`` and ``activity_summary``: one point per night or day, at most 90, days only.
  The fields are a closed subset of the normalised Withings fields (``SLEEP_FIELDS``,
  ``ACTIVITY_FIELDS`` of ``derived.normalize_stream_spec``); their units are Withings' public
  API's (seconds, metres, kcal), a documented assumption until the probe shows them.
"""

from collections.abc import Mapping
from datetime import date
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from ownvitals.derived.normalize_stream_spec import ACTIVITY_PREFIX, SLEEP_PREFIX

EcgPeriod = Literal[30, 90, 180, 365]
StreamPeriod = Literal[7, 14, 30, 60, 90]
ECG_MAX_ITEMS: Final = 90  # recordings listed per result (the counts cover the whole period)
STREAM_MAX_POINTS: Final = 90
# Withings' ``afib`` codes (withings-findings section 3) and the two words for the rest.
EcgClassification = Literal["negative", "positive", "inconclusive", "not_reported", "undocumented"]

# Output field -> the normalised metric key it reads (prefix + Withings field name).
SLEEP_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "sleep_seconds": SLEEP_PREFIX + "total_sleep_time",
        "deep_seconds": SLEEP_PREFIX + "deepsleepduration",
        "light_seconds": SLEEP_PREFIX + "lightsleepduration",
        "rem_seconds": SLEEP_PREFIX + "remsleepduration",
        "wakeups": SLEEP_PREFIX + "wakeupcount",
        "efficiency": SLEEP_PREFIX + "sleep_efficiency",
        "sleep_score": SLEEP_PREFIX + "sleep_score",
        "heart_rate_bpm": SLEEP_PREFIX + "hr_average",
    }
)
ACTIVITY_KEYS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "steps": ACTIVITY_PREFIX + "steps",
        "distance_m": ACTIVITY_PREFIX + "distance",
        "elevation_m": ACTIVITY_PREFIX + "elevation",
        "soft_seconds": ACTIVITY_PREFIX + "soft",
        "moderate_seconds": ACTIVITY_PREFIX + "moderate",
        "intense_seconds": ACTIVITY_PREFIX + "intense",
        "active_kcal": ACTIVITY_PREFIX + "calories",
    }
)


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- arguments ----------------------------------------------------------------------------------


class EcgSummaryArgs(Closed):
    period: EcgPeriod


class SleepSummaryArgs(Closed):
    period: StreamPeriod


class ActivitySummaryArgs(Closed):
    period: StreamPeriod


# --- outputs ------------------------------------------------------------------------------------


class EcgClassCount(Closed):
    classification: EcgClassification
    count: int


class EcgRecordingItem(Closed):
    day: date
    classification: EcgClassification
    heart_rate_bpm: int | None
    qrs_duration_ms: float | None
    pr_interval_ms: float | None
    qt_interval_ms: float | None
    qtc_interval_ms: float | None


class EcgEstimateItem(Closed):
    day: date
    heart_rate_bpm: float | None
    rmssd_ms: float | None
    sdnn_ms: float | None


class EcgSummaryOut(Closed):
    period_days: int
    recordings: int
    classifications: list[EcgClassCount]
    items: list[EcgRecordingItem]
    truncated: bool


class EcgSummaryExperimentalOut(EcgSummaryOut):
    """``EcgSummaryOut`` with ownvitals' estimates: only past the tier filter."""

    estimates: list[EcgEstimateItem]


class SleepNight(Closed):
    day: date
    sleep_seconds: float | None
    deep_seconds: float | None
    light_seconds: float | None
    rem_seconds: float | None
    wakeups: float | None
    efficiency: float | None
    sleep_score: float | None
    heart_rate_bpm: float | None


class SleepSummaryOut(Closed):
    period_days: int
    nights_with_data: int
    nights: list[SleepNight]


class ActivityDay(Closed):
    day: date
    steps: float | None
    distance_m: float | None
    elevation_m: float | None
    soft_seconds: float | None
    moderate_seconds: float | None
    intense_seconds: float | None
    active_kcal: float | None


class ActivitySummaryOut(Closed):
    period_days: int
    days_with_data: int
    days: list[ActivityDay]


# The allowlist entries of these models, merged into ``tools_model.OUTPUT_FIELDS``. Written out,
# so a field added to a model fails ``check_fields`` until it is added here too (the per-day
# models read their field names from the key tables, which are the allowlist themselves).
VITALS_OUTPUT_FIELDS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "EcgClassCount": frozenset({"classification", "count"}),
        "EcgRecordingItem": frozenset(
            {
                "day",
                "classification",
                "heart_rate_bpm",
                "qrs_duration_ms",
                "pr_interval_ms",
                "qt_interval_ms",
                "qtc_interval_ms",
            }
        ),
        "EcgEstimateItem": frozenset({"day", "heart_rate_bpm", "rmssd_ms", "sdnn_ms"}),
        "EcgSummaryOut": frozenset(
            {"period_days", "recordings", "classifications", "items", "truncated"}
        ),
        "EcgSummaryExperimentalOut": frozenset(
            {"period_days", "recordings", "classifications", "items", "truncated", "estimates"}
        ),
        "SleepNight": frozenset({"day", *SLEEP_KEYS}),
        "SleepSummaryOut": frozenset({"period_days", "nights_with_data", "nights"}),
        "ActivityDay": frozenset({"day", *ACTIVITY_KEYS}),
        "ActivitySummaryOut": frozenset({"period_days", "days_with_data", "days"}),
    }
)
