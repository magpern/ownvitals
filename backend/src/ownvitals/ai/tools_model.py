"""The data-minimised tool set the model may call: names, arguments and output schemas (ADR 0014
section 4; PLAN section 7.4; #206). Pure: no I/O.

The model never receives a data dump. It asks for data through these read-only tools, whose
arguments are closed sets (enums, a known metric key) and whose outputs are fixed schemas built
from ``OUTPUT_FIELDS``, the allowlist. No output carries a row id, user id, Withings user id,
hashed device id, group or signal id, email, name, token, raw payload, signal, free text from
Withings or a time of day: days only, metric keys from the catalogue, rule ids, tiers and
states from closed vocabularies. ``tests/test_ai_tools_schema.py`` walks every output model and
fails on any field outside the allowlist, and ``check_fields`` refuses one at run time too.

``TOOLSET_VERSION`` is bumped whenever a tool, its arguments or its output schema change (the
owner's evaluation reference names it, ADR 0014 section 1, lock 3).
"""

from collections.abc import Mapping
from datetime import date
from types import MappingProxyType
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from ownvitals.analysis.quality import METRICS

TOOLSET_VERSION: Final = "1"
RESULT_MAX_BYTES: Final = 64 * 1024  # serialized, per result; over it the tool answers an error
MAX_TOOL_CALLS_PER_TURN: Final = 8
FINDINGS_MAX: Final = 100
DAILY_MAX_POINTS: Final = 90
VALUE_DIGITS: Final = 2  # display precision of every value

Period = Literal[30, 90, 180, 365]
DailyPeriod = Literal[7, 14, 30, 60, 90]
FindingStatus = Literal["active", "history"]
ScoreKind = Literal["readiness", "his", "body_path"]
ErrorCode = Literal["unknown_tool", "invalid_arguments", "result_too_large", "not_enabled"]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _MetricArgs(_Closed):
    metric: str

    @field_validator("metric")
    @classmethod
    def _known(cls, value: str) -> str:
        if value not in METRICS:
            raise ValueError("unknown metric")
        return value


# --- arguments ----------------------------------------------------------------------------------


class ListMetricsArgs(_Closed):
    pass


class MetricSummaryArgs(_MetricArgs):
    period: Period


class DailyValuesArgs(_MetricArgs):
    period: DailyPeriod


class FindingsArgs(_Closed):
    status: FindingStatus
    period: Period


class ScoresArgs(_Closed):
    kind: ScoreKind


# --- outputs ------------------------------------------------------------------------------------


class MetricInfo(_Closed):
    metric: str
    unit: str
    area: str
    state: str


class ListMetricsOut(_Closed):
    metrics: list[MetricInfo]


class MetricSummaryOut(_Closed):
    metric: str
    unit: str
    period_days: int
    state: str
    days_with_data: int
    median: float | None
    typical_low: float | None
    typical_high: float | None
    latest_value: float | None
    latest_day: date | None
    trend_state: str
    trend_direction: str | None
    trend_significant: bool | None
    slope_per_month: float | None
    slope_per_month_low: float | None
    slope_per_month_high: float | None
    coverage: float | None
    longest_gap_days: int | None


class DayValue(_Closed):
    day: date
    value: float


class DailyValuesOut(_Closed):
    metric: str
    unit: str
    period_days: int
    points: list[DayValue]


class FindingItem(_Closed):
    rule_id: str
    tier: str
    severity: str
    confidence: str
    origin: str
    status: str
    window_start: date
    window_end: date
    metric_keys: list[str]


class FindingsOut(_Closed):
    findings: list[FindingItem]
    truncated: bool


class ScoreItem(_Closed):
    kind: str
    day: date
    status: str
    value: float | None
    stale: bool


class ScoresOut(_Closed):
    scores: list[ScoreItem]


class ToolError(_Closed):
    error: ErrorCode


# name -> (arguments, output)
TOOLS: Final[Mapping[str, tuple[type[_Closed], type[_Closed]]]] = MappingProxyType(
    {
        "list_metrics": (ListMetricsArgs, ListMetricsOut),
        "metric_summary": (MetricSummaryArgs, MetricSummaryOut),
        "daily_values": (DailyValuesArgs, DailyValuesOut),
        "findings": (FindingsArgs, FindingsOut),
        "scores": (ScoresArgs, ScoresOut),
    }
)

# The allowlist: every model a tool may return (nested ones included) and its fields, exactly.
OUTPUT_FIELDS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "MetricInfo": frozenset({"metric", "unit", "area", "state"}),
        "ListMetricsOut": frozenset({"metrics"}),
        "MetricSummaryOut": frozenset(
            {
                "metric",
                "unit",
                "period_days",
                "state",
                "days_with_data",
                "median",
                "typical_low",
                "typical_high",
                "latest_value",
                "latest_day",
                "trend_state",
                "trend_direction",
                "trend_significant",
                "slope_per_month",
                "slope_per_month_low",
                "slope_per_month_high",
                "coverage",
                "longest_gap_days",
            }
        ),
        "DayValue": frozenset({"day", "value"}),
        "DailyValuesOut": frozenset({"metric", "unit", "period_days", "points"}),
        "FindingItem": frozenset(
            {
                "rule_id",
                "tier",
                "severity",
                "confidence",
                "origin",
                "status",
                "window_start",
                "window_end",
                "metric_keys",
            }
        ),
        "FindingsOut": frozenset({"findings", "truncated"}),
        "ScoreItem": frozenset({"kind", "day", "status", "value", "stale"}),
        "ScoresOut": frozenset({"scores"}),
        "ToolError": frozenset({"error"}),
    }
)


def check_fields(model: BaseModel) -> None:
    """Refuse a result whose model, or any nested model, is not exactly on the allowlist."""
    name = type(model).__name__
    allowed = OUTPUT_FIELDS.get(name)
    if allowed is None or frozenset(type(model).model_fields) != allowed:
        raise ValueError("a tool result outside the allowlist")
    for field in type(model).model_fields:
        value = getattr(model, field)
        items = value if isinstance(value, list) else [value]
        for item in items:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(item, BaseModel):
                check_fields(item)


def rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), VALUE_DIGITS)


def definitions() -> list[dict[str, Any]]:
    """The tool definitions for the model request: name and argument schema, no description of
    the user's data."""
    return [
        {"name": name, "input_schema": args.model_json_schema()}
        for name, (args, _) in TOOLS.items()
    ]
