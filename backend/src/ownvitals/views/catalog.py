"""What the read API knows about the metrics it lists (PLAN section 8, the design's information
architecture): the area each metric belongs to, the stream whose capability decides its data
state, the chart ranges and the thresholds that are ours to set. Pure; no database.

The keys are those of ``analysis.quality.METRICS``, the metrics that have daily values
(``derived.daily.KEYS``). Sleep and activity keys have no daily value yet; the segmental
tiles are not here, they are built from the latest reading
(``views.assemble_segmental``).
"""

from types import MappingProxyType
from typing import Final

from ownvitals.analysis.quality import METRICS

AREAS: Final = MappingProxyType(
    {
        **dict.fromkeys(
            (
                "weight",
                "fat_free_mass",
                "fat_ratio",
                "fat_mass",
                "muscle_mass",
                "hydration",
                "bone_mass",
                "extracellular_water",
                "intracellular_water",
                "visceral_fat",
                "metabolic_age",
            ),
            "body",
        ),
        **dict.fromkeys(
            (
                "diastolic_bp",
                "systolic_bp",
                "heart_pulse",
                "spo2",
                "pulse_wave_velocity",
                "qrs_duration",
                "pr_interval",
                "qt_interval",
                "qtc_interval",
                "vascular_age",
            ),
            "heart",
        ),
        **dict.fromkeys(("nerve_health_score", "nerve_response_score", "esc"), "nerve"),
    }
)
NERVE_KEYS: Final = ("nerve_health_score", "nerve_response_score", "esc")

# The stream whose capability decides a metric's data state. Every metric arrives in a measure
# group (``measures``); the ECG intervals are the ones that depend on the ECG feature, whose
# capability alone can be ``not_available`` (no device, no recording; withings.capabilities).
_ECG_INTERVALS: Final = ("qrs_duration", "pr_interval", "qt_interval", "qtc_interval")
STREAM_OF: Final = MappingProxyType(
    {key: ("ecg" if key in _ECG_INTERVALS else "measures") for key in METRICS}
)
# The streams the status shows, in display order (devices and goals are not data streams).
STATUS_STREAMS: Final = (
    "measures",
    "ecg",
    "activity_daily",
    "workouts",
    "sleep_summary",
    "sleep_series",
    "intraday",
)

# ``?range=`` of the metric detail: the profile's chart ranges (``derived.tables.CHART_RANGES``)
# and ``all``. Days of the window; ``all`` runs from the first day with data.
RANGE_DAYS: Final = MappingProxyType({"7d": 7, "30d": 30, "90d": 90, "6mo": 182, "1y": 365})
RANGES: Final = (*RANGE_DAYS, "all")
DEFAULT_RANGE: Final = "7d"

SPARKLINE_DAYS: Final = 30
BAND_DAYS: Final = 90  # the display band's window (ADR 0010 section 2)
# A stream whose last successful sync is older than this is ``stale``: the daily poll (PLAN
# section 4.4) is the safety net behind the webhooks, so two days without one is a fault.
STALE_AFTER_HOURS: Final = 48
PLAN_BLOCK: Final = "blocked_by_provider_plan"


def area_of(metric_key: str) -> str:
    return AREAS[metric_key]
