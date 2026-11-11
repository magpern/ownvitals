"""The tool set's output schemas are pinned, and nothing outside them reaches the model (ADR 0014
section 4; #206).

- The pure part walks every output model a tool may return (nested ones included) and compares
  it with the schema pinned here: a field added to a model without changing this file fails,
  and ``check_fields`` refuses a result whose model is not on the allowlist.
- The database part runs every tool with every argument set on a synthetic account that holds
  planted identifiers and free text, and fails if any of them appears in any output.

Synthetic data only.
"""

import json
import types
import typing
from datetime import date, timedelta
from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel, ConfigDict

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from ownvitals.ai import tools, tools_model
from ownvitals.ai.gates import ModelRefused, check_locks
from ownvitals.ai.pricing import Price
from ownvitals.ai.requests import PROMPT_VERSION, eval_ref
from ownvitals.ai.tools import run_tool
from ownvitals.ai.tools_model import OUTPUT_FIELDS, OUTPUT_VARIANTS, TOOLS, check_fields
from ownvitals.ai.tools_vitals_model import EcgClassification
from ownvitals.config import Settings
from test_ai_tools_plant import (  # noqa: F401 - fixture
    CALLS,
    STREAM_DAYS,
    STREAM_DUPLICATE,
    ai_env_fixture,
    call_all,
    forbidden_strings,
    plant_account,
)
from viewtools import NOW

SYNTHETIC = True
PRICE = Price(1, 1, 1, 1, date(2026, 11, 11), "synthetic test price")  # made up

# The pinned output schema: model -> field -> type. Changing a tool's output means changing this
# table, bumping TOOLSET_VERSION and passing the owner's eval again (ADR 0014 section 1).
PINNED: dict[str, dict[str, Any]] = {
    "ListMetricsOut": {"metrics": "list[MetricInfo]"},
    "MetricInfo": {"metric": str, "unit": str, "area": str, "state": str},
    "MetricSummaryOut": {
        "metric": str,
        "unit": str,
        "period_days": int,
        "state": str,
        "days_with_data": int,
        "median": float | None,
        "typical_low": float | None,
        "typical_high": float | None,
        "latest_value": float | None,
        "latest_day": date | None,
        "trend_state": str,
        "trend_direction": str | None,
        "trend_significant": bool | None,
        "slope_per_month": float | None,
        "slope_per_month_low": float | None,
        "slope_per_month_high": float | None,
        "coverage": float | None,
        "longest_gap_days": int | None,
    },
    "DailyValuesOut": {"metric": str, "unit": str, "period_days": int, "points": "list[DayValue]"},
    "DayValue": {"day": date, "value": float},
    "FindingsOut": {"findings": "list[FindingItem]", "truncated": bool},
    "FindingItem": {
        "rule_id": str,
        "tier": str,
        "severity": str,
        "confidence": str,
        "origin": str,
        "status": str,
        "window_start": date,
        "window_end": date,
        "metric_keys": list[str],
    },
    "ScoresOut": {"scores": "list[ScoreItem]"},
    "ScoreItem": {"kind": str, "day": date, "status": str, "value": float | None, "stale": bool},
    "ToolError": {"error": tools_model.ErrorCode},
    # #401: the ECG, sleep and activity tools.
    "EcgSummaryOut": {
        "period_days": int,
        "recordings": int,
        "classifications": "list[EcgClassCount]",
        "items": "list[EcgRecordingItem]",
        "truncated": bool,
    },
    "EcgSummaryExperimentalOut": {
        "period_days": int,
        "recordings": int,
        "classifications": "list[EcgClassCount]",
        "items": "list[EcgRecordingItem]",
        "truncated": bool,
        "estimates": "list[EcgEstimateItem]",
    },
    "EcgClassCount": {"classification": EcgClassification, "count": int},
    "EcgRecordingItem": {
        "day": date,
        "classification": EcgClassification,
        "heart_rate_bpm": int | None,
        "qrs_duration_ms": float | None,
        "pr_interval_ms": float | None,
        "qt_interval_ms": float | None,
        "qtc_interval_ms": float | None,
    },
    "EcgEstimateItem": {
        "day": date,
        "heart_rate_bpm": float | None,
        "rmssd_ms": float | None,
        "sdnn_ms": float | None,
    },
    "SleepSummaryOut": {"period_days": int, "nights_with_data": int, "nights": "list[SleepNight]"},
    "SleepNight": {
        "day": date,
        **dict.fromkeys(
            (
                "sleep_seconds",
                "deep_seconds",
                "light_seconds",
                "rem_seconds",
                "wakeups",
                "efficiency",
                "sleep_score",
                "heart_rate_bpm",
            ),
            float | None,
        ),
    },
    "ActivitySummaryOut": {"period_days": int, "days_with_data": int, "days": "list[ActivityDay]"},
    "ActivityDay": {
        "day": date,
        **dict.fromkeys(
            (
                "steps",
                "distance_m",
                "elevation_m",
                "soft_seconds",
                "moderate_seconds",
                "intense_seconds",
                "active_kcal",
            ),
            float | None,
        ),
    },
}


def _walk(model: type[BaseModel], found: dict[str, dict[str, Any]]) -> None:
    """Every field of ``model`` and of the models it nests, with its declared type."""
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        annotation = info.annotation
        nested = [
            arg
            for arg in (annotation, *get_args(annotation))
            if isinstance(arg, type) and issubclass(arg, BaseModel)
        ]
        if nested:
            (inner,) = nested
            fields[name] = f"list[{inner.__name__}]" if get_origin(annotation) is list else inner
            _walk(inner, found)
        else:
            fields[name] = annotation
        assert not _has_any(annotation), (model.__name__, name)
    found[model.__name__] = fields


def _has_any(annotation: Any) -> bool:
    if annotation is Any or annotation is dict or get_origin(annotation) is dict:
        return True
    return any(_has_any(arg) for arg in get_args(annotation) if not isinstance(arg, str))


def _same_type(a: Any, b: Any) -> bool:
    if a == b:
        return True
    union = (typing.Union, types.UnionType)
    return (
        get_origin(a) in union and get_origin(b) in union and set(get_args(a)) == set(get_args(b))
    )


def test_every_output_model_is_exactly_the_pinned_schema() -> None:
    found: dict[str, dict[str, Any]] = {}
    for _, output in TOOLS.values():
        _walk(output, found)
    for name, variants in OUTPUT_VARIANTS.items():
        for variant in variants:
            assert issubclass(variant, TOOLS[name][1]), name
            _walk(variant, found)
    _walk(tools_model.ToolError, found)
    assert set(found) == set(PINNED)
    for model, fields in PINNED.items():
        assert set(found[model]) == set(fields), model
        for name, expected in fields.items():
            assert _same_type(found[model][name], expected), (model, name, found[model][name])
    # The runtime allowlist is the same table.
    assert {k: set(v) for k, v in OUTPUT_FIELDS.items()} == {k: set(v) for k, v in PINNED.items()}


def test_a_field_outside_the_allowlist_is_refused() -> None:
    class ScoreItem(tools_model.ScoreItem):  # the same name, one field more
        model_config = ConfigDict(extra="forbid", frozen=True)
        owner_id: str

    leaky = ScoreItem(
        kind="readiness", day=NOW.date(), status="computed", value=1.0, stale=False, owner_id="x"
    )
    with pytest.raises(ValueError, match="allowlist"):
        check_fields(leaky)
    with pytest.raises(ValueError, match="allowlist"):
        check_fields(tools_model.ScoresOut.model_construct(scores=[leaky]))

    class Unknown(BaseModel):
        error: str

    with pytest.raises(ValueError, match="allowlist"):
        check_fields(Unknown(error="unknown_tool"))
    check_fields(tools_model.ToolError(error="unknown_tool"))


def test_the_output_schemas_name_no_identifier() -> None:
    banned = {"id", "owner_id", "user_id", "email", "name", "grpid", "signalid", "hash_deviceid"}
    banned |= {"connection_id", "deviceid", "token", "subject", "evidence", "ts", "time"}
    banned |= {"recording_id", "signal", "samples", "leads", "rpeaks", "recorded_at", "source_tz"}
    banned |= {"model_id", "device", "zone", "sampling_hz", "wearposition", "afib_details"}
    for fields in PINNED.values():
        assert not set(fields) & banned


def test_the_arguments_are_closed_sets() -> None:
    for name, (args, _) in TOOLS.items():
        assert args.model_config.get("extra") == "forbid", name
        schema = args.model_json_schema()
        for field, spec in schema.get("properties", {}).items():
            if field == "metric":
                continue  # validated against the metric catalogue
            assert "enum" in spec or "const" in spec, (name, field, spec)
    definitions = tools_model.definitions()
    assert [d["name"] for d in definitions] == list(TOOLS)
    assert all(set(d) == {"name", "input_schema"} for d in definitions)


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("metric_summary", {"metric": "weight", "period": 31}),
        ("metric_summary", {"metric": "not_a_metric", "period": 30}),
        ("metric_summary", {"metric": "weight", "period": 30, "owner_id": "x"}),
        ("daily_values", {"metric": "weight", "period": 365}),
        ("findings", {"status": "all", "period": 30}),
        ("scores", {"kind": "body_path:weight"}),
        ("list_metrics", {"user": "someone"}),
        ("list_metrics", ["not", "a", "mapping"]),
        ("ecg_summary", {"period": 7}),
        ("ecg_summary", {}),
        ("ecg_summary", {"period": 30, "include_estimates": True}),
        ("ecg_summary", {"period": "30"}),
        ("sleep_summary", {"period": 180}),
        ("sleep_summary", {"period": 30, "resolution": "intraday"}),
        ("activity_summary", {"period": 365}),
        ("activity_summary", {"period": 30, "user_id": "someone"}),
    ],
)
async def test_arguments_outside_the_sets_are_refused(
    ai_env: Env, name: str, arguments: Any
) -> None:
    planted = await plant_account(
        ai_env, "synthetic-args@example.com", weight=70, pulse=60, grpid=1
    )
    result = await run_tool(ai_env.factory, planted.ctx, ai_env.settings, name, arguments, now=NOW)
    assert result == {"error": "invalid_arguments"}


async def test_an_unknown_tool_is_refused(ai_env: Env) -> None:
    planted = await plant_account(ai_env, "synthetic-unk@example.com", weight=70, pulse=60, grpid=1)
    for name in ("delete_account", "send_mail", "sql", "export", ""):
        result = await run_tool(ai_env.factory, planted.ctx, ai_env.settings, name, {}, now=NOW)
        assert result == {"error": "unknown_tool"}


async def test_no_planted_identifier_or_text_appears_in_any_output(ai_env: Env) -> None:
    planted = await plant_account(
        ai_env, "synthetic-leak@example.com", weight=81.5, pulse=63, grpid=987_000_000
    )
    results = await call_all(ai_env, planted.ctx)
    assert len(results) == len(CALLS)
    forbidden = forbidden_strings(planted)
    for name, args, result in results:
        assert "error" not in result, (name, args, result)
        blob = json.dumps(result)
        for value in forbidden:
            assert value not in blob, (name, args, value)
        for time_of_day in ("T0", "T1", ":00"):  # days only, never a time of day
            assert time_of_day not in blob, (name, args)
    by_name = {(name, json.dumps(args)): result for name, args, result in results}
    metrics = by_name[("list_metrics", "{}")]["metrics"]
    assert {m["metric"] for m in metrics} >= {"weight", "heart_pulse"}
    summary = by_name[("metric_summary", json.dumps({"metric": "weight", "period": 90}))]
    assert summary["days_with_data"] > 0
    assert summary["median"] is not None
    daily = by_name[("daily_values", json.dumps({"metric": "weight", "period": 90}))]
    assert 0 < len(daily["points"]) <= 90


async def test_the_vitals_tools_return_days_and_withings_values_only(ai_env: Env) -> None:
    planted = await plant_account(
        ai_env, "synthetic-vitals@example.com", weight=70, pulse=60, grpid=1
    )

    async def call(name: str, period: int) -> dict[str, Any]:
        args = {"period": period}
        return await run_tool(ai_env.factory, planted.ctx, ai_env.settings, name, args, now=NOW)

    ecg = await call("ecg_summary", 90)
    assert set(ecg) == {"period_days", "recordings", "classifications", "items", "truncated"}
    # Three of ECG_PLAN's recordings and the hidden-finding recording fall in the 90 days.
    assert ecg["recordings"] == 4
    assert {c["classification"]: c["count"] for c in ecg["classifications"]} == {
        "negative": 1,
        "positive": 1,
        "not_reported": 2,
    }
    newest = ecg["items"][0]
    assert newest == {
        "day": (NOW.date() - timedelta(days=2)).isoformat(),
        "classification": "negative",
        "heart_rate_bpm": 60,
        "qrs_duration_ms": 96.0,
        "pr_interval_ms": 158.0,
        "qt_interval_ms": 392.0,
        "qtc_interval_ms": 411.0,
    }
    assert (await call("ecg_summary", 365))["recordings"] == 5
    assert (await call("ecg_summary", 30))["recordings"] == 2

    sleep = await call("sleep_summary", 90)
    # The period ends today; the planted days end yesterday.
    assert sleep["nights_with_data"] == len(sleep["nights"]) == 89
    last = sleep["nights"][-1]
    assert last["day"] == (NOW.date() - timedelta(days=1)).isoformat()
    assert last["sleep_seconds"] == 25_000 + 10 * (STREAM_DAYS - 1)
    assert last["efficiency"] == 0.9
    assert set(last) == set(PINNED["SleepNight"])
    assert (await call("sleep_summary", 7))["nights_with_data"] == 6  # the days end yesterday

    activity = await call("activity_summary", 90)
    assert activity["days_with_data"] == 89
    steps = [d["steps"] for d in activity["days"]]
    assert STREAM_DUPLICATE not in steps  # a duplicate row is left out
    assert all(6_000 <= s <= 6_002 for s in steps)
    assert "hr_zone_3" not in json.dumps(activity)
    assert "breathing" not in json.dumps(sleep)


async def test_a_result_over_the_cap_is_an_error_not_truncated_data(
    ai_env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = await plant_account(ai_env, "synthetic-cap@example.com", weight=70, pulse=60, grpid=1)
    sizes = {}
    for name, args in CALLS:
        result = await run_tool(ai_env.factory, planted.ctx, ai_env.settings, name, args, now=NOW)
        sizes[name, json.dumps(args)] = len(json.dumps(result, separators=(",", ":")).encode())
    assert max(sizes.values()) <= tools_model.RESULT_MAX_BYTES
    monkeypatch.setattr(tools, "RESULT_MAX_BYTES", 200)
    for name in ("ecg_summary", "sleep_summary", "activity_summary"):
        result = await run_tool(
            ai_env.factory, planted.ctx, ai_env.settings, name, {"period": 90}, now=NOW
        )
        assert result == {"error": "result_too_large"}, name


def test_a_new_tool_set_needs_a_new_eval_run() -> None:
    """ADR 0014 section 1, lock 3: an evaluation that passed for the previous tool set (version
    1, before #401's tools) no longer opens the gate."""
    assert tools_model.TOOLSET_VERSION == "2"
    settings = Settings.model_construct(
        ai_insights=True,
        ai_model="synthetic-model-1",
        ai_provider_review_ref="synthetic-review",
        ai_eval_ref=f"synthetic-model-1/{PROMPT_VERSION}/1",
    )
    with pytest.raises(ModelRefused, match="ai_eval_mismatch"):
        check_locks(settings, prices={"synthetic-model-1": PRICE})
    current = settings.model_copy(update={"ai_eval_ref": eval_ref("synthetic-model-1")})
    assert check_locks(current, prices={"synthetic-model-1": PRICE}) == "synthetic-model-1"
