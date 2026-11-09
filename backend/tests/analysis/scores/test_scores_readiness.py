"""Readiness (ADR 0012 §4). All data SYNTHETIC."""

import random
from datetime import timedelta

import pytest
from scores_builders import DAY, SYNTHETIC

from ownvitals.analysis.scores import Status
from ownvitals.analysis.scores.readiness import Night, evaluate_readiness

assert SYNTHETIC


def _nights(n: int, *, hrv: float | None = 40.0, dur: float = 7.0, hr: float = 55.0) -> list[Night]:
    out = []
    for i in range(n):
        wiggle = (i % 3 - 1) * 0.3
        out.append(
            Night(
                DAY - timedelta(days=n - i),
                duration_h=dur + wiggle,
                night_hr=hr + wiggle * 2,
                hrv_ms=None if hrv is None else hrv + wiggle * 4,
            )
        )
    return out


def test_usual_night_scores_about_fifty() -> None:
    nights = [*_nights(28), Night(DAY, 7.0, 55.0, 40.0)]
    res = evaluate_readiness(nights, day=DAY)
    assert res.status is Status.COMPUTED
    assert res.value == 50.0
    assert res.components["text_key"] == "score.readiness.how"


def test_direction_of_components() -> None:
    better = evaluate_readiness([*_nights(28), Night(DAY, 8.0, 52.0, 48.0)], day=DAY)
    worse = evaluate_readiness([*_nights(28), Night(DAY, 5.5, 60.0, 30.0)], day=DAY)
    assert better.value is not None
    assert worse.value is not None
    assert better.value > 50 > worse.value


def test_scores_clamped() -> None:
    res = evaluate_readiness([*_nights(28), Night(DAY, 14.0, 20.0, 200.0)], day=DAY)
    assert res.value == 100.0


def test_no_sleep_for_the_day() -> None:
    res = evaluate_readiness(_nights(28), day=DAY)
    assert res.status is Status.NOT_AVAILABLE
    assert res.components["reason"] == "no_sleep_data"
    assert res.value is None


def test_baseline_too_short() -> None:
    res = evaluate_readiness([*_nights(5), Night(DAY, 7.0, 55.0, 40.0)], day=DAY)
    assert res.status is Status.INSUFFICIENT_DATA
    assert res.components["reason"] == "baseline_insufficient"


def test_hrv_not_reported_is_optional() -> None:
    nights = [*_nights(28, hrv=None), Night(DAY, 7.0, 55.0, None)]
    res = evaluate_readiness(nights, day=DAY)
    assert res.status is Status.COMPUTED
    assert res.components["components"]["hrv"]["reason"] == "not_reported"


def test_hrv_baseline_insufficient_still_computes() -> None:
    nights = [Night(n.day, n.duration_h, n.night_hr, None) for n in _nights(28)]
    nights += [Night(DAY - timedelta(days=1), 7.0, 55.0, 40.0), Night(DAY, 7.0, 55.0, 40.0)]
    nights = [n for n in nights if n.day != DAY - timedelta(days=1)] + [nights[-2]]
    res = evaluate_readiness(nights, day=DAY)
    assert res.status is Status.COMPUTED
    assert res.components["components"]["hrv"]["reason"] == "baseline_insufficient"


def test_night_hr_missing_blocks() -> None:
    res = evaluate_readiness([*_nights(28), Night(DAY, 7.0, None, 40.0)], day=DAY)
    assert res.status is Status.INSUFFICIENT_DATA
    assert res.components["reason"] == "hr_missing"


def test_scale_floor_prevents_blowup() -> None:
    flat = [Night(DAY - timedelta(days=i + 1), 7.0, 55.0, 40.0) for i in range(28)]
    res = evaluate_readiness([*flat, Night(DAY, 7.1, 55.0, 40.0)], day=DAY)
    assert res.value is not None
    assert 50 < res.value < 100


def test_input_order_and_future_nights_ignored() -> None:
    nights = [*_nights(28), Night(DAY, 7.5, 54.0, 42.0)]
    base = evaluate_readiness(nights, day=DAY)
    shuffled = nights[:]
    random.Random(3).shuffle(shuffled)  # noqa: S311
    assert evaluate_readiness(shuffled, day=DAY) == base
    future = [*nights, Night(DAY + timedelta(days=1), 1.0, 90.0, 1.0)]
    assert evaluate_readiness(future, day=DAY) == base


@pytest.mark.parametrize("n", [10, 28])
def test_deterministic(n: int) -> None:
    nights = [*_nights(n), Night(DAY, 7.5, 54.0, 42.0)]
    assert evaluate_readiness(nights, day=DAY) == evaluate_readiness(nights, day=DAY)
