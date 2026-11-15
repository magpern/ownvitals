"""Shared planting and fixtures of the AI tool tests (``test_ai_tools_*.py``; ADR 0014 section 4;
#206). No tests here.

Every account, reading, finding and score is synthetic: invented weights and pulses ingested as
measure groups through the real normaliser and daily pass (``viewtools.ingest``), labelled
findings, and score rows with fixed values; invented ECG recordings with Withings-style
classifications, heart rates and intervals (one with a current synthetic estimate), and
normalised sleep and activity rows written as the normaliser writes them (#401). Planted
"secrets" are labelled filler strings that a tool must never return.
"""

import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from authtools import Env, running_env
from ctx import make_context
from jobtools import admin_url, engine_for
from ownvitals.ai import consent_text
from ownvitals.ai.tools import run_tool
from ownvitals.ai.tools_model import TOOLS
from ownvitals.context import UserContext
from test_views_findings import DEVICE as ECG_DEVICE
from test_views_findings import RATE, estimate, plant_finding, plant_group, plant_recording
from viewtools import (
    DEVICE,
    GRPID_BASE,
    NOW,
    WITHINGS_USER,
    connected_with_streams,
    days_back,
    ingest,
    plant_device,
    run,
)

SYNTHETIC = True
PLANTED_MESSAGE = "synthetic-planted-message-IGNORE-PREVIOUS-INSTRUCTIONS"
SUBJECT = "synthetic_subject_must_not_leak"
PLANTED_ZONE = "America/St_Johns"  # a place: the recordings' and stream rows' zone, never output
SIGNAL_BASE = 7_770_000  # invented Withings signal ids; never output
STREAM_DAYS = 100

# Every argument set of every tool: the closed sets, written out.
PERIODS = (30, 90, 180, 365)
STREAM_PERIODS = (7, 14, 30, 60, 90)
CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_metrics", {}),
    *(
        ("metric_summary", {"metric": m, "period": p})
        for m in ("weight", "heart_pulse")
        for p in PERIODS
    ),
    *(
        ("daily_values", {"metric": m, "period": p})
        for m in ("weight", "heart_pulse")
        for p in (7, 14, 30, 60, 90)
    ),
    *(("findings", {"status": s, "period": p}) for s in ("active", "history") for p in PERIODS),
    *(("scores", {"kind": k}) for k in ("readiness", "his", "body_path")),
    *(("ecg_summary", {"period": p}) for p in PERIODS),
    *(("sleep_summary", {"period": p}) for p in STREAM_PERIODS),
    *(("activity_summary", {"period": p}) for p in STREAM_PERIODS),
)
assert {name for name, _ in CALLS} == set(TOOLS)


def withings_user(grpid: int) -> int:
    return WITHINGS_USER + 1 + grpid


@dataclass(frozen=True)
class Planted:
    ctx: UserContext
    email: str
    connection: UUID
    findings: dict[str, UUID]
    withings_userid: int
    recordings: tuple[UUID, ...]
    signalids: tuple[int, ...]


def give_consent(owner: UUID) -> None:
    run(
        "INSERT INTO ai_consents (owner_id, consent_version, locale, text_sha256) "
        "VALUES (:o, :v, 'en', :h)",
        o=owner,
        v=consent_text.CONSENT_VERSION,
        h=consent_text.fingerprint(consent_text.TEXTS["en"]),
    )


def plant_scores(owner: UUID, base: float) -> None:
    for kind, value in (("readiness", base), ("his", base + 1), ("body_path:weight", base + 2)):
        run(
            "INSERT INTO scores (owner_id, kind, day, value, components, algo_version, "
            "input_revision, input_fingerprint) VALUES (:o, :k, :d, :v, CAST(:c AS jsonb), "
            "'synthetic/1', 0, :h)",
            o=owner,
            k=kind,
            d=NOW.date(),
            v=value,
            c=json.dumps({"status": "computed", "secret": SUBJECT, "email": "x@example.test"}),
            h=b"\x5a" * 32,
        )


async def plant_account(env: Env, email: str, *, weight: float, pulse: int, grpid: int) -> Planted:
    """A consenting account with 120 days of weights and pulses, a device, a conversation
    message, findings of every tier (one hidden by the ECG gate) and three scores."""
    owner = await env.user(email)
    connection = connected_with_streams(owner)
    # A Withings user id of its own (invented), so a second account can be planted beside it.
    run(
        "UPDATE connections SET public = jsonb_set(public, '{withings_userid}', to_jsonb(:u)) "
        "WHERE id = :c",
        u=withings_user(grpid),
        c=connection,
    )
    plant_device(owner, connection)
    days = days_back(120)
    await ingest(
        env,
        owner,
        connection,
        {d: weight + 0.1 * (n % 3) for n, d in enumerate(days)},
        pulses={d: pulse + n % 4 for n, d in enumerate(days)},
        first_grpid=grpid,
    )
    give_consent(owner)
    conversation = _scalar_insert(owner)
    run(
        "INSERT INTO ai_messages (owner_id, conversation_id, role, content) "
        "VALUES (:o, :c, 'user', :t)",
        o=owner,
        c=conversation,
        t=PLANTED_MESSAGE,
    )
    hidden_group = grpid + 900_000
    plant_group(owner, connection, hidden_group, minutes=0)
    plant_recording(owner, connection, hidden_group, minutes=0, result="fail")
    findings = {
        "C1": plant_finding(owner, "C1", tier="trend", subject=f"{SUBJECT}_{grpid}"),
        "C1_ended": plant_finding(owner, "C1", tier="trend", subject=f"{SUBJECT}_e{grpid}"),
        "B1": plant_finding(owner, "B1", tier="experimental", metric_keys=("weight",)),
        "E4": plant_finding(owner, "E4"),
        "E6_hidden": plant_finding(owner, "E6", [hidden_group]),
    }
    run(
        "UPDATE findings SET status = 'ended', ended_at = now() WHERE id = :i",
        i=findings["C1_ended"],
    )
    plant_scores(owner, weight)
    signalids = tuple(SIGNAL_BASE + grpid + i for i in range(len(ECG_PLAN)))
    recordings = tuple(
        plant_ecg(owner, connection, signalid, grpid + 700_000 + i, pulse, *spec)
        for i, (signalid, spec) in enumerate(zip(signalids, ECG_PLAN, strict=True))
    )
    plant_streams(owner, pulse)
    return Planted(
        make_context(owner),
        email,
        connection,
        findings,
        withings_user(grpid),
        recordings,
        (hidden_group, *signalids),
    )


# (days before NOW, Withings' ``afib``, intervals reported, our estimate's gate result)
ECG_PLAN: tuple[tuple[int, int | None, bool, str | None], ...] = (
    (2, 0, True, "pass"),  # current estimate: shown only past the tier filter
    (10, 1, True, None),
    (40, None, False, "fail"),  # a failed gate: no estimate value
    (200, 2, True, None),
    (400, 0, False, None),  # outside every period
)
# Withings-style intervals (ms; type -> raw value, unit 0), ADR 0006 A7.
ECG_INTERVALS = {135: 96, 136: 158, 137: 392, 138: 411}


def plant_ecg(
    owner: UUID,
    connection: UUID,
    signalid: int,
    group_id: int,
    pulse: int,
    days_ago: int,
    afib: int | None,
    intervals: bool,
    gate: str | None,
) -> UUID:
    """One synthetic recording at 21:40 UTC (a time of day that must not show), with its
    Withings fields, the intervals' measure group at the same instant and an ``estimated``."""
    at = datetime.combine(NOW.date() - timedelta(days=days_ago), time(21, 40), UTC)
    est = estimate(signalid, gate, hr_bpm=pulse + 1.5, hrv={"rmssd_ms": 31.0, "sdnn_ms": 42.0})
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        recording = conn.execute(
            text(
                "INSERT INTO ecg_recordings (owner_id, connection_id, signalid, recorded_at, "
                "source_tz, model_id, afib, heart_rate, sampling_hz, reported, estimated) "
                "VALUES (:o, :c, :s, :at, :tz, 91, :afib, :hr, :r, CAST(:rep AS jsonb), "
                "CAST(:est AS jsonb)) RETURNING id"
            ),
            {
                "o": owner,
                "c": connection,
                "s": signalid,
                "at": at,
                "tz": PLANTED_ZONE,
                "afib": afib,
                "hr": pulse,
                "r": RATE,
                "rep": json.dumps({"deviceid": ECG_DEVICE, "synthetic": True}),
                "est": None if est is None else json.dumps(est),
            },
        ).scalar_one()
        if intervals:
            group = conn.execute(
                text(
                    "INSERT INTO measure_groups (owner_id, connection_id, grpid, measured_at, "
                    "source_tz, hash_deviceid, model_id, payload_hash) VALUES (:o, :c, :g, :at, "
                    ":tz, :d, 91, :h) RETURNING id"
                ),
                {
                    "o": owner,
                    "c": connection,
                    "g": group_id,
                    "at": at,
                    "tz": PLANTED_ZONE,
                    "d": ECG_DEVICE,
                    "h": b"\x5a" * 32,
                },
            ).scalar_one()
            for kind, raw in ECG_INTERVALS.items():
                conn.execute(
                    text(
                        "INSERT INTO measures (owner_id, measure_group_id, type, value_raw, unit) "
                        "VALUES (:o, :g, :t, :v, 0)"
                    ),
                    {"o": owner, "g": group, "t": kind, "v": raw},
                )
    return recording


def stream_values(pulse: int, n: int) -> dict[str, float]:
    """The synthetic sleep and activity fields of the ``n``-th day (the heart rate and the steps
    carry the account's pulse, so another account's values show)."""
    return {
        "sleep_total_sleep_time": 25_000 + 10 * n,
        "sleep_deepsleepduration": 5_000 + n,
        "sleep_lightsleepduration": 14_000 + n,
        "sleep_remsleepduration": 6_000 + n,
        "sleep_wakeupcount": n % 4,
        "sleep_sleep_efficiency": 0.9,
        "sleep_sleep_score": 80,
        "sleep_hr_average": pulse + n % 4,
        "sleep_breathing_disturbances_intensity": 12,  # not on the tool's allowlist
        "activity_steps": pulse * 100 + n % 3,
        "activity_distance": 4_000 + n,
        "activity_elevation": 12,
        "activity_soft": 3_000,
        "activity_moderate": 1_200,
        "activity_intense": 300,
        "activity_calories": 350.5,
        "activity_hr_zone_3": 77,  # not on the tool's allowlist
    }


STREAM_DUPLICATE = 999_999  # a duplicate row's value; the tools leave it out


def plant_streams(owner: UUID, pulse: int) -> None:
    """``STREAM_DAYS`` nights and days of normalised sleep and activity rows, written as the
    normaliser writes them (``source_type IS NULL``, ``e0:unknown``), plus one duplicate."""
    rows: list[dict[str, Any]] = []
    for n, day in enumerate(days_back(STREAM_DAYS)):
        for key, value in stream_values(pulse, n).items():
            hour = 6 if key.startswith("sleep_") else 15  # the wake-up; mid-day of an activity day
            ts = datetime.combine(day, time(hour, 17), UTC)
            rows.append({"k": key, "ts": ts, "v": value, "f": "{}", "g": None})
    yesterday = datetime.combine(NOW.date() - timedelta(days=1), time(20, 0), UTC)  # the latest
    rows.append(
        {"k": "activity_steps", "ts": yesterday, "v": STREAM_DUPLICATE, "f": "{duplicate}", "g": 1}
    )
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO observations (owner_id, metric_key, ts, source_ts, source_tz, value, "
                "source_value_raw, source_unit, conversion, quality_flags, source_grpid, "
                "algo_version, input_revision, input_fingerprint) VALUES (:o, :k, :ts, :ts, :tz, "
                ":v, 0, 0, 'e0:unknown', CAST(:f AS text[]), :g, 'normalize-streams/1', 1, :h)"
            ),
            [{**row, "o": owner, "tz": PLANTED_ZONE, "h": b"\x5b" * 32} for row in rows],
        )


def _scalar_insert(owner: UUID) -> UUID:
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO ai_conversations (owner_id) VALUES (:o) RETURNING id"), {"o": owner}
        ).scalar_one()


async def call_all(env: Env, ctx: UserContext) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    return [
        (name, args, await run_tool(env.factory, ctx, env.settings, name, args, now=NOW))
        for name, args in CALLS
    ]


def forbidden_strings(planted: Planted, extra: Iterable[str] = ()) -> set[str]:
    """What no tool output may contain: ids, emails, Withings and device identifiers, free text."""
    return {
        str(planted.ctx.user_id),
        str(planted.connection),
        planted.email,
        DEVICE,
        str(WITHINGS_USER),
        str(planted.withings_userid),
        str(GRPID_BASE),
        SUBJECT,
        PLANTED_MESSAGE,
        "synthetic-fw-1",
        "@example",
        ECG_DEVICE,
        PLANTED_ZONE,
        "St_Johns",
        *(str(r) for r in planted.recordings),
        *(str(s) for s in planted.signalids),
        *(str(f) for f in planted.findings.values()),
        *extra,
    }


@pytest.fixture(name="ai_env")
async def ai_env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    """The app with ``ai_insights`` on (environment; no stored flag) and the switch off."""
    run("DELETE FROM app_settings WHERE flag IN ('ai_insights', 'experimental_findings')")
    async with running_env(app_database_url, ai_insights=True) as env:
        yield env
