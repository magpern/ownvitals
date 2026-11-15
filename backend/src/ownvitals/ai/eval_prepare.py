"""``python -m ownvitals ai-evaluation prepare``: the owner's prepare step of the AI evaluation
(ADR 0014 section 9; #527).

Run by the owner with the **owner role's** URL (``OWNVITALS_MIGRATIONS_DATABASE_URL`` and the
``database_owner_password`` secret), on an ``_ai_eval`` database alone in its own cluster that
``alembic upgrade head`` and the db-functions scripts have prepared (``docs/dev/ai.md``). It
refuses (exit 1, nothing written) unless that database's name ends in ``_ai_eval``, its cluster
holds no other database (``synthetic_jobs.cluster_refusal``) and its schema is at
``SCHEMA_HEAD``; then, in one transaction each, it creates or finds the marked synthetic
accounts (``eval_seed.seed_accounts``) and queues one run per account
(``synthetic_jobs.queue_run``). It prints counts only. The evaluation's own process never holds
this URL: it refuses to start with an owner credential in its settings (``eval_gates``).
"""

import argparse
from typing import Any, TextIO

from ownvitals.ai.eval_seed import seed_accounts
from ownvitals.ai.eval_seed_model import PLANS
from ownvitals.auth.synthetic import SyntheticRefused
from ownvitals.config import Settings
from ownvitals.db import create_migrations_engine
from ownvitals.db.synthetic_jobs import (
    SyntheticJobRefused,
    cluster_refusal,
    queue_run,
    schema_refusal,
)

COMMAND = "ai-evaluation"


def add_prepare_subcommand(sub: Any) -> None:
    actions = sub.add_parser(
        COMMAND, help="the owner's AI evaluation database (owner role only)"
    ).add_subparsers(dest="action", required=True)
    actions.add_parser(
        "prepare",
        help="seed the synthetic accounts and queue their runs in an _ai_eval database",
    )


async def run(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    if args.action != "prepare":
        print(f"error: unknown {COMMAND} action {args.action!r}", file=err)
        return 1
    engine = create_migrations_engine(settings)
    try:
        async with engine.connect() as conn:
            refused = await cluster_refusal(conn) or await schema_refusal(conn)
        if refused is not None:
            print(f"refused: {refused}", file=err)
            return 1
        try:
            accounts = await seed_accounts(engine, PLANS)
            async with engine.begin() as conn:
                for plan in PLANS:
                    await queue_run(conn, accounts[plan.name], plan.name)
        except SyntheticJobRefused as job_refused:
            print(f"refused: {job_refused.code}", file=err)
            return 1
        except SyntheticRefused:
            print("refused: synthetic_accounts", file=err)
            return 1
    finally:
        await engine.dispose()
    print(f"accounts {len(accounts)}", file=out)
    print(f"queued_runs {len(PLANS)}", file=out)
    return 0
