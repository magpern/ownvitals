"""Setup shared by the real-model eval's tests (ADR 0014 section 9; #527): the settings of a run,
the secrets of its directory, and a fake model that answers a case as a good model would. Every
model request goes to the fake; nothing reaches a network. Synthetic data only.
"""

import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from fakemodel import API_KEY, text
from ownvitals.ai.eval import EvalEnv
from ownvitals.ai.eval_cases import all_cases
from ownvitals.ai.pricing import PRICES
from ownvitals.config import Settings
from ownvitals.egress import core

SYNTHETIC = True
MODEL = "claude-sonnet-5-5"
APP_SECRET = "synthetic-app-secret-" + "S" * 32
CAP = 50_000_000
OTHER_URL = "postgresql+psycopg://synthetic@127.0.0.1:5432/ownvitals"  # the app's, never reached
ANSWERS = {
    "must": "Please talk to your doctor, or call the emergency number if it is urgent.",
    "insufficient": "There is insufficient data to say anything about a trend.",
    "other": "I can only talk about your own measurements here.",
}
DIGEST = "There is insufficient data for a trend; ask your doctor about anything else."
BAD = "You have hypertension and you should take ibuprofen 400 mg."
BAD_TIER = "That is an experimental finding, an ECG estimate of ownvitals."


def settings(eval_urls: tuple[str, str] | None, **overrides: Any) -> Settings:
    """Every lock of the eval open; ``eval_urls`` are the eval database's (app, owner) URLs."""
    values: dict[str, Any] = {
        "database_url": SecretStr(OTHER_URL),
        "ai_eval": True,
        "ai_model": MODEL,
        "ai_provider_review_ref": "synthetic-review-ref",
        "ai_eval_cap_microusd": CAP,
        "app_secret_key": SecretStr(APP_SECRET),
    }
    if eval_urls is not None:
        values["ai_eval_database_url"] = SecretStr(eval_urls[0])
        values["ai_eval_migrations_database_url"] = SecretStr(eval_urls[1])
    return Settings(**(values | overrides))


@dataclass
class GoodModel:
    """Answers each case as a safe, grounded model would (graders in ``wrong`` get a bad
    answer), and counts every request it receives."""

    wrong: set[str] = field(default_factory=set[str])
    attempts: int = 0
    prompts: list[str] = field(default_factory=list[str])

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        last = json.loads(request.content)["messages"][-1]["content"]
        said = "".join(b.get("text", "") for b in last) if isinstance(last, list) else str(last)
        self.prompts.append(said)
        case = next((c for c in all_cases() if c.prompt and c.prompt == said), None)
        if case is None:
            answer = DIGEST
        elif case.grader in self.wrong:
            answer = BAD_TIER if case.grader == "tier" else BAD
        else:
            answer = ANSWERS.get(case.grader, ANSWERS["other"])
        code, content = text(answer)
        return httpx.Response(code, json=content)


def eval_env(secrets: Path, model: GoodModel | None = None, **changes: Any) -> EvalEnv:
    """The seam of a test: the fake model, the test's secrets, the eval allowlist step."""
    (secrets / "anthropic_api_key").write_text(API_KEY + "\n", encoding="ascii")
    env = EvalEnv(
        inner=(model or GoodModel()).transport(),
        secrets_directory=secrets,
        prices=dict(PRICES),
        before_calls=lambda: core._install_worker_list(core.MODEL_ALLOWLIST),  # pyright: ignore[reportPrivateUsage]
    )
    for name, value in changes.items():
        setattr(env, name, value)
    return env


def fresh_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """No allowlist installed yet, as at the start of the eval process."""
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
