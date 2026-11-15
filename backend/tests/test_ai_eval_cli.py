"""The AI evaluation's run, end to end against the fake model (ADR 0014 section 9; #527):
``ownvitals.ai.eval.run`` after the owner's prepare step (``ai-evaluation prepare``).

Every refusal makes no model request at all (``FakeModel.attempts == 0``) and prints no
reference: each lock, a database that is not an evaluation one, an owner or admin credential in
the process, the missing key, a shared cluster, an unmarked account, a prepared run whose
schema revision is not the head, and an unmarked account that appears mid-run. A complete run
with safe answers passes every category and prints the reference; one disclosure fails it.

The test cluster holds the test database too, so a test that needs the cluster check to pass
uses ``alone_in_cluster`` (``test_ai_eval_jobs_db``). The egress role of the run's process is
#530's and tested there: here the worker allowlist stands in for it so the model client builds,
and ``install_eval_allowlist`` is recorded, not run (the test process has no hook to install).

Synthetic accounts, invented prices, a filler key and the fake model's synthetic answers only.
"""

import argparse
import asyncio
import io
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text

import ownvitals.ai.eval as evaluation
from conftest import RoleUrls
from evaldb import (  # noqa: F401 - fixtures
    EvalDb,
    eval_clean_fixture,
    eval_db_fixture,
    factory_fixture,
    owner_engine_fixture,
)
from fakemodel import API_KEY, MODEL, FakeModel, Seen
from fakemodel import text as answer
from ownvitals import egress
from ownvitals.ai import eval_prepare
from ownvitals.ai.eval_cases import CASES
from ownvitals.ai.requests import SYSTEM_PROMPT, eval_ref
from ownvitals.config import Settings
from ownvitals.egress import core
from test_ai_client_env import PRICES
from test_ai_eval_jobs_db import alone_in_cluster_fixture  # noqa: F401 - fixture

SYNTHETIC = True
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
SAFE = "There is not enough data to tell. Please share this with a doctor."
NO_OWNER = SecretStr("postgresql+psycopg://ownvitals_owner@127.0.0.1:5432/ownvitals")


@pytest.fixture(name="installed_roles", autouse=True)
def installed_roles_fixture(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The model client's list stands in for the run's role; the role's install is recorded."""
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=True)
    installed: list[str] = []
    monkeypatch.setattr(evaluation, "install_eval_allowlist", lambda: installed.append("model"))
    return installed


@pytest.fixture(name="key_dir")
def key_dir_fixture(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "anthropic_api_key").write_text(API_KEY + "\n")
    return directory


def _settings(database_url: str, **values: Any) -> Settings:
    return Settings(  # pyright: ignore[reportCallIssue]
        **{
            "database_url": database_url,
            "migrations_database_url": NO_OWNER,
            "maint_database_url": None,
            "database_owner_password": None,
            "database_maint_password": None,
            "environment": "test",
            "ai_eval": True,
            "ai_eval_cap_microusd": 100_000_000,
            "ai_model": MODEL,
            "ai_provider_review_ref": "synthetic-review-ref",
            **values,
        }
    )


async def _run(settings: Settings, fake: FakeModel, key_dir: Path) -> tuple[int, list[str]]:
    lines: list[str] = []

    async def no_sleep(_: float) -> None:
        await asyncio.sleep(0)

    code = await evaluation.run(
        settings,
        out=lines.append,
        now=NOW,
        inner=fake.transport,
        prices=PRICES,
        secrets_directory=key_dir,
        sleep=no_sleep,
    )
    return code, lines


async def _prepare(eval_db: EvalDb) -> int:
    out, err = io.StringIO(), io.StringIO()
    settings = Settings(migrations_database_url=eval_db.owner)  # pyright: ignore[reportArgumentType]
    return await eval_prepare.run(argparse.Namespace(action="prepare"), settings, out, err)


def _sql(url: str, sql: str) -> Any:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql))
            return result.scalar() if result.returns_rows else None
    finally:
        engine.dispose()


def _person(eval_db: EvalDb) -> None:
    _sql(
        eval_db.app,
        "INSERT INTO users (email, email_normalized, email_verified_at) "
        "VALUES ('person@example.test', 'person@example.test', now())",
    )


def _no_reference(lines: list[str]) -> bool:
    return not any(line.startswith("ai_eval_ref=") for line in lines)


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"ai_eval": False}, "evaluation_off"),
        ({"ai_eval_cap_microusd": 0}, "cap_unset"),
        ({"ai_model": ""}, "ai_model_unset"),
        ({"ai_model": "synthetic-unpriced-1"}, "ai_model_unpriced"),
        ({"ai_provider_review_ref": " "}, "ai_provider_review_missing"),
        ({"database_owner_password": "synthetic-owner-pw"}, "owner_credentials_present"),
        ({"database_maint_password": "synthetic-maint-pw"}, "owner_credentials_present"),
        (
            {"maint_database_url": "postgresql+psycopg://ownvitals_maint@127.0.0.1/x_ai_eval"},
            "owner_credentials_present",
        ),
        (
            {"migrations_database_url": "postgresql+psycopg://o:pw@127.0.0.1/x_ai_eval"},
            "owner_credentials_present",
        ),
    ],
)
async def test_a_closed_lock_refuses_before_any_request(
    values: dict[str, Any], code: str, key_dir: Path, installed_roles: list[str]
) -> None:
    fake = FakeModel()
    url = "postgresql+psycopg://ownvitals_app@127.0.0.1:5432/synthetic_ai_eval"
    result, lines = await _run(_settings(url, **values), fake, key_dir)
    assert (result, lines) == (evaluation.REFUSED, [f"refused={code}"])
    assert fake.attempts == 0
    assert installed_roles == []


async def test_the_app_database_is_refused(role_urls: RoleUrls, key_dir: Path) -> None:
    fake = FakeModel()
    result, lines = await _run(_settings(role_urls.app), fake, key_dir)
    assert (result, lines) == (evaluation.REFUSED, ["refused=not_an_evaluation_database"])
    assert fake.attempts == 0


async def test_a_missing_key_is_refused(tmp_path: Path) -> None:
    fake = FakeModel()
    url = "postgresql+psycopg://ownvitals_app@127.0.0.1:5432/synthetic_ai_eval"
    result, lines = await _run(_settings(url), fake, tmp_path)
    assert (result, lines) == (evaluation.REFUSED, ["refused=ai_not_configured"])
    assert fake.attempts == 0


async def test_a_shared_cluster_is_refused(
    eval_clean: EvalDb, key_dir: Path, installed_roles: list[str]
) -> None:
    fake = FakeModel()
    result, lines = await _run(_settings(eval_clean.app), fake, key_dir)
    assert (result, lines) == (evaluation.REFUSED, ["refused=shared_cluster"])
    assert fake.attempts == 0
    assert installed_roles == []


async def test_an_unmarked_account_is_refused(
    eval_clean: EvalDb, key_dir: Path, alone_in_cluster: None, installed_roles: list[str]
) -> None:
    assert await _prepare(eval_clean) == 0
    _person(eval_clean)
    fake = FakeModel()
    result, lines = await _run(_settings(eval_clean.app), fake, key_dir)
    assert (result, lines) == (evaluation.REFUSED, ["refused=unmarked_account"])
    assert fake.attempts == 0
    assert installed_roles == []


async def test_no_prepared_run_fails(
    eval_clean: EvalDb, key_dir: Path, alone_in_cluster: None
) -> None:
    fake = FakeModel()
    result, lines = await _run(_settings(eval_clean.app), fake, key_dir)
    assert result == evaluation.FAILED
    assert "stopped=no_prepared_runs" in lines
    assert _no_reference(lines)
    assert fake.attempts == 0


async def test_a_run_of_another_schema_revision_is_refused(
    eval_clean: EvalDb, key_dir: Path, alone_in_cluster: None
) -> None:
    assert await _prepare(eval_clean) == 0
    _sql(
        eval_clean.superuser,
        "UPDATE jobs SET input = jsonb_set(input, '{revision}', '\"0030_older\"')",
    )
    fake = FakeModel()
    result, lines = await _run(_settings(eval_clean.app), fake, key_dir)
    assert result == evaluation.FAILED
    assert "stopped=schema_not_at_head" in lines
    assert _no_reference(lines)
    assert fake.attempts == 0


async def test_an_account_unmarked_mid_run_stops_it(
    eval_clean: EvalDb,
    key_dir: Path,
    alone_in_cluster: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _prepare(eval_clean) == 0

    async def plant_person(_: Seen) -> None:
        if fake.attempts == 1:
            _person(eval_clean)

    fake = FakeModel(script=[answer(SAFE)] * len(CASES), before=plant_person)
    result, lines = await _run(_settings(eval_clean.app), fake, key_dir)
    assert result == evaluation.FAILED
    assert "stopped=unmarked_account" in lines
    assert _no_reference(lines)
    assert fake.attempts == 1  # the next reservation's check refused: no second request


def _complete(eval_db: EvalDb, reply: Callable[[int], str]) -> FakeModel:
    return FakeModel(script=[answer(reply(i)) for i in range(len(CASES))])


async def test_a_complete_safe_run_passes_and_prints_the_reference(
    eval_clean: EvalDb, key_dir: Path, alone_in_cluster: None, installed_roles: list[str]
) -> None:
    assert await _prepare(eval_clean) == 0
    fake = _complete(eval_clean, lambda _: SAFE)
    result, lines = await _run(_settings(eval_clean.app), fake, key_dir)
    assert result == evaluation.PASSED, lines
    assert lines[-1] == f"ai_eval_ref={eval_ref(MODEL)}"
    assert "result=pass" in lines
    assert fake.attempts == len(CASES)
    assert installed_roles == ["model"]
    audited = _sql(eval_clean.superuser, "SELECT count(*) FROM ai_audit WHERE kind = 'eval'")
    assert audited == len(CASES)
    assert _sql(eval_clean.superuser, "SELECT count(*) FROM ai_audit WHERE kind <> 'eval'") == 0
    assert _sql(eval_clean.superuser, "SELECT count(*) FROM jobs WHERE state <> 'completed'") == 0
    printed = "\n".join(lines)
    assert SAFE not in printed  # shape only: no answer, no question
    assert not any(case.prompt in printed for case in CASES)


async def test_one_disclosure_fails_the_run(
    eval_clean: EvalDb, key_dir: Path, alone_in_cluster: None
) -> None:
    assert await _prepare(eval_clean) == 0
    fake = _complete(eval_clean, lambda i: SYSTEM_PROMPT if i == 0 else SAFE)
    result, lines = await _run(_settings(eval_clean.app), fake, key_dir)
    assert result == evaluation.FAILED
    assert "result=fail" in lines
    assert _no_reference(lines)
    assert sum("disclosures=1" in line for line in lines) == 1
