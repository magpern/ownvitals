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

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import partial

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.policy import InsufficientData
from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules import body, heart, nerve, system
from ownvitals.analysis.rules.core import (
    SPECS,
    Band,
    Finding,
    RuleResult,
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
from ownvitals.derived.evaluate_fingerprint import fingerprint
from ownvitals.derived.evaluate_model import (
    EXPERIMENTAL_RULES,
    FOOT_PAIRS,
    NERVE_KEYS,
    OWNED_RULES,
    Q1_RULES,
    READING_KEYS,
    REPLAYED_RULES,
    RULES_VERSION,
    SEGMENT_KEY,
    SEGMENT_TYPE,
    EpisodeRow,
    Evaluation,
    Inputs,
    Q1Result,
    Q1State,
    canonical,
    json_safe,
    rule_version,
    subject_ok,
)
from ownvitals.derived.metric_keys import position_name

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


__all__ = [
    "EXPERIMENTAL_RULES",
    "FOOT_PAIRS",
    "NERVE_KEYS",
    "OWNED_RULES",
    "Q1_RULES",
    "READING_KEYS",
    "REPLAYED_RULES",
    "RULES_VERSION",
    "SEGMENT_TYPE",
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
