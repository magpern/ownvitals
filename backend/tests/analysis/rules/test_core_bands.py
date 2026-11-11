"""Bands, origin and the minimum half-width (ADR 0010 §2, §4; test specification "Bands").
Synthetic data only."""

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from rules_builders import DAY, constant, dv, preceding, run_of

from ownvitals.analysis.methods.baseline import Window
from ownvitals.analysis.methods.series import MAD_SCALE
from ownvitals.analysis.policy import InsufficientData
from ownvitals.analysis.rules import core
from ownvitals.analysis.rules.core import (
    Band,
    display_band,
    finding_band,
    min_half_width,
)
from ownvitals.analysis.rules.episodes import Origin, origin

Q = DAY


def band(result: Band | InsufficientData) -> Band:
    assert isinstance(result, Band), result
    return result


def test_the_display_band_uses_q_minus_89_to_q_and_a_value_on_q_minus_90_does_not_move_it() -> None:
    # 45 days at 80, then 45 days at 100: the median is 90 and moves with a single day's value
    base = run_of(Q - timedelta(days=89), [80.0] * 45 + [100.0] * 45)
    before = band(display_band("weight", base, Q))
    assert before.median == 90.0
    assert band(display_band("weight", [*base, dv(Q - timedelta(days=90), 500)], Q)).median == 90.0
    # day q is inside the trailing window; day q - 89 is its first day
    assert band(display_band("weight", [*base[:-1], dv(Q, 80.0)], Q)).median == 80.0
    assert band(display_band("weight", [dv(base[0].day, 100.0), *base[1:]], Q)).median == 100.0


def test_the_finding_band_uses_q_minus_90_to_q_minus_1_and_a_value_on_q_does_not_move_it() -> None:
    base = run_of(Q - timedelta(days=90), [80.0] * 45 + [100.0] * 45)
    assert band(finding_band("weight", base, Q)).median == 90.0
    assert band(finding_band("weight", [*base, dv(Q, 500)], Q)).median == 90.0
    assert band(finding_band("weight", [*base, dv(Q - timedelta(days=91), 500)], Q)).median == 90.0
    # q - 90 is its first day, q - 1 its last
    assert band(finding_band("weight", [dv(base[0].day, 100.0), *base[1:]], Q)).median == 100.0
    assert (
        band(finding_band("weight", [*base[:-1], dv(Q - timedelta(days=1), 80.0)], Q)).median
        == 80.0
    )


def test_the_finding_threshold_is_exactly_median_plus_two_and_a_half_scaled_mads() -> None:
    result = band(finding_band("weight", preceding(Q, [79.0, 81.0] * 45), Q))
    assert result.k == 2.5
    assert result.high == pytest.approx(80.0 + 2.5 * MAD_SCALE * 1.0)
    assert result.low == pytest.approx(80.0 - 2.5 * MAD_SCALE * 1.0)
    assert band(display_band("weight", preceding(Q, [79.0, 81.0] * 45), Q)).k == 1.5


def test_a_constant_baseline_gives_the_minimum_half_width_in_both_bands() -> None:
    weight = constant(Q - timedelta(days=90), 90, "80.0")
    for result in (
        finding_band("weight", weight, Q),
        display_band("weight", weight, Q, window=Window.PRECEDING),
    ):
        got = band(result)
        assert (got.low, got.high) == (pytest.approx(79.55), pytest.approx(80.45))
    pulse = band(finding_band("heart_pulse", constant(Q - timedelta(days=90), 90, 61), Q))
    assert (pulse.low, pulse.high) == (59.0, 63.0)


def test_a_constant_trailing_display_band_is_never_zero_width() -> None:
    # the chart's band (TRAILING, q - 89 ... q): a flat series has MAD 0, so only the
    # minimum half-width keeps the band from collapsing to a line (#394)
    flat = constant(Q - timedelta(days=89), 90, "80.0")
    got = band(display_band("weight", flat, Q))
    assert got.mad == 0.0
    assert got.half_width == pytest.approx(0.45)
    assert got.low < got.median < got.high
    assert (got.low, got.high) == (pytest.approx(79.55), pytest.approx(80.45))


def test_the_minimum_applies_to_the_display_band_only_when_the_scaled_mad_is_below_it() -> None:
    # a = 0.15: 1.5 * 1.4826 * a = 0.334 < 0.45 <= 2.5 * 1.4826 * a = 0.556
    data = preceding(Q, [80.15, 79.85] * 45)
    assert band(display_band("weight", data, Q, window=Window.PRECEDING)).half_width == 0.45
    assert band(finding_band("weight", data, Q)).half_width == pytest.approx(2.5 * MAD_SCALE * 0.15)
    assert band(finding_band("weight", data, Q)).half_width > 0.45


@pytest.mark.parametrize(
    "key", ["weight", "heart_pulse", "muscle_mass", "esc", "extracellular_water"]
)
def test_a_finding_value_is_always_outside_the_displayed_band(key: str) -> None:
    for spread in (0.0, 0.05, 0.2, 1.0, 3.0):
        data = preceding(Q, [50 + spread * (1 if i % 2 else -1) for i in range(90)])
        display = band(display_band(key, data, Q, window=Window.PRECEDING))
        finding = band(finding_band(key, data, Q))
        assert finding.half_width >= display.half_width
        assert finding.median == display.median


def test_a_sparse_baseline_gives_no_band() -> None:
    nine = [dv(Q - timedelta(days=10 * i + 1), 80) for i in range(9)]  # 9 days over 90
    ten_in_twenty = [dv(Q - timedelta(days=2 * i + 1), 80) for i in range(10)]  # 10 days, span 18
    for data in (nine, ten_in_twenty):
        assert isinstance(finding_band("weight", data, Q), InsufficientData)
        assert isinstance(display_band("weight", data, Q), InsufficientData)
    spanning = [dv(Q - timedelta(days=i * 2 + 1), 80) for i in range(11)]  # 11 days, 20 elapsed
    assert isinstance(finding_band("weight", spanning, Q), InsufficientData)
    enough = [dv(Q - timedelta(days=i * 3 + 1), 80) for i in range(10)]  # 10 days, 27 elapsed
    assert isinstance(finding_band("weight", enough, Q), Band)


def test_a_centered_band_is_refused_and_a_metric_without_a_minimum_is_named() -> None:
    with pytest.raises(ValueError, match="centered"):
        core.band_at("weight", [], Q, k=1.5, window=Window.CENTERED)
    with pytest.raises(KeyError, match="fat_mass"):
        min_half_width("fat_mass")


def test_every_band_call_passes_k_and_nothing_uses_default_k() -> None:
    rules = Path(core.__file__).parent
    calls = 0
    for path in sorted(rules.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name | ast.Attribute):
                assert getattr(node, "id", getattr(node, "attr", "")) != "DEFAULT_K"
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name in ("rolling_baseline", "band_at"):
                    calls += 1
                    assert "k" in {kw.arg for kw in node.keywords}, (path.name, node.lineno)
    assert calls >= 3  # band_at's own call and the two wrappers


@pytest.mark.parametrize(
    ("window_end", "today_offset", "expected"),
    [
        (0, 6, Origin.LIVE),
        (0, 7, Origin.BACKFILL),
        (0, 0, Origin.LIVE),
        (0, -1, Origin.LIVE),  # a window end after today (a zone ahead) is live
    ],
)
def test_origin_is_live_through_six_days_back_and_backfill_from_seven(
    window_end: int, today_offset: int, expected: Origin
) -> None:
    end = DAY + timedelta(days=window_end)
    assert origin(end, end + timedelta(days=today_offset)) is expected


def test_no_rule_builds_a_series_with_from_instants() -> None:
    for path in sorted(Path(core.__file__).parent.glob("*.py")):
        names = {
            getattr(n, "id", getattr(n, "attr", ""))
            for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.Name | ast.Attribute)
        }
        assert "from_instants" not in names, path.name
