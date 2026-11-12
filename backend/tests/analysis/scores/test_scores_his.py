"""HIS (ADR 0012 §2). The band table below is SYNTHETIC and registered in this module only: the
product ships none."""

import ast
import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from scores_builders import DAY, SYNTHETIC

from ownvitals.analysis.scores import Status
from ownvitals.analysis.scores import his as his_mod
from ownvitals.analysis.scores.his import CriterionInput, criterion_score, evaluate_his
from ownvitals.analysis.scores.his_inputs import (
    afib_ecg_count,
    bmi,
    high_bp_ratio,
    median_criterion,
    vascular_age_gap,
    weekly_active_minutes,
)
from ownvitals.analysis.scores.his_table import REGISTERED, InvalidBandTable, parse_table

assert SYNTHETIC


def _row(criterion: str, vertical: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "criterion": criterion,
        "vertical": vertical,
        "sex": "any",
        "age_min": 18,
        "age_max": 99,
        "unit": "u",
    }
    return {**base, **kw}


LOW = {"perfect": 10, "good": 8, "average": 5, "bad": 0}
HIGH = {"perfect": 20, "good": 25, "average": 30, "bad": 40}
DOC = {
    "table_id": "synthetic_table",
    "version": "0",
    "source": "synthetic test table",
    "rows": [
        _row("bmi", "body", low=LOW, high=HIGH),
        _row("night_hr", "heart", high={"perfect": 50, "good": 60, "average": 70, "bad": 90}),
        _row(
            "daily_steps",
            "activity",
            low={"perfect": 9000, "good": 7000, "average": 4000, "bad": 0},
        ),
        _row("sleep_ahi", "sleep", high={"perfect": 5, "good": 10, "average": 15, "bad": 30}),
    ],
}


def _raw(doc: dict[str, object]) -> bytes:
    return json.dumps(doc, sort_keys=True).encode()


def _reg(raw: bytes) -> dict[str, str]:
    return {"synthetic_table": hashlib.sha256(raw).hexdigest()}


def _table(doc: dict[str, object] = DOC):
    raw = _raw(doc)
    return parse_table(raw, registered=_reg(raw))


def _in(value: float | None, status: Status = Status.COMPUTED) -> CriterionInput:
    return CriterionInput(status, value, "u", reason=None if value is not None else "x")


def test_only_the_shipped_table_is_registered() -> None:
    assert set(REGISTERED) == {"withings_card_v1"}  # pinned by test_scores_his_withings_card
    with pytest.raises(InvalidBandTable, match="not_registered"):
        parse_table(_raw(DOC))


def test_no_table_is_not_set() -> None:
    res = evaluate_his(None, inputs={}, day=DAY, birth_year=1980, sex="male")
    assert res.status is Status.NOT_SET
    assert res.value is None
    assert res.components["reason"] == "no_band_table"
    bad = evaluate_his(None, table_error="x", inputs={}, day=DAY, birth_year=1980, sex="male")
    assert bad.components["reason"] == "invalid_band_table"


def test_sha_must_match() -> None:
    with pytest.raises(InvalidBandTable, match="not_registered"):
        parse_table(_raw(DOC) + b" ", registered=_reg(_raw(DOC)))


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"low": {"perfect": 1, "good": 2, "average": 3, "bad": 4}}, "low_not_monotone"),
        ({"high": {"perfect": 4, "good": 3, "average": 2, "bad": 1}}, "high_not_monotone"),
        ({"low": {"perfect": 30, "good": 8, "average": 5, "bad": 0}}, "perfect_range_inverted"),
        ({"criterion": "mood"}, "unknown_criterion"),
        ({"age_min": 60, "age_max": 20}, "bad_row"),
        ({"low": {"perfect": 1}}, "bad_row"),
    ],
)
def test_invalid_rows(patch: dict[str, object], code: str) -> None:
    doc = json.loads(json.dumps(DOC))
    doc["rows"][0].update(patch)
    with pytest.raises(InvalidBandTable, match=code):
        _table(doc)


def test_overlapping_rows_and_garbage() -> None:
    doc = json.loads(json.dumps(DOC))
    doc["rows"].append(_row("bmi", "body", sex="male", high=HIGH))
    with pytest.raises(InvalidBandTable, match="overlapping_rows"):
        _table(doc)
    with pytest.raises(InvalidBandTable, match="bad_document"):
        parse_table(b"not json")


def test_criterion_scores_anchors() -> None:
    row = _table().rows[0]
    assert criterion_score(15, row) == 100
    assert criterion_score(10, row) == 100
    assert criterion_score(8, row) == 75
    assert criterion_score(5, row) == 50
    assert criterion_score(0, row) == 0
    assert criterion_score(-3, row) == 0
    assert criterion_score(6.5, row) == pytest.approx(62.5)
    assert criterion_score(25, row) == 75
    assert criterion_score(40, row) == 0
    assert criterion_score(80, row) == 0


def test_aggregate_and_integer_rounding() -> None:
    inputs = {
        "bmi": _in(15),  # 100
        "night_hr": _in(60),  # 75
        "daily_steps": _in(4000),  # 50
        "sleep_ahi": _in(5),  # 100
    }
    res = evaluate_his(_table(), inputs=inputs, day=DAY, birth_year=1980, sex=None)
    assert res.status is Status.COMPUTED
    assert res.components["verticals"] == {"body": 100, "heart": 75, "activity": 50, "sleep": 100}
    assert res.value == 81.0  # 81.25 -> 81
    assert res.components["disclaimer_key"] == "score.his.not_medical"


def test_needs_two_verticals() -> None:
    res = evaluate_his(_table(), inputs={"bmi": _in(15)}, day=DAY, birth_year=1980, sex=None)
    assert res.status is Status.INSUFFICIENT_DATA
    assert res.value is None
    two = evaluate_his(
        _table(), inputs={"bmi": _in(15), "night_hr": _in(60)}, day=DAY, birth_year=1980, sex=None
    )
    assert two.value == 88.0  # 87.5 -> 88 (half up)


def test_missing_input_is_not_zero() -> None:
    inputs = {
        "bmi": _in(15),
        "night_hr": _in(60),
        "daily_steps": _in(None, Status.INSUFFICIENT_DATA),
    }
    res = evaluate_his(_table(), inputs=inputs, day=DAY, birth_year=1980, sex=None)
    assert res.components["criteria"]["daily_steps"]["status"] == "insufficient_data"
    assert res.components["verticals"]["activity"] is None
    assert res.value == 88.0


def test_birth_year_unset_is_not_set() -> None:
    res = evaluate_his(_table(), inputs={"bmi": _in(15)}, day=DAY, birth_year=None, sex="male")
    assert res.status is Status.NOT_SET
    assert res.components["reason"] == "birth_year_unset"


def test_row_by_age_and_sex() -> None:
    doc = json.loads(json.dumps(DOC))
    doc["rows"][0].update(age_max=39)
    doc["rows"].append(
        _row("bmi", "body", age_min=40, age_max=99, sex="female", low=LOW, high=HIGH)
    )
    table = _table(doc)
    young = his_mod.pick_row(table, "bmi", age=30, sex=None)
    assert young[0] == 0  # type: ignore[index]
    assert his_mod.pick_row(table, "bmi", age=50, sex="female")[0] == 4  # type: ignore[index]
    assert his_mod.pick_row(table, "bmi", age=50, sex=None) == "sex_unset"
    assert his_mod.pick_row(table, "bmi", age=50, sex="male") == "no_band_row"
    assert his_mod.pick_row(table, "bmi", age=None, sex="male") == "birth_year_unset"


def test_deterministic() -> None:
    inputs = {"bmi": _in(15), "night_hr": _in(60)}
    a = evaluate_his(_table(), inputs=inputs, day=DAY, birth_year=1980, sex=None)
    b = evaluate_his(
        _table(), inputs=dict(reversed(list(inputs.items()))), day=DAY, birth_year=1980, sex=None
    )
    assert a == b


# --- inputs -----------------------------------------------------------------------------------


def _pts(n: int, v: float, step: int = 1) -> list[tuple]:  # type: ignore[type-arg]
    return [(DAY - timedelta(days=i * step), v) for i in range(n)]


def test_median_criterion() -> None:
    assert median_criterion([], DAY, days=30, minimum=4, unit="u").status is Status.NOT_AVAILABLE
    short = median_criterion(_pts(3, 1.0), DAY, days=30, minimum=4, unit="u")
    assert short.status is Status.INSUFFICIENT_DATA
    assert short.value is None
    ok = median_criterion(_pts(5, 2.0), DAY, days=30, minimum=4, unit="u")
    assert ok.status is Status.COMPUTED
    assert ok.value == 2.0
    old = median_criterion(_pts(6, 2.0, step=10), DAY, days=30, minimum=4, unit="u")
    assert old.data_quality["n"] == 3


def test_bmi_and_vascular_gap() -> None:
    assert bmi(_pts(5, 80.0), None, DAY).reason == "no_height"
    assert bmi(_pts(5, 81.0), 1.8, DAY).value == pytest.approx(25.0)
    gap = vascular_age_gap(_pts(3, 50.0, 10), 45, DAY)
    assert gap.value == 5.0


def test_afib_and_bp() -> None:
    rec = [(DAY - timedelta(days=i), i == 0) for i in range(4)]
    assert afib_ecg_count(rec, DAY, ecg_supported=False).status is Status.NOT_SUPPORTED
    assert afib_ecg_count(rec[:2], DAY, ecg_supported=True).status is Status.INSUFFICIENT_DATA
    assert afib_ecg_count(rec, DAY, ecg_supported=True).value == 1.0
    groups = [(DAY - timedelta(days=i), 140.0 if i < 3 else 120.0, 70.0) for i in range(6)]
    assert high_bp_ratio(groups, DAY).value == 0.5
    one_sided = [(DAY, 150.0, None), *groups]
    assert high_bp_ratio(one_sided, DAY).value == 0.5
    mid = [(DAY - timedelta(days=i), 132.0, 70.0) for i in range(6)]
    assert high_bp_ratio(mid, DAY, variant="eu").value == 0.0
    assert high_bp_ratio(mid, DAY, variant="us").value == 1.0
    assert high_bp_ratio([], DAY).status is Status.NOT_AVAILABLE


def test_weekly_active_minutes_scales_present_days() -> None:
    days = [(DAY - timedelta(days=i), 30.0) for i in range(20)]
    assert weekly_active_minutes(days, DAY).value == 210.0
    assert weekly_active_minutes(days[:19], DAY).status is Status.INSUFFICIENT_DATA
    assert weekly_active_minutes([], DAY).status is Status.NOT_AVAILABLE


def test_no_value_judging_words_in_scores_package() -> None:
    """String literals describe; they never say healthy / good / bad (ADR 0012, PLAN §4.2)."""
    root = Path(his_mod.__file__).parent
    banned = re.compile(r"\b(healthy|unhealthy|recovered|good|bad|excellent|poor)\b", re.I)
    for f in root.glob("*.py"):
        tree = ast.parse(f.read_text())
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef)
            and n.body
            and isinstance(n.body[0], ast.Expr)
        }
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
                assert " " not in n.value or not banned.search(n.value), (f.name, n.value)
