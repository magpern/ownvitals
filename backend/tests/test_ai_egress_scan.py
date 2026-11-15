"""The request-body scan (ADR 0014 sections 4, 5 and 8; #207): over turns that call every tool
with every argument set on a planted synthetic account, no request the fake model receives holds
a secret, a token, an identifier, an email address, free text from Withings, a stored message or
a signal sample, and the API key travels only in its header.

The account is ``test_ai_tools_plant``'s: invented readings, labelled planted strings, a device,
an ECG recording, findings of every tier, scores and a conversation message. No network.
"""

import json
import re
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from fakemodel import API_KEY, FakeModel, text, tools
from jobtools import admin_url, engine_for, new_job
from ownvitals import egress
from ownvitals.ai.requests import Message, Text
from ownvitals.ai.tools_model import MAX_TOOL_CALLS_PER_TURN
from ownvitals.ai.turn import run_turn
from ownvitals.egress import core
from test_ai_client_env import APP_SECRET, PRICES, ai_settings, job_run
from test_ai_tools_plant import (  # noqa: F401 - fixture
    CALLS,
    PLANTED_MESSAGE,
    Planted,
    ai_env_fixture,
    forbidden_strings,
    plant_account,
)
from viewtools import NOW

SYNTHETIC = True
_UUID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.I)
_EMAIL = re.compile(r"[^\s@\"]+@[^\s@\"]+\.[a-z]{2,}", re.I)


@pytest.fixture(autouse=True)
def _ai_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=True)


@pytest.fixture(name="planted")
async def planted_fixture(ai_env: Env) -> Planted:
    return await plant_account(ai_env, "synthetic-scan@example.com", weight=70, pulse=60, grpid=1)


def _job(owner: uuid.UUID) -> uuid.UUID:
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        return new_job(
            conn,
            owner,
            state="running",
            attempt=1,
            lease_version=1,
            leased_by="synthetic-worker",
            lease_expires_at=NOW.replace(year=NOW.year + 1),
            heartbeat_at=NOW,
            started_at=NOW,
        )


def _chunks() -> list[list[tuple[str, dict[str, Any]]]]:
    calls = list(CALLS)
    return [
        calls[i : i + MAX_TOOL_CALLS_PER_TURN]
        for i in range(0, len(calls), MAX_TOOL_CALLS_PER_TURN)
    ]


async def test_no_request_body_holds_a_secret_id_or_sample(
    ai_env: Env, planted: Planted, tmp_path: Path
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "anthropic_api_key").write_text(API_KEY)
    settings = ai_settings(ai_env.settings, app_secret_key=SecretStr(APP_SECRET))
    job_id = _job(planted.ctx.user_id)
    job = job_run(ai_env.factory, planted.ctx.user_id, job_id)
    fake = FakeModel()
    turns = []
    for chunk in _chunks():
        fake.script = [tools(*chunk), text("Synthetic summary.")]
        history = [Message("user", (Text("How am I doing? (synthetic)"),))]
        turns.append(
            await run_turn(
                job,
                settings,
                kind="chat",
                history=history,
                now=NOW,
                prices=PRICES,
                inner=fake.transport,
                secrets_directory=secrets,
            )
        )
    assert all(t.stopped == "answered" for t in turns)
    assert sum(t.tool_calls for t in turns) == len(CALLS)
    assert len(fake.seen) == 2 * len(turns)
    results = [
        block
        for seen in fake.seen
        for message in seen.json["messages"]
        for block in message["content"]
        if block["type"] == "tool_result"
    ]
    assert len(results) == len(CALLS)
    assert not any("error" in json.loads(b["content"]) for b in results)  # every tool really ran
    assert any(json.loads(b["content"]).get("points") for b in results)  # with real values
    forbidden = forbidden_strings(
        planted,
        (API_KEY, APP_SECRET, PLANTED_MESSAGE, str(job_id), "synthetic-scan", "deviceid"),
    )
    for seen in fake.seen:
        body = seen.body.decode()
        for value in forbidden:
            assert value not in body, value
        assert not _UUID.search(body)
        assert not _EMAIL.search(body)
        payload = seen.json
        assert set(payload) == {"model", "max_tokens", "system", "tools", "messages"}
        for key in ("samples", "signal", "metadata", "user_id"):
            assert f'"{key}"' not in body
        assert seen.headers["x-api-key"] == API_KEY
        assert set(seen.headers) >= {"anthropic-version", "content-type"}
        assert not any(API_KEY in v for k, v in seen.headers.items() if k != "x-api-key")
    for block in results:
        points = json.loads(block["content"]).get("points")
        assert points is None or len(points) <= 90


async def test_a_turn_with_an_unknown_tool_ends_without_running_anything(
    ai_env: Env, planted: Planted, tmp_path: Path
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "anthropic_api_key").write_text(API_KEY)
    job = job_run(ai_env.factory, planted.ctx.user_id, _job(planted.ctx.user_id))
    fake = FakeModel(script=[tools(("send_mail", {"to": "synthetic@example.test"}))])
    turn = await run_turn(
        job,
        ai_settings(ai_env.settings),
        kind="chat",
        history=[Message("user", (Text("synthetic"),))],
        now=NOW,
        prices=PRICES,
        inner=fake.transport,
        secrets_directory=secrets,
    )
    assert (turn.stopped, turn.text, turn.tool_calls, len(fake.seen)) == (
        "unknown_tool",
        None,
        0,
        1,
    )


async def test_too_many_tool_calls_end_the_turn(
    ai_env: Env, planted: Planted, tmp_path: Path
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "anthropic_api_key").write_text(API_KEY)
    job = job_run(ai_env.factory, planted.ctx.user_id, _job(planted.ctx.user_id))
    many = [("list_metrics", {})] * (MAX_TOOL_CALLS_PER_TURN - 1)
    fake = FakeModel(script=[tools(*many), tools(("list_metrics", {}), ("list_metrics", {}))])
    turn = await run_turn(
        job,
        ai_settings(ai_env.settings),
        kind="chat",
        history=[Message("user", (Text("synthetic"),))],
        now=NOW,
        prices=PRICES,
        inner=fake.transport,
        secrets_directory=secrets,
    )
    assert (turn.stopped, turn.tool_calls, len(fake.seen)) == (
        "tool_limit",
        MAX_TOOL_CALLS_PER_TURN - 1,
        2,
    )
