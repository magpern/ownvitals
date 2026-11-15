"""The pure rule evaluation (#187): replay into episodes, persistence, origin, the experimental
switch, the reading-level rules, Q1, the fingerprint and the diff the writer applies. No
database. **Synthetic** daily values and readings only (made-up numbers, ``synthetic_`` ids).
"""

import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules.body import Segment
from ownvitals.analysis.rules.core import SPECS, Confidence, Finding, NoFinding, Tier, make_finding
from ownvitals.analysis.rules.heart import c1
from ownvitals.analysis.rules.system import ConnectionState, DeviceState, StreamState
from ownvitals.derived.evaluate import (
    EXPERIMENTAL_RULES,
    OWNED_RULES,
    Q1_RULES,
    READING_KEYS,
    Inputs,
    Q1State,
    _Plan,  # pyright: ignore[reportPrivateUsage]
    evaluate,
    fingerprint,
    json_safe,
    replay,
    rule_version,
)
from ownvitals.derived.findings_store import content, day_start, diff_rows

START = date(2026, 1, 1)
TODAY = date(2026, 6, 1)


def dv(day: date, value: float | str) -> DailyValue:
    v = Decimal(str(value))
    return DailyValue(day, v, DailyMethod.MORNING, 1, v, v, Decimal(0))


def pulses(values: Mapping[int, float], days: int = 150) -> tuple[DailyValue, ...]:
    """A synthetic heart pulse every day: 60/62 alternating, ``values`` by day index."""
    return tuple(
        dv(START + timedelta(days=i), values.get(i, 60 if i % 2 else 62)) for i in range(days)
    )


def reading(grpid: int, day: date, value: float | str, *, attrib: int = 0) -> Reading:
    when = datetime(day.year, day.month, day.day, 6, tzinfo=UTC)
    return Reading(grpid, when, "UTC", attrib, Decimal(str(value)))


def inputs(**kw: Any) -> Inputs:
    return Inputs(today=kw.pop("today", TODAY), experimental=kw.pop("experimental", False), **kw)


# --- the rule tables ----------------------------------------------------------------------------


def test_the_writer_owns_exactly_the_v1_rules_and_q1_is_not_replayed() -> None:
    assert set(OWNED_RULES) == set(SPECS)
    assert set(Q1_RULES) == {r for r in SPECS if SPECS[r].tier is Tier.SYSTEM}
    assert {"B1", "B3", "C1x", "E4"} == EXPERIMENTAL_RULES
    assert {r for r in OWNED_RULES if r.startswith("E")} == {"E1", "E2", "E3", "E4", "E6"}  # #191
    assert "E5" not in OWNED_RULES  # deferred (ADR 0006 section 12): never touched
    assert rule_version("C1x") == "rules/1.1"


def test_no_activity_sleep_or_workout_observation_is_an_input() -> None:
    """No v1 rule reads a stream field (ADR 0010 §8); a later one reads the stored activity day."""
    for key in READING_KEYS:
        assert not key.startswith(("activity_", "sleep_", "workout_")), key


# --- replay into episodes (ADR 0010 §4) ---------------------------------------------------------


def test_c1_episodes_open_extend_and_end_with_their_origin() -> None:
    raised = {40: 71, 41: 72, 42: 71, 43: 73, 146: 71, 147: 72, 148: 71, 149: 72}
    result = evaluate(inputs(daily={"heart_pulse": pulses(raised)}, today=START + timedelta(150)))
    rows = [e for e in result.episodes if e.rule_id == "C1"]
    assert [(e.reference_day, e.window_end, e.status, e.origin) for e in rows] == [
        (START + timedelta(40), START + timedelta(43), "ended", "backfill"),
        (START + timedelta(146), START + timedelta(149), "active", "live"),
    ]
    first, last = rows
    assert (first.first_day, first.last_day, first.ended_day) == (
        START + timedelta(42),  # opened on the run's third day
        START + timedelta(43),  # extended once
        START + timedelta(44),  # ended by the first non-firing evaluation
    )
    assert first.evidence["values"] == [71.0, 72.0, 71.0, 73.0]  # the latest firing's
    assert first.confidence == "medium"  # a run of 4: R is high only from 6
    assert last.ended_day is None
    assert result.evaluated["C1"] == 150


def test_the_band_memo_gives_the_same_results_as_no_memo() -> None:
    daily = pulses({40: 71, 41: 72, 42: 71, 90: 49})
    memo: dict[Any, Any] = {}
    for i in range(30, 100):
        day = START + timedelta(i)
        assert c1(daily, day, bands=memo) == c1(daily, day)


def finding_on(day: date, *, rule: str = "B5", subject: str = "visceral_fat") -> Finding:
    return make_finding(
        rule,
        subject,
        day,
        window_start=day - timedelta(days=89),
        window_end=day,
        reference_day=day,
        confidence=Confidence.HIGH,
        evidence={"synthetic": True},
        data_quality=None,
    )


def scripted(true_days: set[int], days: list[int]) -> _Plan:
    def run(day: date) -> Any:
        index = (day - START).days
        if index in true_days:
            return finding_on(day)
        return NoFinding("B5", "visceral_fat", day)

    return _Plan(
        "B5", "visceral_fat", ("visceral_fat",), tuple(START + timedelta(d) for d in days), run
    )


def test_a_trend_rule_opens_after_21_days_and_6_evaluations_and_ends_after_7() -> None:
    days = list(range(1, 60))
    true = set(range(1, 30))
    rows, count = replay(scripted(true, days), START + timedelta(59))
    assert count == len(days)
    (episode,) = rows
    assert episode.first_day == START + timedelta(21)  # the 21st calendar day of the run
    assert episode.reference_day == START + timedelta(1)  # the run's first day
    assert episode.window_start == START + timedelta(1) - timedelta(days=89)
    assert episode.ended_day == START + timedelta(36)  # 7 failing calendar days after day 29
    assert episode.origin == "backfill"
    # Weekly evaluations open on the 6th (ADR 0010 test spec, persistence 3).
    weekly = [1, 8, 15, 22, 29, 36]
    rows, _ = replay(scripted(set(weekly), weekly), START + timedelta(36))
    assert [(r.first_day, r.status, r.origin) for r in rows] == [
        (START + timedelta(36), "active", "live")
    ]


# --- the experimental switch (D9) ---------------------------------------------------------------


def test_experimental_rules_are_not_computed_without_the_switch() -> None:
    daily = {
        "heart_pulse": pulses({60: 49, 61: 48, 62: 49}),
        "weight": tuple(dv(START + timedelta(i), 80) for i in range(150)),
        "extracellular_water": tuple(dv(START + timedelta(i), 17) for i in range(150)),
    }
    readings = {"qtc_interval": (reading(900, START, 510),)}  # above every QTc band: E4
    off = evaluate(inputs(daily=daily, readings=readings))
    assert not set(off.evaluated) & EXPERIMENTAL_RULES
    assert not [e for e in off.episodes if e.tier == "experimental"]
    on = evaluate(inputs(daily=daily, readings=readings, experimental=True))
    assert set(on.evaluated) >= EXPERIMENTAL_RULES
    (c1x,) = [e for e in on.episodes if e.rule_id == "C1x"]
    assert (c1x.tier, c1x.reference_day, c1x.status) == (
        "experimental", START + timedelta(60), "ended",
    )  # fmt: skip


# --- reading-level rules ------------------------------------------------------------------------


def test_n5_b6_b3_and_n2_are_evaluated_on_their_trigger_days() -> None:
    days = [START + timedelta(7 * i) for i in range(10)]
    first = tuple(reading(100 + i, d, 50) for i, d in enumerate(days))
    second = tuple(reading(100 + i, d, 38) for i, d in enumerate(days))  # SI 0.2727
    left = [Segment(3, reading(200 + i, d, "3.00")) for i, d in enumerate(days)]
    right = [Segment(2, reading(200 + i, d, "2.70")) for i, d in enumerate(days)]
    nerve = tuple(reading(300 + i, d, 70, attrib=15 if i == 9 else 0) for i, d in enumerate(days))
    result = evaluate(
        inputs(
            readings={"meastype_158": first, "meastype_159": second, "nerve_health_score": nerve},
            segments=(*left, *right),
            today=days[-1],
        )
    )
    assert result.evaluated["N5"] == 10
    assert result.evaluated["B6"] == 10  # arms only: no leg pair
    assert result.evaluated["N2:guided"] == 1
    assert result.evaluated["N2:routine"] == 9
    assert "B3" not in result.evaluated  # experimental, and no ECW daily value
    n5 = [e for e in result.episodes if e.rule_id == "N5"]
    assert [(e.subject, e.status, e.metric_keys) for e in n5] == [
        ("nerve_foot_158_159", "active", ("meastype_158", "meastype_159"))
    ]
    b6 = [e for e in result.episodes if e.rule_id == "B6"]
    assert [(e.subject, e.metric_keys) for e in b6] == [
        ("arms", ("segmental_muscle_mass:left_arm", "segmental_muscle_mass:right_arm"))
    ]


# --- Q1 -----------------------------------------------------------------------------------------


def test_q1_reads_todays_state_and_skips_a_subject_the_table_cannot_hold() -> None:
    state = Q1State(
        weight_capability="supported",
        connection=ConnectionState("needs_reconnect"),
        devices=(
            DeviceState("synthetic_low", "low"),
            DeviceState("synthetic_fine", "high"),
            DeviceState("synthetic bad id", "low"),  # a space: not a subject the table accepts
        ),
        streams=(StreamState("sleep_summary", "supported", complete=False, importing=False),),
    )
    weight = (reading(1, TODAY - timedelta(days=20), 80),)
    result = evaluate(inputs(q1=state, readings={"weight": weight}))
    fired = sorted((r.rule_id, r.subject) for r in result.q1 if r.finding is not None)
    assert fired == [
        ("Q1:import_incomplete", "sleep_summary"),
        ("Q1:low_battery", "synthetic_low"),
        ("Q1:no_weigh_in", "user"),
        ("Q1:reconnect", "connection"),
    ]
    assert ("Q1:low_battery", "synthetic_fine") in {(r.rule_id, r.subject) for r in result.q1}
    assert all(r.subject != "synthetic bad id" for r in result.q1)


# --- provenance ---------------------------------------------------------------------------------


def test_the_fingerprint_covers_every_input_and_not_the_order() -> None:
    daily = {"heart_pulse": pulses({}, 20)}
    base = inputs(daily=daily, readings={"nerve_health_score": (reading(1, START, 70),)})
    digest = fingerprint(base)
    assert len(digest) == 32
    reordered = replace(base, daily={"heart_pulse": tuple(reversed(daily["heart_pulse"]))})
    assert fingerprint(reordered) == digest
    changes = [
        replace(base, today=TODAY + timedelta(days=1)),
        replace(base, experimental=True),
        replace(base, daily={"heart_pulse": pulses({3: 61}, 20)}),
        replace(base, readings={"nerve_health_score": (reading(1, START, 70, attrib=15),)}),
        replace(base, q1=Q1State(connection=ConnectionState("needs_reconnect"))),
        replace(base, q1=Q1State(devices=(DeviceState("synthetic_d", "low"),))),
        replace(base, segments=(Segment(3, reading(9, START, "3.0")),)),
    ]
    assert len({fingerprint(c) for c in changes} | {digest}) == len(changes) + 1


def test_json_safe_turns_non_finite_numbers_into_null() -> None:
    assert json_safe({"a": math.nan, "b": [math.inf, 1.5], "c": (2,)}) == {
        "a": None,
        "b": [None, 1.5],
        "c": [2],
    }


# --- the diff the writer applies -----------------------------------------------------------------


def stored_from(params: dict[str, Any], **changes: Any) -> SimpleNamespace:
    row = {**params, "id": uuid4(), "first_seen": params["first_seen"], **changes}
    return SimpleNamespace(_mapping=row, **row)


def test_the_diff_matches_by_rule_subject_and_reference_day() -> None:
    raised = {40: 71, 41: 72, 42: 71, 146: 71, 147: 72, 148: 71}
    today = START + timedelta(149)
    result = evaluate(inputs(daily={"heart_pulse": pulses(raised)}, today=today))
    owner, digest = uuid4(), b"\x01" * 32
    first = diff_rows([], result, today, owner, 1, digest)
    assert len(first.inserts) == 2
    assert sorted(p["notify_state"] for p in first.inserts) == ["none", "pending"]
    # Stored as written: nothing to do, even with the stored origin and first_seen differing.
    stored = [stored_from(p, origin="backfill", first_seen=day_start(START)) for p in first.inserts]
    again = diff_rows(stored, result, today, owner, 2, b"\x02" * 32)
    assert (again.inserts, again.updates, again.ends, again.deletes) == ([], [], [], [])
    # A stored episode no recompute produces is deleted; a changed one is updated in place.
    gone = stored_from(first.inserts[0], reference_day=START, subject="heart_pulse")
    changed = stored_from(first.inserts[1], confidence="high")
    diff = diff_rows([gone, changed], result, today, owner, 2, b"\x02" * 32)
    assert [d["id"] for d in diff.deletes] == [gone.id]
    assert [u["id"] for u in (*diff.updates, *diff.ends)] == [changed.id]
    assert len(diff.inserts) == 1
    assert content(changed) != content(first.inserts[1])
