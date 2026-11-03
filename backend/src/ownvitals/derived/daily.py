"""Daily values from observations (PLAN §7.1 step 3): the pure part of the ``metric_daily`` pass.

``compute`` takes the observations of one owner (``ObservationRow``) and returns one ``DailyRow``
per (metric, local day), using #37's ``daily_values()`` for the value, the method, ``n``, the
spread and the MAD. A reading is used when none of the exclusion flags the normaliser stored on
it (``unconfirmed``, ``implausible``, ``duplicate``) is set; ``jump`` is advisory and the reading
stays. Only the metrics of ``analysis.quality.METRICS`` get daily values; every other
``metric_key`` is ignored here.

What ``daily_values()`` does not return is added here, from the same readings:

* ``source_groups``: the ``grpid`` of the first morning reading when the method is ``morning``,
  otherwise every ``grpid`` of the day (the median uses them all; ADR 0006 §11), ascending;
* ``morning``: the daily value, set exactly when the method is ``morning``;
* ``mean``: the arithmetic mean of the day's readings, computed in a 60-digit context that rounds
  half-even (a mean such as 1/3 has no finite decimal form) and clamped into ``[min, max]``.

The result depends only on the observations' content, never on the order they arrive in.
``fingerprint`` is the provenance hash of exactly the observation rows read (PLAN §5).
"""

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Final

from ownvitals.analysis.daily import ALGO_VERSION as DAILY_VERSION
from ownvitals.analysis.daily import DailyMethod, DailyValue, daily_values, is_morning
from ownvitals.analysis.quality import EXACT, EXCLUDING, METRICS, Reading
from ownvitals.derived.normalize import ALGO_VERSION as NORMALIZE_VERSION

ALGO_VERSION: Final = DAILY_VERSION  # "daily/1": what ``metric_daily.algo_version`` stores
KEYS: Final = tuple(sorted(METRICS))  # the metric keys this pass owns

_MEAN = Context(prec=60, rounding=ROUND_HALF_EVEN)
_EXCLUDING = frozenset(flag.value for flag in EXCLUDING)


@dataclass(frozen=True, slots=True)
class ObservationRow:
    """What the daily pass reads of one ``observations`` row."""

    metric_key: str
    source_grpid: int
    source_type: int
    source_position: int | None
    ts: datetime
    source_tz: str
    value: Decimal
    quality_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DailyRow:
    """One ``metric_daily`` row's content (provenance is added by the writer)."""

    metric_key: str
    day: date
    method: str
    value: Decimal
    morning: Decimal | None
    mean: Decimal
    minimum: Decimal
    maximum: Decimal
    mad: Decimal
    n: int
    source_groups: tuple[int, ...]

    @property
    def key(self) -> tuple[str, date]:
        return (self.metric_key, self.day)


@dataclass(frozen=True, slots=True)
class Computed:
    rows: tuple[DailyRow, ...]
    skipped: int  # observations that could not be a reading (see ``compute``); counts only


def _used(value: DailyValue, readings: list[Reading]) -> tuple[Reading, ...]:
    """The readings the day's value came from, in canonical order."""
    ordered = sorted(readings, key=lambda r: r.sort_key)
    if value.method is DailyMethod.MORNING:
        return (next(r for r in ordered if is_morning(r)),)
    return tuple(ordered)


def _mean(values: list[Decimal], low: Decimal, high: Decimal) -> Decimal:
    with localcontext(_MEAN):
        mean = sum(values, Decimal(0)) / len(values)
    return min(max(mean, low), high)


def _row(metric_key: str, value: DailyValue, readings: list[Reading]) -> DailyRow:
    used = _used(value, readings)
    morning = value.method is DailyMethod.MORNING
    return DailyRow(
        metric_key=metric_key,
        day=value.day,
        method=value.method.value,
        value=value.value,
        morning=value.value if morning else None,
        mean=_mean([r.value for r in readings], value.minimum, value.maximum),
        minimum=value.minimum,
        maximum=value.maximum,
        mad=value.mad,
        n=value.n,
        source_groups=tuple(sorted(r.grpid for r in used)),
    )


def _reading(row: ObservationRow) -> Reading | None:
    try:
        return Reading(
            grpid=row.source_grpid,
            measured_at=row.ts,
            source_tz=row.source_tz,
            attrib=0,  # an unconfirmed reading is excluded by its flag before this point
            value=row.value,
        )
    except (ValueError, TypeError):
        return None


def compute(observations: Iterable[ObservationRow]) -> Computed:
    """Every daily value of ``observations``, ascending by (metric, day).

    ``skipped`` counts observations that cannot be a reading (a value or zone ``Reading``
    refuses) and a second observation of a group already read for the same metric (only the
    first in canonical order is used; the normaliser stores none, so this is a safeguard).
    """
    by_metric: dict[str, list[Reading]] = defaultdict(list)
    skipped = 0
    for obs in sorted(observations, key=_order):
        if obs.metric_key not in METRICS or _EXCLUDING & set(obs.quality_flags):
            continue
        reading = _reading(obs)
        if reading is None:
            skipped += 1
            continue
        by_metric[obs.metric_key].append(reading)
    rows: list[DailyRow] = []
    for metric_key in sorted(by_metric):
        readings: list[Reading] = []
        seen: set[int] = set()
        for reading in sorted(by_metric[metric_key], key=lambda r: r.sort_key):
            if reading.grpid in seen:
                skipped += 1
                continue
            seen.add(reading.grpid)
            readings.append(reading)
        by_day: dict[date, list[Reading]] = defaultdict(list)
        for reading in readings:
            by_day[reading.local_date].append(reading)
        with localcontext(EXACT):
            rows.extend(
                _row(metric_key, value, by_day[value.day]) for value in daily_values(readings)
            )
    return Computed(tuple(rows), skipped)


def _order(obs: ObservationRow) -> tuple[object, ...]:
    return (
        obs.metric_key,
        obs.ts,
        obs.source_grpid,
        obs.source_type,
        -1 if obs.source_position is None else obs.source_position,
        obs.source_tz,
        obs.value,
    )


def fingerprint(observations: Iterable[ObservationRow]) -> bytes:
    """SHA-256 over the algorithm versions and every observation read, sorted (PLAN §5).

    Any change to a row's value, instant, zone, flags or source reference, and any row added or
    removed, changes it; the order the rows arrive in does not.
    """
    digest = hashlib.sha256()
    digest.update(f"{ALGO_VERSION}|{NORMALIZE_VERSION}\n".encode())
    lines = sorted(
        "|".join(
            (
                obs.metric_key,
                str(obs.source_grpid),
                str(obs.source_type),
                "-" if obs.source_position is None else str(obs.source_position),
                obs.ts.astimezone(UTC).isoformat(),
                obs.source_tz,
                format(obs.value, "f"),
                ",".join(sorted(obs.quality_flags)),
            )
        )
        for obs in observations
        if obs.metric_key in METRICS
    )
    for line in lines:
        digest.update(line.encode())
        digest.update(b"\n")
    return digest.digest()


__all__ = [
    "ALGO_VERSION",
    "KEYS",
    "Computed",
    "DailyRow",
    "ObservationRow",
    "compute",
    "fingerprint",
]
