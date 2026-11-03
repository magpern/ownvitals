"""Q1 (ADR 0010 §7 "System"; test specification "Rules"). Synthetic data only."""

from datetime import date, timedelta

import pytest
from nerve_builders import nreading

from ownvitals.analysis.rules.core import Confidence, Finding, NoFinding, Severity, Tier
from ownvitals.analysis.rules.episodes import Transition, advance_simple
from ownvitals.analysis.rules.system import (
    ConnectionState,
    DeviceState,
    StreamState,
    q1_import_incomplete,
    q1_low_battery,
    q1_no_weigh_in,
    q1_reconnect,
)

TODAY = date(2026, 11, 3)


def weigh(days_ago: int, *, attrib: int = 0):
    return nreading(TODAY - timedelta(days=days_ago), 80.0, attrib=attrib)


def test_q1_no_weigh_in_14_days_ago_fires_and_13_does_not() -> None:
    found = q1_no_weigh_in([weigh(14)], "supported", TODAY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("Q1:no_weigh_in", Tier.SYSTEM, "user")
    assert (found.confidence, found.severity) == (Confidence.HIGH, Severity.INFO)
    assert found.reference_day == found.window_start == TODAY - timedelta(days=14)
    assert found.evidence["state"]["days_since"] == 14
    assert isinstance(q1_no_weigh_in([weigh(13)], "supported", TODAY), NoFinding)


def test_q1_no_weigh_in_uses_the_latest_usable_weigh_in() -> None:
    found = q1_no_weigh_in([weigh(40), weigh(20), weigh(30)], "supported", TODAY)
    assert isinstance(found, Finding)
    assert found.evidence["state"]["days_since"] == 20


def test_q1_an_unconfirmed_attrib_1_reading_yesterday_does_not_reset_it() -> None:
    data = [weigh(20), weigh(1, attrib=1)]
    found = q1_no_weigh_in(data, "supported", TODAY)
    assert isinstance(found, Finding)
    assert found.evidence["state"]["days_since"] == 20


def test_q1_no_weigh_in_never_weighed_or_capability_not_supported_gets_nothing() -> None:
    assert isinstance(q1_no_weigh_in([], "supported", TODAY), NoFinding)
    assert isinstance(q1_no_weigh_in([weigh(1, attrib=1)], "supported", TODAY), NoFinding)
    for capability in (None, "unsupported", "not_authorized", "not_available", "unknown"):
        assert isinstance(q1_no_weigh_in([weigh(30)], capability, TODAY), NoFinding)


def test_q1_no_weigh_in_ends_on_the_first_weigh_in() -> None:
    first = q1_no_weigh_in([weigh(15)], "supported", TODAY)
    step = advance_simple(None, first, today=TODAY)
    assert step.transition is Transition.OPENED
    assert step.notify
    later = TODAY + timedelta(days=1)
    again = q1_no_weigh_in([weigh(15)], "supported", later)
    assert advance_simple(step.episode, again, today=later).transition is Transition.UPDATED
    weighed = q1_no_weigh_in([weigh(15), nreading(later, 80.0)], "supported", later)
    assert advance_simple(step.episode, weighed, today=later).transition is Transition.ENDED


def test_q1_low_battery_fires_per_device_for_low_only() -> None:
    low = DeviceState("hash-a", "low")
    found = q1_low_battery(low, TODAY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("Q1:low_battery", Tier.SYSTEM, "hash-a")
    assert (found.confidence, found.severity) == (Confidence.HIGH, Severity.INFO)
    assert found.evidence["state"] == {"hash_deviceid": "hash-a", "battery": "low"}
    assert q1_low_battery(DeviceState("hash-b", "low"), TODAY).subject == "hash-b"  # type: ignore[union-attr]


@pytest.mark.parametrize("battery", ["medium", "high", None, "LOW", "critical", ""])
def test_q1_low_battery_medium_high_missing_and_undocumented_values_do_not_fire(
    battery: str | None,
) -> None:
    assert isinstance(q1_low_battery(DeviceState("hash-a", battery), TODAY), NoFinding)


def test_q1_needs_reconnect_fires_urgent() -> None:
    found = q1_reconnect(ConnectionState("needs_reconnect"), TODAY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("Q1:reconnect", Tier.SYSTEM, "connection")
    assert (found.confidence, found.severity) == (Confidence.HIGH, Severity.URGENT)
    for status in ("connected", "disconnected", "", "unknown"):
        assert isinstance(q1_reconnect(ConnectionState(status), TODAY), NoFinding)


def test_q1_import_incomplete_fires_and_importing_or_complete_does_not() -> None:
    found = q1_import_incomplete(StreamState("measures", "supported", False, False), TODAY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == (
        "Q1:import_incomplete",
        Tier.SYSTEM,
        "measures",
    )
    assert (found.confidence, found.severity) == (Confidence.HIGH, Severity.INFO)
    importing = StreamState("measures", "supported", False, True)
    assert isinstance(q1_import_incomplete(importing, TODAY), NoFinding)
    complete = StreamState("measures", "supported", True, False)
    assert isinstance(q1_import_incomplete(complete, TODAY), NoFinding)


@pytest.mark.parametrize(
    "capability", [None, "unsupported", "not_authorized", "not_available", "unknown", "bogus"]
)
def test_q1_a_missing_capability_or_a_not_supported_stream_never_fires_import_incomplete(
    capability: str | None,
) -> None:
    stream = StreamState("heart", capability, False, False)
    assert isinstance(q1_import_incomplete(stream, TODAY), NoFinding)


def test_q1_import_incomplete_is_per_stream() -> None:
    one = q1_import_incomplete(StreamState("measures", "supported", False, False), TODAY)
    two = q1_import_incomplete(StreamState("sleep", "supported", False, False), TODAY)
    assert isinstance(one, Finding)
    assert isinstance(two, Finding)
    assert {one.subject, two.subject} == {"measures", "sleep"}


def test_q1_every_finding_has_tier_system() -> None:
    results = [
        q1_no_weigh_in([weigh(20)], "supported", TODAY),
        q1_low_battery(DeviceState("d", "low"), TODAY),
        q1_reconnect(ConnectionState("needs_reconnect"), TODAY),
        q1_import_incomplete(StreamState("s", "supported", False, False), TODAY),
    ]
    assert all(isinstance(r, Finding) and r.tier is Tier.SYSTEM for r in results)
