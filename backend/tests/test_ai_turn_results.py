"""What a turn keeps for the AI evaluation (ADR 0014 section 9; #527): ``Turn.results`` and
``Turn.sizes``, the client's ``audit_kind`` and ``precheck``, and the closed audit kinds.

Synthetic account and readings (``test_ai_tools_plant``), the fake model, a filler key.
"""

import json
import urllib.request
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sql

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from fakemodel import API_KEY, FakeModel, text, tools
from jobtools import admin_url, engine_for
from ownvitals import egress
from ownvitals.ai.audit import AUDIT_KINDS, EVAL_AUDIT_KIND, AuditEntry
from ownvitals.ai.gates import ModelRefused
from ownvitals.ai.requests import Message, Text, build_request, serialize
from ownvitals.ai.turn import run_turn
from ownvitals.egress import EVAL_HOOK_KIND, core
from ownvitals.jobs.fence import FencedSession
from test_ai_client_env import PRICES, ai_settings, job_run
from test_ai_egress_scan import _job  # pyright: ignore[reportPrivateUsage]
from test_ai_tools_plant import Planted, ai_env_fixture, plant_account  # noqa: F401 - fixture
from viewtools import NOW

SYNTHETIC = True
QUESTION = "How was my weight over the last month?"


@pytest.fixture(autouse=True)
def _ai_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=True)


@pytest.fixture(name="planted")
async def planted_fixture(ai_env: Env) -> Planted:
    return await plant_account(ai_env, "synthetic-turn@example.com", weight=70, pulse=60, grpid=8)


@pytest.fixture(name="key_dir")
def key_dir_fixture(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "anthropic_api_key").write_text(API_KEY)
    return directory


def _kinds(owner: uuid.UUID) -> list[str]:
    with engine_for(admin_url()) as engine, engine.connect() as conn:
        rows = conn.execute(sql("SELECT kind FROM ai_audit WHERE owner_id = :o"), {"o": owner})
        return [str(r[0]) for r in rows]


async def test_a_turn_keeps_its_results_and_sizes_and_audits_its_kind(
    ai_env: Env, planted: Planted, key_dir: Path
) -> None:
    job = job_run(ai_env.factory, planted.ctx.user_id, _job(planted.ctx.user_id))
    fake = FakeModel(
        script=[
            tools(("daily_values", {"metric": "weight", "period": 30}), input_tokens=300),
            text("Synthetic answer.", input_tokens=400),
        ]
    )
    settings = ai_settings(ai_env.settings)
    turn = await run_turn(
        job,
        settings,
        kind="chat",
        history=[Message("user", (Text(QUESTION),))],
        now=NOW,
        prices=PRICES,
        inner=fake.transport,
        secrets_directory=key_dir,
        audit_kind=EVAL_AUDIT_KIND,
    )
    second = fake.seen[1].json["messages"]
    [sent] = [b for m in second for b in m["content"] if b["type"] == "tool_result"]
    assert [json.loads(sent["content"])] == [json.loads(json.dumps(r)) for r in turn.results]
    first = serialize(
        build_request(settings.ai_model, "chat", [Message("user", (Text(QUESTION),))])
    )
    assert turn.sizes[0] == (len(first), 300)
    assert [len(fake.seen[0].body), len(fake.seen[1].body)] == [s for s, _ in turn.sizes]
    assert turn.sizes[1][1] == 400
    assert _kinds(planted.ctx.user_id) == [EVAL_AUDIT_KIND, EVAL_AUDIT_KIND]


async def test_a_refusing_precheck_sends_nothing(
    ai_env: Env, planted: Planted, key_dir: Path
) -> None:
    job = job_run(ai_env.factory, planted.ctx.user_id, _job(planted.ctx.user_id))
    fake = FakeModel()
    checked: list[FencedSession] = []

    async def refuse(fs: FencedSession) -> None:
        checked.append(fs)
        raise ModelRefused("unmarked_account")

    with pytest.raises(ModelRefused) as refused:
        await run_turn(
            job,
            ai_settings(ai_env.settings),
            kind="chat",
            history=[Message("user", (Text(QUESTION),))],
            now=NOW,
            prices=PRICES,
            inner=fake.transport,
            secrets_directory=key_dir,
            precheck=refuse,
        )
    assert refused.value.code == "unmarked_account"
    assert len(checked) == 1
    assert fake.attempts == 0
    assert _kinds(planted.ctx.user_id) == []


def test_the_audit_kinds_are_closed() -> None:
    assert {"chat", "digest", EVAL_HOOK_KIND} == AUDIT_KINDS
    assert EVAL_AUDIT_KIND == EVAL_HOOK_KIND
    with pytest.raises(ValueError, match="audit kind"):
        AuditEntry(
            job_id=None,
            kind="other",
            model="m",
            prompt_version="1",
            toolset_version="1",
            reservation_id=uuid.uuid4(),
            request_hmac=b"",
            status="ok",  # pyright: ignore[reportArgumentType]
            settlement="settled",  # pyright: ignore[reportArgumentType]
            latency_ms=0,
            attempts=1,
            cost_microusd=0,
        )
