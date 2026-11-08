"""The stream tables of normalisation (#229, ADR 0012 §2 and §4): the key prefixes, the known
fields of each stream and the score inputs. Pure data.

Split from ``derived.normalize_streams`` (no behaviour change; its docstring describes the
normalisation).
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

ALGO_VERSION: Final = "normalize-streams/1"

SLEEP_PREFIX: Final = "sleep_"
ACTIVITY_PREFIX: Final = "activity_"
WORKOUT_PREFIX: Final = "workout_"

# ``withings.models.SleepSummaryData``'s numeric fields (``night_events`` is an array or a map,
# not a reading), plus ``rmssd``: ADR 0012 §4 reads "the summary's RMSSD", a field the public
# summary shape does not list, so its name is a documented assumption until the probe shows it.
SLEEP_FIELDS: Final = (
    "total_timeinbed",
    "total_sleep_time",
    "asleepduration",
    "lightsleepduration",
    "deepsleepduration",
    "remsleepduration",
    "sleep_efficiency",
    "sleep_latency",
    "wakeup_latency",
    "wakeupduration",
    "wakeupcount",
    "waso",
    "nb_rem_episodes",
    "out_of_bed_count",
    "durationtosleep",
    "durationtowakeup",
    "hr_average",
    "hr_min",
    "hr_max",
    "rr_average",
    "rr_min",
    "rr_max",
    "snoring",
    "snoringepisodecount",
    "sleep_score",
    "apnea_hypopnea_index",
    "breathing_disturbances_intensity",
    "rmssd",
)
# ``ingest.codec.ACTIVITY_FIELDS``: what the ingest keeps of an activity day (a test holds both
# equal; the codec is not imported here because it loads the ECG pipeline).
ACTIVITY_FIELDS: Final = (
    "steps",
    "distance",
    "elevation",
    "soft",
    "moderate",
    "intense",
    "active",
    "calories",
    "totalcalories",
    "hr_average",
    "hr_min",
    "hr_max",
    "hr_zone_0",
    "hr_zone_1",
    "hr_zone_2",
    "hr_zone_3",
)
# ``withings.models.WorkoutData``'s fields.
WORKOUT_FIELDS: Final = (
    "calories",
    "intensity",
    "manual_distance",
    "manual_calories",
    "hr_average",
    "hr_min",
    "hr_max",
    "hr_zone_0",
    "hr_zone_1",
    "hr_zone_2",
    "hr_zone_3",
    "pause_duration",
    "algo_pause_duration",
    "spo2_average",
    "steps",
    "distance",
    "elevation",
    "pool_laps",
    "strokes",
    "pool_length",
)

# The sleep and activity inputs of ADR 0012 (§2's criteria, §4's Readiness components) and the
# key each one reads. ADR 0010's v1 rules read none (night HR is out of v1, its §8). The field
# names are documented assumptions until the probe confirms them (ADR 0012 §2).
SCORE_INPUTS: Final = MappingProxyType(
    {
        "night_hr": SLEEP_PREFIX + "hr_average",
        "sleep_ahi": SLEEP_PREFIX + "apnea_hypopnea_index",
        "readiness_sleep_duration": SLEEP_PREFIX + "total_sleep_time",
        "readiness_night_hr": SLEEP_PREFIX + "hr_average",
        "readiness_hrv": SLEEP_PREFIX + "rmssd",
        "daily_steps": ACTIVITY_PREFIX + "steps",
        "weekly_active_minutes_moderate": ACTIVITY_PREFIX + "moderate",
        "weekly_active_minutes_intense": ACTIVITY_PREFIX + "intense",
    }
)


@dataclass(frozen=True, slots=True)
class Stream:
    """What ``normalise_stream`` needs to know of one stream."""

    prefix: str
    known: tuple[str, ...]
    extras: bool  # keep numeric fields outside ``known``


SLEEP: Final = Stream(SLEEP_PREFIX, SLEEP_FIELDS, True)
ACTIVITY: Final = Stream(ACTIVITY_PREFIX, ACTIVITY_FIELDS, False)
WORKOUT: Final = Stream(WORKOUT_PREFIX, WORKOUT_FIELDS, True)
