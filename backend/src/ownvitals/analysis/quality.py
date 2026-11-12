"""Quality gates (PLAN §7.1 step 2). Pure and deterministic.

``assess`` takes the readings of one metric and returns every one of them, flagged:

* ``unconfirmed``: ``attrib=1`` (a household measure that may belong to someone else). Kept and
  shown, never used.
* ``implausible``: outside the metric's plausibility range. Kept and shown, never used.
* ``duplicate``: the same ``grpid`` seen again (the first reading in canonical order is the
  original). Only identical ``grpid`` values are duplicates: deliberate repeat measurements are
  distinct readings and are all kept.
* ``jump``: a change over max(absolute, relative) against an earlier usable reading within 24 h.
  Advisory: a flagged reading is still used.

Values are exact ``Decimal`` real values (``value_raw x 10^unit`` applied by ``real_value``),
never ``float``; instants are timezone-aware ``datetime`` values built from integer epoch
seconds (``from_epoch_seconds``), never from float seconds. The day a reading belongs to is the
calendar date in the reading's own ``source_tz``.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import (
    Context,
    Decimal,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    Rounded,
    localcontext,
)
from enum import StrEnum
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ALGO_VERSION = "quality/1"

UNCONFIRMED_ATTRIB = 1

# Every Decimal operation in this package runs in this context: 60 digits and a trap on any
# rounding, so a result is either exact or an error, never silently rounded.
EXACT = Context(prec=60, traps=[InvalidOperation, DivisionByZero, Overflow, Inexact, Rounded])

_MAX_DIGITS = 30
_MAX_MAGNITUDE = 30


class UnknownMetricError(KeyError):
    """The metric key has no row in ``METRICS``: add its range (with a source) first."""


class Flag(StrEnum):
    UNCONFIRMED = "unconfirmed"
    IMPLAUSIBLE = "implausible"
    DUPLICATE = "duplicate"
    JUMP = "jump"


# A reading carrying one of these is kept and shown but not used by anything downstream.
EXCLUDING = frozenset({Flag.UNCONFIRMED, Flag.IMPLAUSIBLE, Flag.DUPLICATE})


def real_value(value_raw: int, unit: int) -> Decimal:
    """``value x 10^unit`` exactly (Withings ``measures.value_raw`` and ``unit``)."""
    if isinstance(value_raw, bool) or not isinstance(value_raw, int):
        raise TypeError("value_raw must be an int: floats would round the value")
    if isinstance(unit, bool) or not isinstance(unit, int):
        raise TypeError("unit must be an int")
    with localcontext(EXACT):
        return Decimal(value_raw).scaleb(unit)


def from_epoch_seconds(seconds: int) -> datetime:
    """The instant of integer epoch seconds, timezone-aware (UTC). Floats are refused: a float
    just below a midnight can round across it and change the day a reading belongs to."""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise TypeError("epoch seconds must be an int")
    return datetime.fromtimestamp(seconds, UTC)


def zone(key: str) -> ZoneInfo:
    """The IANA zone ``key``; ``ValueError`` when it is not one."""
    try:
        return ZoneInfo(key)
    except (ValueError, ZoneInfoNotFoundError, OSError) as exc:
        raise ValueError(f"unknown time zone {key!r}") from exc


@dataclass(frozen=True, slots=True)
class Reading:
    """One distinct reading of one metric (one measure of one measure group).

    ``measured_at`` must be timezone-aware (it is normalised to UTC); ``source_tz`` is the IANA
    zone the reading was taken in (``measure_groups.source_tz``); ``value`` is the real value.
    """

    grpid: int
    measured_at: datetime
    source_tz: str
    attrib: int
    value: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.grpid, bool) or not isinstance(self.grpid, int):
            raise TypeError("grpid must be an int")
        if isinstance(self.attrib, bool) or not isinstance(self.attrib, int):
            raise TypeError("attrib must be an int")
        if not isinstance(self.measured_at, datetime) or self.measured_at.utcoffset() is None:
            raise ValueError("measured_at must be a timezone-aware datetime")
        object.__setattr__(self, "measured_at", self.measured_at.astimezone(UTC))
        zone(self.source_tz)
        if not isinstance(self.value, Decimal):
            raise TypeError("value must be a Decimal (see real_value): floats would round")
        if (
            not self.value.is_finite()
            or len(self.value.as_tuple().digits) > _MAX_DIGITS
            or abs(self.value.adjusted()) > _MAX_MAGNITUDE
        ):
            raise ValueError("value must be a finite Decimal of at most 30 digits")

    @property
    def local(self) -> datetime:
        """The reading in its own time zone (DST and fold resolved from the instant)."""
        return self.measured_at.astimezone(zone(self.source_tz))

    @property
    def local_date(self) -> date:
        """The day the reading belongs to: its calendar date in its own ``source_tz``."""
        return self.local.date()

    @property
    def sort_key(self) -> tuple[datetime, int, Decimal, str, int]:
        """Canonical order: results never depend on the order readings were passed in."""
        return (self.measured_at, self.grpid, self.value, self.source_tz, self.attrib)


@dataclass(frozen=True, slots=True)
class JumpRule:
    """A change of more than ``max(absolute, relative x |earlier value|)`` within ``window``."""

    absolute: Decimal
    relative: Decimal
    window: timedelta = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Plausibility range (inclusive) and jump rule of a metric, with the range's source."""

    key: str
    meastype: int
    unit: str
    low: Decimal
    high: Decimal
    bound: str
    jump: JumpRule | None = None


ENGINEERING = "engineering bound"
_NEURO = "findings §3: NeuropathyFeature score range 0-100"


def _spec(
    key: str,
    meastype: int,
    unit: str,
    low: str,
    high: str,
    bound: str = ENGINEERING,
    jump: JumpRule | None = None,
) -> MetricSpec:
    return MetricSpec(key, meastype, unit, Decimal(low), Decimal(high), bound, jump)


# Weight: PLAN §7.1 step 2, "flag jumps over max(2 kg, 3 %) within 24 h".
_WEIGHT_JUMP = JumpRule(absolute=Decimal(2), relative=Decimal("0.03"))

# The table. An "engineering bound" is ours: wide enough that no real reading falls outside, so
# only a broken scale, a unit mix-up or a typo is excluded. None is a clinical statement. Add a
# row (with its source) before a metric is analysed; a metric without a row is refused.
_SPECS = (
    _spec("weight", 1, "kg", "10", "300", jump=_WEIGHT_JUMP),
    _spec("fat_free_mass", 5, "kg", "5", "250"),
    _spec("fat_ratio", 6, "%", "1", "75"),
    _spec("fat_mass", 8, "kg", "0.5", "250"),
    _spec("diastolic_bp", 9, "mmHg", "20", "200"),
    _spec("systolic_bp", 10, "mmHg", "40", "300"),
    _spec("heart_pulse", 11, "bpm", "20", "250"),
    _spec("spo2", 54, "%", "50", "100"),
    _spec("muscle_mass", 76, "kg", "5", "200"),
    _spec("hydration", 77, "kg", "5", "150"),
    _spec("bone_mass", 88, "kg", "0.3", "10"),
    _spec("pulse_wave_velocity", 91, "m/s", "1", "30"),
    _spec("qrs_duration", 135, "ms", "20", "300"),
    _spec("pr_interval", 136, "ms", "40", "500"),
    _spec("qt_interval", 137, "ms", "100", "800"),
    _spec("qtc_interval", 138, "ms", "200", "800"),
    _spec("vascular_age", 155, "years", "10", "120"),
    _spec("nerve_health_score", 167, "score", "0", "100", bound=_NEURO),
    _spec("extracellular_water", 168, "kg", "1", "50"),
    _spec("intracellular_water", 169, "kg", "2", "100"),
    _spec("visceral_fat", 170, "index", "0", "100"),
    _spec("nerve_response_score", 196, "score", "0", "100", bound=_NEURO),
    _spec("metabolic_age", 227, "years", "5", "150"),
    _spec("esc", 229, "µS", "0", "200"),
)

METRICS = MappingProxyType({spec.key: spec for spec in _SPECS})


def metric_spec(metric_key: str) -> MetricSpec:
    try:
        return METRICS[metric_key]
    except KeyError:
        raise UnknownMetricError(metric_key) from None


@dataclass(frozen=True, slots=True)
class AssessedReading:
    reading: Reading
    flags: frozenset[Flag]

    @property
    def usable(self) -> bool:
        return not (self.flags & EXCLUDING)


@dataclass(frozen=True, slots=True)
class QualityResult:
    """Every input reading, in canonical order, with its flags."""

    metric_key: str
    readings: tuple[AssessedReading, ...]
    algo_version: str = ALGO_VERSION

    @property
    def usable(self) -> tuple[Reading, ...]:
        return tuple(a.reading for a in self.readings if a.usable)

    def flagged(self, flag: Flag) -> tuple[Reading, ...]:
        return tuple(a.reading for a in self.readings if flag in a.flags)

    def count(self, flag: Flag) -> int:
        return sum(1 for a in self.readings if flag in a.flags)


def _is_jump(rule: JumpRule, earlier: Reading, later: Reading) -> bool:
    limit = max(rule.absolute, rule.relative * abs(earlier.value))
    return abs(later.value - earlier.value) > limit


def _jump_indices(ordered: list[Reading], eligible: list[int], rule: JumpRule) -> set[int]:
    """Indices (into ``ordered``) of readings that jump against an earlier eligible one."""
    jumps: set[int] = set()
    for pos, later_index in enumerate(eligible):
        later = ordered[later_index]
        for earlier_pos in range(pos - 1, -1, -1):
            earlier = ordered[eligible[earlier_pos]]
            if later.measured_at - earlier.measured_at > rule.window:
                break
            if _is_jump(rule, earlier, later):
                jumps.add(later_index)
                break
    return jumps


def assess(metric_key: str, readings: Iterable[Reading]) -> QualityResult:
    """Flag every reading of ``metric_key``; ``UnknownMetricError`` for a metric without a row."""
    spec = metric_spec(metric_key)
    ordered = sorted(readings, key=lambda r: r.sort_key)
    flags: list[set[Flag]] = [set() for _ in ordered]
    seen: set[int] = set()
    for index, reading in enumerate(ordered):
        if reading.grpid in seen:
            flags[index].add(Flag.DUPLICATE)
        seen.add(reading.grpid)
        if reading.attrib == UNCONFIRMED_ATTRIB:
            flags[index].add(Flag.UNCONFIRMED)
        if not spec.low <= reading.value <= spec.high:
            flags[index].add(Flag.IMPLAUSIBLE)
    if spec.jump is not None:
        eligible = [i for i, f in enumerate(flags) if not f & EXCLUDING]
        with localcontext(EXACT):
            for index in _jump_indices(ordered, eligible, spec.jump):
                flags[index].add(Flag.JUMP)
    return QualityResult(
        metric_key=metric_key,
        readings=tuple(
            AssessedReading(reading, frozenset(f))
            for reading, f in zip(ordered, flags, strict=True)
        ),
    )
