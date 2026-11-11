"""When the weekly digest is queued (issue 209; ADR 0014 sections 1, 3 and 7): the notification
pass, wrapped with ``then_digest`` as the worker's registry wraps it, queues one ``ai_digest`` job
for its owner only when the environment flag and every owner lock are open, the stored flag is on,
a live consent to the current text exists, the owner opted in and the week is not stored yet. The
job holds the week only, is one per owner (the singleton) and runs once; with the defaults nothing
is ever queued.

Synthetic users and dates; the fake model is never called here.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from aichat import (  # noqa: F401 - fixtures
    ChatEnv,
    _ai_worker,
    chat_fixture,
    chat_secrets_fixture,
    sql,
)
from aidigest import digest_jobs, digest_user, opt_in, run_digest_job, run_notify_pass
from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from fakemodel import text as answer
from ownvitals.ai.digest_model import digest_week
from ownvitals.config import Settings
from ownvitals.notify.findings_job import KIND as NOTIFY_KIND
from ownvitals.worker.kinds import worker_registry

SYNTHETIC = True


def _week() -> str:
    return digest_week(datetime.now(UTC)).isoformat()


async def test_a_due_digest_is_queued_once_with_the_week_only(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-queue-a@example.com")
    execution = await run_notify_pass(chat, who)
    assert execution is not None
    assert execution.result == "completed"
    (job,) = digest_jobs(who.id)
    assert (job.state, job.origin, job.input) == ("queued", "scheduler", {"week_start": _week()})
    # A second pass while it waits: the singleton keeps one job.
    await run_notify_pass(chat, who)
    assert len(digest_jobs(who.id)) == 1
    max_attempts = sql("SELECT max_attempts FROM jobs WHERE id = :j", j=job.id).scalar_one()
    assert max_attempts == 1  # each try may be billed
    # The job runs and stores the week; the next pass queues nothing more. (The second pass
    # moved the queued job's start 30 s later, as the singleton does; brought back here.)
    sql("UPDATE jobs SET available_at = now() WHERE id = :j", j=job.id)
    chat.fake.script = [answer("Synthetic weekly summary.")]
    ran = await run_digest_job(chat, now=datetime.now(UTC))
    assert ran is not None
    assert ran.result == "completed"
    await run_notify_pass(chat, who)
    assert [j.state for j in digest_jobs(who.id)] == ["completed"]


@pytest.mark.parametrize(
    "lock",
    [
        {"ai_insights": False},
        {"ai_model": ""},
        {"ai_provider_review_ref": ""},
        {"ai_eval_ref": "synthetic-model-1/0/0"},
    ],
)
async def test_an_owner_lock_off_queues_nothing(chat: ChatEnv, lock: dict[str, Any]) -> None:
    who = await digest_user(chat, "synthetic-queue-b@example.com")
    execution = await run_notify_pass(chat, who, **lock)
    assert execution is not None
    assert execution.result == "completed"  # the pass itself is not affected
    assert digest_jobs(who.id) == []


async def test_the_stored_flag_off_queues_nothing(chat: ChatEnv) -> None:
    who = await digest_user(chat, "synthetic-queue-c@example.com")
    sql("INSERT INTO app_settings (flag, value) VALUES ('ai_insights', '\"off\"')")
    await run_notify_pass(chat, who)
    assert digest_jobs(who.id) == []


async def test_without_consent_or_with_an_outdated_one_nothing_is_queued(chat: ChatEnv) -> None:
    none = await digest_user(chat, "synthetic-queue-d@example.com", consent=False)
    await run_notify_pass(chat, none)
    assert digest_jobs(none.id) == []
    old = await digest_user(chat, "synthetic-queue-e@example.com")
    sql(
        "UPDATE ai_consents SET consent_version = consent_version + 1 WHERE owner_id = :o", o=old.id
    )
    await run_notify_pass(chat, old)
    assert digest_jobs(old.id) == []
    withdrawn = await digest_user(chat, "synthetic-queue-f@example.com")
    sql("UPDATE ai_consents SET withdrawn_at = now() WHERE owner_id = :o", o=withdrawn.id)
    await run_notify_pass(chat, withdrawn)
    assert digest_jobs(withdrawn.id) == []


async def test_without_the_opt_in_nothing_is_queued_and_anothers_opt_in_does_not_count(
    chat: ChatEnv,
) -> None:
    alice = await digest_user(chat, "synthetic-queue-g@example.com", opted_in=False)
    bob = await digest_user(chat, "synthetic-queue-h@example.com")
    await run_notify_pass(chat, alice)
    assert digest_jobs(alice.id) == []
    assert digest_jobs(bob.id) == []  # Alice's pass never queues Bob's digest
    opt_in(alice.id)
    await run_notify_pass(chat, alice)
    assert len(digest_jobs(alice.id)) == 1
    assert digest_jobs(bob.id) == []


async def test_no_second_digest_is_queued_while_one_runs(chat: ChatEnv) -> None:
    """The singleton collapses queued jobs only; a running one must not be followed by a second
    that could bill the same week twice (#355)."""
    who = await digest_user(chat, "synthetic-queue-r@example.com")
    await run_notify_pass(chat, who)
    (job,) = digest_jobs(who.id)
    sql(
        "UPDATE jobs SET state = 'running', attempt = 1, lease_version = 1, "
        "leased_by = 'synthetic-worker', lease_expires_at = now() + interval '10 minutes', "
        "heartbeat_at = now(), started_at = now() WHERE id = :j",
        j=job.id,
    )
    await run_notify_pass(chat, who)
    assert [j.state for j in digest_jobs(who.id)] == ["running"]


async def test_a_week_already_stored_queues_nothing(chat: ChatEnv) -> None:
    alice = await digest_user(chat, "synthetic-queue-i@example.com")
    bob = await digest_user(chat, "synthetic-queue-j@example.com")
    for owner in (alice.id, bob.id):
        sql(
            "INSERT INTO ai_digests (owner_id, week_start, status) VALUES (:o, :w, 'unavailable')",
            o=owner,
            w=digest_week(datetime.now(UTC)),
        )
    await run_notify_pass(chat, alice)
    assert digest_jobs(alice.id) == []
    sql("DELETE FROM ai_digests WHERE owner_id = :o", o=alice.id)
    await run_notify_pass(chat, alice)  # Bob's stored week is not Alice's
    assert len(digest_jobs(alice.id)) == 1


def test_the_defaults_never_queue_and_the_worker_wraps_the_notification_pass() -> None:
    from ownvitals.ai.gates import ModelRefused, check_locks

    with pytest.raises(ModelRefused):
        check_locks(Settings())  # AI off, no model, no review, no eval, no price
    registry = worker_registry()
    notify = registry.get(NOTIFY_KIND)
    assert notify is not None
    assert notify.run.__qualname__ == "then_digest.<locals>.run"
    assert registry.get("ai_digest") is not None
