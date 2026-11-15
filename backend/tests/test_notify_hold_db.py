"""The notification hold on a real database (ADR 0006 section 11 and the hold cases of
``docs/adr/0006-ecg-subsystem-tests.md``; #195): the step (``ownvitals.notify.findings``) as the
app role under forced RLS, with a fake clock and the bound changed through its setting.

Synthetic users, findings, groups, recordings, ``ecg`` cursors and sync jobs only; no real
account or measurement. The planting helpers are ``test_views_findings``'s and are shared with
``test_notify_findings_db.py``.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
)
from jobtools import digest
from ownvitals.config_notify import NotifySettings
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.notify.findings import NotifyOutcome, notify_owner
from ownvitals.notify.tables import HOLD_OUTCOMES, REASONS
from test_views_findings import (  # noqa: F401 - fixtures
    _conn,
    estimate,
    owner_env_fixture,
    plain_fixture,
    plant_finding,
    plant_group,
    run,
    user,
)
from test_views_findings import plant_recording as plant_with_estimate

SYNTHETIC = True
T0 = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
BOUND = timedelta(hours=24)


# --- planting and reading (as the superuser) ---------------------------------------------------


def pending(owner: UUID, rule: str, groups: list[int], **kw: Any) -> UUID:
    """A live finding just written by the rules engine: ``notify_state`` ``pending``."""
    finding = plant_finding(owner, rule, groups, **kw)
    if kw.get("origin", "live") == "live":
        run(
            "UPDATE findings SET notify_state = 'pending', first_seen = :t, last_seen = :t "
            "WHERE id = :id",
            id=finding,
            t=T0,
        )
    return finding


def cursor(owner: UUID, connection: UUID, capability: str | None = "supported", **kw: Any) -> None:
    """The connection's ``ecg`` cursor: complete unless told otherwise."""
    run(
        "INSERT INTO sync_cursors (owner_id, connection_id, stream, capability, backfill_state, "
        "last_ok_at) VALUES (:o, :c, 'ecg', :cap, CAST(:state AS jsonb), :ok) "
        "ON CONFLICT (owner_id, connection_id, stream) DO UPDATE SET "
        "capability = EXCLUDED.capability, backfill_state = EXCLUDED.backfill_state, "
        "last_ok_at = EXCLUDED.last_ok_at",
        o=owner,
        c=connection,
        cap=capability,
        state=json.dumps({"status": kw.get("status", "complete")}),
        ok=kw.get("ok"),
    )


def sync_job(
    owner: UUID,
    connection: UUID,
    started: datetime,
    *,
    state: str = "completed",
    kind: str = "ecg_fetch",
    stream: str = "ecg",
) -> None:
    """One synthetic ``ecg`` sync job that started at ``started``."""
    run(
        "INSERT INTO jobs (owner_id, kind, state, input, idempotency_key, request_hash, "
        "started_at, finished_at, error_category) VALUES (:o, :k, :s, CAST(:i AS jsonb), :key, "
        ":h, :at, :end, :err)",
        o=owner,
        k=kind,
        s=state,
        i=json.dumps({"connection_id": str(connection), "stream": stream}),
        key=uuid4().hex,
        h=digest("synthetic-request"),
        at=started,
        end=started + timedelta(minutes=1),
        err="internal" if state == "failed" else None,
    )


def plant_recording(
    owner: UUID, connection: UUID, signalid: int, *, minutes: int, result: str | None
) -> UUID:
    """``test_views_findings.plant_recording``, with ``estimated`` NULL for ``result=None`` (a
    recording stored before its recompute ran)."""
    recording = plant_with_estimate(
        owner, connection, signalid, minutes=minutes, result=result or "pass"
    )
    if result is None:
        run("UPDATE ecg_recordings SET estimated = NULL WHERE id = :id", id=recording)
    return recording


def set_estimate(recording: UUID, signalid: int, result: str | None, **changes: Any) -> None:
    record = estimate(signalid, result, **changes)
    run(
        "UPDATE ecg_recordings SET estimated = CAST(:e AS jsonb) WHERE id = :id",
        id=recording,
        e=json.dumps(record),
    )


def state_of(finding: UUID) -> tuple[str, datetime | None] | None:
    with _conn() as engine, engine.begin() as conn:
        row = conn.execute(
            text("SELECT notify_state, notified_at FROM findings WHERE id = :id"), {"id": finding}
        ).first()
    return None if row is None else (str(row.notify_state), row.notified_at)


def hold_of(finding: UUID) -> Any:
    with _conn() as engine, engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT started_at, revision_at, ended_at, reason, outcome FROM notify_holds "
                "WHERE finding_id = :id"
            ),
            {"id": finding},
        ).first()


def mails(owner: UUID) -> list[tuple[str, str, str]]:
    with _conn() as engine, engine.begin() as conn:
        rows = conn.execute(
            text("SELECT kind, route, state FROM mail_outbox WHERE owner_id = :o"), {"o": owner}
        ).all()
    return [(str(r.kind), str(r.route), str(r.state)) for r in rows]


def email_on(owner: UUID, tiers: list[str] | None = None) -> None:
    run(
        "INSERT INTO user_profiles (owner_id, notify_email, notify_tiers) VALUES (:o, true, "
        "CAST(:t AS text[])) ON CONFLICT (owner_id) DO UPDATE SET notify_email = true, "
        "notify_tiers = EXCLUDED.notify_tiers",
        o=owner,
        t=tiers or ["device", "trend", "experimental", "system"],
    )


async def step(
    env: Env, ctx: UserContext, now: datetime, bound: timedelta = BOUND
) -> NotifyOutcome:
    @asynccontextmanager
    async def open_tx() -> AsyncIterator[AsyncSession]:
        async with user_session(env.factory, ctx) as db:
            yield db

    return await notify_owner(open_tx, ctx.user_id, settings=env.settings, bound=bound, now=now)


def ended(finding: UUID) -> tuple[str, str] | None:
    hold = hold_of(finding)
    if hold is None or hold.ended_at is None:
        return None
    return str(hold.reason), str(hold.outcome)


def e6_waiting(owner: UUID, connection: UUID, grpid: int = 91) -> UUID:
    """An E6 whose trigger group is live and has no recording yet, on a supported stream."""
    plant_group(owner, connection, grpid, minutes=0)
    cursor(owner, connection)
    return pending(owner, "E6", [grpid])


# --- the hold, the gate result and the bound ---------------------------------------------------


async def test_an_unlinked_trigger_holds_until_the_bound_then_sends_not_evaluated(
    plain: Env,
) -> None:
    ctx, connection = user()
    finding = e6_waiting(ctx.user_id, connection)
    await step(plain, ctx, T0)
    assert state_of(finding) == ("held", None)
    hold = hold_of(finding)
    assert (hold.started_at, hold.revision_at, hold.ended_at) == (T0, T0, None)
    await step(plain, ctx, T0 + BOUND - timedelta(seconds=1))
    assert state_of(finding) == ("held", None)
    await step(plain, ctx, T0 + BOUND)
    assert state_of(finding) == ("sent", T0 + BOUND)
    assert ended(finding) == ("timeout", "sent_not_evaluated")


async def test_the_bound_is_changed_through_its_setting(plain: Env) -> None:
    ctx, connection = user()
    finding = e6_waiting(ctx.user_id, connection)
    bound = NotifySettings(notify_hold_hours=2).hold_bound
    await step(plain, ctx, T0, bound)
    await step(plain, ctx, T0 + bound - timedelta(seconds=1), bound)
    assert state_of(finding) == ("held", None)
    await step(plain, ctx, T0 + bound, bound)
    assert ended(finding) == ("timeout", "sent_not_evaluated")


async def test_a_pass_sends_it(plain: Env) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 91, minutes=0)
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="pass")
    finding = pending(ctx.user_id, "E6", [91])
    await step(plain, ctx, T0)
    assert state_of(finding) == ("sent", T0)
    assert ended(finding) == ("linked", "sent")


async def test_a_failure_never_sends_even_after_a_later_recompute_passes(plain: Env) -> None:
    ctx, connection = user()
    email_on(ctx.user_id)
    plant_group(ctx.user_id, connection, 91, minutes=0)
    recording = plant_recording(ctx.user_id, connection, 1, minutes=0, result="fail")
    finding = pending(ctx.user_id, "E6", [91])
    await step(plain, ctx, T0)
    assert state_of(finding) == ("none", None)
    assert ended(finding) == ("linked", "hidden")
    set_estimate(recording, 1, "pass")
    await step(plain, ctx, T0 + timedelta(hours=1))
    await step(plain, ctx, T0 + BOUND)
    assert state_of(finding) == ("none", None)
    assert mails(ctx.user_id) == []


async def test_a_no_result_estimated_sends_it_marked_not_evaluated(plain: Env) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 91, minutes=0)
    recording = plant_recording(ctx.user_id, connection, 1, minutes=0, result=None)
    finding = pending(ctx.user_id, "E6", [91])
    await step(plain, ctx, T0)  # linked, but no current estimated yet: held
    assert state_of(finding) == ("held", None)
    set_estimate(recording, 1, "not_run", sqi={"result": "not_run", "reasons": ["signal_missing"]})
    await step(plain, ctx, T0 + timedelta(minutes=5))
    assert state_of(finding) == ("sent", T0 + timedelta(minutes=5))
    assert ended(finding) == ("linked", "sent_not_evaluated")


async def test_a_stale_estimated_holds_like_a_missing_one(plain: Env) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 91, minutes=0)
    recording = plant_recording(ctx.user_id, connection, 1, minutes=0, result=None)
    set_estimate(recording, 1, "pass", input_fingerprint="synthetic-other-fingerprint")
    finding = pending(ctx.user_id, "E6", [91])
    await step(plain, ctx, T0)
    assert state_of(finding) == ("held", None)
    set_estimate(recording, 1, "pass")
    await step(plain, ctx, T0 + timedelta(minutes=1))
    assert ended(finding) == ("linked", "sent")


async def test_a_link_that_appears_during_the_hold_counts(plain: Env) -> None:
    ctx, connection = user()
    finding = e6_waiting(ctx.user_id, connection)
    await step(plain, ctx, T0)
    assert state_of(finding) == ("held", None)
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="fail")
    await step(plain, ctx, T0 + timedelta(hours=3))
    assert state_of(finding) == ("none", None)
    assert ended(finding) == ("linked", "hidden")


async def test_a_new_revision_restarts_the_hold_and_the_bound_counts_from_the_first(
    plain: Env,
) -> None:
    """A sync that started after the first revision but before the second ends the hold of the
    first (control) and not of the second; the second still ends at the first's bound."""
    ctx, connection = user()
    control = e6_waiting(ctx.user_id, connection, 91)
    finding = pending(ctx.user_id, "E6", [91])
    await step(plain, ctx, T0)
    plant_group(ctx.user_id, connection, 92, minutes=5)
    run(
        "UPDATE findings SET last_seen = :t, evidence = CAST(:e AS jsonb) WHERE id = :id",
        id=finding,
        t=T0 + timedelta(days=1),
        e=json.dumps({"trigger_groups": [91, 92], "synthetic": True}),
    )
    revision = T0 + timedelta(hours=1)
    await step(plain, ctx, revision)  # the new revision is seen: it restarts
    assert hold_of(finding).revision_at == revision
    assert hold_of(finding).started_at == T0
    sync_job(ctx.user_id, connection, T0 + timedelta(minutes=30))
    cursor(ctx.user_id, connection, ok=revision + timedelta(minutes=1))
    await step(plain, ctx, revision + timedelta(minutes=2))
    assert ended(control) == ("ecg_sync_no_match", "sent_not_evaluated")
    assert state_of(finding) == ("held", None)
    await step(plain, ctx, T0 + BOUND - timedelta(seconds=1))
    assert state_of(finding) == ("held", None)
    await step(plain, ctx, T0 + BOUND)
    assert ended(finding) == ("timeout", "sent_not_evaluated")


async def test_a_finding_withdrawn_during_the_hold_sends_nothing(plain: Env) -> None:
    ctx, connection = user()
    email_on(ctx.user_id)
    finding = e6_waiting(ctx.user_id, connection)
    await step(plain, ctx, T0)
    run("DELETE FROM findings WHERE id = :id", id=finding)
    await step(plain, ctx, T0 + timedelta(hours=1))
    assert ended(finding) == ("withdrawn", "none")
    await step(plain, ctx, T0 + BOUND)
    assert mails(ctx.user_id) == []


@pytest.mark.parametrize("rule", ["E1", "E2", "E3"])
async def test_e1_to_e3_are_sent_at_once_and_never_held(plain: Env, rule: str) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 11, minutes=0, types=(130,))
    cursor(ctx.user_id, connection)
    finding = pending(ctx.user_id, rule, [11], metric_keys=())  # unlinked: E4 would wait
    await step(plain, ctx, T0)
    assert state_of(finding) == ("sent", T0)
    assert hold_of(finding) is None


async def test_a_later_failure_recalls_nothing(owner_env: Env) -> None:
    ctx, connection = user()
    email_on(ctx.user_id)
    plant_group(ctx.user_id, connection, 91, minutes=0)
    recording = plant_recording(ctx.user_id, connection, 1, minutes=0, result="pass")
    finding = pending(ctx.user_id, "E4", [91])
    await step(owner_env, ctx, T0)
    assert state_of(finding) == ("sent", T0)
    set_estimate(recording, 1, "fail")
    await step(owner_env, ctx, T0 + timedelta(hours=1))
    assert state_of(finding) == ("sent", T0)
    assert ended(finding) == ("linked", "sent")
    assert mails(ctx.user_id) == [("findings_waiting", "findings", "pending")]


# --- only the trigger holds, and no link possible ----------------------------------------------


async def test_a_baseline_recording_without_an_estimated_never_holds_it(plain: Env) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 91, minutes=0)
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="pass")
    plant_group(ctx.user_id, connection, 92, minutes=60)  # a baseline group, not a trigger
    plant_recording(ctx.user_id, connection, 2, minutes=60, result=None)
    finding = pending(ctx.user_id, "E6", [91])
    await step(plain, ctx, T0)
    assert ended(finding) == ("linked", "sent")


@pytest.mark.parametrize("capability", ["unsupported", "not_authorized", "not_available"])
async def test_no_linkable_recording_ends_it_at_once_for_each_capability(
    plain: Env, capability: str
) -> None:
    ctx, connection = user()
    finding = e6_waiting(ctx.user_id, connection)
    cursor(ctx.user_id, connection, capability)
    await step(plain, ctx, T0)
    assert state_of(finding) == ("sent", T0)
    assert ended(finding) == ("capability_unsupported", "sent_not_evaluated")


async def test_a_sync_that_started_after_and_completed_without_a_match_ends_it(
    plain: Env,
) -> None:
    ctx, connection = user()
    finding = e6_waiting(ctx.user_id, connection)
    await step(plain, ctx, T0)
    sync_job(ctx.user_id, connection, T0 + timedelta(minutes=10))
    cursor(ctx.user_id, connection, ok=T0 + timedelta(minutes=11))
    await step(plain, ctx, T0 + timedelta(minutes=12))
    assert state_of(finding) == ("sent", T0 + timedelta(minutes=12))
    assert ended(finding) == ("ecg_sync_no_match", "sent_not_evaluated")


@pytest.mark.parametrize(
    ("started", "state", "kind", "cursor_state", "ok"),
    [
        (-10, "completed", "ecg_fetch", "complete", 11),  # started before the revision
        (10, "failed", "ecg_fetch", "complete", 11),  # failed
        (10, "completed", "ecg_fetch", "running", 11),  # completed only partly
        (10, "completed", "ecg_fetch", "complete", None),  # no success stored
        (10, "completed", "withings_sync", "complete", 11),  # the measures stream's sync
    ],
)
async def test_a_sync_that_does_not_count_keeps_it_held(
    plain: Env, started: int, state: str, kind: str, cursor_state: str, ok: int | None
) -> None:
    ctx, connection = user()
    finding = e6_waiting(ctx.user_id, connection)
    await step(plain, ctx, T0)
    stream = "measures" if kind == "withings_sync" else "ecg"
    sync_job(
        ctx.user_id,
        connection,
        T0 + timedelta(minutes=started),
        state=state,
        kind=kind,
        stream=stream,
    )
    cursor(
        ctx.user_id,
        connection,
        status=cursor_state,
        ok=None if ok is None else T0 + timedelta(minutes=ok),
    )
    await step(plain, ctx, T0 + timedelta(minutes=12))
    assert state_of(finding) == ("held", None)


@pytest.mark.parametrize("kind", ["withings_sync", "withings_backfill"])
async def test_an_ecg_stream_sync_or_backfill_counts(plain: Env, kind: str) -> None:
    ctx, connection = user()
    finding = e6_waiting(ctx.user_id, connection)
    await step(plain, ctx, T0)
    sync_job(ctx.user_id, connection, T0 + timedelta(minutes=10), kind=kind)
    cursor(ctx.user_id, connection, ok=T0 + timedelta(minutes=11))
    await step(plain, ctx, T0 + timedelta(minutes=12))
    assert ended(finding) == ("ecg_sync_no_match", "sent_not_evaluated")


async def test_signal_missing_ends_it_when_no_other_trigger_group_can_be_linked(
    plain: Env,
) -> None:
    ctx, connection = user()
    cursor(ctx.user_id, connection)
    plant_group(ctx.user_id, connection, 91, minutes=0)
    recording = plant_recording(ctx.user_id, connection, 1, minutes=0, result=None)
    plant_group(ctx.user_id, connection, 92, minutes=30)  # can still be linked
    alone = pending(ctx.user_id, "E6", [91])
    with_other = pending(ctx.user_id, "E6", [91, 92])
    await step(plain, ctx, T0)
    set_estimate(recording, 1, "not_run", sqi={"result": "not_run", "reasons": ["signal_missing"]})
    await step(plain, ctx, T0 + timedelta(minutes=1))
    assert ended(alone) == ("linked", "sent_not_evaluated")
    assert state_of(with_other) == ("held", None)


async def test_no_trigger_group_ends_it_at_once(plain: Env) -> None:
    ctx, _ = user()
    none_at_all = pending(ctx.user_id, "E6", [])
    gone = pending(ctx.user_id, "E6", [404])  # no live measure group has the grpid
    await step(plain, ctx, T0)
    assert ended(none_at_all) == ("no_trigger", "sent_not_evaluated")
    assert ended(gone) == ("no_trigger", "sent_not_evaluated")


# --- every end writes one reason code ----------------------------------------------------------


async def test_every_reason_and_outcome_is_the_migrations(plain: Env) -> None:
    """The check constraints of 0020 admit exactly ``REASONS`` and ``HOLD_OUTCOMES``."""
    ctx, _ = user()
    for reason in REASONS:
        for outcome in HOLD_OUTCOMES:
            run(
                "INSERT INTO notify_holds (owner_id, finding_id, rule_id, started_at, "
                "revision_at, revision_seen, ended_at, reason, outcome) VALUES (:o, :f, 'E6', "
                ":t, :t, :t, :t, :r, :x)",
                o=ctx.user_id,
                f=uuid4(),
                t=T0,
                r=reason,
                x=outcome,
            )
    for reason, outcome in (("synthetic_reason", "sent"), ("timeout", "synthetic_outcome")):
        with pytest.raises(Exception, match="notify_holds_"):
            run(
                "INSERT INTO notify_holds (owner_id, finding_id, rule_id, started_at, "
                "revision_at, revision_seen, ended_at, reason, outcome) VALUES (:o, :f, 'E6', "
                ":t, :t, :t, :t, :r, :x)",
                o=ctx.user_id,
                f=uuid4(),
                t=T0,
                r=reason,
                x=outcome,
            )
    with pytest.raises(Exception, match="notify_holds_end"):  # a reason without an end
        run(
            "INSERT INTO notify_holds (owner_id, finding_id, rule_id, started_at, revision_at, "
            "revision_seen, reason) VALUES (:o, :f, 'E6', :t, :t, :t, 'timeout')",
            o=ctx.user_id,
            f=uuid4(),
            t=T0,
        )


async def test_an_end_is_written_once_and_kept(plain: Env) -> None:
    ctx, connection = user()
    finding = e6_waiting(ctx.user_id, connection)
    await step(plain, ctx, T0)
    await step(plain, ctx, T0 + BOUND)
    first = hold_of(finding)
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="fail")  # a late link
    await step(plain, ctx, T0 + BOUND + timedelta(hours=1))
    assert hold_of(finding) == first
    assert (first.reason, first.outcome) == ("timeout", "sent_not_evaluated")
