"""Pydantic models of the public Withings API's answers (PLAN §4.2), one per response body.

What the API sends that we did not expect must not break a sync, and what it leaves out must not
be read as zero (PLAN §4.2, "Missing data is never read as zero or normal"):

- **Unknown fields are kept** in ``raw`` (every model has one), never dropped and never an error.
  Token and nonce answers keep only the names of unknown fields: a value there could be a secret.
- **Enums are integers or strings**, not Python enums, so an undocumented value (``attrib`` 99, a
  new device type) is stored as sent. ``DOCUMENTED_*`` say which values the docs know.
- **Optional fields default to None**, never to 0: a partial record parses, and None is "not
  sent". Only what identifies a record is required (a group's ``grpid``, ``date`` and
  ``measures``; a measure's ``value``, ``type`` and ``unit``...).
- **Numbers are never floats.** The client parses JSON with ``Decimal`` for every non-integer,
  integer fields are strict (no ``bool``, no float), and fixed-point values go through
  ``units.real_value``.
- Secrets (tokens, nonces, callback URLs, which hold the webhook secret) are ``SecretStr``, so a
  ``repr`` or a log of a model never shows them.

Spec quirks accepted here (``tests/withingsshapes.py``): ``more`` is an integer for ``getmeas``
and a boolean elsewhere; ``updatetime`` may be a string; ``sleep get``'s ``series`` may be one
segment instead of a list; an empty ``getintradayactivity`` series may be ``[]``; the token
answer's ``userid`` may be a string of digits.
"""

from decimal import Decimal
from typing import Annotated, Any, ClassVar, Final

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    Strict,
    model_validator,
)

from ownvitals.withings.units import real_value

Int = Annotated[int, Strict()]
Num = Decimal  # the client parses every JSON non-integer as a Decimal

DOCUMENTED_ATTRIB: Final = frozenset({0, 1, 2, 4, 5, 7, 8, 15})  # findings §2
DOCUMENTED_POSITION: Final = frozenset({2, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14})
DOCUMENTED_CATEGORY: Final = frozenset({1, 2})  # 1 real measurement, 2 objective
DOCUMENTED_AFIB: Final = frozenset({0, 1, 2})
DOCUMENTED_AFIB_DETAILS: Final = frozenset({-1, 0, 1, 2, 5, 6, 7, 9, 10, 11, 12, 13})
DOCUMENTED_SLEEP_STATE: Final = frozenset({0, 1, 2, 3, 4, 5, 15})


def _more(value: Any) -> Any:
    """``more``: an integer (``getmeas``) or a boolean (everything else)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    raise ValueError("more must be an integer or a boolean")


def _digits(value: Any) -> Any:
    """An integer that the API sometimes sends as a string of digits."""
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return value


def _timestamp_keys(value: Any) -> Any:
    """A map keyed by epoch seconds as strings (``{"1793404800": 61}``); ``[]`` when empty."""
    if isinstance(value, list) and not value:
        return {}
    if isinstance(value, dict):
        return {_digits(k): v for k, v in value.items()}
    return value


More = Annotated[bool, BeforeValidator(_more)]
IntOrDigits = Annotated[Int, BeforeValidator(_digits)]
Series = Annotated[dict[Int, Num], BeforeValidator(_timestamp_keys)]


class WithingsModel(BaseModel):
    """Every answer model: immutable, unknown fields in ``raw``."""

    model_config = ConfigDict(frozen=True, extra="ignore")
    raw_values: ClassVar[bool] = True  # False: keep only the names of unknown fields

    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _keep_unknown(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        known = set(cls.model_fields) - {"raw"}
        fields = {k: v for k, v in data.items() if k in known}
        unknown = {
            str(k): (v if cls.raw_values else None) for k, v in data.items() if k not in known
        }
        return {**fields, "raw": unknown}


# ------------------------------------------------------------------------------------- OAuth


class TokenResponse(WithingsModel):
    """``oauth2 requesttoken`` (both grants). Validated before anything is stored: both tokens
    and the user id must be there (withings-findings §4)."""

    raw_values: ClassVar[bool] = False

    userid: IntOrDigits
    access_token: Annotated[SecretStr, Field(min_length=1)]
    refresh_token: Annotated[SecretStr, Field(min_length=1)]
    expires_in: Int
    scope: str = ""
    token_type: str | None = None
    csrf_token: SecretStr | None = None


class NonceResponse(WithingsModel):
    """``signature getnonce``: the nonce of the next signed call."""

    raw_values: ClassVar[bool] = False

    nonce: Annotated[SecretStr, Field(min_length=1)]


class EmptyBody(WithingsModel):
    """An answer whose body carries nothing we read (``notify subscribe``, ``oauth2 revoke``)."""


# ------------------------------------------------------------------------------------ getmeas


class Measure(WithingsModel):
    value: Int
    type: Int
    unit: Int
    algo: Int | None = None
    fm: Int | None = None
    position: Int | None = None

    @property
    def real(self) -> Decimal:
        """The measurement: ``value * 10^unit`` as an exact ``Decimal``."""
        return real_value(self.value, self.unit)


class MeasureGroup(WithingsModel):
    grpid: Int
    date: Int
    measures: list[Measure]
    attrib: Int | None = None
    category: Int | None = None
    created: Int | None = None
    modified: Int | None = None
    deviceid: str | None = None
    hash_deviceid: str | None = None
    model: str | None = None
    model_id: Int | None = None
    comment: str | None = None
    timezone: str | None = None


class MeasureBody(WithingsModel):
    measuregrps: list[MeasureGroup]
    more: More = False
    offset: Int = 0
    updatetime: IntOrDigits | None = None
    timezone: str | None = None


# -------------------------------------------------------------------------------------- heart


class EcgResult(WithingsModel):
    signalid: Int
    afib: Int | None = None
    afib_details: Int | None = None


class BloodPressure(WithingsModel):
    diastole: Int | None = None
    systole: Int | None = None


class HeartListEntry(WithingsModel):
    timestamp: Int
    ecg: EcgResult | None = None
    deviceid: str | None = None
    model: Int | None = None
    bloodpressure: BloodPressure | None = None
    heart_rate: Int | None = None
    modified: Int | None = None
    timezone: str | None = None


class HeartListBody(WithingsModel):
    series: list[HeartListEntry]
    more: More = False
    offset: Int = 0


class HeartRate(WithingsModel):
    grpid: Int | None = None
    value: Int | None = None
    date: Int | None = None
    is_deleted: bool | None = None


class HeartSignal(WithingsModel):
    """``heart get``: one recording, ``signal`` in µV at ``sampling_frequency`` Hz."""

    signal: list[Int]
    sampling_frequency: Int | None = None
    wearposition: Int | None = None
    model: Int | None = None
    heart_rate: HeartRate | None = None


# ----------------------------------------------------------------------------------- activity


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


# -------------------------------------------------------------------------------------- sleep


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


# --------------------------------------------------------------------------------------- user


class Device(WithingsModel):
    deviceid: str
    type: str | None = None
    model: str | None = None
    model_id: Int | None = None
    battery: str | None = None
    hash_deviceid: str | None = None
    timezone: str | None = None
    first_session_date: Int | None = None
    last_session_date: Int | None = None


class DevicesBody(WithingsModel):
    devices: list[Device]


class GoalWeight(WithingsModel):
    value: Int
    unit: Int

    @property
    def real(self) -> Decimal:
        return real_value(self.value, self.unit)


class Goals(WithingsModel):
    steps: Int | None = None
    sleep: Int | None = None
    weight: GoalWeight | None = None


class GoalsBody(WithingsModel):
    goals: Goals


# ------------------------------------------------------------------------------------- notify


class NotifyProfile(WithingsModel):
    """One subscription. The callback URL holds the webhook secret (ADR 0008): ``SecretStr``."""

    appli: Int
    callbackurl: SecretStr
    comment: str | None = None
    expires: Int | None = None


class NotifyListBody(WithingsModel):
    profiles: list[NotifyProfile]
