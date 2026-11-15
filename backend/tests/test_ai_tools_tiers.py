"""The tier filter of the tool layer (ADR 0014 section 4, PLAN section 7.4; #206): experimental
findings, the HIS and ownvitals' ECG estimates (#401) reach a tool only with the owner-only switch
**and** ``ai_include_experimental``; hidden findings and stale estimates never; and without the
flag or consent every tool answers ``not_enabled``.

Synthetic accounts only.
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from authtools import Env, auth_schema_fixture, clean_auth_fixture, running_env  # noqa: F401
from ownvitals.ai import consent
from ownvitals.ai.tools import run_tool
from test_ai_tools_plant import (  # noqa: F401 - fixture
    CALLS,
    Planted,
    ai_env_fixture,
    plant_account,
)
from viewtools import NOW, run

SYNTHETIC = True


@pytest.fixture(name="switch_env")
async def switch_env_fixture(
    clean_auth: None, app_database_url: str, request: pytest.FixtureRequest
) -> AsyncIterator[Env]:
    """``(experimental_findings, ai_include_experimental)`` from the test's parameter."""
    switch, include = request.param
    run("DELETE FROM app_settings WHERE flag IN ('ai_insights', 'experimental_findings')")
    async with running_env(
        app_database_url,
        ai_insights=True,
        experimental_findings=switch,
        ai_include_experimental=include,
    ) as env:
        yield env


async def _rules(env: Env, planted: Planted, status: str = "active") -> list[str]:
    result = await run_tool(
        env.factory,
        planted.ctx,
        env.settings,
        "findings",
        {"status": status, "period": 365},
        now=NOW,
    )
    return sorted(f["rule_id"] for f in result["findings"])


async def _score_kinds(env: Env, planted: Planted, kind: str) -> list[str]:
    result = await run_tool(
        env.factory, planted.ctx, env.settings, "scores", {"kind": kind}, now=NOW
    )
    return [s["kind"] for s in result["scores"]]


@pytest.mark.parametrize(
    ("switch_env", "experimental"),
    [((False, False), False), ((False, True), False), ((True, False), False), ((True, True), True)],
    indirect=["switch_env"],
)
async def test_experimental_items_need_the_switch_and_the_setting(
    switch_env: Env, experimental: bool
) -> None:
    planted = await plant_account(
        switch_env, "synthetic-tier@example.com", weight=70, pulse=60, grpid=1
    )
    rules = await _rules(switch_env, planted)
    if experimental:
        assert rules == ["B1", "C1", "E4"]
    else:
        assert rules == ["C1"]  # absent, not redacted: no B1, no E4 (an experimental rule)
    assert await _rules(switch_env, planted, "history") == ["C1"]
    assert await _score_kinds(switch_env, planted, "his") == (["his"] if experimental else [])
    assert await _score_kinds(switch_env, planted, "readiness") == ["readiness"]
    assert await _score_kinds(switch_env, planted, "body_path") == ["body_path:weight"]


async def _ecg(env: Env, planted: Planted) -> dict[str, Any]:
    return await run_tool(
        env.factory, planted.ctx, env.settings, "ecg_summary", {"period": 365}, now=NOW
    )


@pytest.mark.parametrize(
    ("switch_env", "experimental"),
    [((False, False), False), ((False, True), False), ((True, False), False), ((True, True), True)],
    indirect=["switch_env"],
)
async def test_ecg_estimates_need_the_switch_and_the_setting(
    switch_env: Env, experimental: bool
) -> None:
    planted = await plant_account(
        switch_env, "synthetic-ecg-tier@example.com", weight=70, pulse=60, grpid=1
    )
    result = await _ecg(switch_env, planted)
    assert result["recordings"] == 5  # Withings' own values: for every account
    if experimental:
        # Only the recording with a current, passing estimate; the failed gate has no value.
        assert result["estimates"] == [
            {
                "day": result["items"][0]["day"],
                "heart_rate_bpm": 61.5,
                "rmssd_ms": 31.0,
                "sdnn_ms": 42.0,
            }
        ]
    else:
        assert "estimates" not in result  # absent, not redacted
        assert "61.5" not in json.dumps(result)


@pytest.mark.parametrize("switch_env", [(True, True)], indirect=True)
async def test_a_stale_ecg_estimate_never_reaches_a_tool(switch_env: Env) -> None:
    planted = await plant_account(
        switch_env, "synthetic-ecg-stale@example.com", weight=70, pulse=60, grpid=1
    )
    run(
        "UPDATE ecg_recordings SET estimated = jsonb_set(estimated, '{algo_version}', "
        "'\"synthetic-old\"') WHERE owner_id = :o AND estimated IS NOT NULL",
        o=planted.ctx.user_id,
    )
    assert "estimates" not in await _ecg(switch_env, planted)


@pytest.mark.parametrize("switch_env", [(True, True), (False, False)], indirect=True)
async def test_a_hidden_finding_never_reaches_a_tool(switch_env: Env) -> None:
    planted = await plant_account(
        switch_env, "synthetic-hidden@example.com", weight=70, pulse=60, grpid=1
    )
    for status in ("active", "history"):
        assert "E6" not in await _rules(switch_env, planted, status)


async def test_a_stored_off_switch_keeps_experimental_items_out(
    clean_auth: None, app_database_url: str
) -> None:
    run("DELETE FROM app_settings WHERE flag IN ('ai_insights', 'experimental_findings')")
    run("INSERT INTO app_settings (flag, value) VALUES ('experimental_findings', '\"off\"')")
    async with running_env(
        app_database_url, ai_insights=True, experimental_findings=True, ai_include_experimental=True
    ) as env:
        planted = await plant_account(
            env, "synthetic-stored@example.com", weight=70, pulse=60, grpid=1
        )
        assert await _rules(env, planted) == ["C1"]
        assert await _score_kinds(env, planted, "his") == []
        assert "estimates" not in await _ecg(env, planted)


async def _all_not_enabled(env: Env, planted: Planted) -> None:
    for name, args in CALLS:
        result: dict[str, Any] = await run_tool(
            env.factory, planted.ctx, env.settings, name, args, now=NOW
        )
        assert result == {"error": "not_enabled"}, (name, args)
    # Even an unknown tool or bad arguments answer "not enabled" first: nothing is read.
    assert await run_tool(env.factory, planted.ctx, env.settings, "x", None, now=NOW) == {
        "error": "not_enabled"
    }


async def test_without_consent_every_tool_answers_not_enabled(ai_env: Env) -> None:
    planted = await plant_account(
        ai_env, "synthetic-noconsent@example.com", weight=70, pulse=60, grpid=1
    )
    await consent.withdraw(ai_env.factory, planted.ctx)
    await _all_not_enabled(ai_env, planted)


async def test_without_the_flag_every_tool_answers_not_enabled(
    clean_auth: None, app_database_url: str
) -> None:
    run("DELETE FROM app_settings WHERE flag IN ('ai_insights', 'experimental_findings')")
    async with running_env(app_database_url) as env:
        planted = await plant_account(
            env, "synthetic-noflag@example.com", weight=70, pulse=60, grpid=1
        )
        await _all_not_enabled(env, planted)
    async with running_env(app_database_url, ai_insights=True) as env:
        run("INSERT INTO app_settings (flag, value) VALUES ('ai_insights', '\"off\"')")
        await _all_not_enabled(env, planted)


async def test_an_outdated_consent_answers_not_enabled(ai_env: Env) -> None:
    planted = await plant_account(ai_env, "synthetic-old@example.com", weight=70, pulse=60, grpid=1)
    run(
        "UPDATE ai_consents SET text_sha256 = :h WHERE owner_id = :o",
        h=b"\x01" * 32,
        o=planted.ctx.user_id,
    )
    await _all_not_enabled(ai_env, planted)
