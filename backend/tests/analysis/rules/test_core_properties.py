"""Properties of every rule: deterministic, independent of input order, and "insufficient data"
(never a finding) on sparse or irregular series (M4 done-when). Synthetic data only."""

from collections.abc import Callable
from datetime import timedelta

import numpy as np
import pytest
from rules_builders import DAY, alternating, constant, dv, ending, noise, run_of

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.rules.body import b1, b2, b4, b5
from ownvitals.analysis.rules.core import Finding, Insufficient, NoFinding, RuleResult
from ownvitals.analysis.rules.heart import c1, c1x, c2

type Case = Callable[[list[DailyValue]], RuleResult]


def stepping(n: int, level: float) -> list[DailyValue]:
    e = noise(3, n)
    return ending(DAY, [round(level + (1 if i >= n // 2 else 0) + 0.1 * e[i], 3) for i in range(n)])


CASES: dict[str, tuple[Case, list[DailyValue]]] = {
    "b2": (lambda d: b2(d, DAY), ending(DAY, [round(80 - 0.03 * i, 3) for i in range(182)])),
    "b4": (lambda d: b4(d, DAY), ending(DAY, [round(30 - 0.03 * i, 3) for i in range(90)])),
    "b5": (lambda d: b5(d, DAY, metric_key="visceral_fat"), stepping(90, 10)),
    "c2": (lambda d: c2(d, DAY), ending(DAY, [round(50 + i / 30.4, 4) for i in range(90)])),
    "c1": (
        lambda d: c1(d, DAY),
        [*constant(DAY - timedelta(days=92), 90, 61), *run_of(DAY - timedelta(days=2), [75] * 3)],
    ),
    "c1x": (lambda d: c1x(d, DAY), run_of(DAY - timedelta(days=3), [120] * 4)),
    "b1": (
        lambda d: b1(
            [dv(DAY - timedelta(days=3), "80.0"), dv(DAY, "83.0")],
            d,
            DAY,
        ),
        [*alternating(DAY - timedelta(days=93), 90, 15.0, 0.5), dv(DAY, 19.0)],
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_rule_is_deterministic_and_independent_of_input_order(name: str) -> None:
    rule, data = CASES[name]
    first = rule(list(data))
    assert isinstance(first, Finding), (name, first)
    assert rule(list(data)) == first
    for seed in range(5):
        order = np.random.default_rng(seed).permutation(len(data))
        shuffled = [data[int(i)] for i in order]
        assert rule(shuffled) == first


def sparse_daily(count: int, step: int, level: float = 80.0) -> list[DailyValue]:
    """``count`` irregular days (a growing step), ending on DAY, below every policy."""
    out = [dv(DAY, level)]
    gap = 0
    for i in range(1, count):
        gap += step + i
        out.append(dv(DAY - timedelta(days=gap), level + i))
    return out


@pytest.mark.parametrize("name", ["b2", "b4", "b5", "c2", "c1"])
def test_sparse_and_irregular_series_give_insufficient_data_never_a_finding(name: str) -> None:
    rule, _ = CASES[name]
    for count, step in ((5, 3), (7, 9), (4, 20)):
        result = rule(sparse_daily(count, step))
        assert not isinstance(result, Finding), (name, count, step)
        assert isinstance(result, Insufficient | NoFinding)
    if name != "c1":
        assert isinstance(rule(sparse_daily(7, 9)), Insufficient)


def test_c1_with_a_sparse_baseline_is_insufficient_not_a_finding() -> None:
    data = [*sparse_daily(6, 12, 61), *run_of(DAY - timedelta(days=2), [90, 90, 90])]
    data = [v for v in data if v.day not in {DAY - timedelta(days=i) for i in range(3)}] + run_of(
        DAY - timedelta(days=2), [90, 90, 90]
    )
    assert isinstance(c1(data, DAY), Insufficient)


def test_b1_with_a_sparse_ecw_history_is_insufficient_data() -> None:
    ecw = [dv(DAY - timedelta(days=20 * i + 4), 15.0) for i in range(4)] + [dv(DAY, 30.0)]
    weights = [dv(DAY - timedelta(days=3), "80.0"), dv(DAY, "84.0")]
    assert isinstance(b1(weights, ecw, DAY), Insufficient)
