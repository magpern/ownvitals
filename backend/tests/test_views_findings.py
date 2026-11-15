"""The one findings read function (``ownvitals.views.findings_read``; ADR 0006 sections 9 and 11,
ADR 0010, D9) on a real database, as the app role under forced RLS. Synthetic users, findings,
groups and recordings only; no real account or measurement.

The API on top of it is ``test_views_findings_api.py``; the pure overlay is
``tests/ecg/test_overlay*.py``. The planting helpers here are shared with the API tests.
"""

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    running_env,
)
from ctx import make_context
from jobtools import admin_url, engine_for, ensure_user
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.ecg.pipeline import MODULES, PIPELINE_VERSION
from ownvitals.ecg.validate_cli import input_fingerprint
from ownvitals.views import findings_read
from ownvitals.views.findings_read import (
    CONSUMERS,
    FindingsQuery,
    read_ecg_estimates,
    read_finding,
    read_findings,
)

SYNTHETIC = True
DEVICE = "synthetic_device_a"
BASE = datetime(2026, 9, 27, 7, 0, tzinfo=UTC)
RATE = 300
HASH = b"\x5a" * 32  # a labelled synthetic fingerprint
NOT_EXPORT = tuple(c for c in CONSUMERS if c != "export")


# --- planting (as the superuser) ---------------------------------------------------------------


def _conn() -> Any:
    return engine_for(admin_url())


def plant_user(owner: UUID) -> UUID:
    """The synthetic account of ``owner`` and one connection; returns the connection id."""
    with _conn() as engine, engine.begin() as conn:
        ensure_user(conn, owner)
        return conn.execute(
            text(
                "INSERT INTO connections (owner_id, provider, status) "
                "VALUES (:o, 'test', 'disconnected') RETURNING id"
            ),
            {"o": owner},
        ).scalar_one()


def plant_group(
    owner: UUID,
    connection: UUID,
    grpid: int,
    *,
    minutes: int,
    types: Sequence[int] = (135, 138),
    device: str = DEVICE,
) -> UUID:
    with _conn() as engine, engine.begin() as conn:
        group = conn.execute(
            text(
                "INSERT INTO measure_groups (owner_id, connection_id, grpid, measured_at, "
                "source_tz, hash_deviceid, model_id, payload_hash) VALUES (:o, :c, :g, :at, "
                "'Etc/UTC', :d, 91, :h) RETURNING id"
            ),
            {
                "o": owner,
                "c": connection,
                "g": grpid,
                "at": BASE + timedelta(minutes=minutes),
                "d": device,
                "h": HASH,
            },
        ).scalar_one()
        for kind in types:
            conn.execute(
                text(
                    "INSERT INTO measures (owner_id, measure_group_id, type, value_raw, unit) "
                    "VALUES (:o, :g, :t, 0, 0)"
                ),
                {"o": owner, "g": group, "t": kind},
            )
        return group


def fingerprint(signalid: int) -> str:
    return input_fingerprint(signalid, None, RATE)


def estimate(signalid: int, result: str | None, **changes: Any) -> dict[str, Any] | None:
    """A synthetic current ``estimated`` for the recording ``signalid`` (``None``: no row)."""
    if result is None:
        return None
    record: dict[str, Any] = {
        "algo_version": PIPELINE_VERSION,
        "modules": dict(MODULES),
        "input_fingerprint": fingerprint(signalid),
        "input_revision": 1,
        "lead": 0,
        "sqi": {"result": result, "reasons": [] if result == "pass" else ["low_bsqi"]},
    }
    if result == "pass":
        record |= {"rpeaks": [100.0], "hr_bpm": 61.5, "hrv": {"rmssd_ms": 31.0}, "n_rr": 20}
    record |= changes
    return record


def plant_recording(
    owner: UUID, connection: UUID, signalid: int, *, minutes: int, result: str | None
) -> UUID:
    with _conn() as engine, engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO ecg_recordings (owner_id, connection_id, signalid, recorded_at, "
                "source_tz, model_id, sampling_hz, reported, estimated) VALUES (:o, :c, :s, :at, "
                "'Etc/UTC', 91, :r, CAST(:rep AS jsonb), CAST(:est AS jsonb)) RETURNING id"
            ),
            {
                "o": owner,
                "c": connection,
                "s": signalid,
                "at": BASE + timedelta(minutes=minutes),
                "r": RATE,
                "rep": json.dumps({"deviceid": DEVICE}),
                "est": json.dumps(estimate(signalid, result)),
            },
        ).scalar_one()


_TIER = {"E1": "device", "E2": "device", "E3": "device", "E4": "experimental", "E6": "trend"}


def plant_finding(
    owner: UUID,
    rule: str,
    groups: Sequence[int] = (),
    *,
    tier: str | None = None,
    origin: str = "live",
    subject: str | None = None,
    metric_keys: Sequence[str] = ("qtc_interval",),
) -> UUID:
    tier = tier or _TIER.get(rule, "trend")
    with _conn() as engine, engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO findings (owner_id, rule_id, rule_version, tier, severity, "
                "confidence, origin, metric_keys, subject, window_start, window_end, evidence, "
                "input_revision, input_fingerprint) VALUES (:o, :r, '1', :t, 'attention', :c, "
                ":origin, CAST(:keys AS text[]), :s, :ws, :we, CAST(:e AS jsonb), 1, :h) "
                "RETURNING id"
            ),
            {
                "o": owner,
                "r": rule,
                "t": tier,
                "c": "high" if tier in ("device", "system") else "medium",
                "origin": origin,
                "keys": list(metric_keys),
                "s": subject or f"synthetic_{rule.lower()}_{uuid4().hex[:8]}",
                "ws": date(2026, 9, 20),
                "we": date(2026, 9, 27),
                "e": json.dumps({"trigger_groups": list(groups), "synthetic": True}),
                "h": HASH,
            },
        ).scalar_one()


def run(sql: str, **params: Any) -> None:
    with _conn() as engine, engine.begin() as conn:
        conn.execute(text(sql), params)


def store_flag(value: str | None) -> None:
    with _conn() as engine, engine.begin() as conn:
        _store(conn, value)


def _store(conn: Connection, value: str | None) -> None:
    conn.execute(text("DELETE FROM app_settings WHERE flag = 'experimental_findings'"))
    if value is not None:
        conn.execute(
            text(
                "INSERT INTO app_settings (flag, value) "
                "VALUES ('experimental_findings', CAST(:v AS jsonb))"
            ),
            {"v": json.dumps(value)},
        )


@pytest.fixture(name="plain")
async def plain_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    """The app with the experimental switch off (the default)."""
    store_flag(None)
    async with running_env(app_database_url) as running:
        yield running


@pytest.fixture(name="owner_env")
async def owner_env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    """The app with the environment's experimental switch on and no stored flag."""
    store_flag(None)
    async with running_env(app_database_url, experimental_findings=True) as running:
        yield running


async def views(env: Env, ctx: UserContext, consumer: Any = "dashboard") -> list[Any]:
    found = await read_findings(env.factory, ctx, env.settings, consumer=consumer)
    return list(found.findings)


async def ids(env: Env, ctx: UserContext, consumer: Any = "dashboard", **query: Any) -> set[UUID]:
    found = await read_findings(
        env.factory, ctx, env.settings, consumer=consumer, query=FindingsQuery(**query)
    )
    return {view.id for view in found.findings}


def user() -> tuple[UserContext, UUID]:
    owner = uuid4()
    return make_context(owner), plant_user(owner)


# --- the experimental tier ---------------------------------------------------------------------


@pytest.mark.parametrize("consumer", CONSUMERS)
async def test_experimental_findings_never_reach_a_caller_without_the_switch(
    plain: Env, consumer: Any
) -> None:
    ctx, _ = user()
    e4 = plant_finding(ctx.user_id, "E4")
    b1 = plant_finding(ctx.user_id, "B1", tier="experimental")
    disguised = plant_finding(ctx.user_id, "E4", tier="trend")  # an experimental rule's row
    c1 = plant_finding(ctx.user_id, "C1")
    assert await ids(plain, ctx, consumer) == {c1}
    assert await ids(plain, ctx, consumer, tier="experimental") == set()
    for hidden in (e4, b1, disguised):
        assert await read_finding(plain.factory, ctx, plain.settings, hidden) is None
    store_flag("on")  # a stored "on" never turns on what the environment has off
    assert await ids(plain, ctx, consumer) == {c1}


async def test_the_switch_shows_experimental_findings_and_a_stored_off_wins(
    owner_env: Env,
) -> None:
    ctx, _ = user()
    e4 = plant_finding(ctx.user_id, "E4")
    c1 = plant_finding(ctx.user_id, "C1")
    assert await ids(owner_env, ctx) == {e4, c1}
    assert await ids(owner_env, ctx, tier="experimental") == {e4}
    store_flag("off")
    assert await ids(owner_env, ctx) == {c1}


# --- the overlay at read time ------------------------------------------------------------------


@pytest.mark.parametrize("rule", ["E1", "E2", "E3"])
async def test_a_device_finding_is_never_hidden_and_its_gate_needs_the_switch(
    plain: Env, rule: str
) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 11, minutes=0, types=(130,))
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="fail")
    finding = plant_finding(ctx.user_id, rule, [11], metric_keys=())
    for consumer in CONSUMERS:
        (view,) = await views(plain, ctx, consumer)
        assert (view.id, view.hidden, view.not_evaluated) == (finding, False, False)
        assert view.gate is None


@pytest.mark.parametrize("rule", ["E1", "E2", "E3"])
async def test_a_device_finding_never_carries_the_not_evaluated_mark(
    plain: Env, owner_env: Env, rule: str
) -> None:
    """No gate result at all (no linked recording): E1 to E3 are shown with no mark, with or
    without the switch; with it the gate says why (section 11)."""
    ctx, _ = user()
    finding = plant_finding(ctx.user_id, rule, [404], metric_keys=())
    for env in (plain, owner_env):
        for consumer in CONSUMERS:
            (view,) = await views(env, ctx, consumer)
            assert (view.id, view.hidden, view.not_evaluated) == (finding, False, False)
    (view,) = await views(owner_env, ctx)
    assert view.gate is not None
    assert view.gate.outcome == "not_evaluated"


async def test_pages_follow_last_seen_and_end(plain: Env) -> None:
    ctx, _ = user()
    planted = [plant_finding(ctx.user_id, "C1") for _ in range(5)]
    for n, finding in enumerate(planted):
        run(
            "UPDATE findings SET first_seen = :t, last_seen = :t WHERE id = :i",
            t=BASE - timedelta(days=n),
            i=finding,
        )
    seen: list[UUID] = []
    offset: int | None = 0
    while offset is not None:
        page = await read_findings(
            plain.factory,
            ctx,
            plain.settings,
            consumer="dashboard",
            query=FindingsQuery(limit=2, offset=offset),
        )
        assert len(page.findings) <= 2
        seen += [view.id for view in page.findings]
        offset = page.next_offset
    assert seen == planted  # newest first, each once, and the last page ends it
    out_of_range: list[dict[str, Any]] = [{"limit": 0}, {"limit": 201}, {"offset": -1}]
    for bad in out_of_range:
        with pytest.raises(ValueError, match="out of range"):
            FindingsQuery(**bad)


async def test_a_page_never_holds_an_experimental_or_other_area_row(plain: Env) -> None:
    """The experimental and area filters run in SQL, before the page is cut."""
    ctx, _ = user()
    c1 = plant_finding(ctx.user_id, "C1")
    for _ in range(3):
        plant_finding(ctx.user_id, "E4", tier="trend")  # an experimental rule's row
        plant_finding(ctx.user_id, "N3", metric_keys=("sudomotor_feet",))
    run("UPDATE findings SET last_seen = first_seen + interval '1 day' WHERE id <> :i", i=c1)
    page = await read_findings(
        plain.factory,
        ctx,
        plain.settings,
        consumer="dashboard",
        query=FindingsQuery(area="heart", limit=1),
    )
    assert ([v.id for v in page.findings], page.next_offset) == ([c1], None)


async def test_with_the_switch_an_e1_shows_its_gate_outcome(owner_env: Env) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 11, minutes=0, types=(130,))
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="fail")
    plant_finding(ctx.user_id, "E1", [11], metric_keys=())
    (view,) = await views(owner_env, ctx)
    assert view.hidden is False
    assert view.gate is not None
    assert (view.gate.outcome, view.gate.counted, view.gate.failed) == ("failed", 1, 1)


@pytest.mark.parametrize("consumer", NOT_EXPORT)
async def test_a_failed_e6_is_left_out_except_from_the_export(plain: Env, consumer: Any) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 21, minutes=0)
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="fail")
    e6 = plant_finding(ctx.user_id, "E6", [21])
    assert await ids(plain, ctx, consumer) == set()
    assert await read_finding(plain.factory, ctx, plain.settings, e6, consumer=consumer) is None
    export = await read_findings(plain.factory, ctx, plain.settings, consumer="export")
    (view,) = export.findings
    assert (view.id, view.hidden) == (e6, True)


async def test_the_link_is_resolved_at_every_read_with_no_write_to_the_finding(
    plain: Env,
) -> None:
    ctx, connection = user()
    group = plant_group(ctx.user_id, connection, 31, minutes=5)
    e6 = plant_finding(ctx.user_id, "E6", [31])

    def stored() -> Any:
        with _conn() as engine, engine.connect() as conn:
            return conn.execute(
                text("SELECT xmin::text, evidence, last_seen FROM findings WHERE id = :i"),
                {"i": e6},
            ).one()

    before = stored()

    async def state() -> tuple[bool, bool] | None:
        found = await read_findings(plain.factory, ctx, plain.settings, consumer="export")
        return next(((v.hidden, v.not_evaluated) for v in found.findings if v.id == e6), None)

    assert await state() == (False, True)  # no recording yet: not evaluated
    recording = plant_recording(ctx.user_id, connection, 7, minutes=5, result="fail")
    assert await state() == (True, False)  # linked later, failed: hidden
    run(
        "UPDATE ecg_recordings SET estimated = CAST(:e AS jsonb) WHERE id = :i",
        e=json.dumps(estimate(7, "pass")),
        i=recording,
    )
    assert await state() == (False, False)  # recomputed, passed
    stale = estimate(7, "fail", modules={**MODULES, "sqi": "999.0.0"})
    run(
        "UPDATE ecg_recordings SET estimated = CAST(:e AS jsonb) WHERE id = :i",
        e=json.dumps(stale),
        i=recording,
    )
    assert await state() == (False, True)  # another gate version: no result
    run(
        "UPDATE ecg_recordings SET estimated = CAST(:e AS jsonb) WHERE id = :i",
        e=json.dumps(estimate(7, "fail")),
        i=recording,
    )
    run("UPDATE measure_groups SET deleted_at = now() WHERE id = :i", i=group)
    assert await state() == (False, True)  # the trigger group was deleted: no link
    run("UPDATE measure_groups SET deleted_at = NULL WHERE id = :i", i=group)
    assert await state() == (True, False)
    run("DELETE FROM ecg_recordings WHERE id = :i", i=recording)
    assert await state() == (False, True)  # a deleted recording is no link
    assert stored() == before


async def test_only_the_trigger_groups_are_linked(plain: Env) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 41, minutes=0)
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="pass")
    for n in range(2, 6):  # baseline strips that failed
        plant_group(ctx.user_id, connection, 40 + n, minutes=n)
        plant_recording(ctx.user_id, connection, n, minutes=n, result="fail")
    e6 = plant_finding(ctx.user_id, "E6", [41])
    assert await ids(plain, ctx) == {e6}


async def test_another_devices_strip_is_no_link(plain: Env) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 51, minutes=0, device="synthetic_device_b")
    plant_recording(ctx.user_id, connection, 1, minutes=0, result="fail")
    e4 = plant_finding(ctx.user_id, "E6", [51])
    (view,) = await views(plain, ctx)
    assert (view.id, view.not_evaluated) == (e4, True)


# --- filters, detail, estimates ----------------------------------------------------------------


async def test_tab_tier_and_area_filters(plain: Env) -> None:
    ctx, _ = user()
    live = plant_finding(ctx.user_id, "C1")
    history = plant_finding(ctx.user_id, "B2", origin="backfill", metric_keys=("weight",))
    nerve = plant_finding(ctx.user_id, "N3", metric_keys=("sudomotor_feet",))
    assert await ids(plain, ctx, tab="live") == {live, nerve}
    assert await ids(plain, ctx, tab="history") == {history}
    assert await ids(plain, ctx, area="nerve") == {nerve}
    assert await ids(plain, ctx, area="body") == {history}
    assert await ids(plain, ctx, tier="device") == set()
    bad_queries: list[dict[str, Any]] = [{"tab": "x"}, {"tier": "x"}, {"area": "x"}]
    for bad in bad_queries:
        with pytest.raises(ValueError, match="unknown"):
            FindingsQuery(**bad)
    with pytest.raises(ValueError, match="unknown consumer"):
        await read_findings(plain.factory, ctx, plain.settings, consumer="nobody")  # pyright: ignore[reportArgumentType]


async def test_the_detail_carries_the_trigger_readings_only(plain: Env) -> None:
    ctx, _ = user()
    run(
        "CREATE TABLE IF NOT EXISTS public.observations_y2026m09 PARTITION OF "
        "public.observations FOR VALUES FROM ('2026-09-01') TO ('2026-10-01')"
    )
    for grpid, key in ((61, "qtc_interval"), (62, "qtc_interval"), (61, "weight")):
        run(
            "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
            "source_value_raw, source_unit, conversion, source_grpid, source_type, "
            "algo_version, input_revision, input_fingerprint) VALUES (:o, :k, :ts, :ts, "
            "'Etc/UTC', 455, 455, 0, 'synthetic', :g, 138, 'synthetic/1', 0, :h)",
            o=ctx.user_id,
            k=key,
            ts=BASE + timedelta(minutes=grpid),
            g=grpid,
            h=HASH,
        )
    e6 = plant_finding(ctx.user_id, "E6", [61])
    detail = await read_finding(plain.factory, ctx, plain.settings, e6)
    assert detail is not None
    assert [(r.metric_key, r.grpid) for r in detail.readings] == [("qtc_interval", 61)]
    assert detail.estimates == ()  # no switch: no estimate
    assert repr(detail) == "FindingDetail redacted"


async def test_estimates_need_the_switch_and_are_up_to_date(plain: Env, owner_env: Env) -> None:
    ctx, connection = user()
    plant_group(ctx.user_id, connection, 71, minutes=0)
    recording = plant_recording(ctx.user_id, connection, 9, minutes=0, result="pass")
    e6 = plant_finding(ctx.user_id, "E6", [71])
    assert await read_ecg_estimates(plain.factory, ctx, plain.settings, [recording]) == {}
    detail = await read_finding(owner_env.factory, ctx, owner_env.settings, e6)
    assert detail is not None
    (linked,) = detail.estimates
    assert (linked.recording_id, linked.view.gate, linked.view.hr_bpm) == (recording, "pass", 61.5)
    hrv_bump = estimate(9, "pass", algo_version="ecg-99.0.0", modules={**MODULES, "hrv": "9.0.0"})
    run(
        "UPDATE ecg_recordings SET estimated = CAST(:e AS jsonb) WHERE id = :i",
        e=json.dumps(hrv_bump),
        i=recording,
    )
    views = await read_ecg_estimates(owner_env.factory, ctx, owner_env.settings, [recording])
    assert (views[recording].gate, views[recording].hr_bpm, views[recording].hrv) == (
        "pass",
        None,
        None,
    )
    assert views[recording].rpeaks == (100.0,)


# --- isolation ---------------------------------------------------------------------------------


async def test_another_users_findings_and_recordings_are_never_read(owner_env: Env) -> None:
    alice, connection = user()
    bob, _ = user()
    plant_group(alice.user_id, connection, 81, minutes=0)
    recording = plant_recording(alice.user_id, connection, 1, minutes=0, result="pass")
    finding = plant_finding(alice.user_id, "E6", [81])
    assert await ids(owner_env, bob, "export") == set()
    assert await read_finding(owner_env.factory, bob, owner_env.settings, finding) is None
    found = await read_ecg_estimates(owner_env.factory, bob, owner_env.settings, [recording])
    assert found == {}
    assert await ids(owner_env, alice) == {finding}


@pytest.mark.parametrize(
    "statement", ["_FINDINGS", "_GROUPS", "_LINKABLE_RECORDINGS", "_RECORDINGS_BY_ID", "_READINGS"]
)
async def test_rls_alone_hides_another_owners_rows(owner_env: Env, statement: str) -> None:
    """Each of the module's statements, run in Bob's session with Alice's id as the owner
    filter: RLS still returns nothing, so the owner filter is not the only wall."""
    alice, connection = user()
    bob, _ = user()
    plant_group(alice.user_id, connection, 91, minutes=0)
    recording = plant_recording(alice.user_id, connection, 1, minutes=0, result="pass")
    plant_finding(alice.user_id, "E6", [91])
    params: dict[str, Any] = {
        "owner": alice.user_id,
        "origin": None,
        "tier": None,
        "finding_ids": None,
        "notified": False,
        "experimental": True,
        "experimental_rules": [],
        "letters": None,
        "offset": 0,
        "groups": [91],
        "ids": [recording],
        "keys": ["qtc_interval"],
        "limit": 10,
    }
    sql = getattr(findings_read, statement)
    async with user_session(owner_env.factory, alice) as db:
        mine = (await db.execute(sql, params)).all()
    async with user_session(owner_env.factory, bob) as db:
        theirs = (await db.execute(sql, params)).all()
    assert theirs == []
    if statement != "_READINGS":
        assert mine  # the control: the same statement sees the owner's own rows
