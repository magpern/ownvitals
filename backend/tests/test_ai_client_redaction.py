"""The log-redaction test extended to AI (ADR 0014 section 6; #207): a turn that carries a planted
user message, a tool result and a planted answer leaves no message text, tool result, system
prompt or API key in any log line (formatted by the production ``JsonFormatter``) or in any
column of ``ai_audit``; the row holds the tool names, counts, codes and keyed fingerprints, and
a fingerprint matches only the exact bytes sent.

Synthetic account (``test_ai_tools_plant``), the fake model, no network.
"""

import hashlib
import hmac
import json
import logging
import urllib.request
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sql

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from fakemodel import API_KEY, FakeModel, text, tools
from jobtools import admin_url, engine_for
from ownvitals import egress
from ownvitals.ai.audit import AUDIT_LABEL
from ownvitals.ai.requests import SYSTEM_PROMPT, Message, Text
from ownvitals.ai.turn import run_turn
from ownvitals.auth.keys import derive_key
from ownvitals.egress import core
from ownvitals.log import QUIET_LOGGERS, JsonFormatter
from test_ai_client_env import APP_SECRET, PRICES, ai_settings, job_run
from test_ai_egress_scan import _job  # pyright: ignore[reportPrivateUsage]
from test_ai_tools_plant import Planted, ai_env_fixture, plant_account  # noqa: F401 - fixture
from viewtools import NOW

SYNTHETIC = True
QUESTION = "synthetic-planted-question-" + "Q" * 20
ANSWER = "synthetic-planted-answer-" + "A" * 20


@pytest.fixture(autouse=True)
def _ai_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=True)


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.handler.setFormatter(JsonFormatter())
    caplog.set_level(logging.DEBUG)
    for name in QUIET_LOGGERS:
        caplog.set_level(logging.DEBUG, logger=name)
    return caplog


@pytest.fixture(name="planted")
async def planted_fixture(ai_env: Env) -> Planted:
    return await plant_account(ai_env, "synthetic-redact@example.com", weight=70, pulse=60, grpid=7)


def _audit_rows(owner: uuid.UUID) -> list[dict[str, object]]:
    with engine_for(admin_url()) as engine, engine.connect() as conn:
        rows = conn.execute(
            sql("SELECT * FROM ai_audit WHERE owner_id = :o ORDER BY created_at"), {"o": owner}
        ).mappings()
        return [dict(r) for r in rows]


async def test_no_content_reaches_a_log_line_or_an_audit_row(
    ai_env: Env, planted: Planted, tmp_path: Path, logs: pytest.LogCaptureFixture
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "anthropic_api_key").write_text(API_KEY)
    job = job_run(ai_env.factory, planted.ctx.user_id, _job(planted.ctx.user_id))
    fake = FakeModel(
        script=[tools(("daily_values", {"metric": "weight", "period": 30})), text(ANSWER)]
    )
    turn = await run_turn(
        job,
        ai_settings(ai_env.settings),
        kind="chat",
        history=[Message("user", (Text(QUESTION),))],
        now=NOW,
        prices=PRICES,
        inner=fake.transport,
        secrets_directory=secrets,
    )
    assert turn.text == ANSWER
    second = fake.seen[1].json
    [result] = [b for m in second["messages"] for b in m["content"] if b["type"] == "tool_result"]
    tool_result = result["content"]
    assert json.loads(tool_result)["points"]  # a real result with values went to the model
    rows = _audit_rows(planted.ctx.user_id)
    assert len(rows) == 2
    stored = json.dumps(rows, default=lambda v: v.hex() if isinstance(v, bytes) else str(v))
    logged = logs.text
    assert any(r.getMessage() == "ai call" for r in logs.records)
    first_value = str(json.loads(tool_result)["points"][0]["value"])
    for haystack in (stored, logged):
        for planted_text in (
            QUESTION,
            ANSWER,
            tool_result,
            SYSTEM_PROMPT[:40],
            API_KEY,
            APP_SECRET,
        ):
            assert planted_text not in haystack
        assert "Q" * 20 not in haystack
        assert "A" * 20 not in haystack
        assert '"points"' not in haystack
    assert first_value not in json.dumps([r["tools"] for r in rows])
    assert rows[0]["tools"] == [
        {"name": "daily_values", "arguments": {"metric": "weight", "period": 30}}
    ]
    assert rows[1]["tools"] == []
    key = derive_key(APP_SECRET.encode(), AUDIT_LABEL)
    for row, seen in zip(rows, fake.seen, strict=True):
        assert row["request_hmac"] == hmac.new(key, seen.body, hashlib.sha256).digest()
        assert row["request_hmac"] != hashlib.sha256(seen.body).digest()
        assert row["status"] == "completed"


async def test_free_text_in_a_tool_request_reaches_no_audit_column_or_log_line(
    ai_env: Env, planted: Planted, tmp_path: Path, logs: pytest.LogCaptureFixture
) -> None:
    """The model's ``tool_use`` input is text the provider controls (it can echo the user): only
    a known tool's name and its validated arguments are recorded, an unknown name as
    ``unknown``, invalid arguments as ``null``."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "anthropic_api_key").write_text(API_KEY)
    settings = ai_settings(ai_env.settings)
    echoed = {"metric": "weight", "period": 30, "note": QUESTION}
    for script, stopped in (
        ([tools(("daily_values", echoed)), text(ANSWER)], "answered"),
        ([tools((QUESTION, {"q": QUESTION}))], "unknown_tool"),
    ):
        job = job_run(ai_env.factory, planted.ctx.user_id, _job(planted.ctx.user_id))
        fake = FakeModel(script=script)
        turn = await run_turn(
            job,
            settings,
            kind="chat",
            history=[Message("user", (Text("synthetic question"),))],
            now=NOW,
            prices=PRICES,
            inner=fake.transport,
            secrets_directory=secrets,
        )
        assert turn.stopped == stopped
    rows = _audit_rows(planted.ctx.user_id)
    assert [r["tools"] for r in rows] == [
        [{"name": "daily_values", "arguments": None}],
        [],
        [{"name": "unknown", "arguments": None}],
    ]
    stored = json.dumps(rows, default=lambda v: v.hex() if isinstance(v, bytes) else str(v))
    for haystack in (stored, logs.text):
        assert QUESTION not in haystack
        assert "Q" * 20 not in haystack
        assert ANSWER not in haystack
