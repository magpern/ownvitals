"""Finding notifications on a real database (PLAN §7.1 step 7, ADR 0010 §4; #195): what notifies,
the user's preferences, the experimental tier, and exactly once under concurrent passes. The
step runs as the app role under forced RLS. Synthetic users and findings only.

The hold is ``test_notify_hold_db.py`` (whose helpers these are); the job, its wiring and a pass
after a real rule evaluation are ``test_notify_findings_job.py``.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
)
from test_notify_hold_db import (
    T0,
    cursor,
    email_on,
    ended,
    hold_of,
    mails,
    pending,
    plant_recording,
    state_of,
    step,
)
from test_views_findings import (  # noqa: F401 - fixtures
    owner_env_fixture,
    plain_fixture,
    plant_finding,
    plant_group,
    run,
    store_flag,
    user,
)

SYNTHETIC = True
WAITING = ("findings_waiting", "findings", "pending")


# --- what notifies -----------------------------------------------------------------------------


async def test_a_backfill_episode_never_notifies(plain: Env) -> None:
    ctx, _ = user()
    email_on(ctx.user_id)
    history = pending(ctx.user_id, "E1", [], origin="backfill", metric_keys=())
    await step(plain, ctx, T0)
    assert state_of(history) == ("none", None)
    assert mails(ctx.user_id) == []
    with pytest.raises(Exception, match="findings_backfill_quiet"):  # the schema keeps it so
        run("UPDATE findings SET notify_state = 'pending' WHERE id = :id", id=history)


async def test_a_live_episode_notifies_once_and_its_updates_never(plain: Env) -> None:
    ctx, _ = user()
    email_on(ctx.user_id)
    finding = pending(ctx.user_id, "E1", [], metric_keys=())
    outcome = await step(plain, ctx, T0)
    assert (outcome.sent, state_of(finding)) == (1, ("sent", T0))
    assert mails(ctx.user_id) == [WAITING]
    run("UPDATE mail_outbox SET state = 'sent', sent_at = now() WHERE owner_id = :o", o=ctx.user_id)
    run(  # the rules engine updates the episode (a later firing): the state stays
        "UPDATE findings SET last_seen = :t, evidence = '{\"synthetic\": 2}' WHERE id = :id",
        id=finding,
        t=T0 + timedelta(days=1),
    )
    again = await step(plain, ctx, T0 + timedelta(days=1))
    assert (again.sent, state_of(finding)) == (0, ("sent", T0))
    assert mails(ctx.user_id) == [("findings_waiting", "findings", "sent")]


async def test_a_finding_that_is_not_pending_is_left_alone(plain: Env) -> None:
    ctx, _ = user()
    quiet = plant_finding(ctx.user_id, "E1", [], metric_keys=())  # live, notify_state none
    await step(plain, ctx, T0)
    assert state_of(quiet) == ("none", None)


# --- the user's preferences --------------------------------------------------------------------


async def test_without_email_it_is_sent_in_app_only(plain: Env) -> None:
    ctx, _ = user()  # no profile row: the defaults (no email, every tier)
    finding = pending(ctx.user_id, "C1", [])
    await step(plain, ctx, T0)
    assert state_of(finding) == ("sent", T0)
    assert mails(ctx.user_id) == []


async def test_several_findings_queue_one_mail(plain: Env) -> None:
    ctx, _ = user()
    email_on(ctx.user_id)
    findings = [pending(ctx.user_id, "C1", []) for _ in range(3)]
    assert (await step(plain, ctx, T0)).sent == 3
    assert [state_of(f) for f in findings] == [("sent", T0)] * 3
    assert mails(ctx.user_id) == [WAITING]  # coalesced per owner


async def test_a_tier_the_user_does_not_notify_never_notifies(plain: Env) -> None:
    ctx, connection = user()
    email_on(ctx.user_id, ["device", "system"])
    trend = pending(ctx.user_id, "C1", [])
    device = pending(ctx.user_id, "E1", [], metric_keys=())
    plant_group(ctx.user_id, connection, 91, minutes=0)
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="pass")
    held_trend = pending(ctx.user_id, "E6", [91])  # held first, then not delivered
    await step(plain, ctx, T0)
    assert state_of(trend) == ("none", None)
    assert state_of(device) == ("sent", T0)
    assert state_of(held_trend) == ("none", None)
    assert ended(held_trend) == ("linked", "none")
    assert mails(ctx.user_id) == [WAITING]


# --- the experimental tier ---------------------------------------------------------------------


async def test_experimental_findings_notify_only_with_the_owner_only_flag(
    plain: Env, owner_env: Env
) -> None:
    ctx, connection = user()
    email_on(ctx.user_id)
    plant_group(ctx.user_id, connection, 91, minutes=0)
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="pass")
    without = pending(ctx.user_id, "E4", [91])
    disguised = pending(ctx.user_id, "E4", [91], tier="trend")  # an experimental rule's row
    experimental_b = pending(ctx.user_id, "B1", [], tier="experimental")
    await step(plain, ctx, T0)
    for finding in (without, disguised, experimental_b):
        assert state_of(finding) == ("none", None)
        assert hold_of(finding) is None
    await step(owner_env, ctx, T0 + timedelta(hours=1))  # the flag on later: still never
    assert state_of(without) == ("none", None)
    assert mails(ctx.user_id) == []
    with_flag = pending(ctx.user_id, "E4", [91])
    await step(owner_env, ctx, T0 + timedelta(hours=2))
    assert state_of(with_flag) == ("sent", T0 + timedelta(hours=2))
    assert ended(with_flag) == ("linked", "sent")
    assert mails(ctx.user_id) == [WAITING]


async def test_a_held_finding_the_flag_no_longer_shows_ends_withdrawn(owner_env: Env) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 91, minutes=0)
    cursor(ctx.user_id, connection)
    finding = pending(ctx.user_id, "E4", [91])
    await step(owner_env, ctx, T0)
    assert state_of(finding) == ("held", None)
    store_flag("off")  # the stored flag turns it off for the account
    await step(owner_env, ctx, T0 + timedelta(hours=1))
    assert state_of(finding) == ("none", None)
    assert ended(finding) == ("withdrawn", "none")


# --- exactly once, and only the owner's --------------------------------------------------------


@pytest.mark.parametrize("rule", ["C1", "E6"])
async def test_concurrent_passes_notify_a_live_episode_exactly_once(plain: Env, rule: str) -> None:
    ctx, connection = user()
    email_on(ctx.user_id)
    plant_group(ctx.user_id, connection, 91, minutes=0)
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="pass")
    findings = [pending(ctx.user_id, rule, [91]) for _ in range(5)]
    outcomes = await asyncio.gather(*(step(plain, ctx, T0) for _ in range(4)))
    assert sum(o.sent for o in outcomes) == 5
    assert [state_of(f) for f in findings] == [("sent", T0)] * 5
    assert mails(ctx.user_id) == [WAITING]
    if rule == "E6":
        assert [ended(f) for f in findings] == [("linked", "sent")] * 5


async def test_a_pass_reads_and_moves_only_its_owners_findings(plain: Env) -> None:
    alice, _ = user()
    bob, _ = user()
    email_on(bob.user_id)
    theirs = pending(bob.user_id, "C1", [])
    await step(plain, alice, T0)
    assert state_of(theirs) == ("pending", None)
    assert mails(bob.user_id) == []


async def test_an_unknown_finding_id_moves_nothing(plain: Env) -> None:
    ctx, _ = user()
    assert state_of(uuid4()) is None
    assert (await step(plain, ctx, T0)).sent == 0
