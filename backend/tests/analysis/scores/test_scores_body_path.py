"""Body Path (ADR 0012 tests §3). Synthetic data only."""

import random
from datetime import timedelta

import numpy as np
import pytest
from scores_builders import DAY, dv, line

from ownvitals.analysis.scores import Status
from ownvitals.analysis.scores.body_path import Z80, evaluate_body_path


def _h(res, h):
    return res.components["horizons"][str(h)]


def test_exact_linear_series() -> None:
    res = evaluate_body_path("weight", line(DAY, [80.0 - 0.02 * i for i in range(90)]), day=DAY)
    assert res.status is Status.COMPUTED
    c = res.components
    assert c["slope_per_day"] == pytest.approx(-0.02, abs=1e-9)
    assert c["level"] == pytest.approx(78.22, abs=1e-6)
    assert c["sigma"] == pytest.approx(0.3, abs=1e-9)
    assert _h(res, 30)["centre"] == pytest.approx(77.62, abs=1e-6)
    assert _h(res, 30)["low"] == pytest.approx(77.2355, abs=1e-4)
    assert _h(res, 30)["high"] == pytest.approx(78.0045, abs=1e-4)
    assert _h(res, 60)["centre"] == pytest.approx(77.02, abs=1e-6)
    assert _h(res, 90)["centre"] == pytest.approx(76.42, abs=1e-6)
    assert res.value == pytest.approx(_h(res, 30)["centre"])
    assert set(c["horizons"]) == {"30", "60", "90"}  # nothing beyond 90
    for key in ("ar1_phi", "n_over_n_star", "changes", "data_quality", "window", "slope_interval"):
        assert key in c


def test_constant_series_has_a_floor_width() -> None:
    res = evaluate_body_path("weight", line(DAY, [80.0] * 90), day=DAY)
    for h in (30, 60, 90):
        band = _h(res, h)
        assert band["centre"] == pytest.approx(80.0)
        assert band["high"] - band["centre"] == pytest.approx(Z80 * 0.3, abs=1e-9)


def test_fat_mass_uses_its_own_floor() -> None:
    res = evaluate_body_path("fat_mass", line(DAY, [20.0] * 90), day=DAY)
    assert res.kind == "body_path:fat_mass"
    assert res.components["sigma"] == pytest.approx(0.1 / 1.5)


def _sparse(n_days: int, span: int):
    step = span / (n_days - 1)
    days = sorted({round(i * step) for i in range(n_days)})
    assert len(days) == n_days
    start = DAY - timedelta(days=89)
    return [dv(start + timedelta(days=d), 80.0 + 0.01 * d) for d in days]


@pytest.mark.parametrize(
    ("n", "span", "status"),
    [
        (13, 89, Status.INSUFFICIENT_DATA),
        (14, 54, Status.COMPUTED),
        (14, 53, Status.INSUFFICIENT_DATA),
    ],
)
def test_minimum_data(n: int, span: int, status: Status) -> None:
    assert evaluate_body_path("weight", _sparse(n, span), day=DAY).status is status


def test_no_data_is_not_available() -> None:
    res = evaluate_body_path("weight", [], day=DAY)
    assert res.status is Status.NOT_AVAILABLE
    assert res.value is None


def test_outliers_leave_the_slope_and_sigma_floor() -> None:
    values = [80.0 - 0.02 * i for i in range(90)]
    for i in range(5, 90, 9):
        values[i] += 3.0
    res = evaluate_body_path("weight", line(DAY, values), day=DAY)
    assert res.components["slope_per_day"] == pytest.approx(-0.02, abs=1e-9)
    assert res.components["sigma"] == pytest.approx(0.3, abs=1e-9)


def test_no_change_point_cut() -> None:
    values = [80.0] * 60 + [80.0 - 0.05 * (i - 59) for i in range(60, 90)]
    res = evaluate_body_path("weight", line(DAY, values), day=DAY)
    assert res.status is Status.COMPUTED
    assert res.components["window"]["start"] == (DAY - timedelta(days=89)).isoformat()
    assert "changes" in res.components


def test_interval_is_additive_not_quadrature() -> None:
    rng = random.Random(7)  # noqa: S311
    res = evaluate_body_path(
        "weight", line(DAY, [80 - 0.02 * i + rng.gauss(0, 0.5) for i in range(90)]), day=DAY
    )
    c = res.components
    lo, hi = c["slope_interval"]
    band = _h(res, 60)
    assert band["low"] == pytest.approx(c["level"] + lo * 60 - Z80 * c["sigma"])
    assert band["high"] == pytest.approx(c["level"] + hi * 60 + Z80 * c["sigma"])


def test_order_of_inputs_does_not_matter() -> None:
    data = line(DAY, [80.0 - 0.02 * i + (i % 3) * 0.1 for i in range(90)])
    base = evaluate_body_path("weight", data, day=DAY)
    for seed in (1, 2, 3):
        shuffled = data[:]
        random.Random(seed).shuffle(shuffled)  # noqa: S311
        assert evaluate_body_path("weight", shuffled, day=DAY) == base


@pytest.mark.parametrize(("phi", "frac"), [(0.0, 1.0), (0.6, 0.16)])
def test_calibration_coverage(phi: float, frac: float) -> None:
    """Synthetic simulation, seed 20261103, 200 series per cell (reduced to two cells)."""
    rng = np.random.default_rng(20261103)
    hits = {30: 0, 60: 0, 90: 0}
    runs = 200
    for _ in range(runs):
        noise = np.zeros(220)
        eps = rng.normal(0, 0.5 * (1 - phi**2) ** 0.5, 220)
        for i in range(220):
            noise[i] = (phi * noise[i - 1] if i else 0.0) + eps[i]
        truth = [80.0 - 0.02 * i for i in range(220)]
        keep = (
            sorted(rng.choice(90, size=max(14, round(90 * frac)), replace=False))
            if frac < 1
            else range(90)
        )
        start = DAY - timedelta(days=89)
        data = [dv(start + timedelta(days=int(i)), truth[i] + noise[i]) for i in keep]
        res = evaluate_body_path("weight", data, day=DAY)
        if res.status is not Status.COMPUTED:
            continue
        for h in hits:
            band = _h(res, h)
            hits[h] += band["low"] <= truth[89 + h] + noise[89 + h] <= band["high"]
    for h, n in hits.items():
        assert 0.72 <= n / runs <= 0.95, (h, n / runs)
