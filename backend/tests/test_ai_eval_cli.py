"""The real-model eval's refusals (ADR 0014 section 9; #527): ``python -m ownvitals.ai.eval`` stops,
with a closed code, before it makes one model request, whichever lock or check fails. Every model
request of these tests goes to the fake model (``evaltools.GoodModel``): none must arrive.

The first group needs no database (the locks and the URLs are checked before anything connects);
the second runs on an ``_ai_eval`` database prepared as the owner prepares the eval's (the
fixtures of ``test_ai_eval_seed_db.py``). Synthetic data only.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from evaltools import OTHER_URL, GoodModel, eval_env, fresh_process, settings
from ownvitals.ai import eval as eval_cli
from ownvitals.ai.eval import EvalEnv, run_eval
from ownvitals.ai.eval_locks import EvalRefused
from test_ai_eval_seed_db import (  # noqa: F401 - fixtures
    EvalDb,
    _app_insert_person,  # pyright: ignore[reportPrivateUsage]
    eval_clean_fixture,
    eval_db_fixture,
)

SYNTHETIC = True
URLS = (
    "postgresql+psycopg://a@127.0.0.1:5432/x_ai_eval",
    "postgresql+psycopg://o@127.0.0.1:5432/x_ai_eval",
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh_process(monkeypatch)


def _refused(settings_: Any, env: EvalEnv) -> str:
    with pytest.raises(EvalRefused) as raised:
        asyncio.run(run_eval(settings_, env))
    return raised.value.code


CASES: list[tuple[str, dict[str, Any], str]] = [
    ("eval off", {"ai_eval": False}, "eval_off"),
    ("no review reference", {"ai_provider_review_ref": " "}, "ai_provider_review_missing"),
    ("no model", {"ai_model": ""}, "ai_model_unset"),
    ("an unpriced model", {"ai_model": "claude-unpriced-9"}, "ai_model_unpriced"),
    ("cap zero", {"ai_eval_cap_microusd": 0}, "eval_cap_zero"),
    ("no app secret", {"app_secret_key": None}, "ai_not_configured"),
]


@pytest.mark.parametrize(("label", "change", "code"), CASES, ids=[c[0] for c in CASES])
def test_a_lock_that_is_not_open_makes_no_request(
    label: str, change: dict[str, Any], code: str, secrets_dir: Path
) -> None:
    fake = GoodModel()
    assert _refused(settings(URLS, **change), eval_env(secrets_dir, fake)) == code
    assert fake.attempts == 0


def test_without_the_api_key_file_no_request_is_made(secrets_dir: Path) -> None:
    fake = GoodModel()
    env = eval_env(secrets_dir, fake)
    (secrets_dir / "anthropic_api_key").unlink()
    assert _refused(settings(URLS), env) == "ai_not_configured"
    assert fake.attempts == 0


def test_the_default_is_off() -> None:
    assert settings(URLS).ai_eval is True
    from ownvitals.config import Settings

    assert Settings().ai_eval is False
    assert Settings().ai_eval_cap_microusd == 0


URL_CASES: list[tuple[str, dict[str, Any], str]] = [
    ("no eval url", {"ai_eval_database_url": None}, "eval_database_unset"),
    ("no eval owner url", {"ai_eval_migrations_database_url": None}, "eval_database_unset"),
    (
        "the app's own database",
        {"ai_eval_database_url": OTHER_URL, "ai_eval_migrations_database_url": OTHER_URL},
        "eval_database_is_app",
    ),
    (
        "the app's database name elsewhere",
        {
            "ai_eval_database_url": "postgresql+psycopg://a@db.example.test:5432/ownvitals",
            "ai_eval_migrations_database_url": "postgresql+psycopg://o@db.example.test:5432/ownvitals",
        },
        "eval_database_is_app",
    ),
    (
        "a name without the suffix",
        {
            "ai_eval_database_url": "postgresql+psycopg://a@127.0.0.1:5432/other",
            "ai_eval_migrations_database_url": "postgresql+psycopg://o@127.0.0.1:5432/other",
        },
        "eval_database_name",
    ),
    (
        "two different databases",
        {"ai_eval_migrations_database_url": "postgresql+psycopg://o@127.0.0.1:5432/y_ai_eval"},
        "eval_database_mismatch",
    ),
]


@pytest.mark.parametrize(("label", "change", "code"), URL_CASES, ids=[c[0] for c in URL_CASES])
def test_a_database_url_that_is_not_the_evals_makes_no_request(
    label: str, change: dict[str, Any], code: str, secrets_dir: Path
) -> None:
    from pydantic import SecretStr

    fake = GoodModel()
    values = {k: None if v is None else SecretStr(v) for k, v in change.items()}
    assert _refused(settings(URLS, **values), eval_env(secrets_dir, fake)) == code
    assert fake.attempts == 0


def test_a_password_in_a_prod_url_is_refused(secrets_dir: Path) -> None:
    fake = GoodModel()
    with_password = (URLS[0].replace("a@", "a:pw@"), URLS[1])
    given = settings(with_password).model_copy(update={"environment": "prod"})
    assert _refused(given, eval_env(secrets_dir, fake)) == "eval_url_password"
    assert fake.attempts == 0


def test_the_allowlist_is_not_installed_by_a_refusal(secrets_dir: Path) -> None:
    called: list[bool] = []
    env = eval_env(secrets_dir, before_calls=lambda: called.append(True))
    assert _refused(settings(URLS, ai_eval=False), env) == "eval_off"
    assert called == []


# --- the database checks --------------------------------------------------------------------


def _urls(db: EvalDb) -> tuple[str, str]:
    return (db.app, db.owner)


def _version(db: EvalDb, value: str | None) -> str | None:
    engine = create_engine(db.superuser)
    try:
        with engine.begin() as conn:
            old = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            if value is not None:
                conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": value})
            return str(old)
    finally:
        engine.dispose()


@pytest.fixture
def behind(eval_clean: EvalDb) -> Iterator[EvalDb]:
    """The database one step before head (its version row names an older revision)."""
    old = _version(eval_clean, "0000_behind")
    try:
        yield eval_clean
    finally:
        _version(eval_clean, old)


def _audit_rows(db: EvalDb) -> int:
    engine = create_engine(db.superuser)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text("SELECT count(*) FROM ai_audit")).scalar_one())
    finally:
        engine.dispose()


def test_a_schema_that_is_not_at_head_makes_no_request(behind: EvalDb, secrets_dir: Path) -> None:
    fake = GoodModel()
    assert _refused(settings(_urls(behind)), eval_env(secrets_dir, fake)) == "schema_not_at_head"
    assert fake.attempts == 0
    assert _audit_rows(behind) == 0


def test_an_unmarked_account_makes_no_request_and_seeds_nothing(
    eval_clean: EvalDb, secrets_dir: Path
) -> None:
    _app_insert_person(eval_clean.app)
    fake = GoodModel()
    code = _refused(settings(_urls(eval_clean)), eval_env(secrets_dir, fake))
    assert code == "accounts_not_synthetic"
    assert fake.attempts == 0
    engine = create_engine(eval_clean.superuser)
    try:
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT count(*) FROM users WHERE synthetic")).scalar_one() == 0
            )
    finally:
        engine.dispose()


def test_an_unreachable_database_makes_no_request(secrets_dir: Path) -> None:
    fake = GoodModel()
    urls = (
        "postgresql+psycopg://a:b@127.0.0.1:9/z_ai_eval",
        "postgresql+psycopg://o:b@127.0.0.1:9/z_ai_eval",
    )
    assert _refused(settings(urls), eval_env(secrets_dir, fake)) == "database_unreachable"
    assert fake.attempts == 0


# --- main -----------------------------------------------------------------------------------


def test_main_installs_the_eval_hook_first_and_prints_only_a_closed_code(
    monkeypatch: pytest.MonkeyPatch, secrets_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    order: list[str] = []
    monkeypatch.setattr(eval_cli, "install_egress_hook", lambda kind, _s: order.append(kind))
    real = eval_cli.run_eval

    async def spy(given: Any, env: EvalEnv) -> Any:
        order.append("checks")
        return await real(given, env)

    monkeypatch.setattr(eval_cli, "run_eval", spy)
    monkeypatch.setenv("OWNVITALS_AI_EVAL", "off")
    fake = GoodModel()
    code = eval_cli.main([], eval_env(secrets_dir, fake))
    assert code == 2
    assert order == ["eval", "checks"]
    out = capsys.readouterr().out
    assert out.strip() == "ownvitals ai evaluation refused: eval_off"
    assert fake.attempts == 0


def test_the_order_of_the_run_is_hook_checks_allowlist_then_calls() -> None:
    """ADR 0014 section 9: the egress hook first, then every check, the allowlist last before
    the first call; the source's own order, as the guard pins the role's modules."""
    import inspect

    main_source = inspect.getsource(eval_cli.main)
    assert main_source.index("install_egress_hook(") < main_source.index("asyncio.run(")
    assert "install_eval_allowlist()" not in main_source  # only handed over, called after checks
    run_source = inspect.getsource(eval_cli.run_eval)
    steps = [
        "require_standard_event_loop()",
        "check_locks(",
        "check_urls(",
        "_prepare(",
        "ledger_total(",
        "env.before_calls()",
        "_drive(",
    ]
    assert [run_source.index(step) for step in steps] == sorted(run_source.index(s) for s in steps)
    prepare = inspect.getsource(eval_cli._prepare)  # pyright: ignore[reportPrivateUsage]
    order = ["schema_at_head(", "verify_database(", "synthetic_only(", "seed_accounts("]
    assert [prepare.index(s) for s in order] == sorted(prepare.index(s) for s in order)


def test_the_documented_database_function_files_are_the_scripts() -> None:
    """``docs/dev/ai.md`` step 3 installs the files that ``db-functions.sh`` installs."""
    import re

    root = Path(__file__).resolve().parents[2]
    script = (root / "docker/postgres/queue/db-functions.sh").read_text(encoding="utf-8")
    docs = (root / "docs/dev/ai.md").read_text(encoding="utf-8")
    wanted = re.findall(r"/app/db-functions/[\w.-]+\.sql", script)
    assert wanted
    assert re.findall(r"/app/db-functions/[\w.-]+\.sql", docs) == wanted
