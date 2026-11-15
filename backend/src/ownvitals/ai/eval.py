"""The owner's real-model evaluation run (ADR 0014 section 9; #527).

``python -m`` on this module, by the owner, once per model and prompt and tool-set version,
against an ``_ai_eval`` database in a Postgres cluster of its own that the owner prepared
(``docs/dev/ai.md``: migrate, db-functions, then ``python -m ownvitals ai-evaluation prepare``
with the owner role's URL, which this process never holds). In this order, each step refusing
before the next:

1. the egress hook of the evaluation's role (``EVAL_HOOK_KIND``), before anything connects;
2. the locks (``eval_gates.refusal``): ``OWNVITALS_AI_EVAL`` on, a cap, a priced model, the
   provider review, the database's name, an output directory, no owner or admin credential;
   then the provider key, the audit log's ``app_secret_key`` and that the output directory
   exists;
3. the app role's engine with the role and queue checks, then ``synthetic_jobs.database_refusal``
   (the database alone in its cluster, every account marked synthetic);
4. only then the model's allowlist (``install_eval_allowlist``), the one host it may reach.

It then claims and executes the prepared runs one by one with the jobs runner (``eval_job``):
every model call is a leased running job's, under the run's own cap. No prepared run, or a run
stopped by a refusal, fails the whole evaluation. The summary is shape-only
(``eval_summary``) and ends with the reference to record only when every category passed. The
answers of the cases for the owner's reading go to one new file in ``ai_eval_out_dir`` (case id
and answer, one JSON line each; synthetic questions only), the one place the owner reads them.
Exit status: 0 passed, 1 failed, 2 refused before any model call.
"""

import asyncio
import json
import os
import secrets
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from ownvitals.ai.audit import AuditNotConfigured, audit_key
from ownvitals.ai.eval_gates import refusal, run_settings
from ownvitals.ai.eval_job import Record, register_run_kind
from ownvitals.ai.eval_summary import summarize
from ownvitals.ai.gates import ModelRefused, read_api_key
from ownvitals.ai.pricing import PRICES
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.config import Settings
from ownvitals.db import (
    DatabaseRoleError,
    create_engine,
    create_session_factory,
    event_loop_factory,
    verify_database_role,
)
from ownvitals.db.queue_check import install_queue_check, verify_queue
from ownvitals.db.synthetic_jobs import EVAL_RUN_KIND, database_refusal
from ownvitals.egress import EVAL_HOOK_KIND, install_egress_hook, install_eval_allowlist
from ownvitals.jobs import KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute

LEASE_SECONDS: Final = 300
PASSED, FAILED, REFUSED = 0, 1, 2
Print = Callable[[str], None]


def _worker_id() -> str:
    return f"ai-evaluation:{os.getpid()}:{secrets.token_hex(4)}"


async def _claim_and_run(factory: Any, registry: KindRegistry, record: Record) -> None:
    worker = _worker_id()
    config = RunConfig(lease_seconds=LEASE_SECONDS, accounts=ACCOUNTS)
    stop = asyncio.Event()
    ran = 0
    while record.stopped is None:
        claimed = await queue.claim(factory, worker, LEASE_SECONDS, [EVAL_RUN_KIND])
        if claimed is None:
            break
        ran += 1
        await execute(Execution(claimed), factory, registry, config, stop)
    if ran == 0:
        record.stopped = "no_prepared_runs"
    elif record.stopped is None and record.completed != ran:
        record.stopped = "run_failed"


async def run(
    settings: Settings, *, out: Print = print, now: datetime | None = None, **call_options: Any
) -> int:
    """The steps after the hook (module docstring); ``call_options`` reach the model client
    (the tests' fake transport, prices and key directory)."""
    refused = _start_refusal(settings, call_options)
    if refused is not None:
        out(f"refused={refused}")
        return REFUSED
    engine = create_engine(settings)
    install_queue_check(engine)
    try:
        try:
            await verify_database_role(engine)
            await verify_queue(engine)
        except DatabaseRoleError:
            out("refused=database_role")
            return REFUSED
        factory = create_session_factory(engine)
        refused = await database_refusal(factory)
        if refused is not None:
            out(f"refused={refused}")
            return REFUSED
        install_eval_allowlist()
        record = Record()
        registry = KindRegistry()
        register_run_kind(
            registry,
            settings=run_settings(settings),
            record=record,
            now=now or datetime.now(UTC),
            **call_options,
        )
        await _claim_and_run(factory, registry, record)
    finally:
        await engine.dispose()
    summary = summarize(record.outcomes, model=settings.ai_model, stopped=record.stopped)
    for line in summary.lines:
        out(line)
    if record.readings:
        out(f"readings_file={_write_readings(Path(settings.ai_eval_out_dir), record).name}")
    return PASSED if summary.passed else FAILED


def _start_refusal(settings: Settings, call_options: dict[str, Any]) -> str | None:
    refused = refusal(settings, call_options.get("prices", PRICES))
    if refused is not None:
        return refused
    try:
        read_api_key(call_options.get("secrets_directory"))
    except ModelRefused as missing:
        return missing.code
    try:
        audit_key(settings)
    except AuditNotConfigured:
        return "app_secret_missing"
    if not Path(settings.ai_eval_out_dir).is_dir():
        return "output_unavailable"
    return None


def _write_readings(directory: Path, record: Record) -> Path:
    """A new file (never an existing one), readable by its owner only."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"ai-evaluation-{stamp}-{secrets.token_hex(3)}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        for case_id, answer in record.readings:
            file.write(json.dumps({"case": case_id, "answer": answer}, ensure_ascii=False) + "\n")
    return path


def main() -> int:
    settings = Settings()
    install_egress_hook(EVAL_HOOK_KIND, settings)  # first: nothing has connected yet
    return asyncio.run(run(settings), loop_factory=event_loop_factory())


if __name__ == "__main__":
    sys.exit(main())
