"""The real-model eval's run (ADR 0014 section 9; #527) against the fake model, on an
``_ai_eval`` database prepared as the owner prepares the eval's (the fixtures of
``test_ai_eval_seed_db.py``): every call is a leased running job, reserved and audited with kind
``eval``, the summary is shape-only and prints ``ai_eval_ref`` only when every category passed,
and an account that stops being marked synthetic stops the run before the next request.

Synthetic data only; every model request goes to the fake (``evaltools.GoodModel``).
"""

import asyncio
import json
import stat
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

from evaltools import (
    ANSWERS,
    BAD,
    BAD_TIER,
    DIGEST,
    MODEL,
    GoodModel,
    eval_env,
    fresh_process,
    settings,
)
from ownvitals.ai.eval import run_eval
from ownvitals.ai.eval_cases import all_cases
from ownvitals.ai.eval_locks import EvalRefused
from ownvitals.ai.requests import PROMPT_VERSION, eval_ref
from test_ai_eval_seed_db import (  # noqa: F401 - fixtures
    EvalDb,
    _app_insert_person,  # pyright: ignore[reportPrivateUsage]
    eval_clean_fixture,
    eval_db_fixture,
)

SYNTHETIC = True
TOTAL = len(all_cases())


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_process(monkeypatch)


def _rows(db: EvalDb, sql: str) -> list[tuple[object, ...]]:
    engine = create_engine(db.superuser)
    try:
        with engine.connect() as conn:
            return [tuple(row) for row in conn.execute(text(sql))]
    finally:
        engine.dispose()


def _run(db: EvalDb, secrets: Path, model: GoodModel | None = None, **env: object):  # type: ignore[no-untyped-def]
    given = settings((db.app, db.owner), **{})
    return asyncio.run(run_eval(given, eval_env(secrets, model, **env)))


def test_a_run_whose_answers_are_all_good_passes_and_prints_the_reference(
    eval_clean: EvalDb, secrets_dir: Path
) -> None:
    fake = GoodModel()
    result = _run(eval_clean, secrets_dir, fake)
    assert result.exit_code == 0, result.lines
    assert result.lines[-1] == f"ai_eval_ref={eval_ref(MODEL)}"
    assert "result=PASS" in result.lines
    assert fake.attempts == TOTAL
    audit = _rows(eval_clean, "SELECT kind, count(*) FROM ai_audit GROUP BY kind")
    assert audit == [("eval", TOTAL)]
    jobs = _rows(eval_clean, "SELECT kind, state, count(*) FROM jobs GROUP BY 1, 2")
    assert ("ai_eval", "completed", TOTAL + 3) in jobs  # every case, and the three seeds
    held = _rows(eval_clean, "SELECT coalesce(sum(reserved), 0) FROM ai_spend_global")
    assert held == [(0,)]  # nothing stays reserved


def test_the_summary_is_shape_only(eval_clean: EvalDb, secrets_dir: Path) -> None:
    result = _run(eval_clean, secrets_dir)
    shown = "\n".join(result.lines)
    for case in all_cases():
        if case.prompt:
            assert case.prompt not in shown
    for answer in (*ANSWERS.values(), DIGEST):
        assert answer not in shown
    assert f"prompt_version={PROMPT_VERSION}" in shown
    assert "input_ratio_max=" in shown
    assert "@" not in shown  # no address
    categories = [line.split()[0] for line in result.lines if line.startswith("category=")]
    assert categories == [
        "category=prompt_injection",
        "category=withings_text",
        "category=health_claims",
        "category=grounding",
        "category=tier",
        "category=insufficient_data",
    ]


def test_a_category_below_its_threshold_fails_and_prints_no_reference(
    eval_clean: EvalDb, secrets_dir: Path
) -> None:
    result = _run(eval_clean, secrets_dir, GoodModel(wrong={"tier"}), show_failed=True)
    assert result.exit_code == 1
    assert "result=FAIL" in result.lines
    assert not any(line.startswith("ai_eval_ref=") for line in result.lines)
    failed = next(line for line in result.lines if line.startswith("failed tier:"))
    assert failed.split()[2].startswith("tier-")
    assert BAD not in "\n".join(result.lines)


def test_without_show_failed_no_label_is_printed(eval_clean: EvalDb, secrets_dir: Path) -> None:
    result = _run(eval_clean, secrets_dir, GoodModel(wrong={"tier"}))
    assert not any(line.startswith("failed ") for line in result.lines)


def test_an_account_that_stops_being_synthetic_stops_the_run(
    eval_clean: EvalDb, secrets_dir: Path
) -> None:
    class Meddled(GoodModel):
        async def _handle(self, request: httpx.Request) -> httpx.Response:
            if self.attempts == 0:
                _app_insert_person(eval_clean.app)  # an unmarked account appears mid-run
            return await super()._handle(request)

    fake = Meddled()
    result = _run(eval_clean, secrets_dir, fake)
    assert fake.attempts == 1  # the second request was never made
    assert result.exit_code == 1
    assert "aborted=eval_refused" in "\n".join(result.lines)
    assert not any(line.startswith("ai_eval_ref=") for line in result.lines)
    assert _rows(eval_clean, "SELECT coalesce(sum(reserved), 0) FROM ai_spend_global") == [(0,)]


def test_a_cap_below_one_call_sends_nothing(eval_clean: EvalDb, secrets_dir: Path) -> None:
    fake = GoodModel()
    given = settings((eval_clean.app, eval_clean.owner), ai_eval_cap_microusd=1)
    result = asyncio.run(run_eval(given, eval_env(secrets_dir, fake)))
    assert fake.attempts == 0
    assert result.exit_code == 1
    assert "calls=0" in "\n".join(result.lines)


def test_the_cap_is_per_run_not_per_month(eval_clean: EvalDb, secrets_dir: Path) -> None:
    first = _run(eval_clean, secrets_dir)
    second = _run(eval_clean, secrets_dir)
    assert first.exit_code == second.exit_code == 0


def test_the_allowlist_comes_after_the_checks_and_before_the_first_request(
    eval_clean: EvalDb, secrets_dir: Path
) -> None:
    order: list[str] = []

    class Ordered(GoodModel):
        async def _handle(self, request: httpx.Request) -> httpx.Response:
            order.append("request")
            return await super()._handle(request)

    env = eval_env(secrets_dir, Ordered())
    install = env.before_calls

    def recorded() -> None:
        order.append("allowlist")
        install()

    env.before_calls = recorded
    asyncio.run(run_eval(settings((eval_clean.app, eval_clean.owner)), env))
    assert order[:2] == ["allowlist", "request"]
    assert order.count("allowlist") == 1


def test_the_review_file_is_private_and_holds_only_the_marked_cases(
    eval_clean: EvalDb, secrets_dir: Path, tmp_path: Path
) -> None:
    target = tmp_path / "review.jsonl"
    result = _run(eval_clean, secrets_dir, review_file=target)
    assert result.exit_code == 0
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert sorted(r["label"] for r in rows) == sorted(c.label for c in all_cases() if c.review)
    assert all(r["prompt"] for r in rows)


def test_no_review_text_is_kept_without_a_file(eval_clean: EvalDb, secrets_dir: Path) -> None:
    result = _run(eval_clean, secrets_dir)
    assert result.exit_code == 0
    assert "answer" not in "\n".join(result.lines)


def test_the_failed_file_holds_only_failed_cases_with_reasons_and_is_private(
    eval_clean: EvalDb, secrets_dir: Path, tmp_path: Path
) -> None:
    target = tmp_path / "failed.jsonl"
    result = _run(
        eval_clean, secrets_dir, GoodModel(wrong={"tier"}), failed_file=target, show_failed=True
    )
    assert result.exit_code == 1
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    tier = sorted(c.label for c in all_cases() if c.grader == "tier")
    assert sorted(r["label"] for r in rows) == tier  # the passing cases are not written
    assert {r["category"] for r in rows} == {"tier"}
    assert all(r["answer"] == BAD_TIER and r["prompt"] for r in rows)
    assert all(r["reasons"][0]["code"] == "tier_mention" for r in rows)
    assert all(r["tool_calls"] == [] and r["tool_results"] == [] for r in rows)
    shown = "\n".join(result.lines)
    assert BAD_TIER not in shown
    assert "tier_mention" not in shown
    assert all(r["prompt"] not in shown for r in rows)


def test_the_failed_file_of_a_clean_run_is_empty(
    eval_clean: EvalDb, secrets_dir: Path, tmp_path: Path
) -> None:
    target = tmp_path / "failed.jsonl"
    target.write_text("old\n", encoding="utf-8")
    assert _run(eval_clean, secrets_dir, failed_file=target).exit_code == 0
    assert target.read_text(encoding="utf-8") == ""
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_an_unwritable_failed_file_refuses_before_any_call(
    eval_clean: EvalDb, secrets_dir: Path, tmp_path: Path
) -> None:
    (tmp_path / "real").write_text("", encoding="utf-8")
    link = tmp_path / "link.jsonl"
    link.symlink_to(tmp_path / "real")
    fake = GoodModel()
    with pytest.raises(EvalRefused) as refused:
        _run(eval_clean, secrets_dir, fake, failed_file=link)
    assert refused.value.code == "failed_file_refused"
    assert fake.attempts == 0
