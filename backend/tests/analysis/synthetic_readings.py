"""Seeded generator of synthetic, irregular weight series for the analysis tests.

SYNTHETIC: every number, time and identifier produced here is invented. There is no real health
data in it or anywhere else in the repository (AGENTS.md).

``make_series(seed)`` plants, deterministically from the seed:

* gaps: a fixed three-week hole and random missed days (``miss_probability``);
* clustered repeats: several distinct readings (own ``grpid``) a few minutes apart;
* travel: two trips whose readings carry another ``source_tz`` and local clock times;
* DST days: the series covers both 2026 European changes, with readings on those days,
  including right after local midnight;
* ``attrib=1`` readings: another household member (about 55 kg) and look-alikes (about 82 kg);
* jumps: readings 4.5 kg from the reading before them, two hours later;
* implausible readings (0.8 kg and 650 kg) and re-delivered duplicates (a repeated ``grpid``).

``truth`` names what was planted so tests can assert on it without re-deriving it.
"""

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from ownvitals.analysis.quality import Reading, real_value

SYNTHETIC = True

HOME_TZ = "Europe/Stockholm"
TRIPS = (
    (date(2026, 6, 10), date(2026, 6, 16), "America/New_York"),
    (date(2026, 8, 20), date(2026, 8, 25), "Asia/Tokyo"),
)
HOLE = (date(2026, 4, 20), date(2026, 5, 10))
DST_DAYS = (date(2026, 3, 29), date(2026, 10, 25))
START = date(2026, 3, 1)
DAYS = 240
FIRST_GRPID = 9_000_000_001


@dataclass(frozen=True)
class Truth:
    household: frozenset[int]  # grpids planted with attrib=1
    jumps: frozenset[int]  # grpids planted as jumps
    implausible: frozenset[int]  # grpids planted outside the weight range
    duplicated: frozenset[int]  # grpids delivered twice


@dataclass(frozen=True)
class SyntheticSeries:
    seed: int
    readings: tuple[Reading, ...]
    truth: Truth


def _tz_for(day: date) -> str:
    for first, last, key in TRIPS:
        if first <= day <= last:
            return key
    return HOME_TZ


def _kg(weight: float) -> Decimal:
    return real_value(round(weight * 100), -2)  # raw value and unit, as Withings stores it


def make_series(
    seed: int, *, days: int = DAYS, miss_probability: float = 0.15, start: date = START
) -> SyntheticSeries:
    rng = random.Random(seed)  # noqa: S311 - seeded for reproducible tests, not for security
    readings: list[Reading] = []
    household: set[int] = set()
    jumps: set[int] = set()
    implausible: set[int] = set()
    next_grpid = FIRST_GRPID

    def add(
        day: date, hour: int, minute: int, weight: float, *, attrib: int = 0, after: int = 0
    ) -> int:
        nonlocal next_grpid
        tz_key = _tz_for(day)
        local = datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo(tz_key))
        instant = local.astimezone(UTC) + timedelta(minutes=after)
        grpid, next_grpid = next_grpid, next_grpid + 1
        readings.append(Reading(grpid, instant, tz_key, attrib, _kg(weight)))
        return grpid

    mornings: list[tuple[date, int, int, float]] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        weight = 82.0 - 0.01 * offset + rng.gauss(0, 0.3)
        wanted = day in DST_DAYS or not (
            HOLE[0] <= day <= HOLE[1] or rng.random() < miss_probability
        )
        if rng.random() < 0.08:
            add(day, rng.randrange(5, 10), rng.randrange(60), 55 + rng.gauss(0, 0.4), attrib=1)
            household.add(next_grpid - 1)
        if rng.random() < 0.04:
            add(day, rng.randrange(5, 10), rng.randrange(60), weight + 0.2, attrib=1)
            household.add(next_grpid - 1)
        if not wanted:
            continue
        hour, minute = rng.randrange(5, 10), rng.randrange(60)
        add(day, hour, minute, weight)
        mornings.append((day, hour, minute, weight))
        if rng.random() < 0.12:  # clustered repeats, a few minutes apart
            for step in range(1, rng.randrange(2, 4)):
                add(day, hour, minute, weight + rng.gauss(0, 0.15), after=2 * step)
        if rng.random() < 0.2:
            add(day, rng.randrange(18, 22), rng.randrange(60), weight + 0.4)
        if day in DST_DAYS or rng.random() < 0.03:  # just after local midnight
            add(day, 0, rng.randrange(5, 50), weight + 0.1)

    for day, hour, minute, weight in rng.sample(mornings, k=min(6, len(mornings))):
        sign = 1 if rng.random() < 0.5 else -1
        jumps.add(add(day, hour, minute, weight + sign * 4.5, after=120))
    for day, hour, minute, _ in rng.sample(mornings, k=min(3, len(mornings))):
        implausible.add(add(day, hour, minute, rng.choice((0.8, 650.0)), after=30))
    duplicated = {r.grpid for r in rng.sample(readings, k=min(5, len(readings)))}
    readings.extend([r for r in readings if r.grpid in duplicated])
    return SyntheticSeries(
        seed,
        tuple(readings),
        Truth(
            frozenset(household), frozenset(jumps), frozenset(implausible), frozenset(duplicated)
        ),
    )
