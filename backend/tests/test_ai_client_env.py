"""Shared setup of the model-client tests (``test_ai_client*.py``, ``test_ai_egress*.py``; ADR
0014 sections 1, 5, 6 and 7; #207). No tests here.

Every owner is a random UUID with an ``example.test`` address, every job a synthetic running
job, the model is ``fakemodel.FakeModel`` behind the real egress client (no network), the price
is made up, and the API key and app secret are labelled filler strings in a temporary secrets
directory.
"""

import asyncio
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr
from sqlalchemy import text
from sqlalchemy.engine import Connection

from aitools import ai_fixture, running_job  # noqa: F401 - fixtures
from ctx import make_context
from fakemodel import API_KEY, MODEL, FakeModel
from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals import egress
from ownvitals.ai import consent_text
from ownvitals.ai.client import call_model
from ownvitals.ai.pricing import Price
from ownvitals.ai.requests import Message, Text, eval_ref
from ownvitals.ai.responses import Reply
from ownvitals.config import Settings
from ownvitals.egress import core
from ownvitals.jobs import JobRun
from ownvitals.jobs.fence import Lease

SYNTHETIC = True
APP_SECRET = "synthetic-app-secret-" + "S" * 32
BIG = 10**12
# Made-up prices, micro-USD per million tokens (not a real price list).
PRICES = {
    MODEL: Price(
        input=3_000_000,
        output=15_000_000,
        cache_write=3_750_000,
        cache_read=300_000,
        as_of=date(2026, 11, 9),
        source="synthetic test price",
    )
}
QUESTION = [Message("user", (Text("How has my weight changed? (synthetic)"),))]


class NoInput(BaseModel):
    pass


@pytest.fixture(autouse=True)
def _ai_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """This process is a worker with AI on; a test that wants web or AI off resets it."""
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=True)


def as_web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_worker_list", None)


def as_worker_without_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_worker_list", None)
    monkeypatch.setattr(urllib.request, "_opener", None)
    egress.install_worker_allowlist(ai_enabled=False)


def ai_settings(base: Settings, **overrides: Any) -> Settings:
    """Every lock open: the flag, a model, the review and eval references, caps and the secret."""
    values: dict[str, Any] = {
        "ai_insights": True,
        "ai_model": MODEL,
        "ai_provider_review_ref": "synthetic-review-ref",
        "ai_eval_ref": eval_ref(MODEL),
        "ai_user_monthly_cap_microusd": BIG,
        "ai_global_monthly_cap_microusd": BIG,
        "app_secret_key": SecretStr(APP_SECRET),
    }
    return base.model_copy(update=values | overrides)


def give_consent(admin: Connection, owner: uuid.UUID) -> None:
    admin.execute(
        text(
            "INSERT INTO ai_consents (owner_id, consent_version, locale, text_sha256) "
            "VALUES (:o, :v, 'en', :h)"
        ),
        {
            "o": owner,
            "v": consent_text.CONSENT_VERSION,
            "h": consent_text.fingerprint(consent_text.TEXTS["en"]),
        },
    )


def job_run(factory: Any, owner: uuid.UUID, job: uuid.UUID) -> JobRun:
    return JobRun(
        factory,
        Lease(job, 1, make_context(owner)),
        kind="test_kind",
        input=NoInput(),
        attempt=1,
        lease_seconds=60,
        shutdown=asyncio.Event(),
    )


@dataclass
class AiCall:
    """One consenting owner with a running job, the fake model and the secrets directory."""

    jenv: JobsEnv
    admin: Connection
    secrets: Path
    owner: uuid.UUID
    job: JobRun
    fake: FakeModel = field(default_factory=FakeModel)

    @property
    def settings(self) -> Settings:
        return ai_settings(self.jenv.settings)

    async def call(self, settings: Settings | None = None, **options: Any) -> Reply:
        kwargs: dict[str, Any] = {
            "kind": "chat",
            "messages": QUESTION,
            "prices": PRICES,
            "inner": self.fake.transport,
            "secrets_directory": self.secrets,
            "sleep": _no_sleep,
        }
        return await call_model(self.job, settings or self.settings, **(kwargs | options))

    def sql(self, statement: str, **params: Any) -> Any:
        return self.admin.execute(text(statement), params)

    def reservations(self) -> list[Any]:
        return self.sql(
            "SELECT state, amount, actual FROM ai_reservations WHERE owner_id = :o", o=self.owner
        ).all()

    def audit(self) -> list[Any]:
        return self.sql(
            "SELECT * FROM ai_audit WHERE owner_id = :o ORDER BY created_at", o=self.owner
        ).all()


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


@pytest.fixture(name="secrets")
def secrets_fixture(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "anthropic_api_key").write_text(API_KEY + "\n")
    return directory


@pytest.fixture(name="aicall")
def aicall_fixture(ai: Connection, jenv: JobsEnv, secrets: Path) -> AiCall:
    ai.execute(text("TRUNCATE ai_audit, ai_consents"))
    ai.execute(text("DELETE FROM app_settings WHERE flag = 'ai_insights'"))
    owner = uuid.uuid4()
    job = running_job(ai, owner)
    give_consent(ai, owner)
    return AiCall(jenv, ai, secrets, owner, job_run(jenv.factory, owner, job))
