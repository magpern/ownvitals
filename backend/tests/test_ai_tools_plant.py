"""Shared planting and fixtures of the AI tool tests (``test_ai_tools_*.py``; ADR 0014 section 4;
#206). No tests here.

Every account, reading, finding and score is synthetic: invented weights and pulses ingested as
measure groups through the real normaliser and daily pass (``viewtools.ingest``), labelled
findings, and score rows with fixed values. Planted "secrets" are labelled filler strings that a
tool must never return.
"""

import json
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
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
from test_views_findings import plant_finding, plant_group, plant_recording
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

# Every argument set of every tool: the closed sets, written out.
PERIODS = (30, 90, 180, 365)
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
    return Planted(make_context(owner), email, connection, findings, withings_user(grpid))


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
        *(str(f) for f in planted.findings.values()),
        *extra,
    }


@pytest.fixture(name="ai_env")
async def ai_env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    """The app with ``ai_insights`` on (environment; no stored flag) and the switch off."""
    run("DELETE FROM app_settings WHERE flag IN ('ai_insights', 'experimental_findings')")
    async with running_env(app_database_url, ai_insights=True) as env:
        yield env
