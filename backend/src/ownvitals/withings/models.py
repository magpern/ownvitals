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

The base types and ``WithingsModel`` live in ``models_base.py``, the activity models in
``models_activity.py`` and the sleep models in ``models_sleep.py``; all are re-exported here, so
``models.<Name>`` stays the one import path.
"""

from decimal import Decimal
from typing import Annotated, ClassVar

from pydantic import Field, SecretStr

from ownvitals.withings.models_activity import Activity as Activity
from ownvitals.withings.models_activity import ActivityBody as ActivityBody
from ownvitals.withings.models_activity import IntradayBody as IntradayBody
from ownvitals.withings.models_activity import IntradaySample as IntradaySample
from ownvitals.withings.models_activity import Workout as Workout
from ownvitals.withings.models_activity import WorkoutData as WorkoutData
from ownvitals.withings.models_activity import WorkoutsBody as WorkoutsBody
from ownvitals.withings.models_base import DOCUMENTED_AFIB as DOCUMENTED_AFIB
from ownvitals.withings.models_base import DOCUMENTED_AFIB_DETAILS as DOCUMENTED_AFIB_DETAILS
from ownvitals.withings.models_base import DOCUMENTED_ATTRIB as DOCUMENTED_ATTRIB
from ownvitals.withings.models_base import DOCUMENTED_CATEGORY as DOCUMENTED_CATEGORY
from ownvitals.withings.models_base import DOCUMENTED_POSITION as DOCUMENTED_POSITION
from ownvitals.withings.models_base import DOCUMENTED_SLEEP_STATE as DOCUMENTED_SLEEP_STATE
from ownvitals.withings.models_base import Int, IntOrDigits, More, WithingsModel
from ownvitals.withings.models_base import Num as Num
from ownvitals.withings.models_base import Series as Series
from ownvitals.withings.models_sleep import SleepBody as SleepBody
from ownvitals.withings.models_sleep import SleepSegment as SleepSegment
from ownvitals.withings.models_sleep import SleepSummary as SleepSummary
from ownvitals.withings.models_sleep import SleepSummaryBody as SleepSummaryBody
from ownvitals.withings.models_sleep import SleepSummaryData as SleepSummaryData
from ownvitals.withings.units import real_value

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
