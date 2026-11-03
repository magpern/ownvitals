"""The false-episode rate of the persistence rule at phi 0.6 (ADR 0010 "Repeated testing"). Not a
statistical claim about real data: seeded synthetic AR(1) noise, no change at all, the merged
``trend`` (AR(1) correction, alpha 0.05) on a 90-day window evaluated on each of 365 days, fed to
``advance_persistent``. The ADR gives 8.5 % of years with daily data at phi 0.6 (standard error
about 2 points) against the Architect's bound of 10 %, and 59 to 66 % without persistence."""

from datetime import date, timedelta

import numpy as np
from rules_builders import fired, noise

from ownvitals.analysis.methods.series import Series
from ownvitals.analysis.methods.trend import Direction, trend
from ownvitals.analysis.rules.core import (
    Finding,
    NoFinding,
    RuleResult,
)
from ownvitals.analysis.rules.episodes import Transition, TrendState, advance_persistent

SEED = 20261031
YEARS = 80
PHI = 0.6
FIRST = date(2026, 1, 1)


def year_results(seed: int) -> list[RuleResult]:
    """One no-change year of daily evaluations of the 90-day "rising" condition."""
    y = noise(seed, 365 + 89, PHI)
    t = np.arange(y.size, dtype=np.float64)
    out: list[RuleResult] = []
    for i in range(89, y.size):
        day = FIRST + timedelta(days=i - 89)
        series = Series.from_arrays(t[i - 89 : i + 1], y[i - 89 : i + 1], window_days=90)
        if trend(series, alpha=0.05).direction is Direction.INCREASING:
            out.append(fired(day))
        else:
            out.append(NoFinding("B5", "visceral_fat", day))
    return out


def test_persistence_keeps_the_false_episode_rate_under_the_architects_ten_percent_bound() -> None:
    with_persistence = 0
    without = 0
    for year in range(YEARS):
        results = year_results(SEED + year)
        without += any(isinstance(r, Finding) for r in results)
        state = TrendState()
        opened = False
        for result in results:
            state, step = advance_persistent(state, result, today=result.day)
            opened = opened or step.transition is Transition.OPENED
        with_persistence += opened
    assert with_persistence / YEARS <= 0.10, with_persistence
    assert without / YEARS >= 0.5, without  # persistence is what removes them
