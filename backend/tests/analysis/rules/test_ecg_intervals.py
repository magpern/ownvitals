"""E4 and E6 (ADR 0006 section 11; ADR 0010 section 7 and its test specification, "E6" and
"Owner-only flag"), and the trigger provenance they hand to the read-time overlay. **Synthetic**
readings, daily values and estimates only (AGENTS.md).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from rules_builders import DAY, alternating, constant, dv, preceding, reading

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.analysis.rules.core import (
    Confidence,
    Finding,
    Insufficient,
    NoFinding,
    Provenance,
    Severity,
    Tier,
)
from ownvitals.analysis.rules.ecg_intervals import PR_KEY, QRS_KEY, QTC_KEY, Sex, e4, e6
from ownvitals.derived.evaluate import Inputs, evaluate
from ownvitals.ecg.overlay import RecordingKey, ecg_gate_overlay
from ownvitals.ecg.pipeline import MODULES, PIPELINE_VERSION

SYNTHETIC = True


def e4_on(key: str, value: float | str, sex: Sex | None = None) -> object:
    return e4([reading(DAY, value, grpid=77)], DAY, metric_key=key, sex=sex)


# --- E4 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "sex", "band"),
    [
        (QTC_KEY, "450.1", "male", "qtc_male"),
        (QTC_KEY, "460.1", "female", "qtc_female"),
        (QTC_KEY, "500.1", None, "qtc_any"),
        (QTC_KEY, "500.1", "female", "qtc_any"),
        (PR_KEY, "200.1", None, "pr"),
        (QRS_KEY, "120", None, "qrs"),
    ],
)
def test_e4_fires_above_its_band(key: str, value: str, sex: Sex | None, band: str) -> None:
    found = e4_on(key, value, sex)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("E4", Tier.EXPERIMENTAL, key)
    assert (found.confidence, found.severity) == (Confidence.HIGH, Severity.ATTENTION)
    assert found.evidence["band"] == band
    assert found.evidence["trigger_groups"] == [77]  # the reading's own group
    assert (found.window_start, found.window_end, found.reference_day) == (DAY, DAY, DAY)


@pytest.mark.parametrize(
    ("key", "value", "sex"),
    [
        (QTC_KEY, "450", "male"),  # "more than"
        (QTC_KEY, "460", "female"),
        (QTC_KEY, "455", "female"),  # above the men's band, not the women's
        (QTC_KEY, "500", None),
        (QTC_KEY, "480", None),  # sex unknown: only the 500 ms band
        (PR_KEY, "200", None),
        (QRS_KEY, "119.9", None),
    ],
)
def test_e4_does_not_fire_at_or_below_its_band(key: str, value: str, sex: Sex | None) -> None:
    assert isinstance(e4_on(key, value, sex), NoFinding)


def test_e4_pr_and_qrs_do_not_depend_on_the_sex() -> None:
    for sex in (None, "male", "female"):
        assert isinstance(e4_on(PR_KEY, "210", sex), Finding)  # pyright: ignore[reportArgumentType]
        assert isinstance(e4_on(QRS_KEY, "125", sex), Finding)  # pyright: ignore[reportArgumentType]


def test_e4_every_reading_above_the_band_is_a_trigger() -> None:
    data = [
        reading(DAY, "130", grpid=1),
        reading(DAY, "110", grpid=2, hour=9),
        reading(DAY, "140", grpid=3, hour=10),
        reading(DAY - timedelta(days=1), "150", grpid=4),
    ]
    found = e4(data, DAY, metric_key=QRS_KEY, sex=None)
    assert isinstance(found, Finding)
    assert found.evidence["trigger_groups"] == [1, 3]
    assert (found.evidence["value_ms"], found.evidence["readings_above"]) == (140.0, 2)


def test_e4_is_evaluated_only_on_days_with_its_reading() -> None:
    with pytest.raises(ValueError, match="no reading"):
        e4([reading(DAY - timedelta(days=1), "130")], DAY, metric_key=QRS_KEY, sex=None)
    with pytest.raises(ValueError, match="E4 reads only"):
        e4([reading(DAY, "130")], DAY, metric_key="heart_pulse", sex=None)


# --- E6 (ADR 0010 test specification, "E6") -----------------------------------------------------


def qtc_on(value: float | str, *, base: list[DailyValue] | None = None) -> list[DailyValue]:
    """A constant 90-day QTc baseline of 400 ms before DAY (finding high 401: the minimum
    half-width 1.0), then ``value`` on DAY."""
    return [
        *(constant(DAY - timedelta(days=90), 90, 400) if base is None else base),
        dv(DAY, value),
    ]


def test_e6_a_value_just_above_the_finding_threshold_fires_and_equal_does_not() -> None:
    found = e6(qtc_on("401.1"), DAY, metric_key=QTC_KEY)
    assert isinstance(found, Finding)
    assert (found.rule_id, found.tier, found.subject) == ("E6", Tier.TREND, QTC_KEY)
    assert (found.window_start, found.window_end, found.reference_day) == (DAY, DAY, DAY)
    assert found.evidence["finding_band"]["high"] == 401.0
    assert found.evidence["finding_band"]["k"] == 2.5
    assert found.confidence is Confidence.HIGH  # 90 days: twice BASELINE's 10 and more
    assert isinstance(e6(qtc_on("401"), DAY, metric_key=QTC_KEY), NoFinding)
    assert isinstance(e6(qtc_on("380"), DAY, metric_key=QTC_KEY), NoFinding)  # only "above"


def test_e6_uses_the_flat_baselines_mad_band() -> None:
    base = alternating(DAY - timedelta(days=90), 90, 400, 4)  # half-width 2.5 * 1.4826 * 4
    high = 400 + 2.5 * 1.4826 * 4
    assert isinstance(e6(qtc_on(high + 0.01, base=base), DAY, metric_key=QTC_KEY), Finding)
    assert isinstance(e6(qtc_on(high - 0.01, base=base), DAY, metric_key=QTC_KEY), NoFinding)


def test_e6_below_the_baseline_policy_is_insufficient_data() -> None:
    nine = [dv(DAY - timedelta(days=10 * i + 1), 400) for i in range(9)]  # 9 days with data
    found = e6([*nine, dv(DAY, 450)], DAY, metric_key=QTC_KEY)
    assert isinstance(found, Insufficient)
    narrow = preceding(DAY, [400] * 10)  # 10 days spanning 10, under the 21-day span
    assert isinstance(e6([*narrow, dv(DAY, 450)], DAY, metric_key=QTC_KEY), Insufficient)


def test_e6_qtc_and_qrs_are_separate_subjects() -> None:
    qrs = [*constant(DAY - timedelta(days=90), 90, 90), dv(DAY, 95)]
    inputs = Inputs(
        today=DAY,
        experimental=False,
        daily={QTC_KEY: tuple(qtc_on("420")), QRS_KEY: tuple(qrs)},
    )
    e6s = [e for e in evaluate(inputs).episodes if e.rule_id == "E6"]
    assert sorted((e.subject, e.metric_keys, e.origin) for e in e6s) == [
        (QRS_KEY, (QRS_KEY,), "live"),
        (QTC_KEY, (QTC_KEY,), "live"),
    ]
    with pytest.raises(ValueError, match="E6 reads only"):
        e6(qtc_on("420"), DAY, metric_key=PR_KEY)


def test_e6_trigger_provenance_is_the_daily_values_groups() -> None:
    prov = {QTC_KEY: {DAY: Provenance((31, 32)), DAY - timedelta(days=1): Provenance((30,))}}
    found = e6(qtc_on("420"), DAY, metric_key=QTC_KEY, provenance=prov)
    assert isinstance(found, Finding)
    assert found.evidence["trigger_groups"] == [31, 32]  # never the baseline's groups


# --- the owner-only flag (ADR 0010 test specification) -------------------------------------------


def test_e4_is_neither_computed_nor_returned_without_the_switch() -> None:
    readings = {QTC_KEY: (reading(DAY, "510"),), QRS_KEY: (reading(DAY, "130"),)}
    off = evaluate(Inputs(today=DAY, experimental=False, readings=readings))
    assert "E4" not in off.evaluated
    assert not [e for e in off.episodes if e.rule_id == "E4"]
    on = evaluate(Inputs(today=DAY, experimental=True, readings=readings, sex="male"))
    assert sorted(e.subject for e in on.episodes if e.rule_id == "E4") == [QRS_KEY, QTC_KEY]
    assert {e.tier for e in on.episodes if e.rule_id == "E4"} == {"experimental"}


def test_e6_does_not_depend_on_the_switch() -> None:
    inputs = Inputs(today=DAY, experimental=False, daily={QTC_KEY: tuple(qtc_on("420"))})
    assert [e.rule_id for e in evaluate(inputs).episodes] == ["E6"]


# --- the trigger reaches the overlay (ADR 0006 test specification, "The trigger is the value's
# provenance") ------------------------------------------------------------------------------------

FP = "e" * 64
CONNECTION = uuid4()


def strip(result: str) -> RecordingKey:
    """A synthetic recording with a current ``estimated`` of gate ``result``."""
    return RecordingKey(
        id=uuid4(),
        connection=CONNECTION,
        recorded_at=datetime(2026, 6, 30, 7, tzinfo=UTC),
        device_id="synthetic_device",
        model_id=None,
        fingerprint=FP,
        estimated={
            "algo_version": PIPELINE_VERSION,
            "modules": dict(MODULES),
            "input_fingerprint": FP,
            "sqi": {"result": result, "reasons": [], "windows": []},
        },
    )


def test_a_morning_values_failing_strip_hides_e6_whatever_the_retakes() -> None:
    morning = DailyValue(
        DAY, Decimal(420), DailyMethod.MORNING, 3, Decimal(410), Decimal(430), Decimal(1)
    )
    daily = [*constant(DAY - timedelta(days=90), 90, 400), morning]
    found = e6(daily, DAY, metric_key=QTC_KEY, provenance={QTC_KEY: {DAY: Provenance((50,))}})
    assert isinstance(found, Finding)
    triggers = found.evidence["trigger_groups"]
    links = {50: (strip("fail"),), 51: (strip("pass"),), 52: (strip("pass"),)}  # 51, 52: retakes
    linked = {g: links[g] for g in triggers}
    assert ecg_gate_overlay("E6", linked).hidden
    linked = {g: {50: (strip("pass"),)}[g] for g in triggers}
    assert not ecg_gate_overlay("E6", linked).hidden
