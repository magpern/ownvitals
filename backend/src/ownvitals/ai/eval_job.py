"""The AI evaluation's job kind (ADR 0014 section 9, ADR 0005; #527).

Every model call of the evaluation runs inside a leased running job, as the worker's do: the
owner's prepare step queues one job of ``EVAL_RUN_KIND`` per seeded account
(``db.synthetic_jobs.queue_run``), and the evaluation's process claims and executes them with
the jobs runner, which issues the account's ``UserContext``. ``run_cases`` is that kind's
handler. It refuses (``invalid_input``, with the reason in ``Record.stopped``):

- an input whose ``revision`` is not ``SCHEMA_HEAD``, or a plan it does not know;
- inside the job's fence, a database that is not an evaluation database alone in its cluster,
  an account that is not marked synthetic, or any unmarked account it can see.

It then plants the plan's data through the job's fence (``eval_seed.seed_account_data``) and
asks each of the plan's cases (``eval_cases``) as one chat turn: the real tool loop and model
client, audited under the evaluation's own kind, and before each reservation the same account
checks again (``ModelRefused("unmarked_account")``). A refused call stops the whole run (the
locks, the key, the budget: nothing more is asked); a failed call fails that case only. Each
answer is graded by rule (``eval_grade``) at once, and only the grade and the request sizes are
kept (``Record``): nothing the model said leaves the handler.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.ai.audit import EVAL_AUDIT_KIND
from ownvitals.ai.eval_cases import cases_for
from ownvitals.ai.eval_grade import Grade, grade
from ownvitals.ai.eval_seed import seed_account_data
from ownvitals.ai.eval_seed_model import PLANS
from ownvitals.ai.eval_summary import Outcome
from ownvitals.ai.gates import ModelFailed, ModelRefused
from ownvitals.ai.requests import Message, Text
from ownvitals.ai.turn import run_turn
from ownvitals.auth.synthetic import account_is_synthetic, unmarked_accounts
from ownvitals.config import Settings
from ownvitals.db.synthetic_jobs import EVAL_RUN_KIND, PLAN_NAME, SCHEMA_HEAD, cluster_refusal
from ownvitals.jobs import Complete, JobFailure, JobKind, KindRegistry
from ownvitals.jobs.fence import FencedSession
from ownvitals.jobs.run import JobRun

REFUSED_CATEGORY: Final = "invalid_input"
STOPPED_CATEGORY: Final = "ai_unavailable"


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan: str = Field(pattern=PLAN_NAME.pattern)
    revision: str = Field(max_length=64)


@dataclass
class Record:
    """What the run keeps: graded outcomes, the first stop's code, the completed jobs."""

    outcomes: list[Outcome] = field(default_factory=list[Outcome])
    stopped: str | None = None
    completed: int = 0

    def stop(self, code: str, category: str = REFUSED_CATEGORY) -> JobFailure:
        if self.stopped is None:
            self.stopped = code
        return JobFailure(category)


def _opener(job: JobRun) -> Any:
    """The seeder's transaction opener: the job's fence, its session only."""

    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with job.fenced() as fs:
            yield fs.db

    return open_tx


async def _account_refusal(job: JobRun, db: AsyncSession) -> str | None:
    if not await account_is_synthetic(db, job.ctx.user_id) or await unmarked_accounts(db):
        return "unmarked_account"
    return None


def _precheck(job: JobRun) -> Any:
    async def check(fs: FencedSession) -> None:
        if await _account_refusal(job, fs.db) is not None:
            raise ModelRefused("unmarked_account")

    return check


async def run_cases(
    job: JobRun, *, settings: Settings, record: Record, now: datetime, **call_options: Any
) -> Complete:
    """The handler (see the module docstring); ``settings`` is ``eval_gates.run_settings``."""
    parsed = job.input
    if not isinstance(parsed, RunInput):
        raise record.stop("invalid_input")
    if parsed.revision != SCHEMA_HEAD:
        raise record.stop("schema_not_at_head")
    plan = next((p for p in PLANS if p.name == parsed.plan), None)
    if plan is None:
        raise record.stop("invalid_input")
    async with job.fenced() as fs:
        refused = await cluster_refusal(fs.db) or await _account_refusal(job, fs.db)
    if refused is not None:
        raise record.stop(refused)
    await seed_account_data(job.ctx, _opener(job), settings, plan, now=now)
    for case in cases_for(plan.name):
        await job.checkpoint()
        try:
            turn = await run_turn(
                job,
                settings,
                kind="chat",
                history=[Message("user", (Text(case.prompt),))],
                now=now,
                audit_kind=EVAL_AUDIT_KIND,
                precheck=_precheck(job),
                **call_options,
            )
        except ModelRefused as refusal:
            raise record.stop(refusal.code, STOPPED_CATEGORY) from None
        except ModelFailed:
            record.outcomes.append(Outcome(case, Grade(False, must_not=False, must=False)))
            continue
        answer = turn.text if turn.stopped == "answered" else None
        record.outcomes.append(Outcome(case, grade(case, answer, turn.results), turn.sizes))
    record.completed += 1
    return Complete()


def register_run_kind(
    registry: KindRegistry, *, settings: Settings, record: Record, now: datetime, **options: Any
) -> JobKind:
    """``EVAL_RUN_KIND`` in the evaluation process's own registry (never the worker's)."""

    async def handler(job: JobRun) -> Complete:
        return await run_cases(job, settings=settings, record=record, now=now, **options)

    return registry.register(JobKind(EVAL_RUN_KIND, RunInput, handler))
