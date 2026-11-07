"""Withings answer models (``models.py``) of the sleep endpoints: ``sleep get`` and
``sleep getsummary``."""

from typing import Annotated, Any

from pydantic import BeforeValidator

from ownvitals.withings.models_base import Int, More, Num, Series, WithingsModel


def _segments(value: Any) -> Any:
    """``sleep get``'s ``series``: a list of segments, or one segment (the spec file's shape)."""
    return [value] if isinstance(value, dict) else value


class SleepSegment(WithingsModel):
    startdate: Int
    enddate: Int
    state: Int | None = None
    model: str | None = None
    model_id: Int | None = None
    hr: Series | None = None
    rr: Series | None = None
    snoring: Series | None = None
    sdnn_1: Series | None = None
    rmssd: Series | None = None
    hrv_quality: Series | None = None
    mvt_score: Series | None = None
    chest_movement_rate: Series | None = None
    withings_index: Series | None = None
    breathing_sounds: Series | None = None


class SleepBody(WithingsModel):
    series: Annotated[list[SleepSegment], BeforeValidator(_segments)]
    model: Int | None = None


class SleepSummaryData(WithingsModel):
    total_timeinbed: Num | None = None
    total_sleep_time: Num | None = None
    asleepduration: Num | None = None
    lightsleepduration: Num | None = None
    deepsleepduration: Num | None = None
    remsleepduration: Num | None = None
    sleep_efficiency: Num | None = None
    sleep_latency: Num | None = None
    wakeup_latency: Num | None = None
    wakeupduration: Num | None = None
    wakeupcount: Num | None = None
    waso: Num | None = None
    nb_rem_episodes: Num | None = None
    out_of_bed_count: Num | None = None
    durationtosleep: Num | None = None
    durationtowakeup: Num | None = None
    hr_average: Num | None = None
    hr_min: Num | None = None
    hr_max: Num | None = None
    rr_average: Num | None = None
    rr_min: Num | None = None
    rr_max: Num | None = None
    snoring: Num | None = None
    snoringepisodecount: Num | None = None
    sleep_score: Num | None = None
    apnea_hypopnea_index: Num | None = None
    breathing_disturbances_intensity: Num | None = None
    night_events: Any = None  # an array or a map, depending on the source


class SleepSummary(WithingsModel):
    id: Int
    startdate: Int | None = None
    enddate: Int | None = None
    date: str | None = None
    timezone: str | None = None
    model: Int | None = None
    model_id: Int | None = None
    created: Int | None = None
    modified: Int | None = None
    hash_deviceid: str | None = None
    completed: bool | None = None
    data: SleepSummaryData | None = None


class SleepSummaryBody(WithingsModel):
    series: list[SleepSummary]
    more: More = False
    offset: Int = 0
