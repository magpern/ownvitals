"""The owner's model evaluation: one case as a leased running job (ADR 0014 sections 5 and 9; #527).

Every model call of the evaluation is made inside a job of kind ``ai_eval`` in its database,
exactly as a user's call is inside ``ai_chat_turn``: a queued job of the account's own (the
jobs service, as that account), claimed through ``queue_claim`` and run by the worker's runner
(``jobs.runner.execute``) with a lease, a heartbeat, the fence and ``provider_call``. The
handler is ``run_job``: the seed step plants an account's data (``eval_seed``) and a case step
sends the case through ``ai.turn.run_turn`` (kind ``chat`` or ``digest``) with ``CallHooks``,
so the call is reserved (section 7) against the evaluation database's ledger, audited with kind
the evaluation kind, and made only after ``Run.allowed`` found every account of the database marked
synthetic. The registry holds this kind only; the worker's never does.

``Run`` holds what the handlers share in this process: the outcomes, the largest ratio of
reported to bounded input tokens, the spend of the run and an abort code. A code that means no
call can be made (a lock, the budget, an unmarked account) aborts the run: no later case is
sent. Nothing of an answer is kept but a verdict, except the text of the cases marked for the
owner's reading, and only when a review file was asked for.
"""

import asyncio
import hashlib
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.ai import audit, digest_model
from ownvitals.ai.call_hooks import CallHooks
from ownvitals.ai.eval_failed import Failure, did_not_run
from ownvitals.ai.eval_graders import grade
from ownvitals.ai.eval_locks import EvalRefused
from ownvitals.ai.eval_model import Case, Outcome
from ownvitals.ai.eval_seed import seed_account_data
from ownvitals.ai.eval_seed_model import AccountPlan
from ownvitals.ai.gates import ModelFailed, ModelRefused
from ownvitals.ai.pricing import PRICES, Price, usage_cost
from ownvitals.ai.requests import Message, Text
from ownvitals.ai.responses import Reply
from ownvitals.ai.turn import run_turn
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.config import Settings
from ownvitals.jobs import Complete, JobFailure, JobKind, JobRun, KindRegistry, queue, service
from ownvitals.jobs.issuer import owner_context
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.jobs.user import NewJob

EVAL_KIND: Final = "ai_eval"
LEASE_SECONDS: Final = 120
ACTIVE_LIMIT: Final = 50  # the jobs service's per-user limit (one job runs at a time)
Factory = async_sessionmaker[AsyncSession]


class EvalInput(BaseModel):
    """The job's input: a step and a name of ours (a plan or a case label); ids and text of no
    user, nothing of an answer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step: Literal["seed", "case"]
    name: str = Field(pattern=r"^[a-z0-9-]{1,48}$")


@dataclass(frozen=True, slots=True)
class Reading:
    """A case marked for the owner's reading: the label, the user's text and the answer."""

    label: str
    prompt: str
    answer: str


@dataclass
class Run:
    settings: Settings
    model: str
    cases: Mapping[str, Case]
    plans: Mapping[str, AccountPlan]
    now: datetime
    allowed: Callable[[], Awaitable[bool]]
    secrets_to_hide: tuple[str, ...] = ()
    keep_review: bool = False
    keep_failed: bool = False
    prices: Mapping[str, Price] = PRICES
    options: dict[str, Any] = field(default_factory=dict[str, Any])
    outcomes: dict[str, Outcome] = field(default_factory=dict[str, Outcome])
    readings: list[Reading] = field(default_factory=list[Reading])
    failures: list[Failure] = field(default_factory=list[Failure])
    aborted: str | None = None
    calls: int = 0
    spend: int = 0
    max_ratio: float | None = None

    def hooks(self) -> CallHooks:
        return CallHooks(audit.EVAL_AUDIT_KIND, self.allowed, self.observe)

    def observe(self, bounded: int, reply: Reply) -> None:
        """One answered request: the ratio of reported to bounded input tokens, and its cost."""
        usage = reply.usage
        actual = usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
        ratio = actual / bounded
        self.max_ratio = ratio if self.max_ratio is None else max(self.max_ratio, ratio)
        self.calls += 1
        self.spend += usage_cost(
            self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            prices=self.prices,
        )


def _history(case: Case, now: datetime) -> tuple[list[Message], str]:
    """The messages of the case and the user's text (for the graders)."""
    if case.kind == "digest":
        messages = digest_model.request(digest_model.digest_week(now))
        first = messages[0].blocks[0]
        return messages, first.text if isinstance(first, Text) else ""
    return [Message("user", (Text(case.prompt),))], case.prompt


def unsent(run: Run) -> list[Failure]:
    """The cases that never ran (the run was aborted before them), for the failed file."""
    return [
        did_not_run(case, _history(case, run.now)[1], "not_run")
        for label, case in run.cases.items()
        if label not in run.outcomes
    ]


def _fenced_tx(job: JobRun) -> Callable[[], Any]:
    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with job.fenced() as fs:
            yield fs.db

    return open_tx


async def _case(job: JobRun, run: Run, case: Case) -> None:
    if run.aborted is not None:
        return
    messages, prompt = _history(case, run.now)
    try:
        turn = await run_turn(
            job,
            run.settings,
            kind=case.kind,
            history=messages,
            now=run.now,
            hooks=run.hooks(),
            **run.options,
        )
    except ModelRefused as refused:  # no call was made, and none will be
        run.aborted = refused.code
        return
    except ModelFailed:
        run.outcomes[case.label] = Outcome(case, None, ran=True)
        if run.keep_failed:
            run.failures.append(did_not_run(case, prompt, "model_failed"))
        return
    if case.review:
        if run.keep_review:
            run.readings.append(Reading(case.label, prompt, turn.text or ""))
        run.outcomes[case.label] = Outcome(case, None, ran=True)
        return
    verdict = grade(case, turn, prompt=prompt, secrets=run.secrets_to_hide)
    run.outcomes[case.label] = Outcome(case, verdict, ran=True)
    if run.keep_failed and not verdict.passed:
        run.failures.append(
            Failure(
                case.label,
                case.category,
                prompt,
                turn.text,
                turn.stopped,
                turn.uses,
                turn.results,
                verdict.reasons,
            )
        )


async def run_job(job: JobRun, run: Run) -> Complete:
    """The ``ai_eval`` kind's handler."""
    parsed = job.input
    if not isinstance(parsed, EvalInput):
        raise JobFailure("invalid_input")
    if parsed.step == "seed":
        plan = run.plans.get(parsed.name)
        if plan is None:
            raise JobFailure("invalid_input")
        await seed_account_data(
            job.factory, job.ctx, _fenced_tx(job), run.settings, plan, now=run.now
        )
        return Complete()
    case = run.cases.get(parsed.name)
    if case is None:
        raise JobFailure("invalid_input")
    await _case(job, run, case)
    return Complete()


def registry_for(run: Run) -> KindRegistry:
    registry = KindRegistry()
    registry.register(JobKind(EVAL_KIND, EvalInput, partial(run_job, run=run)))
    return registry


class Driver:
    """Queues, claims and runs the evaluation's jobs one at a time, as the worker would."""

    def __init__(self, factory: Factory, run: Run) -> None:
        self.factory, self.run = factory, run
        self.registry = registry_for(run)
        self.token = secrets.token_hex(8)
        self.counter = 0
        self.config = RunConfig(lease_seconds=LEASE_SECONDS, accounts=ACCOUNTS)

    async def step(self, owner: UUID, step: EvalInput) -> Execution | None:
        """Queue one job for ``owner`` (as that account), claim and run it."""
        self.counter += 1
        stored = json.dumps(step.model_dump(), sort_keys=True, separators=(",", ":"))
        new = NewJob(
            kind=EVAL_KIND,
            input=stored,
            idempotency_key=f"evaluation-{self.token}-{self.counter}",
            request_hash=hashlib.sha256(stored.encode()).digest(),
        )
        created = await service.create(self.factory, owner_context(owner), new, limit=ACTIVE_LIMIT)
        if created.job is None:
            raise EvalRefused("job_not_queued")
        while (
            True
        ):  # an older queued evaluation job (a crashed run's) runs first; ours ends the loop
            claimed = await queue.claim(
                self.factory, f"ai-evaluation:{self.token}", LEASE_SECONDS, [EVAL_KIND]
            )
            if claimed is None:  # ours waits for a retry: it counts as a case that did not run
                return None
            execution = Execution(claimed)
            await execute(execution, self.factory, self.registry, self.config, asyncio.Event())
            if claimed.job_id == created.job.id:
                return execution

    async def start(self) -> None:
        await queue.reap(self.factory)  # expired leases of an earlier run


def write_review(path: Path, readings: list[Reading]) -> None:
    """The review file: owner-only, synthetic prompts and answers of the marked cases."""
    lines = [
        json.dumps({"label": r.label, "prompt": r.prompt, "answer": r.answer}) for r in readings
    ]
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
