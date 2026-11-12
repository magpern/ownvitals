"""Health Improvement Score (ADR 0012 §2): criterion scores, verticals and the aggregate. Pure.

Criterion score: piecewise linear with anchors perfect 100, good 75, average 50, bad 0 (our
choice), 100 between the low and high ``perfect`` boundaries, 0 beyond ``bad``. Vertical = the
weighted mean of its computed criteria; HIS = the mean of the computed verticals (at least two),
shown as an integer (round half up). With no table, or an invalid one, HIS is ``not_set``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from typing import Any, Final

from ownvitals.analysis.scores import ScoreResult, Status, result, round_half_up
from ownvitals.analysis.scores.his_reasons import vertical_reasons
from ownvitals.analysis.scores.his_table import VERTICALS, BandRow, BandTable, Side

ALGO_VERSION: Final = "his/1"
KIND: Final = "his"
MIN_VERTICALS: Final = 2


@dataclass(frozen=True, slots=True)
class CriterionInput:
    """One criterion's reduced input (``his_inputs``): ``value`` only when ``status`` is
    computed; otherwise the reason it has none."""

    status: Status
    value: float | None = None
    unit: str = ""
    reason: str | None = None
    window: Mapping[str, Any] = field(default_factory=dict)
    data_quality: Mapping[str, Any] = field(default_factory=dict)


def _interp(x: float, points: list[tuple[float, float]]) -> float:
    """Linear between consecutive ``(boundary, score)`` points, ordered by boundary."""
    for (x0, y0), (x1, y1) in pairwise(points):
        if x0 <= x <= x1:
            return y1 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[0][1] if x < points[0][0] else points[-1][1]


def _side_score(x: float, side: Side) -> float:
    pts = [(side.bad, 0.0), (side.average, 50.0), (side.good, 75.0), (side.perfect, 100.0)]
    return _interp(x, pts if side.bad <= side.perfect else pts[::-1])


def criterion_score(x: float, row: BandRow) -> float:
    low, high = row.low, row.high
    if low is not None and x < low.perfect:
        return _side_score(x, low)
    if high is not None and x > high.perfect:
        return _side_score(x, high)
    return 100.0


def pick_row(
    table: BandTable, criterion: str, *, age: int | None, sex: str | None
) -> tuple[int, BandRow] | str:
    """``(index, row)`` or the ``not_set`` / ``not_available`` reason."""
    if age is None:
        return "birth_year_unset"
    rows = [(i, r) for i, r in enumerate(table.rows) if r.criterion == criterion]
    usable = [(i, r) for i, r in rows if r.sex == "any" or r.sex == sex]
    hit = next(((i, r) for i, r in usable if r.age_min <= age <= r.age_max), None)
    if hit is not None:
        return hit
    return "sex_unset" if sex is None and rows else "no_band_row"


def _one(
    table: BandTable, name: str, data: CriterionInput | None, age: int | None, sex: str | None
) -> tuple[dict[str, Any], BandRow | None]:
    picked = pick_row(table, name, age=age, sex=sex)
    if isinstance(picked, str):
        status = Status.NOT_AVAILABLE if picked == "no_band_row" else Status.NOT_SET
        return {"status": status.value, "reason": picked}, None
    index, row = picked
    data = data or CriterionInput(Status.NOT_AVAILABLE, reason="no_input")
    out: dict[str, Any] = {
        "status": data.status.value,
        "unit": row.unit,
        "table_id": table.table_id,
        "row": index,
        "window": dict(data.window),
        "data_quality": dict(data.data_quality),
    }
    if data.status is not Status.COMPUTED or data.value is None:
        return {**out, "reason": data.reason}, None
    return {**out, "value": data.value, "score": criterion_score(data.value, row)}, row


def evaluate_his(
    table: BandTable | None,
    *,
    table_error: str | None = None,
    inputs: Mapping[str, CriterionInput],
    day: date,
    birth_year: int | None,
    sex: str | None,
) -> ScoreResult:
    """HIS on ``day``. ``table`` None: ``not_set`` (``no_band_table``, or ``invalid_band_table``
    when ``table_error`` says it failed validation)."""
    if table is None:
        reason = "invalid_band_table" if table_error else "no_band_table"
        return result(KIND, ALGO_VERSION, Status.NOT_SET, {"reason": reason})
    age = None if birth_year is None else day.year - birth_year
    criteria: dict[str, Any] = {}
    scored: dict[str, list[tuple[float, float]]] = {v: [] for v in VERTICALS}
    for name in sorted({r.criterion for r in table.rows}):
        out, row = _one(table, name, inputs.get(name), age, sex)
        criteria[name] = out
        if row is not None:
            scored[row.vertical].append((out["score"], row.weight))
    verticals: dict[str, float | None] = {
        v: (sum(s * w for s, w in pts) / sum(w for _, w in pts) if pts else None)
        for v, pts in scored.items()
    }
    done = [s for s in verticals.values() if s is not None]
    components: dict[str, Any] = {
        "criteria": criteria,
        "verticals": {v: None if s is None else round_half_up(s) for v, s in verticals.items()},
        "vertical_reasons": vertical_reasons(
            {r.criterion: r.vertical for r in table.rows}, criteria, verticals
        ),
        "table": {"id": table.table_id, "version": table.version, "source": table.source},
        "text_key": "score.his.how",
        "disclaimer_key": "score.his.not_medical",
    }
    if age is None:  # every row is age-banded
        return result(
            KIND, ALGO_VERSION, Status.NOT_SET, {**components, "reason": "birth_year_unset"}
        )
    if len(done) < MIN_VERTICALS:
        return result(
            KIND, ALGO_VERSION, Status.INSUFFICIENT_DATA, {**components, "reason": "one_vertical"}
        )
    return result(
        KIND, ALGO_VERSION, Status.COMPUTED, components, float(round_half_up(sum(done) / len(done)))
    )
