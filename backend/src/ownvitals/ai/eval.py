"""The owner's real-model evaluation, run as a module of this package (ADR 0014 section 9; #527).

Run once, by the owner, before ``ai_eval_ref`` is set (lock 3). It sends the cases of
``eval_cases`` to the real model, as synthetic accounts of an ``_ai_eval`` database, through the
same client as a user's call (reservation, audit with the evaluation kind, the five locks' own
checks), each inside a leased running job (``eval_run``), grades the answers by rule
(``eval_graders``) and prints a shape-only summary (``eval_report``): counts, the model, the
versions, the largest ratio of reported to bounded input tokens, and, only if every category
met its threshold, the ``ai_eval_ref`` value. It never writes that setting.

``main`` installs the egress hook of the evaluation kind first, before anything connects, then
``run_eval`` makes the checks in this order, each refusing with exit code 2 and a closed code
(no model request has been made by then): the locks, the database URLs (not the app's, name
ending ``_ai_eval``), the schema at head, the app role and the installed functions, the
accounts (each marked synthetic). Only then does ``main``'s ``before_calls`` install the evaluation
allowlist (``api.anthropic.com`` only), and the cases run. Before **every** model call the
database is asked again that every account in it is marked synthetic (``CallHooks.allowed``).

``--show-failed`` adds the static labels of the failed cases to the summary. ``--review-file
PATH`` writes the synthetic prompts and answers of the three cases that need a person's
reading, to a file only the owner can read; without it no answer text is kept.
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from ownvitals.ai.eval_cases import all_cases
from ownvitals.ai.eval_checks import (
    ledger_total,
    schema_at_head,
    synthetic_only,
    verify_database,
)
from ownvitals.ai.eval_locks import EvalRefused, check_locks, check_urls, run_settings
from ownvitals.ai.eval_model import Outcome as CaseOutcome
from ownvitals.ai.eval_report import passed, render, summarise
from ownvitals.ai.eval_run import Driver, EvalInput, Factory, Run, write_review
from ownvitals.ai.eval_seed import seed_accounts
from ownvitals.ai.eval_seed_model import PLANS
from ownvitals.ai.pricing import PRICES, Price
from ownvitals.auth.synthetic import SyntheticRefused
from ownvitals.config import Settings
from ownvitals.db import (
    create_migrations_engine,
    create_session_factory,
    event_loop_factory,
)
from ownvitals.egress import (
    EVAL_HOOK_KIND,
    install_egress_hook,
    install_eval_allowlist,
    require_standard_event_loop,
)
from ownvitals.worker.pool import create_worker_engine

REFUSED_EXIT = 2
FAILED_EXIT = 1
ABORT_CASES_AFTER = 3  # consecutive failed requests (ModelFailed) before the run stops


@dataclass
class EvalEnv:
    """What a test replaces: the transport, the secrets, the clock, the allowlist step."""

    inner: Any = None  # the model client's transport (a test's fake)
    secrets_directory: Path | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    prices: dict[str, Price] = field(default_factory=lambda: dict(PRICES))
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    before_calls: Callable[[], object] = lambda: None
    show_failed: bool = False
    review_file: Path | None = None


@dataclass(frozen=True, slots=True)
class Result:
    exit_code: int
    lines: list[str]


async def _prepare(owner: AsyncEngine, app: AsyncEngine) -> dict[str, UUID]:
    """Checks that touch the database, in order; the accounts' ids by plan name."""
    try:
        await schema_at_head(owner)
        await verify_database(app)
        if not await synthetic_only(owner):  # before the seed adds any account
            raise EvalRefused("accounts_not_synthetic")
        ids = await seed_accounts(owner)
        if not await synthetic_only(owner):
            raise EvalRefused("accounts_not_synthetic")
    except SyntheticRefused:
        raise EvalRefused("seed_refused") from None
    except (SQLAlchemyError, OSError):
        raise EvalRefused("database_unreachable") from None
    return dict(ids)


async def run_eval(settings: Settings, env: EvalEnv) -> Result:
    """Everything after the egress hook; the summary, or ``EvalRefused`` before any call."""
    require_standard_event_loop()  # the egress hook sees no other loop's sockets
    model = check_locks(settings, env.prices, env.secrets_directory)
    check_urls(settings)
    first = run_settings(settings, model, ceiling=0)
    owner, app = create_migrations_engine(first), create_worker_engine(first)
    try:
        ids = await _prepare(owner, app)
        now = env.now()
        try:
            base = await ledger_total(owner, now)
        except (SQLAlchemyError, OSError):
            raise EvalRefused("database_unreachable") from None
        used = run_settings(settings, model, ceiling=base + settings.ai_eval_cap_microusd)
        cases = {c.label: c for c in all_cases()}
        secrets = tuple(
            s.get_secret_value()
            for s in (settings.app_secret_key,)
            if s is not None and s.get_secret_value()
        )

        async def allowed() -> bool:
            try:
                return await synthetic_only(owner)
            except (SQLAlchemyError, OSError):
                return False

        run = Run(
            used,
            model,
            cases,
            {p.name: p for p in PLANS},
            now,
            allowed,
            secrets_to_hide=secrets,
            keep_review=env.review_file is not None,
            prices=env.prices,
            options={
                "inner": env.inner,
                "secrets_directory": env.secrets_directory,
                "sleep": env.sleep,
                "prices": env.prices,
            },
        )
        env.before_calls()  # the evaluation allowlist: after every check, before the first call
        await _drive(create_session_factory(app), run, ids)
        if env.review_file is not None:
            write_review(env.review_file, run.readings)
        every = [run.outcomes.get(c.label) or CaseOutcome(c, None) for c in cases.values()]
        results = summarise(every)
        lines = render(
            results,
            model=model,
            ratio=run.max_ratio,
            calls=run.calls,
            spend_microusd=run.spend,
            aborted=run.aborted,
            show_failed=env.show_failed,
        )
        ok = passed(results, run.max_ratio, run.aborted)
        return Result(0 if ok else FAILED_EXIT, lines)
    finally:
        await owner.dispose()
        await app.dispose()


async def _drive(factory: Factory, run: Run, ids: dict[str, UUID]) -> None:
    driver = Driver(factory, run)
    await driver.start()
    for plan in PLANS:
        done = await driver.step(ids[plan.name], EvalInput(step="seed", name=plan.name))
        if done is None or done.result != "completed":
            raise EvalRefused("seed_failed")
    failures = 0
    for label, case in run.cases.items():
        if run.aborted is not None:
            break
        await driver.step(ids[case.account], EvalInput(step="case", name=label))
        failures = failures + 1 if label not in run.outcomes else 0
        if failures >= ABORT_CASES_AFTER:
            run.aborted = "model_unavailable"


def _args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ownvitals-ai-evaluation")
    parser.add_argument("--show-failed", action="store_true")
    parser.add_argument("--review-file", type=Path, default=None)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None, env: EvalEnv | None = None) -> int:
    args = _args(sys.argv[1:] if argv is None else argv)
    settings = Settings()
    # First: nothing of this process connects anywhere before the hook (ADR 0009, 0014 section 9).
    install_egress_hook(EVAL_HOOK_KIND, settings)
    env = env or EvalEnv(before_calls=install_eval_allowlist)
    env.show_failed, env.review_file = args.show_failed, args.review_file
    try:
        result = asyncio.run(run_eval(settings, env), loop_factory=event_loop_factory())
    except EvalRefused as refused:
        print(f"ownvitals ai evaluation refused: {refused.code}")
        return REFUSED_EXIT
    for line in result.lines:
        print(line)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
