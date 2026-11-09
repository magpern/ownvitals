"""The tools the model may call, executed for one user (ADR 0014 sections 4 and 8; PLAN section
7.4; #206).

``run_tool`` is the one entry point. In order:

1. **Enabled**: the effective ``ai_insights`` flag and a live consent to the current text
   (``ai.consent.check_enabled``), read as the caller. Otherwise ``not_enabled`` and nothing is
   read.
2. **Arguments**: a known tool name and arguments valid against its closed model
   (``tools_model``); otherwise ``unknown_tool`` or ``invalid_arguments``.
3. **Reads**: only through the read API's reader (``views.reads``, the same assembly as
   ``/api/metrics`` and ``/api/scores``) and the one findings read function
   (``views.findings_read``, the ``ai_context`` consumer: hidden findings absent), each in its
   own ``user_session`` of the caller's ``UserContext`` under forced RLS, in web and in the
   worker alike. No tool takes a user argument, and none writes.
4. **The tier filter, here and not in the prompt**: experimental findings and the experimental
   scores (the HIS) reach a tool only when the owner-only switch is on (``flags.effective``; the
   household gate until M8) **and** ``ai_include_experimental`` is true (default false, ADR 0014
   open question 4). Otherwise the reads run with the switch off, so they are never loaded:
   absent, not redacted. ECG estimates are never returned by any tool.
5. **The result**: the output model is checked against the allowlist (``check_fields``) and
   serialized; over 64 KiB it is ``result_too_large``, never truncated data.

The result is plain JSON for the model; it names no id, token, email, device or time of day.
"""

import json
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.ai import consent
from ownvitals.ai.tools_model import (
    DAILY_MAX_POINTS,
    FINDINGS_MAX,
    RESULT_MAX_BYTES,
    TOOLS,
    DailyValuesArgs,
    DailyValuesOut,
    DayValue,
    FindingItem,
    FindingsArgs,
    FindingsOut,
    ListMetricsOut,
    MetricInfo,
    MetricSummaryArgs,
    MetricSummaryOut,
    ScoreItem,
    ScoresArgs,
    ScoresOut,
    ToolError,
    check_fields,
    rounded,
)
from ownvitals.analysis.quality import METRICS
from ownvitals.config import Settings
from ownvitals.context import UserContext, require_context
from ownvitals.views import assemble, assemble_detail
from ownvitals.views.catalog import STREAM_OF
from ownvitals.views.deps import settle
from ownvitals.views.findings_read import FindingsQuery, read_findings
from ownvitals.views.reads import reading

Factory = async_sessionmaker[AsyncSession]
# (factory, ctx, settings, validated arguments, now) -> the output model
Handler = Callable[..., Awaitable[BaseModel]]

DAYS_PER_MONTH: Final = 30  # the slope is reported per 30 days
_RULE_ID: Final = re.compile(r"[A-Z][0-9]{1,2}")
SCORE_STATUSES: Final = frozenset(
    {"computed", "insufficient_data", "not_available", "not_supported", "not_set"}
)
_BODY_PATH: Final = "body_path:"


def scoped(settings: Settings) -> Settings:
    """The settings the reads run with: the experimental switch off unless
    ``ai_include_experimental`` allows the model to see what the switch shows."""
    if settings.ai_include_experimental:
        return settings
    return settings.model_copy(update={"experimental_findings": False})


async def _list_metrics(
    factory: Factory, ctx: UserContext, settings: Settings, _args: Any, now: datetime
) -> ListMetricsOut:
    keys = tuple(sorted(METRICS))
    async with reading(factory, ctx) as r:
        facts = await r.status_facts(now)
        daily = await r.daily(keys)
        revision = await r.measures_revision()
    tiles = [
        assemble.tile(
            key, daily[key], settle(facts, STREAM_OF[key], now), today=now.date(), revision=revision
        )
        for key in keys
        if daily[key]
    ]
    return ListMetricsOut(
        metrics=[MetricInfo(metric=t.key, unit=t.unit, area=t.area, state=t.state) for t in tiles]
    )


async def _metric_summary(
    factory: Factory, ctx: UserContext, settings: Settings, args: MetricSummaryArgs, now: datetime
) -> MetricSummaryOut:
    key, days = args.metric, args.period
    async with reading(factory, ctx) as r:
        facts = await r.status_facts(now)
        records = (await r.daily((key,)))[key]
        revision = await r.measures_revision()
    anchor = assemble.anchor_day(assemble.to_daily(records), now.date())
    out = assemble_detail.detail(
        key,
        records,
        settle(facts, STREAM_OF[key], now),
        None,
        range_name=f"{days}d",
        start=anchor - timedelta(days=days - 1),
        days=days,
        anchor=anchor,
        revision=revision,
    )
    quality = out.data_quality or {}
    trend = out.trend
    per_month = None if trend.slope_per_day is None else trend.slope_per_day * DAYS_PER_MONTH

    def month(value: float | None) -> float | None:
        return None if value is None else rounded(value * DAYS_PER_MONTH)

    return MetricSummaryOut(
        metric=key,
        unit=out.unit,
        period_days=days,
        state=out.state,
        days_with_data=int(quality.get("n_days", 0)),
        median=None if out.band is None else rounded(out.band.median),
        typical_low=None if out.band is None else rounded(out.band.low),
        typical_high=None if out.band is None else rounded(out.band.high),
        latest_value=None if out.latest is None else rounded(out.latest.value),
        latest_day=None if out.latest is None else out.latest.day,
        trend_state=trend.state,
        trend_direction=trend.direction,
        trend_significant=trend.significant,
        slope_per_month=rounded(per_month),
        slope_per_month_low=month(trend.slope_low),
        slope_per_month_high=month(trend.slope_high),
        coverage=rounded(quality.get("coverage")),
        longest_gap_days=quality.get("longest_gap_days"),
    )


async def _daily_values(
    factory: Factory, ctx: UserContext, settings: Settings, args: DailyValuesArgs, now: datetime
) -> DailyValuesOut:
    key, days = args.metric, args.period
    async with reading(factory, ctx) as r:
        records = (await r.daily((key,)))[key]
    values = assemble.to_daily(records)
    anchor = assemble.anchor_day(values, now.date())
    start = anchor - timedelta(days=days - 1)
    points = [
        DayValue(day=v.day, value=round(float(v.value), 2))
        for v in values
        if start <= v.day <= anchor
    ][-DAILY_MAX_POINTS:]
    return DailyValuesOut(metric=key, unit=METRICS[key].unit, period_days=days, points=points)


async def _findings(
    factory: Factory, ctx: UserContext, settings: Settings, args: FindingsArgs, now: datetime
) -> FindingsOut:
    found = await read_findings(
        factory, ctx, settings, consumer="ai_context", query=FindingsQuery()
    )
    since = now.date() - timedelta(days=args.period - 1)
    items = [
        FindingItem(
            rule_id=v.rule_id,
            tier=v.tier,
            severity=v.severity,
            confidence=v.confidence,
            origin=v.origin,
            status=v.status,
            window_start=v.window_start,
            window_end=v.window_end,
            metric_keys=[k for k in v.metric_keys if k in METRICS],
        )
        for v in found.findings
        if _RULE_ID.fullmatch(v.rule_id)
        and (v.status == "active") == (args.status == "active")
        and v.window_end >= since
    ]
    return FindingsOut(findings=items[:FINDINGS_MAX], truncated=len(items) > FINDINGS_MAX)


def _score_kind(kind: str, wanted: str) -> bool:
    if wanted == "body_path":
        return kind.startswith(_BODY_PATH) and kind.removeprefix(_BODY_PATH) in METRICS
    return kind == wanted


async def _scores(
    factory: Factory, ctx: UserContext, settings: Settings, args: ScoresArgs, now: datetime
) -> ScoresOut:
    async with reading(factory, ctx) as r:
        rows = await r.latest_scores(settings=settings)
    items = [
        ScoreItem(
            kind=str(row.kind),
            day=row.day,
            status=str(row.components["status"]),
            value=None if row.value is None else rounded(float(row.value)),
            stale=stale,
        )
        for row, stale in rows
        if _score_kind(str(row.kind), args.kind)
        and str(row.components.get("status")) in SCORE_STATUSES
    ]
    return ScoresOut(scores=sorted(items, key=lambda s: s.kind))


_HANDLERS: Final[Mapping[str, Handler]] = MappingProxyType(
    {
        "list_metrics": _list_metrics,
        "metric_summary": _metric_summary,
        "daily_values": _daily_values,
        "findings": _findings,
        "scores": _scores,
    }
)


def _error(code: Any) -> dict[str, Any]:
    return ToolError(error=code).model_dump(mode="json")


async def run_tool(
    factory: Factory,
    ctx: UserContext,
    settings: Settings,
    name: str,
    arguments: object,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Run tool ``name`` with ``arguments`` (as the model sent them) for ``ctx``'s user; the
    JSON result, or ``{"error": code}``."""
    require_context(ctx)
    try:
        await consent.check_enabled(factory, ctx, settings)
    except consent.AiNotEnabled:
        return _error("not_enabled")
    spec, handler = TOOLS.get(name), _HANDLERS.get(name)
    if spec is None or handler is None:
        return _error("unknown_tool")
    if not isinstance(arguments, Mapping):
        return _error("invalid_arguments")
    try:
        args = spec[0].model_validate(dict(arguments))  # pyright: ignore[reportUnknownArgumentType]
    except ValidationError:
        return _error("invalid_arguments")
    result = await handler(factory, ctx, scoped(settings), args, now)
    if not isinstance(result, spec[1]):
        raise TypeError("a tool returned another model than its own")
    check_fields(result)
    data = result.model_dump(mode="json")
    if len(json.dumps(data, separators=(",", ":")).encode()) > RESULT_MAX_BYTES:
        return _error("result_too_large")
    return data
