"""Scores (ADR 0012): Health Improvement Score, Body Path and Readiness. Pure.

Every evaluation yields one ``ScoreResult`` whose ``status`` is one of ``Status``. Only
``computed`` has a ``value``; every other status stores ``value`` None and its reasons in
``components`` (a missing value is never 0 or "normal", PLAN §4.2). Nothing here reads a
database, a clock or a file: the wiring (``ownvitals.derived.scores_*``) passes plain values in.
No user-facing string names a quality ("good", "bad", ...): text describes only.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    COMPUTED = "computed"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_AVAILABLE = "not_available"
    NOT_SUPPORTED = "not_supported"
    NOT_SET = "not_set"


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """``components`` always carries ``status``; ``value`` is set only when computed."""

    kind: str
    algo_version: str
    status: Status
    value: float | None = None
    components: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (self.value is not None) != (self.status is Status.COMPUTED):
            raise ValueError("only a computed score has a value")


def result(
    kind: str,
    algo_version: str,
    status: Status,
    components: dict[str, Any],
    value: float | None = None,
) -> ScoreResult:
    return ScoreResult(kind, algo_version, status, value, {"status": status.value, **components})


def clamp(x: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, x))


def round_half_up(x: float) -> int:
    """Integer display value, ties away from zero for the positive scores used here."""
    return int(Decimal(repr(x)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
