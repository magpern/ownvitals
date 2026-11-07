"""Withings answer models (``models.py``) of the activity endpoints: ``getactivity``,
``getintradayactivity`` and ``getworkouts``."""

from typing import Annotated

from pydantic import BeforeValidator

from ownvitals.withings.models_base import Int, More, Num, WithingsModel, _timestamp_keys


class Activity(WithingsModel):
    date: str
    timezone: str | None = None
    deviceid: str | None = None
    hash_deviceid: str | None = None
    brand: Int | None = None
    is_tracker: bool | None = None
    modified: Int | None = None
    model: str | None = None
    modelid: Int | None = None
    steps: Num | None = None
    distance: Num | None = None
    elevation: Num | None = None
    soft: Num | None = None
    moderate: Num | None = None
    intense: Num | None = None
    active: Num | None = None
    calories: Num | None = None
    totalcalories: Num | None = None
    hr_average: Num | None = None
    hr_min: Num | None = None
    hr_max: Num | None = None
    hr_zone_0: Num | None = None
    hr_zone_1: Num | None = None
    hr_zone_2: Num | None = None
    hr_zone_3: Num | None = None


class ActivityBody(WithingsModel):
    activities: list[Activity]
    more: More = False
    offset: Int = 0


class IntradaySample(WithingsModel):
    deviceid: str | None = None
    model: str | None = None
    model_id: Int | None = None
    duration: Num | None = None
    steps: Num | None = None
    elevation: Num | None = None
    calories: Num | None = None
    distance: Num | None = None
    stroke: Num | None = None
    pool_lap: Num | None = None
    heart_rate: Num | None = None
    spo2_auto: Num | None = None
    rr: Num | None = None
    rmssd: Num | None = None
    sdnn1: Num | None = None
    hrv_quality: Num | None = None
    core_body_temperature: Num | None = None


class IntradayBody(WithingsModel):
    """``getintradayactivity``: samples keyed by epoch second (at most 24 h per call)."""

    series: Annotated[dict[Int, IntradaySample], BeforeValidator(_timestamp_keys)]


class WorkoutData(WithingsModel):
    calories: Num | None = None
    intensity: Num | None = None
    manual_distance: Num | None = None
    manual_calories: Num | None = None
    hr_average: Num | None = None
    hr_min: Num | None = None
    hr_max: Num | None = None
    hr_zone_0: Num | None = None
    hr_zone_1: Num | None = None
    hr_zone_2: Num | None = None
    hr_zone_3: Num | None = None
    pause_duration: Num | None = None
    algo_pause_duration: Num | None = None
    spo2_average: Num | None = None
    steps: Num | None = None
    distance: Num | None = None
    elevation: Num | None = None
    pool_laps: Num | None = None
    strokes: Num | None = None
    pool_length: Num | None = None


class Workout(WithingsModel):
    id: Int
    category: Int | None = None
    timezone: str | None = None
    model: Int | None = None
    attrib: Int | None = None
    startdate: Int | None = None
    enddate: Int | None = None
    date: str | None = None
    modified: Int | None = None
    deviceid: str | None = None
    data: WorkoutData | None = None


class WorkoutsBody(WithingsModel):
    series: list[Workout]
    more: More = False
    offset: Int = 0
