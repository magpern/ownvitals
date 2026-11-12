"""The scores pass's pure glue (``derived.scores_compute``): kinds, units, fingerprint. Synthetic
inputs only."""

from datetime import date, timedelta

from ownvitals.derived.normalize_stream_spec import SCORE_INPUTS
from ownvitals.derived.scores_compute import (
    ScoreInputs,
    _nights,
    compute_scores,
    fingerprint,
    kinds,
)

DAY = date(2026, 11, 1)


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
