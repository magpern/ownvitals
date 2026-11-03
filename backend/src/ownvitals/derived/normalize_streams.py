"""Normalisation of the sleep and activity streams (#229, PLAN §7.1 step 1 and D4): the
``sleep_summaries``, ``activity_daily`` and ``workouts`` rows into ``observations``.

Pure and deterministic, like ``derived.normalize``: no database, clock or randomness. The
database side (``normalize_db``) turns each source row into a ``StreamRecord`` and calls
``normalise_stream``; the same input gives the same rows in the same order.

* **Keys.** A field ``f`` of a stream becomes ``<prefix>f``: ``sleep_`` for a sleep summary's
  ``data``, ``activity_`` for an activity day's fields, ``workout_`` for a workout's ``data``.
  The prefix is the stream's ownership mark in ``observations`` (with ``source_type IS NULL``):
  a pass deletes and re-stamps only rows under its own prefix. The known fields are the tables
  below; a sleep summary's or a workout's other numeric fields are kept too (withings-findings
  §2, "store unknown ... too"), an activity day's are not (its stored ``fields`` holds the known
  ones only; the rest stays in ``raw``).
* **Value.** Exact: the stored decimal text (or integer) is read as a ``Decimal`` and split into
  ``value_raw x 10^unit``; ``conversion`` is ``e<unit>:unknown``, because the findings document no
  unit for these fields. No field is converted.
* **Instant.** ``ts`` is the instant the day is assigned from; ``source_ts`` is the source row's
  start. Sleep: ``ts`` is the night's end (the wake-up, so the local day of ``ts`` is ADR 0012
  §4's "night whose wake-up falls on *d*"). Workout: its start. Activity day: 12:00 local time on
  its date in its zone, so any later day assignment within ±11 h of that zone keeps the date.
* **Reference.** ``source_grpid`` is the Withings id of the summary or the workout; an activity
  day has none (NULL). ``source_type`` and ``source_position`` are NULL for every stream row.
* **Flags.** None: these streams carry no ``attrib``, and #37's gates judge measures only.
* Rows the table cannot hold are counted in ``Skipped`` (``invalid`` for a known field that is not
  a number or an extra field whose name cannot be a key), never written and never logged with a
  value. A second copy of the same row (two connections reporting the same night) is a duplicate:
  the first in canonical order is kept.
"""

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ownvitals.derived.metric_keys import UNKNOWN_UNIT, conversion
from ownvitals.derived.normalize import (
    MAX_MAGNITUDE,
    UNIT_MAX,
    UNIT_MIN,
    GroupRef,
    Normalised,
    Observation,
    Skipped,
)
from ownvitals.derived.tables import METRIC_KEY_PATTERN
from ownvitals.ingest.when import in_range

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

_KEY: Final = re.compile(METRIC_KEY_PATTERN)
_NAME: Final = re.compile(r"[a-z][a-z0-9_]*")
_DECIMAL: Final = re.compile(r"-?[0-9]{1,40}(\.[0-9]{1,40})?")
_NOON: Final = time(12, 0)


@dataclass(frozen=True, slots=True)
class Stream:
    """What ``normalise_stream`` needs to know of one stream."""

    prefix: str
    known: tuple[str, ...]
    extras: bool  # keep numeric fields outside ``known``


SLEEP: Final = Stream(SLEEP_PREFIX, SLEEP_FIELDS, True)
ACTIVITY: Final = Stream(ACTIVITY_PREFIX, ACTIVITY_FIELDS, False)
WORKOUT: Final = Stream(WORKOUT_PREFIX, WORKOUT_FIELDS, True)


@dataclass(frozen=True, slots=True)
class StreamRecord:
    """One source row. ``ts`` is the instant of the row, or None with ``noon_of`` set (an
    activity day: 12:00 local time on that date in ``source_tz``)."""

    ref: int | None
    ts: datetime | None
    source_ts: datetime
    source_tz: str
    fields: tuple[tuple[str, object], ...]
    noon_of: date | None = None


def owned_key(prefix: str, key: str) -> bool:
    return key.startswith(prefix)


def _number(value: object) -> Decimal | None:
    """The exact number a stored field holds: an integer, or the decimal text the ingest wrote
    (``ingest.codec.canonical``). Anything else (a float, a boolean, a string that is not a plain
    decimal, an object) is not a reading."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str) and _DECIMAL.fullmatch(value) is not None:
        try:
            return Decimal(value)
        except InvalidOperation:  # pragma: no cover - the pattern admits only plain decimals
            return None
    return None


def _split(value: Decimal) -> tuple[int, int] | None:
    """``(raw, unit)`` with ``raw x 10^unit == value``, or None beyond the table's limits."""
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        return None
    if exponent < 0:
        raw, unit = int(value.scaleb(-exponent)), exponent
    else:
        raw, unit = int(value), 0
    if not UNIT_MIN <= unit <= UNIT_MAX or raw.bit_length() > 62 or abs(value) > MAX_MAGNITUDE:
        return None
    return raw, unit


def _instant(record: StreamRecord) -> datetime | None:
    """The row's ``ts``, or None when its zone is not an IANA zone (or it has no instant)."""
    try:
        zone = ZoneInfo(record.source_tz)
    except (ValueError, ZoneInfoNotFoundError, OSError):
        return None
    if record.noon_of is None:
        return record.ts
    return datetime.combine(record.noon_of, _NOON, tzinfo=zone).astimezone(UTC)


def _canonical(record: StreamRecord) -> tuple[object, ...]:
    return (
        record.ts or datetime.min.replace(tzinfo=UTC),
        record.noon_of or date.min,
        -1 if record.ref is None else record.ref,
        record.ref is None,
        record.source_ts,
        record.source_tz,
        tuple(sorted((name, repr(value)) for name, value in record.fields)),
    )


def _order(row: Observation) -> tuple[object, ...]:
    return (row.metric_key, row.ts, row.source_grpid is not None, row.source_grpid or 0)


def normalise_stream(
    records: Iterable[StreamRecord], stream: Stream, *, now: datetime
) -> Normalised:
    """Every observation of one stream's ``records``, in a canonical order."""
    out_of_range = bad_zone = outside = duplicates = invalid = 0
    seen: set[tuple[str, int | None, None, None, datetime]] = set()
    rows: list[Observation] = []
    known = frozenset(stream.known)
    for record in sorted(records, key=_canonical):
        ts = _instant(record)
        if ts is None:
            bad_zone += 1
            continue
        if not in_range(ts, now):
            outside += 1
            continue
        for name, stored in sorted(record.fields, key=lambda item: item[0]):
            is_known = name in known
            if not is_known and not stream.extras:
                continue
            number = _number(stored)
            if number is None:
                invalid += is_known  # an extra that is not a number is not a reading
                continue
            key = stream.prefix + name
            if _NAME.fullmatch(name) is None or _KEY.fullmatch(key) is None:
                invalid += 1
                continue
            split = _split(number)
            if split is None:
                out_of_range += 1
                continue
            identity = (key, record.ref, None, None, ts)
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
            raw, unit = split
            rows.append(
                Observation(
                    metric_key=key,
                    ts=ts,
                    source_ts=record.source_ts,
                    source_tz=record.source_tz,
                    value=number,
                    source_value_raw=raw,
                    source_unit=unit,
                    conversion=conversion(unit, UNKNOWN_UNIT),
                    quality_flags=(),
                    source_grpid=record.ref,
                    source_type=None,
                    source_position=None,
                )
            )
    rows.sort(key=_order)
    return Normalised(
        observations=tuple(rows),
        skipped=Skipped(out_of_range, bad_zone, outside, duplicates, invalid),
    )


def stream_fingerprint(stream: str, rows: Iterable[GroupRef]) -> bytes:
    """SHA-256 over the algorithm version, the stream and the sorted ``(id, payload hash)`` of
    the stream's source rows (PLAN §5). These tables have no soft delete; the mark is kept so a
    ``GroupRef`` reads the same as for measures."""
    digest = hashlib.sha256()
    digest.update(f"{ALGO_VERSION}|{stream}\n".encode())
    for ref in sorted(rows, key=lambda g: (str(g.id), g.payload_hash, g.deleted)):
        mark = "d" if ref.deleted else "l"
        digest.update(f"{ref.id}|{ref.payload_hash.hex()}|{mark}\n".encode())
    return digest.digest()


def fields_of(body: Mapping[str, object] | None, part: str) -> tuple[tuple[str, object], ...]:
    """The ``part`` object of a stored ``raw`` body as ``(name, value)`` pairs; nothing when the
    body or the part is not an object."""
    if not isinstance(body, Mapping):
        return ()
    inner = body.get(part)
    if not isinstance(inner, Mapping):
        return ()
    return tuple((str(name), value) for name, value in inner.items())


def ref_of(body: Mapping[str, object] | None) -> int | None:
    """The Withings id a stored sleep-summary body carries, when it is an integer."""
    if not isinstance(body, Mapping):
        return None
    value = body.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = [
    "ACTIVITY",
    "ALGO_VERSION",
    "SCORE_INPUTS",
    "SLEEP",
    "WORKOUT",
    "Stream",
    "StreamRecord",
    "fields_of",
    "normalise_stream",
    "owned_key",
    "ref_of",
    "stream_fingerprint",
]
