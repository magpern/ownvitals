"""Rule evaluation: the v1 rules over one owner's data, as episodes (#187; ADR 0010 §1 and §4;
PLAN §7.1 steps 6 and 7). Pure: no database, clock or randomness; ``today`` is an input.

``evaluate(inputs)`` runs every v1 rule of ``analysis.rules`` (``SPECS``) that applies:

* **Replayed rules** (B1 to B6, C1, C1x, C2, N2, N3, N5). Each (rule, subject) is evaluated on
  every one of its evaluation days in order (ADR 0010 §1: the days its trigger has data), and the
  results are folded into episodes by ``advance_simple`` or, for the trend-test rules,
  ``advance_persistent`` (§4). A rule at day *d* reads only data up to *d*, so replaying the whole
  history gives the episodes an incremental evaluation in data order would have given, and a
  recompute after a data revision re-derives them (PLAN §5). The writer keeps ``first_seen`` and
  the origin of a stored episode with the same rule, subject and reference day (§4).
* **Q1** (system) has no history to replay: each part is evaluated on ``today`` from the state
  records read now, and the writer applies ``advance_simple`` to the stored active episode.

**Experimental rules** (B1, B3, C1x; tier ``experimental``) are evaluated only when
``inputs.experimental`` is true (D9, PLAN §7.2: the owner-only switch); otherwise they are not
computed at all and no episode of theirs is returned.

Activity, sleep and workout observations are not read: no v1 rule uses them (ADR 0010 §8). A
later rule that does reads the activity day as the stream stored it (the observation's local
noon, #230), never by re-assigning it.

``fingerprint(inputs)`` is the provenance hash of everything ``evaluate`` reads.
"""

import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import partial
from typing import Any, Final

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.policy import InsufficientData
from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules import body, heart, nerve, system
from ownvitals.analysis.rules.core import (
    ALGO_VERSION,
    SPECS,
    Band,
    Finding,
    Provenance,
    RuleResult,
    Tier,
)
from ownvitals.analysis.rules.episodes import (
    Episode,
    Step,
    Transition,
    TrendState,
    advance_persistent,
    advance_simple,
    origin,
)
from ownvitals.analysis.rules.system import ConnectionState, DeviceState, StreamState
from ownvitals.derived.metric_keys import position_name

RULES_VERSION: Final = ALGO_VERSION  # "rules/1": the shared bands, confidence and episodes
EXPERIMENTAL_RULES: Final = frozenset(
    rule for rule, spec in SPECS.items() if spec.tier is Tier.EXPERIMENTAL
)
Q1_RULES: Final = tuple(sorted(rule for rule in SPECS if rule.startswith("Q1:")))
REPLAYED_RULES: Final = tuple(sorted(rule for rule in SPECS if rule not in Q1_RULES))
# Every rule this writer stores; another writer's rules (E1 to E6, M5) are never touched.
OWNED_RULES: Final = tuple(sorted(SPECS))

NERVE_KEYS: Final = nerve.NERVE_KEYS
# N5's per-foot pairs (ADR 0010 §3): the normaliser keys 158, 159, 197 and 198 as meastype_<n>.
FOOT_PAIRS: Final[Mapping[nerve.FootPair, tuple[str, str]]] = {
    "158_159": ("meastype_158", "meastype_159"),
    "197_198": ("meastype_197", "meastype_198"),
}
SEGMENT_TYPE: Final = 175  # segmental muscle mass: B6 reads it by position
SEGMENT_KEY: Final = "segmental_muscle_mass"
# The reading-level inputs besides the daily values: B3's water, N2's nerve readings (guided is
# attrib 15), N5's foot types, Q1's weigh-ins.
READING_KEYS: Final = tuple(
    sorted(
        {
            "weight",
            "extracellular_water",
            "intracellular_water",
            *NERVE_KEYS,
            *(key for pair in FOOT_PAIRS.values() for key in pair),
        }
    )
)
_SUBJECT_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/-")


def rule_version(rule_id: str) -> str:
    """``findings.rule_version``: the shared rules version and the rule's own, ``rules/1.1``."""
    return f"{RULES_VERSION}.{SPECS[rule_id].version}"


@dataclass(frozen=True, slots=True)
class Q1State:
    """What Q1 reads now: the ``measures`` capability, the connection, the devices of the latest
    ``getdevice`` and each stream's completeness (ADR 0010 §7)."""

    weight_capability: str | None = None
    connection: ConnectionState | None = None
    devices: tuple[DeviceState, ...] = ()
    streams: tuple[StreamState, ...] = ()


@dataclass(frozen=True, slots=True)
class Inputs:
    """One owner's evaluation inputs. ``daily`` and ``provenance`` are per metric key (the stored
    ``metric_daily`` rows and their source groups and ``jump`` flags); ``readings`` are the usable
    readings of ``READING_KEYS`` (one per group, the zone already the day zone); ``segments`` are
    the usable segmental muscle readings with their positions."""

    today: date
    experimental: bool
    daily: Mapping[str, tuple[DailyValue, ...]] = field(default_factory=dict)
    provenance: Mapping[str, Mapping[date, Provenance]] = field(default_factory=dict)
    readings: Mapping[str, tuple[Reading, ...]] = field(default_factory=dict)
    segments: tuple[body.Segment, ...] = ()
    q1: Q1State = field(default_factory=Q1State)


@dataclass(frozen=True, slots=True)
class EpisodeRow:
    """One episode as the writer stores it. ``first_day`` and ``last_day`` are the evaluation days
    of its first and latest firing, ``ended_day`` the evaluation that ended it (``None``: still
    active). ``origin`` is ``origin(window_end, today)``: the writer uses it only for an episode
    it writes for the first time (§4: computed once, never changed)."""

    rule_id: str
    rule_version: str
    tier: str
    severity: str
    confidence: str
    subject: str
    metric_keys: tuple[str, ...]
    reference_day: date
    window_start: date
    window_end: date
    first_day: date
    last_day: date
    ended_day: date | None
    origin: str
    evidence: dict[str, Any]
    data_quality: dict[str, Any]

    @property
    def status(self) -> str:
        return "active" if self.ended_day is None else "ended"

    @property
    def match_key(self) -> tuple[str, str, date]:
        return (self.rule_id, self.subject, self.reference_day)


@dataclass(frozen=True, slots=True)
class Q1Result:
    """Today's evaluation of one Q1 part and subject: ``finding`` when it fires."""

    rule_id: str
    subject: str
    finding: Finding | None
    metric_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Evaluation:
    episodes: tuple[EpisodeRow, ...]
    q1: tuple[Q1Result, ...]
    evaluated: Mapping[str, int]  # evaluations per rule id (counts only)


def json_safe(value: Any) -> Any:
    """``value`` as JSON the database accepts: a non-finite float becomes ``None``."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    return value


def canonical(value: Any) -> str:
    """One text per JSON value, to compare a computed document with a stored one."""
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"))


def subject_ok(subject: str) -> bool:
    """``findings.subject``'s check: 1 to 160 of letters, digits and ``_.:/-``."""
    return 0 < len(subject) <= 160 and set(subject) <= _SUBJECT_OK


# --- replay ------------------------------------------------------------------------------------


type Evaluate = Callable[[date], RuleResult]


@dataclass(frozen=True, slots=True)
class _Plan:
    rule_id: str
    subject: str
    metric_keys: tuple[str, ...]
    days: tuple[date, ...]
    run: Evaluate


def _row(
    episode: Episode, metric_keys: tuple[str, ...], today: date, ended: date | None
) -> EpisodeRow:
    latest = episode.latest
    return EpisodeRow(
        rule_id=episode.rule_id,
        rule_version=rule_version(episode.rule_id),
        tier=latest.tier.value,
        severity=latest.severity.value,
        confidence=latest.confidence.value,
        subject=episode.subject,
        metric_keys=metric_keys,
        reference_day=episode.reference_day,
        window_start=episode.window_start,
        window_end=episode.window_end,
        first_day=episode.first_seen,
        last_day=episode.last_seen,
        ended_day=ended,
        origin=origin(episode.window_end, today).value,
        evidence=json_safe(latest.evidence),
        data_quality=json_safe(latest.data_quality),
    )


def replay(plan: _Plan, today: date) -> tuple[list[EpisodeRow], int]:
    """Every episode of one (rule, subject): its evaluation days in order, through §4's state
    transitions. Returns the episodes (ended ones first, in order) and the evaluation count."""
    persistent = SPECS[plan.rule_id].persistent
    rows: list[EpisodeRow] = []
    active: Episode | None = None
    state = TrendState()
    for day in plan.days:
        result = plan.run(day)
        step: Step
        if persistent:
            state, step = advance_persistent(state, result, today=today)
        else:
            step = advance_simple(active, result, today=today)
        if step.transition is Transition.ENDED and step.ended is not None:
            rows.append(_row(step.ended, plan.metric_keys, today, day))
        active = step.episode
    if active is not None:
        rows.append(_row(active, plan.metric_keys, today, None))
    return rows, len(plan.days)


def _days(values: Iterable[DailyValue]) -> tuple[date, ...]:
    return tuple(sorted({v.day for v in values}))


def _reading_days(readings: Iterable[Reading]) -> tuple[date, ...]:
    return tuple(sorted({r.local_date for r in readings}))


def _plans(inputs: Inputs) -> list[_Plan]:
    daily, prov, readings = inputs.daily, inputs.provenance, inputs.readings
    plans: list[_Plan] = []

    def series(key: str) -> tuple[DailyValue, ...]:
        return daily.get(key, ())

    def add(
        rule: str, subject: str, keys: tuple[str, ...], days: Sequence[date], run: Evaluate
    ) -> None:
        if rule in EXPERIMENTAL_RULES and not inputs.experimental:
            return  # not computed at all without the owner-only switch (D9)
        if days:
            plans.append(_Plan(rule, subject, keys, tuple(days), run))

    weight, ecw, pulse = series("weight"), series("extracellular_water"), series("heart_pulse")
    water = ("extracellular_water", "intracellular_water")
    add("B1", "weight", ("weight", water[0]), _days(weight),
        partial(body.b1, weight, ecw, provenance=prov))  # fmt: skip
    add("B2", "weight", ("weight",), _days(weight), partial(body.b2, weight, provenance=prov))
    add("B3", "water_ratio", water, _days(ecw),
        partial(body.b3, readings.get(water[0], ()), readings.get(water[1], ())))  # fmt: skip
    muscle = series("muscle_mass")
    add("B4", "muscle_mass", ("muscle_mass",), _days(muscle),
        partial(body.b4, muscle, provenance=prov))  # fmt: skip
    for key in ("metabolic_age", "visceral_fat"):
        add("B5", key, (key,), _days(series(key)),
            partial(body.b5, series(key), metric_key=key, provenance=prov))  # fmt: skip
    for pair in ("arms", "legs"):
        names = tuple(sorted(f"{SEGMENT_KEY}:{position_name(p)}" for p in body.PAIRS[pair]))
        pair_days = sorted({d for d, _ in body.pair_indices(inputs.segments, pair).values()})
        add("B6", pair, names, pair_days, partial(body.b6, inputs.segments, pair=pair))
    c1_bands: dict[date, Band | InsufficientData] = {}
    add("C1", "heart_pulse", ("heart_pulse",), _days(pulse),
        partial(heart.c1, pulse, provenance=prov, bands=c1_bands))  # fmt: skip
    add("C1x", "heart_pulse", ("heart_pulse",), _days(pulse),
        partial(heart.c1x, pulse, provenance=prov))  # fmt: skip
    vascular = series("vascular_age")
    add("C2", "vascular_age", ("vascular_age",), _days(vascular),
        partial(heart.c2, vascular, provenance=prov))  # fmt: skip
    for key in NERVE_KEYS:
        found = readings.get(key, ())
        guided = [r for r in found if r.attrib == nerve.GUIDED_ATTRIB]
        routine = [r for r in found if r.attrib != nerve.GUIDED_ATTRIB]
        add("N2:guided", key, (key,), _reading_days(guided),
            partial(nerve.n2_guided, found, metric_key=key))  # fmt: skip
        memo: dict[date, Band | InsufficientData] = {}
        n2 = partial(nerve.n2_routine, found, metric_key=key, provenance=prov, bands=memo)
        add("N2:routine", key, (key,), _reading_days(routine), n2)
        add("N3", key, (key,), _days(series(key)),
            partial(nerve.n3, series(key), metric_key=key, provenance=prov))  # fmt: skip
    for foot, (first_key, second_key) in FOOT_PAIRS.items():
        first, second = readings.get(first_key, ()), readings.get(second_key, ())
        add("N5", f"nerve_foot_{foot}", (first_key, second_key), _foot_days(first, second),
            partial(nerve.n5, first, second, pair=foot))  # fmt: skip
    return plans


def _foot_days(first: Sequence[Reading], second: Sequence[Reading]) -> tuple[date, ...]:
    """The days with a measure group holding both members of a foot pair (N5's trigger)."""
    seconds = {r.grpid for r in second}
    return _reading_days(r for r in first if r.grpid in seconds)


# --- Q1 ----------------------------------------------------------------------------------------


def evaluate_q1(inputs: Inputs) -> list[Q1Result]:
    """Today's Q1 results, one per part and subject evaluated (ADR 0010 §7)."""
    today, state = inputs.today, inputs.q1
    results: list[Q1Result] = []

    def keep(result: RuleResult, keys: tuple[str, ...] = ()) -> None:
        if subject_ok(result.subject):
            fired = result if isinstance(result, Finding) else None
            results.append(Q1Result(result.rule_id, result.subject, fired, keys))

    weight = inputs.readings.get("weight", ())
    keep(system.q1_no_weigh_in(weight, state.weight_capability, today), ("weight",))
    for device in sorted(state.devices, key=lambda d: d.hash_deviceid):
        keep(system.q1_low_battery(device, today))
    if state.connection is not None:
        keep(system.q1_reconnect(state.connection, today))
    for stream in sorted(state.streams, key=lambda s: s.stream):
        keep(system.q1_import_incomplete(stream, today))
    return results


def evaluate(inputs: Inputs) -> Evaluation:
    """Every v1 rule that applies to ``inputs`` (see the module docstring)."""
    episodes: list[EpisodeRow] = []
    counts: dict[str, int] = {}
    for plan in _plans(inputs):
        rows, evaluated = replay(plan, inputs.today)
        episodes.extend(rows)
        counts[plan.rule_id] = counts.get(plan.rule_id, 0) + evaluated
    q1 = evaluate_q1(inputs)
    for result in q1:
        counts[result.rule_id] = counts.get(result.rule_id, 0) + 1
    episodes.sort(key=lambda e: (e.rule_id, e.subject, e.first_day, e.reference_day))
    return Evaluation(tuple(episodes), tuple(q1), counts)


# --- provenance --------------------------------------------------------------------------------


def _reading_line(key: str, r: Reading) -> str:
    return "|".join(
        (key, str(r.grpid), r.measured_at.isoformat(), r.source_tz, str(r.attrib), str(r.value))
    )


def fingerprint(inputs: Inputs) -> bytes:
    """SHA-256 over the rule versions, ``today``, the switch and every input ``evaluate`` reads,
    in a canonical order (PLAN §5). Any change to any of them changes it."""
    lines: list[str] = [
        RULES_VERSION,
        ",".join(f"{rule}={spec.version}" for rule, spec in sorted(SPECS.items())),
        f"today={inputs.today.isoformat()}",
        f"experimental={inputs.experimental}",
    ]
    for key in sorted(inputs.daily):
        for v in sorted(inputs.daily[key], key=lambda v: v.day):
            prov = inputs.provenance.get(key, {}).get(v.day, Provenance())
            lines.append(
                "|".join(
                    (
                        "daily",
                        key,
                        v.day.isoformat(),
                        v.method.value,
                        str(v.value),
                        str(v.n),
                        str(v.minimum),
                        str(v.maximum),
                        str(v.mad),
                        ",".join(str(g) for g in prov.groups),
                        str(prov.jump),
                    )
                )
            )
    for key in sorted(inputs.readings):
        lines.extend(sorted(_reading_line(key, r) for r in inputs.readings[key]))
    lines.extend(sorted(_reading_line(f"segment:{s.position}", s.reading) for s in inputs.segments))
    q1 = inputs.q1
    lines.append(f"q1|capability|{q1.weight_capability}")
    if q1.connection is not None:
        lines.append(f"q1|connection|{q1.connection.status}")
    lines.extend(sorted(f"q1|device|{d.hash_deviceid}|{d.battery}" for d in q1.devices))
    lines.extend(
        sorted(
            f"q1|stream|{s.stream}|{s.capability}|{s.complete}|{s.importing}" for s in q1.streams
        )
    )
    digest = hashlib.sha256()
    for line in lines:
        digest.update(line.encode())
        digest.update(b"\n")
    return digest.digest()


__all__ = [
    "EXPERIMENTAL_RULES",
    "OWNED_RULES",
    "Q1_RULES",
    "READING_KEYS",
    "REPLAYED_RULES",
    "RULES_VERSION",
    "EpisodeRow",
    "Evaluation",
    "Inputs",
    "Q1Result",
    "Q1State",
    "canonical",
    "evaluate",
    "evaluate_q1",
    "fingerprint",
    "json_safe",
    "rule_version",
]
