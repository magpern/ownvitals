"""The model client's consent reads inside a fenced transaction do not wait on a withdrawal (ADR
0014 section 3; #208). A fenced transaction holds its job row from its first statement, and a
withdrawal locks the consent row first and the job rows after it: so the reservation and the
re-check before a later attempt read the consent ``FOR SHARE NOWAIT``. While a withdrawal holds
the row, each answers "not enabled" at once, sends nothing, and leaves the job row free for the
withdrawal to finish.

Synthetic owners and jobs, the fake model behind the real egress client; no network.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import text

from fakemodel import CONNECT
from ownvitals.ai.gates import ModelFailed, ModelRefused
from ownvitals.db import user_session
from test_ai_client_env import (  # noqa: F401 - fixtures
    AiCall,
    _ai_worker,
    admin_fixture,
    ai_fixture,
    aicall_fixture,
    jenv_fixture,
    queue_schema_fixture,
    secrets_fixture,
)

SYNTHETIC = True
# Far above a NOWAIT answer, far below a lock wait ending in a deadlock or a lock timeout.
AT_ONCE_SECONDS = 30.0


@asynccontextmanager
async def withdrawal_holding_the_consent_row(aicall: AiCall) -> AsyncIterator[Any]:
    """A withdrawal's first step, uncommitted: the consent row is locked by its ``UPDATE``.
    On leaving, the withdrawal's next step (the job row ``FOR UPDATE``) must not have to wait."""
    async with user_session(aicall.job.factory, aicall.job.ctx) as db:
        await db.execute(
            text("UPDATE ai_consents SET withdrawn_at = now() WHERE owner_id = :o"),
            {"o": aicall.owner},
        )
        yield db
        async with asyncio.timeout(AT_ONCE_SECONDS):
            await db.execute(
                text("SELECT id FROM jobs WHERE id = :j FOR UPDATE"),
                {"j": aicall.job.lease.job_id},
            )


async def test_the_reservation_does_not_wait_on_a_withdrawal(aicall: AiCall) -> None:
    async with withdrawal_holding_the_consent_row(aicall):
        with pytest.raises(ModelRefused, match="ai_not_enabled"):
            async with asyncio.timeout(AT_ONCE_SECONDS):
                await aicall.call()
    assert aicall.fake.attempts == 0
    assert aicall.reservations() == []
    assert aicall.audit() == []


async def test_the_recheck_before_a_later_attempt_does_not_wait_on_a_withdrawal(
    aicall: AiCall,
) -> None:
    holding = asyncio.Event()
    release = asyncio.Event()

    async def withdrawal() -> None:
        async with withdrawal_holding_the_consent_row(aicall):
            holding.set()
            await release.wait()

    tasks: list[asyncio.Task[None]] = []

    async def backoff(_: float) -> None:  # after the reservation: the withdrawal starts now
        tasks.append(asyncio.create_task(withdrawal()))
        await holding.wait()

    sleep: Callable[[float], Awaitable[None]] = backoff
    aicall.fake.script = [CONNECT]  # the first attempt is never sent, so a second is allowed
    try:
        with pytest.raises(ModelFailed) as failed:
            async with asyncio.timeout(AT_ONCE_SECONDS):
                await aicall.call(sleep=sleep)
    finally:
        release.set()
        assert len(tasks) == 1
        await tasks[0]  # the withdrawal finishes: its job row lock did not wait
    assert failed.value.code == "consent"
    assert aicall.fake.attempts == 1  # the refused first attempt only; no second
    assert aicall.fake.seen == []  # nothing reached the provider
    assert [r.state for r in aicall.reservations()] == ["released"]
