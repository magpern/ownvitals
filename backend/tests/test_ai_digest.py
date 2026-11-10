"""The weekly digest's job in the worker (issue 209; ADR 0014 sections 3, 5 and 7; PLAN section
7.4): a digest written by the ``ai_digest`` job with the fake model and stored with its "waiting"
mail; no call without consent, the flag or the opt-in, or over the cap (the week then skipped);
a failing provider tried once a week; a withdrawal mid-call stores nothing and deletes the
digests; the store's ``NOWAIT`` against a withdrawal holding the consent row; one user's job never
writes another's digest (RLS in the worker context).

Synthetic users, dates and text, the fake model; no network.
"""

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aichat import (  # noqa: F401 - fixtures
    ChatEnv,
    _ai_worker,
    chat_fixture,
    chat_secrets_fixture,
    count,
    sql,
)
from aidigest import (
    DIGEST_TEXT,
    WEEK,
    digest_mail,
    digest_user,
    digests,
    queue_digest,
    run_digest_job,
)
from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from ctx import make_context
from fakemodel import Seen, status, tools
from fakemodel import text as answer
from ownvitals.ai import digest
from ownvitals.ai.digest_model import DIGEST_KIND
from ownvitals.db import user_session
from ownvitals.jobs import JobFailure, JobRun
from ownvitals.jobs.fence import Lease

SYNTHETIC = True
# Far above a NOWAIT answer (slow runners), far below a hung lock wait.
STORE_SECONDS = 30


def _job(job_id: uuid.UUID) -> Any:
    return sql("SELECT state, error_category, input FROM jobs WHERE id = :j", j=job_id).one()


# --- a digest written ----------------------------------------------------------------------------


async def test_a_due_digest_is_written_stored_and_announced_without_its_content(
    chat: ChatEnv,
) -> None:
    who = await digest_user(chat, "synthetic-digest-a@example.com")
    job_id = await queue_digest(chat, who)
    # The job row holds the week only, never text or another id (T47).
    assert _job(job_id).input == {"week_start": WEEK.isoformat()}
    chat.fake.script = [tools(("list_metrics", {})), answer(DIGEST_TEXT)]
    execution = await run_digest_job(chat)
    assert execution is not None
    assert execution.result == "completed"
    assert digests(who.id) == [(WEEK, "ready", DIGEST_TEXT)]
    # One "waiting" mail, linking to the assistant page; the row has no place for the text.
    assert digest_mail(who.id) == [("ai_digest_ready", "assistant")]
    row = sql("SELECT * FROM mail_outbox WHERE owner_id = :o", o=who.id).mappings().one()
    assert DIGEST_TEXT not in json.dumps({k: str(v) for k, v in row.items()})
    # The request: the digest kind's bound, the fixed instruction, no identifier.
    first = chat.fake.seen[0].json
    assert first["max_tokens"] == 2048
    assert "weekly summary for the week from 2026-11-02 to 2026-11-08" in json.dumps(first)
    assert str(who.id) not in chat.fake.seen[0].body.decode()
    assert count("ai_audit", who.id) == 2  # one row per call, kind digest
    kinds = sql("SELECT DISTINCT kind FROM ai_audit WHERE owner_id = :o", o=who.id).scalars()
    assert list(kinds) == ["digest"]


async def test_a_week_already_stored_or_opted_out_makes_no_call(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-digest-b@example.com")
    await queue_digest(chat, who)
    chat.fake.script = [answer(DIGEST_TEXT)]
    await run_digest_job(chat)
    assert chat.fake.attempts == 1
    await queue_digest(chat, who)  # the same week again
    execution = await run_digest_job(chat)
    assert execution is not None
    assert execution.result == "completed"
    assert chat.fake.attempts == 1
    sql("DELETE FROM ai_digest_prefs WHERE owner_id = :o", o=who.id)
    await queue_digest(chat, who, WEEK - timedelta(weeks=1))
    execution = await run_digest_job(chat)
    assert execution is not None
    assert execution.result == "completed"
    assert chat.fake.attempts == 1
    assert len(digests(who.id)) == 1


async def test_a_week_out_of_bounds_is_invalid_and_makes_no_call(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-digest-c@example.com")
    job_id = await queue_digest(chat, who, WEEK + timedelta(weeks=1))  # not over yet at NOW
    execution = await run_digest_job(chat)
    assert execution is not None
    assert execution.result == "failed"
    assert _job(job_id).error_category == "invalid_input"
    assert chat.fake.attempts == 0
    assert digests(who.id) == []


# --- no call -------------------------------------------------------------------------------------


async def test_no_consent_means_no_call_and_no_digest(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-digest-d@example.com", consent=False)
    job_id = await queue_digest(chat, who)
    execution = await run_digest_job(chat)
    assert execution is not None
    assert execution.result == "failed"
    assert _job(job_id).error_category == "ai_not_enabled"
    assert chat.fake.attempts == 0
    assert digests(who.id) == []
    assert count("ai_reservations", who.id) == 0


async def test_the_stored_flag_off_means_no_call_and_no_digest(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-digest-e@example.com")
    job_id = await queue_digest(chat, who)
    sql("INSERT INTO app_settings (flag, value) VALUES ('ai_insights', '\"off\"')")
    execution = await run_digest_job(chat)
    assert execution is not None
    assert _job(job_id).error_category == "ai_not_enabled"
    assert chat.fake.attempts == 0
    assert digests(who.id) == []


async def test_an_owner_lock_off_means_no_call_and_no_digest(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-digest-f@example.com")
    job_id = await queue_digest(chat, who)
    locked = chat.env.settings.model_copy(update={"ai_provider_review_ref": ""})
    execution = await run_digest_job(chat, settings=locked)
    assert execution is not None
    assert _job(job_id).error_category == "ai_unavailable"
    assert chat.fake.attempts == 0
    assert digests(who.id) == []


async def test_over_the_cap_the_week_is_skipped_without_a_call_or_a_mail(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-digest-g@example.com")
    await queue_digest(chat, who)
    capped = chat.env.settings.model_copy(update={"ai_user_monthly_cap_microusd": 1})
    execution = await run_digest_job(chat, settings=capped)
    assert execution is not None
    assert execution.result == "completed"
    assert chat.fake.attempts == 0
    assert digests(who.id) == [(WEEK, "skipped_budget", None)]
    assert digest_mail(who.id) == []
    assert count("ai_reservations", who.id) == 0


# --- failures ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        [status(500)],  # charged: the week is not tried again
        [tools(("delete_account", {}))],  # an unknown tool ends the turn
        [tools(*[("list_metrics", {})] * 9)],  # more than eight tool calls
        [answer("   ")],  # no text
    ],
)
async def test_a_failed_turn_stores_the_week_as_unavailable_once(
    chat: ChatEnv, script: list[Any]
) -> None:
    who = await digest_user(chat, f"synthetic-digest-h{uuid.uuid4().hex[:6]}@example.com")
    await queue_digest(chat, who)
    chat.fake.script = list(script)
    execution = await run_digest_job(chat)
    assert execution is not None
    assert execution.result == "completed"
    assert digests(who.id) == [(WEEK, "unavailable", None)]
    assert digest_mail(who.id) == []
    attempts = chat.fake.attempts
    await queue_digest(chat, who)
    await run_digest_job(chat)
    assert chat.fake.attempts == attempts  # never a second call that week


# --- withdrawal ----------------------------------------------------------------------------------


async def test_withdrawal_mid_call_stores_nothing_and_deletes_the_digests(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-digest-i@example.com")
    sql(
        "INSERT INTO ai_digests (owner_id, week_start, status, content) "
        "VALUES (:o, :w, 'ready', 'synthetic earlier digest')",
        o=who.id,
        w=WEEK - timedelta(weeks=1),
    )
    job_id = await queue_digest(chat, who)
    withdrawn: list[int] = []

    async def withdraw_now(_: Seen) -> None:
        response = await who.user.browser.request("DELETE", "/api/ai/consent")
        assert response.status_code == 200, response.text
        withdrawn.append(response.json()["cancelled_jobs"])

    chat.fake.before = withdraw_now
    chat.fake.script = [answer("Synthetic digest that must not be kept.")]
    execution = await run_digest_job(chat)
    assert execution is not None
    assert withdrawn == [1]  # the running digest was asked to stop
    assert execution.result == "cancelled"
    assert _job(job_id).state == "cancelled"
    assert digests(who.id) == []  # the earlier digest went with the withdrawal
    assert sql("SELECT count(*) FROM ai_digest_prefs WHERE owner_id = :o", o=who.id).scalar() == 0
    assert digest_mail(who.id) == []


async def test_a_withdrawal_cancels_a_queued_digest(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-digest-j@example.com")
    job_id = await queue_digest(chat, who)
    response = await who.user.browser.request("DELETE", "/api/ai/consent")
    assert response.status_code == 200, response.text
    assert response.json()["cancelled_jobs"] == 1
    assert _job(job_id).state == "cancelled"
    assert await run_digest_job(chat) is None
    assert chat.fake.attempts == 0


def _running(chat: ChatEnv, job_id: uuid.UUID, owner: uuid.UUID) -> JobRun:
    sql(
        "UPDATE jobs SET state = 'running', attempt = 1, lease_version = 1, "
        "leased_by = 'synthetic-worker', lease_expires_at = now() + interval '10 minutes', "
        "heartbeat_at = now(), started_at = now() WHERE id = :j",
        j=job_id,
    )
    return JobRun(
        chat.worker_factory,
        Lease(job_id, 1, make_context(owner)),
        kind=DIGEST_KIND,
        input=None,  # pyright: ignore[reportArgumentType]
        attempt=1,
        lease_seconds=60,
        shutdown=asyncio.Event(),
    )


async def test_the_store_does_not_wait_on_a_withdrawal_that_holds_the_consent_row(
    chat: ChatEnv,
) -> None:
    """The store holds its job row (the fence); a withdrawal locks the consent row first and the
    job rows after it. The store's ``NOWAIT`` read refuses at once instead of closing the cycle,
    and the withdrawal then gets the job row."""
    who = await digest_user(chat, "synthetic-digest-k@example.com")
    job = _running(chat, await queue_digest(chat, who), who.id)
    store = digest._store(who.id, WEEK, "ready", DIGEST_TEXT)  # pyright: ignore[reportPrivateUsage]
    async with user_session(chat.env.factory, make_context(who.id)) as withdrawing:
        await withdrawing.execute(
            text("UPDATE ai_consents SET withdrawn_at = now() WHERE owner_id = :o"), {"o": who.id}
        )
        refused: JobFailure | None = None
        try:
            async with asyncio.timeout(STORE_SECONDS), job.fenced() as fs:
                await store(fs)
        except JobFailure as failure:
            refused = failure
        assert refused is not None
        assert refused.category == "ai_not_enabled"
        await withdrawing.execute(
            text("SELECT id FROM jobs WHERE id = :j FOR UPDATE"), {"j": job.lease.job_id}
        )
    assert digests(who.id) == []
    assert digest_mail(who.id) == []


async def test_the_store_refuses_after_a_committed_withdrawal(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-digest-l@example.com")
    job = _running(chat, await queue_digest(chat, who), who.id)
    sql("UPDATE ai_consents SET withdrawn_at = now() WHERE owner_id = :o", o=who.id)
    store = digest._store(who.id, WEEK, "ready", DIGEST_TEXT)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(JobFailure) as refused:
        async with job.fenced() as fs:
            await store(fs)
    assert refused.value.category == "ai_not_enabled"
    assert digests(who.id) == []


async def test_the_store_keeps_nothing_after_an_opt_out(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-digest-m@example.com")
    job = _running(chat, await queue_digest(chat, who), who.id)
    sql("DELETE FROM ai_digest_prefs WHERE owner_id = :o", o=who.id)
    store = digest._store(who.id, WEEK, "ready", DIGEST_TEXT)  # pyright: ignore[reportPrivateUsage]
    async with job.fenced() as fs:
        await store(fs)
    assert digests(who.id) == []
    assert digest_mail(who.id) == []


# --- isolation in the worker context -------------------------------------------------------------


async def test_a_digest_job_writes_only_its_owners_digest(chat: ChatEnv) -> None:
    alice = await digest_user(chat, "synthetic-digest-n@example.com")
    bob = await digest_user(chat, "synthetic-digest-o@example.com")
    sql(
        "INSERT INTO ai_digests (owner_id, week_start, status, content) "
        "VALUES (:o, :w, 'ready', 'synthetic digest of the other user')",
        o=bob.id,
        w=WEEK,
    )
    await queue_digest(chat, alice)
    chat.fake.script = [answer(DIGEST_TEXT)]
    execution = await run_digest_job(chat)
    assert execution is not None
    assert execution.result == "completed"
    # Bob's stored week did not make Alice's "already stored", and Alice's job wrote only hers.
    assert digests(alice.id) == [(WEEK, "ready", DIGEST_TEXT)]
    assert digests(bob.id) == [(WEEK, "ready", "synthetic digest of the other user")]
    assert digest_mail(bob.id) == []
    assert "synthetic digest of the other user" not in b"".join(chat.fake.bodies()).decode()


async def test_rls_refuses_a_digest_for_another_owner_in_the_job_context(chat: ChatEnv) -> None:
    alice = await digest_user(chat, "synthetic-digest-p@example.com")
    bob = await digest_user(chat, "synthetic-digest-q@example.com")
    job = _running(chat, await queue_digest(chat, alice), alice.id)
    async with job.fenced() as fs:
        seen = await fs.db.execute(text("SELECT owner_id FROM ai_digest_prefs"))
        assert {r.owner_id for r in seen} == {alice.id}
    with pytest.raises(DBAPIError):
        async with job.fenced() as fs:
            await fs.db.execute(
                text(
                    "INSERT INTO ai_digests (owner_id, week_start, status) "
                    "VALUES (:o, :w, 'unavailable')"
                ),
                {"o": bob.id, "w": WEEK},
            )
    assert digests(bob.id) == []


async def test_an_opt_out_waits_for_a_store_that_read_the_opt_in(chat: ChatEnv) -> None:
    """The store's opt-in read is ``FOR SHARE``: an opt-out landing before the store commits
    cannot delete the preference row under it (a plain read would let the delete through)."""
    who = await digest_user(chat, "synthetic-digest-share@example.com")
    job = _running(chat, await queue_digest(chat, who), who.id)
    store = digest._store(who.id, WEEK, "ready", DIGEST_TEXT)  # pyright: ignore[reportPrivateUsage]
    async with job.fenced() as fs:
        await store(fs)  # the transaction stays open: the share lock is held
        async with user_session(chat.env.factory, make_context(who.id)) as opting_out:
            await opting_out.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(DBAPIError):
                await opting_out.execute(
                    text("DELETE FROM ai_digest_prefs WHERE owner_id = :o"), {"o": who.id}
                )
    assert len(digests(who.id)) == 1
