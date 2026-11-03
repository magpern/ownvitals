"""``GET /api/metrics`` and ``GET /api/metrics/{key}?range=``: the tiles of the overview and the
metric detail (PLAN sections 7.1 and 8, the Dashboard and Metric mock-ups).

A metric the catalog does not know answers 404, like any key that is not the user's: the
catalog is code, the daily values are read as the signed-in user under forced RLS. Every metric
of the catalog is listed with its data state; a metric with no daily value has no latest value and
no sparkline, and a blocked stream (not supported, not shared, not available, needs reconnect,
Withings+ needed, not connected) says so instead of "no data".
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ownvitals.analysis.quality import METRICS
from ownvitals.auth.deps import require_user
from ownvitals.context import UserContext
from ownvitals.views import assemble
from ownvitals.views.catalog import DEFAULT_RANGE, RANGES, STREAM_OF
from ownvitals.views.deps import Now, settle
from ownvitals.views.reads import reading
from ownvitals.views.schemas import MetricDetailOut, MetricsOut

router = APIRouter(prefix="/api/metrics")

Range = Annotated[str, Query(pattern="^(" + "|".join(RANGES) + ")$")]


@router.get("", response_model=MetricsOut)
async def list_metrics(
    request: Request, ctx: Annotated[UserContext, Depends(require_user)], now: Now
) -> MetricsOut:
    keys = tuple(sorted(METRICS))
    async with reading(request.app.state.session_factory, ctx) as r:
        facts = await r.status_facts(now)
        daily = await r.daily(keys)
        revision = await r.measures_revision()
    today = now.date()
    return MetricsOut(
        as_of=now,
        metrics=[
            assemble.tile(
                key,
                daily[key],
                settle(facts, STREAM_OF[key], now),
                today=today,
                revision=revision,
            )
            for key in keys
        ],
    )


@router.get("/{key}", response_model=MetricDetailOut)
async def get_metric(
    key: str,
    request: Request,
    ctx: Annotated[UserContext, Depends(require_user)],
    now: Now,
    range: Range = DEFAULT_RANGE,
) -> MetricDetailOut:
    if key not in METRICS:
        raise HTTPException(status_code=404)
    async with reading(request.app.state.session_factory, ctx) as r:
        facts = await r.status_facts(now)
        records = (await r.daily((key,)))[key]
        revision = await r.measures_revision()
        values = assemble.to_daily(records)
        anchor = assemble.anchor_day(values, now.date())
        start, days = assemble.window_for(range, values, anchor)
        unconfirmed = await r.unconfirmed_days(key, start, anchor)
    return assemble.detail(
        key,
        records,
        settle(facts, STREAM_OF[key], now),
        unconfirmed,
        range_name=range,
        start=start,
        days=days,
        anchor=anchor,
        revision=revision,
    )
