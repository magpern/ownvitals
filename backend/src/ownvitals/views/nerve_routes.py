"""``GET /api/nerve/summary``: the monthly summary of the nerve metrics and of N2 and N3 (ADR 0010
section 8: "a view, not a rule"; the Nerve area of the design).

Per metric (nerve health score, nerve response score, ESC) the data state and N1's progress toward
its minimum (4 days with data over 28 days elapsed); per month the days with data and the median,
lowest and highest daily value, and the N2 and N3 findings whose window ended that month. The
findings come only through ``views.findings_read`` as the ``dashboard`` consumer, so the
experimental tier is returned only when the owner-only switch is effective for the caller, and a
hidden finding never is. Months run newest first; a month without data is still listed.
"""

import statistics
from collections import defaultdict
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from ownvitals.analysis.policy import InsufficientData, Purpose, evaluate_metric
from ownvitals.analysis.quality import METRICS
from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.views import assemble
from ownvitals.views.catalog import BAND_DAYS, NERVE_KEYS, STREAM_OF
from ownvitals.views.deps import Now, settle
from ownvitals.views.findings_read import FindingsQuery, read_findings
from ownvitals.views.findings_schema import FindingOut
from ownvitals.views.reads import DailyRecord, reading
from ownvitals.views.schemas import (
    NerveMetricMonth,
    NerveMetricOut,
    NerveMonthOut,
    NerveSummaryOut,
)

router = APIRouter(prefix="/api/nerve")

RULES = ("N2", "N3")
MONTHS_DEFAULT = 12
MONTHS_MAX = 36


def _month(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_labels(today: date, count: int) -> list[str]:
    """The ``count`` months ending with today's, newest first."""
    index = today.year * 12 + today.month - 1
    return [f"{(i // 12):04d}-{(i % 12 + 1):02d}" for i in range(index, index - count, -1)]


def _stats(key: str, rows: list[DailyRecord]) -> NerveMetricMonth:
    if not rows:
        return NerveMetricMonth(
            metric_key=key, days_with_data=0, median=None, minimum=None, maximum=None
        )
    values = [r.value for r in rows]
    return NerveMetricMonth(
        metric_key=key,
        days_with_data=len(rows),
        median=float(statistics.median(values)),
        minimum=float(min(values)),
        maximum=float(max(values)),
    )


@router.get("/summary", response_model=NerveSummaryOut)
async def nerve_summary(
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    now: Now,
    months: Annotated[int, Query(ge=1, le=MONTHS_MAX)] = MONTHS_DEFAULT,
) -> NerveSummaryOut:
    factory = request.app.state.session_factory
    async with reading(factory, ctx) as r:
        facts = await r.status_facts(now)
        daily = await r.daily(NERVE_KEYS)
        revision = await r.measures_revision()
    read = await read_findings(
        factory,
        ctx,
        request.app.state.settings,
        consumer="dashboard",
        query=FindingsQuery(area="nerve"),
    )
    today = now.date()
    stream = settle(facts, STREAM_OF[NERVE_KEYS[0]], now)
    metrics: list[NerveMetricOut] = []
    for key in NERVE_KEYS:
        records = daily[key]
        values = assemble.to_daily(records)
        anchor = assemble.anchor_day(values, today)
        # N1: the nerve trend policy gates the metric; below it, "insufficient data".
        verdict = evaluate_metric(
            key, Purpose.TREND, values, window_end=anchor, window_days=BAND_DAYS
        )
        state, _ = assemble.metric_state(
            stream,
            verdict,
            pending=assemble.recompute_pending(records, revision),
        )
        metrics.append(
            NerveMetricOut(
                key=key,
                unit=METRICS[key].unit,
                state=state,
                progress_percent=stream.progress_percent,
                progress=(
                    assemble.progress_of(verdict, BAND_DAYS)
                    if isinstance(verdict, InsufficientData)
                    else None
                ),
                latest=assemble.latest_of(records),
            )
        )
    labels = month_labels(today, months)
    by_month: dict[str, dict[str, list[DailyRecord]]] = defaultdict(lambda: defaultdict(list))
    for key in NERVE_KEYS:
        for record in daily[key]:
            by_month[_month(record.day)][key].append(record)
    found: dict[str, list[FindingOut]] = defaultdict(list)
    for view in read.findings:
        if view.rule_id in RULES:
            found[_month(view.window_end)].append(FindingOut.of(view))
    return NerveSummaryOut(
        as_of=now,
        metrics=metrics,
        months=[
            NerveMonthOut(
                month=label,
                metrics=[_stats(key, by_month[label][key]) for key in NERVE_KEYS],
                findings=found.get(label, []),
            )
            for label in labels
        ],
    )
