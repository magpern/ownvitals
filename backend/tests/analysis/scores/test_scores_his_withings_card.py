"""The shipped HIS band table, ``withings_card_v1`` (issue #488), against the findings table it
encodes (``docs/withings-findings.md`` section 3, "Health Improvement Score"). Profiles and
values are SYNTHETIC; the table's numbers are the documented ones."""

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from ownvitals.analysis.scores import Status
from ownvitals.analysis.scores.his import CriterionInput, evaluate_his
from ownvitals.analysis.scores.his_table import REGISTERED, InvalidBandTable, parse_table
from ownvitals.config_scores import DEFAULT_HIS_BAND_TABLE, ScoreSettings

SYNTHETIC = True
PATH = DEFAULT_HIS_BAND_TABLE
RAW = PATH.read_bytes()
TABLE = parse_table(RAW)
DAY = date(2026, 11, 1)
SIDES = ("perfect", "good", "average", "bad")

# One entry per findings row: (criterion, sex, age_min, age_max, low cells, high cells). A cell
# list reads Perfect / Good / Average / Bad. "20-21" has both sides; "<15 or >40" is the bad
# cell of the low and the high side; "<=50" is the high side only; ">=12,000" the low side only.
# "40+" is open-ended: the file's upper bound only has to lie beyond any real age.
DOCUMENTED = [
    ("bmi", "any", 18, 65, (20, 18.5, 16, 15), (21, 25, 30, 40)),
    ("fat_ratio", "male", 18, 40, (7, 6, 5, 3), (10, 20, 25, 40)),
    ("fat_ratio", "female", 18, 40, (15, 13, 11, 8), (18, 27, 34, 45)),
    ("night_hr", "male", 18, 40, None, (50, 64, 70, 80)),
    ("vascular_age_gap", "male", 18, 40, None, (-5, 0, 3, 5)),
    ("vascular_age_gap", "female", 18, 40, None, (-7, -2, 0, 3)),
    ("afib_ecg_count", "any", 18, 40, None, (0, 1, 5, 10)),
    ("high_bp_ratio", "any", 40, None, None, (0, 0.15, 0.5, 0.7)),
    ("daily_steps", "any", 18, 65, (12000, 6000, 3000, 2000), None),
    ("weekly_active_minutes", "any", 18, 65, (1000, 600, 300, 100), None),
    ("sleep_ahi", "any", 18, 40, None, (0, 15, 30, 50)),
]
CELLS = [
    pytest.param(c, sex, lo, hi, side, i, v, id=f"{c}-{sex}-{side}-{SIDES[i]}")
    for c, sex, lo, hi, low, high in DOCUMENTED
    for side, cells in (("low", low), ("high", high))
    if cells is not None
    for i, v in enumerate(cells)
]


def _rows(criterion: str, sex: str):
    return [r for r in TABLE.rows if r.criterion == criterion and r.sex == sex]


def test_file_is_registered_and_loads() -> None:
    assert REGISTERED["withings_card_v1"] == hashlib.sha256(RAW).hexdigest()
    assert (TABLE.table_id, TABLE.version) == ("withings_card_v1", "1")
    assert "docs/withings-findings.md" in TABLE.source
    assert json.loads(RAW)["table_id"] == "withings_card_v1"


def test_only_the_documented_rows_are_encoded() -> None:
    assert len(TABLE.rows) == len(DOCUMENTED)
    assert {(r.criterion, r.sex) for r in TABLE.rows} == {(d[0], d[1]) for d in DOCUMENTED}


@pytest.mark.parametrize(("criterion", "sex", "age_lo", "age_hi", "side", "i", "value"), CELLS)
def test_each_documented_cell(
    criterion: str, sex: str, age_lo: int, age_hi: int | None, side: str, i: int, value: float
) -> None:
    (row,) = _rows(criterion, sex)
    assert row.age_min == age_lo
    assert getattr(getattr(row, side), SIDES[i]) == value


@pytest.mark.parametrize(("criterion", "sex", "age_lo", "age_hi", "low", "high"), DOCUMENTED)
def test_sides_not_in_the_findings_are_absent(
    criterion: str, sex: str, age_lo: int, age_hi: int | None, low: object, high: object
) -> None:
    (row,) = _rows(criterion, sex)
    assert (row.low is None) == (low is None)
    assert (row.high is None) == (high is None)
    if age_hi is not None:
        assert row.age_max == age_hi
    else:
        assert row.age_max >= 120


def test_no_row_for_what_the_findings_leave_out() -> None:
    assert _rows("night_hr", "female") == []  # no women's night-HR row
    assert _rows("night_hr", "any") == []
    for row in TABLE.rows:  # no older fat-ratio bracket, no 65+ bracket (only "looser")
        if row.criterion == "fat_ratio":
            assert row.age_max == 40
        if row.criterion in ("bmi", "daily_steps", "weekly_active_minutes"):
            assert row.age_max == 65


def test_a_changed_file_is_refused() -> None:
    with pytest.raises(InvalidBandTable, match="not_registered"):
        parse_table(RAW.replace(b'"version": "1"', b'"version": "2"'))


def _inputs(**v: float) -> dict[str, CriterionInput]:
    return {k: CriterionInput(Status.COMPUTED, x, "u") for k, x in v.items()}


# "good" boundary of each documented cell: every criterion scores 75, so HIS is 75.
MAN = {"fat_ratio": 20, "night_hr": 64, "vascular_age_gap": 0, "daily_steps": 6000,
       "weekly_active_minutes": 600, "sleep_ahi": 15}  # fmt: skip
WOMAN = {**MAN, "fat_ratio": 27, "vascular_age_gap": -2}


@pytest.mark.parametrize(("sex", "values"), [("male", MAN), ("female", WOMAN)])
def test_adults_18_to_40_compute(sex: str, values: dict[str, float]) -> None:
    res = evaluate_his(TABLE, inputs=_inputs(**values), day=DAY, birth_year=1995, sex=sex)
    assert (res.status, res.value) == (Status.COMPUTED, 75.0)
    crit = res.components["criteria"]
    for name in values:
        if sex == "female" and name == "night_hr":
            continue
        assert (crit[name]["status"], crit[name]["score"]) == ("computed", 75.0)
    assert res.components["verticals"] == {"body": 75, "heart": 75, "activity": 75, "sleep": 75}
    if sex == "female":  # no night-HR row for women: not banded, not guessed
        assert crit["night_hr"] == {
            "status": "not_available",
            "reason": "no_band_row",
            "vertical": "heart",
        }


def test_a_70_year_old_gets_the_undocumented_components_not_computed() -> None:
    res = evaluate_his(
        TABLE,
        inputs=_inputs(**MAN, high_bp_ratio=0.15),
        day=DAY,
        birth_year=1956,
        sex="male",
    )
    crit = res.components["criteria"]
    for name in MAN:
        assert (crit[name]["status"], crit[name]["reason"]) == ("not_available", "no_band_row")
    assert crit["high_bp_ratio"]["status"] == "computed"  # the one documented 40+ row
    assert (res.status, res.value) == (Status.INSUFFICIENT_DATA, None)  # one vertical only


def test_a_user_between_brackets_is_not_banded() -> None:
    res = evaluate_his(TABLE, inputs=_inputs(**MAN), day=DAY, birth_year=1955, sex="male")
    assert res.components["criteria"]["daily_steps"]["status"] == "not_available"  # 71, not 18-65
    old_fat = evaluate_his(TABLE, inputs=_inputs(**MAN), day=DAY, birth_year=1980, sex="male")
    assert old_fat.components["criteria"]["fat_ratio"]["status"] == "not_available"  # 46


def test_the_default_setting_is_the_shipped_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OWNVITALS_HIS_BAND_TABLE", raising=False)
    assert ScoreSettings().his_band_table == DEFAULT_HIS_BAND_TABLE
    assert DEFAULT_HIS_BAND_TABLE.is_file()
    assert DEFAULT_HIS_BAND_TABLE.parent.name == "his_tables"


@pytest.mark.parametrize("empty", ["", "  "])
def test_unsetting_the_setting_leaves_his_not_set(
    monkeypatch: pytest.MonkeyPatch, empty: str
) -> None:
    monkeypatch.setenv("OWNVITALS_HIS_BAND_TABLE", empty)
    assert ScoreSettings().his_band_table is None
    res = evaluate_his(None, inputs={}, day=DAY, birth_year=1995, sex="male")
    assert (res.status, res.components["reason"]) == (Status.NOT_SET, "no_band_table")


def test_a_custom_path_is_kept(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OWNVITALS_HIS_BAND_TABLE", str(tmp_path / "x.json"))
    assert ScoreSettings().his_band_table == tmp_path / "x.json"
