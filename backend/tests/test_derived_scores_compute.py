"""The scores pass's pure glue (``derived.scores_compute``): kinds, units, fingerprint. Synthetic
inputs only."""

from datetime import date, timedelta
from decimal import Decimal

from ownvitals.analysis.daily import DailyMethod, DailyValue
from ownvitals.derived.normalize_stream_spec import SCORE_INPUTS
from ownvitals.derived.scores_compute import (
    ScoreInputs,
    _nights,
    compute_scores,
    fingerprint,
    kinds,
)

DAY = date(2026, 11, 1)
SYNTHETIC = True  # every value in this module is made up


def _shipped_his(**kw: object) -> dict[str, object]:
    """HIS on ``DAY`` with the shipped table, a 1995 male and 30 days of synthetic weight."""
    from ownvitals.analysis.scores.his_table import parse_table
    from ownvitals.config_scores import DEFAULT_HIS_BAND_TABLE

    table = parse_table(DEFAULT_HIS_BAND_TABLE.read_bytes())
    v = Decimal("70.0")
    weight = tuple(
        DailyValue(DAY - timedelta(days=i), v, DailyMethod.MORNING, 1, v, v, Decimal(0))
        for i in range(29, -1, -1)
    )
    res = compute_scores(
        inputs(
            table=table,
            birth_year=1995,
            sex="male",
            experimental=True,
            daily={"weight": weight},
            **kw,
        )
    )["his"]
    return dict(res.components)


def inputs(*, experimental: bool = False, **kw: object) -> ScoreInputs:
    base: dict[str, object] = {
        "day": DAY,
        "daily": {},
        "points": {},
        "birth_year": None,
        "sex": None,
        "table": None,
        "table_error": None,
        "table_digest": "",
        "experimental": experimental,
    }
    return ScoreInputs(**{**base, **kw})  # type: ignore[arg-type]


def test_kinds_list_his_only_with_the_switch() -> None:
    assert "his" not in kinds(inputs())
    assert kinds(inputs(experimental=True))[-1] == "his"
    assert set(compute_scores(inputs())) == set(kinds(inputs()))
    assert set(compute_scores(inputs(experimental=True))) == set(kinds(inputs(experimental=True)))


def test_sleep_duration_seconds_become_hours() -> None:
    key = SCORE_INPUTS["readiness_sleep_duration"]
    (night,) = _nights({key: ((DAY, 27_000.0),)})  # 7.5 h in seconds
    assert night.duration_h == 7.5


def test_his_without_a_table_is_not_set() -> None:
    res = compute_scores(inputs(experimental=True))["his"]
    assert (res.value, res.components["status"]) == (None, "not_set")


def test_the_fingerprint_follows_the_revisions_and_the_points() -> None:
    key = SCORE_INPUTS["readiness_sleep_duration"]
    a = inputs(points={key: ((DAY - timedelta(days=1), 1.0),)})
    assert fingerprint(a, (1, 0, 0)) == fingerprint(a, (1, 0, 0))
    assert fingerprint(a, (1, 0, 0)) != fingerprint(a, (2, 0, 0))
    assert fingerprint(a, (1, 0, 0)) != fingerprint(a, (0, 1, 0))
    b = inputs(points={key: ((DAY - timedelta(days=1), 2.0),)})
    assert fingerprint(a, (1, 0, 0)) != fingerprint(b, (1, 0, 0))


def test_shipped_table_his_is_invisible_without_the_switch() -> None:
    from ownvitals.analysis.scores.his_table import parse_table
    from ownvitals.config_scores import DEFAULT_HIS_BAND_TABLE

    table = parse_table(DEFAULT_HIS_BAND_TABLE.read_bytes())
    off = compute_scores(inputs(table=table, birth_year=1995, sex="male"))
    assert "his" not in off
    assert "his" not in kinds(inputs(table=table))
    on = compute_scores(inputs(table=table, birth_year=1995, sex="male", experimental=True))
    assert "his" in on


def test_bmi_is_computed_from_the_height_and_the_weight() -> None:
    comp = _shipped_his(height=("2026-01-01T08:00:00+00:00", 1.75))
    bmi = comp["criteria"]["bmi"]  # type: ignore[index]
    assert bmi["status"] == "computed"
    assert round(bmi["value"], 2) == round(70 / 1.75**2, 2)  # 22.86
    assert round(bmi["score"], 1) == 88.4  # 100 at 21, 75 at 25 (withings_card_v1 bmi row)


def test_bmi_without_a_height_is_not_computed_and_says_no_height() -> None:
    comp = _shipped_his()
    bmi = comp["criteria"]["bmi"]  # type: ignore[index]
    assert (bmi["status"], bmi["reason"]) == ("not_available", "no_height")
    assert "value" not in bmi
    assert comp["vertical_reasons"]["body"] == "no_height"  # type: ignore[index]


def test_the_height_is_part_of_the_fingerprint() -> None:
    a = inputs(height=("2026-01-01T08:00:00+00:00", 1.75))
    same = inputs(height=("2026-01-01T08:00:00+00:00", 1.75))
    taller = inputs(height=("2026-01-01T08:00:00+00:00", 1.76))
    newer = inputs(height=("2026-02-01T08:00:00+00:00", 1.75))
    none = inputs()
    base = fingerprint(a, (1, 0, 0))
    assert base == fingerprint(same, (1, 0, 0))
    assert len({base, fingerprint(taller, (1, 0, 0)), fingerprint(newer, (1, 0, 0))}) == 3
    assert fingerprint(none, (1, 0, 0)) != base
