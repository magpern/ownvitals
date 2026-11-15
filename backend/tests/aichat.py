"""Shared setup of the chat assistant's tests (``test_ai_assistant*.py``, ``ai_evals/``; ADR 0014
sections 3, 5, 8 and 9; #208). No tests here.

``chat_env``: the app with AI on and every owner lock open (a synthetic model id, review and
evaluation references, large caps), the fake model (``fakemodel.FakeModel``) behind the real
egress client, and a worker session factory. ``Chat`` drives one signed-in, consenting user
through the API and runs the queued turns with the worker's runner, as the worker does.

Synthetic accounts (``example.test``-style addresses), invented prices and filler secrets; no
network, no real model output, no health data.
"""

import asyncio
import urllib.request
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from authtools import Env, running_env
from fakemodel import API_KEY, MODEL, FakeModel
from flows import Browser, signed_in
from jobcore import settings_for
from jobtools import admin_url, engine_for
from ownvitals import egress
from ownvitals.ai.assistant import run_chat_turn
from ownvitals.ai.chat_model import CHAT_KIND, ChatInput
from ownvitals.ai.requests import eval_ref
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.db import create_session_factory
from ownvitals.egress import core
from ownvitals.jobs import JobKind, KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.worker.loop import create_worker_engine
from test_ai_client_env import PRICES
from test_ai_tools_plant import give_consent, run

SYNTHETIC = True
BIG = 10**12
LOCKS_OPEN: dict[str, object] = {
    "ai_insights": True,
    "ai_model": MODEL,
    "ai_provider_review_ref": "synthetic-review-ref",
    "ai_eval_ref": eval_ref(MODEL),
    "ai_user_monthly_cap_microusd": BIG,
    "ai_global_monthly_cap_microusd": BIG,
}


@pytest.fixture(autouse=True)
def _ai_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process is also a worker with AI on (the runner below runs here)."""
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=True)


@pytest.fixture(name="chat_secrets")
def chat_secrets_fixture(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "anthropic_api_key").write_text(API_KEY + "\n")
    return directory


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


@dataclass
class ChatEnv:
    env: Env
    worker_factory: Any
    secrets: Path
    fake: FakeModel = field(default_factory=FakeModel)

    def registry(self, **call_options: Any) -> KindRegistry:
        """The worker's ``ai_chat_turn`` with the fake model, the made-up prices and no sleep."""
        options: dict[str, Any] = {
            "settings": self.env.settings,
            "inner": self.fake.transport,
            "prices": PRICES,
            "secrets_directory": self.secrets,
            "sleep": _no_sleep,
            **call_options,
        }
        registry = KindRegistry()
        handler = partial(run_chat_turn, **options)
        registry.register(JobKind(CHAT_KIND, ChatInput, handler))
        return registry

    async def run_next(self, **call_options: Any) -> Execution | None:
        """Claim and run one queued turn as the worker does; ``None`` when none is queued."""
        claimed = await queue.claim(self.worker_factory, "synthetic-chat-worker", 60, [CHAT_KIND])
        if claimed is None:
            return None
        execution = Execution(claimed)
        config = RunConfig(lease_seconds=60, accounts=ACCOUNTS)
        await execute(
            execution, self.worker_factory, self.registry(**call_options), config, asyncio.Event()
        )
        return execution


@pytest.fixture(name="chat")
async def chat_fixture(
    clean_auth: None, app_database_url: str, chat_secrets: Path
) -> AsyncIterator[ChatEnv]:
    run("DELETE FROM app_settings WHERE flag IN ('ai_insights', 'experimental_findings')")
    run("TRUNCATE ai_audit, ai_consents, ai_reservations, ai_spend, ai_spend_global, rate_limits")
    engine = create_worker_engine(settings_for(app_database_url), pool_size=4)
    try:
        async with running_env(app_database_url, **LOCKS_OPEN) as env:
            yield ChatEnv(env, create_session_factory(engine), chat_secrets)
    finally:
        await engine.dispose()


@dataclass
class User:
    browser: Browser
    id: uuid.UUID


async def user(chat: ChatEnv, email: str, *, consent: bool = True) -> User:
    """A signed-in user (full session); with ``consent``, consenting to the current text."""
    browser = await signed_in(chat.env, email)
    found = await chat.env.scalar("SELECT id FROM users WHERE email_normalized = :e", e=email)
    assert isinstance(found, uuid.UUID)
    if consent:
        give_consent(found)
    return User(browser, found)


async def conversation(chat: ChatEnv, who: User) -> uuid.UUID:
    response = await who.browser.post("/api/ai/conversations")
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation"]["id"])


def request_id() -> str:
    return "synthetic-" + uuid.uuid4().hex


async def send(who: User, conversation_id: uuid.UUID, content: str, **body: Any) -> Any:
    return await who.browser.post(
        f"/api/ai/conversations/{conversation_id}/messages",
        {"content": content, "request_id": request_id(), **body},
    )


def sql(statement: str, **params: Any) -> Any:
    from sqlalchemy import text

    with engine_for(admin_url()) as engine, engine.begin() as conn:
        return conn.execute(text(statement), params)


def count(table: str, owner: uuid.UUID) -> int:
    assert table in {"ai_messages", "ai_conversations", "ai_audit", "ai_reservations"}
    return int(sql(f"SELECT count(*) FROM {table} WHERE owner_id = :o", o=owner).scalar_one())  # noqa: S608


def messages(owner: uuid.UUID) -> list[tuple[str, str]]:
    rows = sql(
        "SELECT role, content FROM ai_messages WHERE owner_id = :o ORDER BY created_at, id",
        o=owner,
    ).all()
    return [(str(r.role), str(r.content)) for r in rows]
