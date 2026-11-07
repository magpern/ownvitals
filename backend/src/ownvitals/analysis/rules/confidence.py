"""Confidence levels of the v1 finding rules (ADR 0010 §5). Pure.

D (data), T (trend), R (run) and J (jump) each give a level and a finding takes the lowest.
Split out of ``ownvitals.analysis.rules.core``, which re-exports every name here.
"""

from enum import StrEnum

from ownvitals.analysis.methods.trend import TrendResult
from ownvitals.analysis.policy import Sufficient

MORE_THAN_LOW_TREND = 20  # fewer points with phi above LOW_TREND_PHI is low confidence (#87)
LOW_TREND_PHI = 0.5


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


def lowest(*levels: Confidence) -> Confidence:
    return min(levels, key=lambda level: _RANK[level])


def confidence_data(*verdicts: Sufficient) -> Confidence:
    """D: ``high`` when every window holds at least twice its requirement's days with data."""
    enough = all(v.data_quality.n_days >= 2 * v.requirement.min_days_with_data for v in verdicts)
    return Confidence.HIGH if enough else Confidence.MEDIUM


def confidence_trend(result: TrendResult) -> Confidence:
    """T: ``low`` on under 20 points with phi above 0.5 (#87), ``high`` when p < alpha/5."""
    phi = result.ar1_phi
    if result.inputs.n < MORE_THAN_LOW_TREND and phi is not None and phi > LOW_TREND_PHI:
        return Confidence.LOW
    return Confidence.HIGH if result.p_value < result.alpha / 5 else Confidence.MEDIUM


def confidence_run(length: int, minimum: int) -> Confidence:
    """R: ``high`` from twice the rule's minimum run (C1: 6 days; N2: 4)."""
    return Confidence.HIGH if length >= 2 * minimum else Confidence.MEDIUM


def confidence_jump(jumped: bool) -> Confidence:
    """J: a trigger reading with #37's ``jump`` flag makes the finding low."""
    return Confidence.LOW if jumped else Confidence.HIGH
