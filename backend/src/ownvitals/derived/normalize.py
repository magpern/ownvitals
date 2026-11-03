"""Normalisation (PLAN §7.1 step 1 and D4): Withings measures into ``observations`` rows.

Pure and deterministic: no database, clock or randomness. ``normalise`` takes every measure of
one owner (one ``SourceMeasure`` per ``measures`` row, with its group's instant, zone and
``attrib``) and returns every observation, with provenance left to the caller. The same input
gives the same rows in the same order, whatever order it arrived in (``test_derived_normalize``
holds that).

* The value is ``value_raw x 10^unit`` exactly (``analysis.quality.real_value``); the raw value,
  the unit and the conversion are kept beside it.
* The metric key comes from ``metric_keys`` (an unknown type is kept as ``meastype_<n>``;
  segmental types 173 to 175 are keyed on their position).
* The quality flags are #37's ``assess()`` for a metric it has a row for. Every other metric gets
  only ``unconfirmed`` for ``attrib = 1``. Flags are stored, never used to drop a row here.
* ``ts`` and ``source_ts`` are the group's instant (``measure_groups.measured_at``, UTC), and
  ``source_tz`` its zone. The day a reading belongs to is decided later from these two.
* Rows the table cannot hold (a value beyond ±10^12, an unknown zone, an instant outside the
  partition range) are counted in ``Skipped``, never silently lost and never logged with a value.
  A second copy of the same ``(metric, group, type, position)`` is a duplicate row of the
  source; the first in canonical order is kept.

``fingerprint`` is the provenance hash of the exact input rows (PLAN §5): SHA-256 over the
sorted group ids, payload hashes and deleted marks, and the algorithm versions.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

from ownvitals.analysis.quality import (
    ALGO_VERSION as QUALITY_VERSION,
)
from ownvitals.analysis.quality import (
    METRICS,
    UNCONFIRMED_ATTRIB,
    Flag,
    Reading,
    assess,
    real_value,
    zone,
)
from ownvitals.derived.metric_keys import conversion, metric_key
from ownvitals.ingest.when import in_range

ALGO_VERSION: Final = "normalize/1"
# ``observations.value`` is checked to this magnitude.
MAX_MAGNITUDE: Final = Decimal(10) ** 12
UNIT_MIN: Final = -30
UNIT_MAX: Final = 30
# Provenance names both versions: a change of the quality gates changes the stored flags.
PINNED_QUALITY_VERSION: Final = "quality/1"


@dataclass(frozen=True, slots=True)
class SourceMeasure:
    """One ``measures`` row with the facts of its ``measure_groups`` row."""

    grpid: int
    measured_at: datetime
    source_tz: str
    attrib: int | None
    type: int
    value_raw: int
    unit: int
    position: int | None


@dataclass(frozen=True, slots=True)
class Observation:
    metric_key: str
    ts: datetime
    source_ts: datetime
    source_tz: str
    value: Decimal
    source_value_raw: int
    source_unit: int
    conversion: str
    quality_flags: tuple[str, ...]
    source_grpid: int
    source_type: int
    source_position: int | None

    @property
    def natural_key(self) -> tuple[str, int, int, int | None, datetime]:
        """The unique key of ``observations`` (``observations_source_key``)."""
        return (
            self.metric_key,
            self.source_grpid,
            self.source_type,
            self.source_position,
            self.ts,
        )


@dataclass(frozen=True, slots=True)
class Skipped:
    """Counts only: why a measure produced no row."""

    out_of_range: int = 0  # value beyond ±10^12, or a unit outside -30..30
    bad_zone: int = 0
    outside_partitions: int = 0  # instant before 2009 or three months past ``now``
    duplicate_rows: int = 0

    def total(self) -> int:
        return self.out_of_range + self.bad_zone + self.outside_partitions + self.duplicate_rows


@dataclass(frozen=True, slots=True)
class Normalised:
    observations: tuple[Observation, ...]
    skipped: Skipped


def _canonical(m: SourceMeasure) -> tuple[object, ...]:
    return (
        m.measured_at,
        m.grpid,
        m.type,
        -1 if m.position is None else m.position,
        m.position is None,
        m.value_raw,
        m.unit,
        m.source_tz,
        -1 if m.attrib is None else m.attrib,
    )


def normalise(measures: Iterable[SourceMeasure], *, now: datetime) -> Normalised:
    """Every observation of ``measures``, in a canonical order. ``now`` bounds the partitions
    (an instant must be in [2009-01-01, now + 3 months), as the ingest requires)."""
    if QUALITY_VERSION != PINNED_QUALITY_VERSION:
        raise RuntimeError("the quality gates changed: bump ALGO_VERSION and the pin")
    out_of_range = bad_zone = outside = duplicates = 0
    seen: set[tuple[str, int, int, int | None, datetime]] = set()
    pending: dict[str, list[tuple[Reading, SourceMeasure, str]]] = {}
    for m in sorted(measures, key=_canonical):
        if not UNIT_MIN <= m.unit <= UNIT_MAX:
            out_of_range += 1
            continue
        value = real_value(m.value_raw, m.unit)
        if abs(value) > MAX_MAGNITUDE:
            out_of_range += 1
            continue
        if not in_range(m.measured_at, now):
            outside += 1
            continue
        try:
            zone(m.source_tz)
        except ValueError:
            bad_zone += 1
            continue
        key = metric_key(m.type, m.position)
        identity = (key.key, m.grpid, m.type, m.position, m.measured_at)
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        reading = Reading(
            grpid=m.grpid,
            measured_at=m.measured_at,
            source_tz=m.source_tz,
            attrib=0 if m.attrib is None else m.attrib,
            value=value,
        )
        pending.setdefault(key.key, []).append((reading, m, conversion(m.unit, key.unit)))
    rows: list[Observation] = []
    for name in sorted(pending):
        entries = pending[name]
        flags = _flags(name, entries)
        for reading, m, how in entries:
            rows.append(
                Observation(
                    metric_key=name,
                    ts=reading.measured_at,
                    source_ts=reading.measured_at,
                    source_tz=m.source_tz,
                    value=reading.value,
                    source_value_raw=m.value_raw,
                    source_unit=m.unit,
                    conversion=how,
                    quality_flags=tuple(sorted(flags[id(reading)])),
                    source_grpid=m.grpid,
                    source_type=m.type,
                    source_position=m.position,
                )
            )
    return Normalised(
        observations=tuple(rows),
        skipped=Skipped(out_of_range, bad_zone, outside, duplicates),
    )


def _flags(name: str, entries: list[tuple[Reading, SourceMeasure, str]]) -> dict[int, set[str]]:
    """The quality flags of one metric's readings, keyed by the reading's ``id``."""
    flags: dict[int, set[str]] = {id(reading): set() for reading, _, _ in entries}
    if name in METRICS:
        for assessed in assess(name, [reading for reading, _, _ in entries]).readings:
            flags[id(assessed.reading)] = {str(flag.value) for flag in assessed.flags}
        return flags
    for reading, _, _ in entries:
        if reading.attrib == UNCONFIRMED_ATTRIB:
            flags[id(reading)].add(Flag.UNCONFIRMED.value)
    return flags


@dataclass(frozen=True, slots=True)
class GroupRef:
    """What the fingerprint reads of one measure group: its id, payload hash and deleted mark."""

    id: UUID
    payload_hash: bytes
    deleted: bool


def fingerprint(groups: Iterable[GroupRef]) -> bytes:
    """SHA-256 over the algorithm versions and the sorted ``(id, payload hash, deleted)`` of the
    groups the observations are made from (PLAN §5). A deleted group is marked, not left out."""
    digest = hashlib.sha256()
    digest.update(f"{ALGO_VERSION}|{QUALITY_VERSION}\n".encode())
    for ref in sorted(groups, key=lambda g: (str(g.id), g.payload_hash, g.deleted)):
        mark = "d" if ref.deleted else "l"
        digest.update(f"{ref.id}|{ref.payload_hash.hex()}|{mark}\n".encode())
    return digest.digest()


__all__ = [
    "ALGO_VERSION",
    "GroupRef",
    "Normalised",
    "Observation",
    "Skipped",
    "SourceMeasure",
    "fingerprint",
    "normalise",
]
