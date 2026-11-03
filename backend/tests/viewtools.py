"""Planting for the read API tests (``test_views_*``): synthetic users, connections, cursors and
Withings measure groups (the fake's shape) that go through the real ingest, normalisation and
daily-value passes, so the API reads rows that carry real provenance. Synthetic data only; no real
account or measurement. Every identifier below is invented and labelled.
"""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from authtools import Env, running_env
from ctx import make_context
from flows import Browser, signed_in
from jobtools import admin_url, engine_for
from ownvitals.db import user_session
from ownvitals.derived.daily_db import daily_owner
from ownvitals.derived.normalize_db import normalise_owner
from ownvitals.ingest.rows import upsert_measure_group
from ownvitals.views.deps import view_now
from ownvitals.withings.models import MeasureGroup

SYNTHETIC = True
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
ZONE = "Europe/Stockholm"
DEVICE = "synthetic_device_scale_a"  # a hashed device id as the API would hold it (invented)
WITHINGS_USER = 424_242_424  # an invented Withings user id; must never appear in a response
GRPID_BASE = 987_000_000  # invented group ids; must never appear in a response
TOKEN = b"SYNTHETIC-TOKEN-0"  # sealed-looking bytes of a planted connection (not a real token)


def epoch(day: date, hour: int = 6, minute: int = 30) -> int:
    """06:30 UTC is 07:30 or 08:30 in Stockholm: a morning reading of that local day."""
    return int(datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC).timestamp())


def group(
    grpid: int, when: int, *measures: tuple[int, int, int, int | None], **extra: Any
) -> MeasureGroup:
    """A synthetic measure group; each measure is ``(type, value, unit, position)``."""
    return MeasureGroup.model_validate(
        {
            "grpid": grpid,
            "date": when,
            "category": extra.pop("category", 1),
            "attrib": extra.pop("attrib", 0),
            "timezone": ZONE,
            "hash_deviceid": extra.pop("hash_deviceid", DEVICE),
            "measures": [
                {"type": t, "value": v, "unit": u, **({} if p is None else {"position": p})}
                for t, v, u, p in measures
            ],
            **extra,
        }
    )


def _conn() -> Any:
    return engine_for(admin_url())


def run(sql: str, **params: Any) -> None:
    with _conn() as engine, engine.begin() as conn:
        conn.execute(text(sql), params)


def plant_connection(owner: UUID, status: str = "connected", last_error: str | None = None) -> UUID:
    """The owner's Withings connection (sealed-looking dummy bytes, never a token)."""
    sealed = status == "connected"
    with _conn() as engine, engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO connections (owner_id, provider, status, last_error, public, secret, "
                "secret_nonce, wrapped_dek, dek_nonce, key_id) VALUES (:o, 'withings', :s, :e, "
                "CAST(:p AS jsonb), :sec, :n, :w, :n, :k) RETURNING id"
            ),
            {
                "o": owner,
                "s": status,
                "e": last_error,
                "p": json.dumps({"withings_userid": WITHINGS_USER, "app_id": 1}),
                "sec": TOKEN if sealed else None,
                "n": b"\x01" * 12 if sealed else None,
                "w": b"\x02" * 48 if sealed else None,
                "k": "kek-1" if sealed else None,
            },
        ).scalar_one()


def plant_cursor(
    owner: UUID,
    connection: UUID,
    stream: str,
    *,
    capability: str | None = "supported",
    status: str | None = "complete",
    index: int = 1,
    last_ok: datetime | None = NOW - timedelta(hours=1),
    error: str | None = None,
) -> None:
    state: dict[str, Any] = {} if status is None else {"status": status, "index": index}
    run(
        "INSERT INTO sync_cursors (owner_id, connection_id, stream, capability, backfill_state, "
        "last_ok_at, last_error_category) VALUES (:o, :c, :s, :cap, CAST(:st AS jsonb), :ok, :e) "
        "ON CONFLICT (owner_id, connection_id, stream) DO UPDATE SET capability = :cap, "
        "backfill_state = CAST(:st AS jsonb), last_ok_at = :ok, last_error_category = :e",
        o=owner,
        c=connection,
        s=stream,
        cap=capability,
        st=json.dumps(state),
        ok=last_ok,
        e=error,
    )


def plant_import_job(owner: UUID, connection: UUID, stream: str) -> None:
    """A queued ``withings_backfill`` job for ``stream``: an import in progress."""
    run(
        "INSERT INTO jobs (owner_id, kind, state, input, idempotency_key, request_hash) VALUES "
        "(:o, 'withings_backfill', 'queued', CAST(:i AS jsonb), :k, :h)",
        o=owner,
        i=json.dumps({"connection_id": str(connection), "stream": stream}),
        k=uuid4().hex,
        h=b"\x00" * 32,
    )


def plant_device(owner: UUID, connection: UUID) -> None:
    run(
        "INSERT INTO devices (owner_id, connection_id, hash_deviceid, model_id, type, fw) "
        "VALUES (:o, :c, :d, 5, 'scale', 'synthetic-fw-1')",
        o=owner,
        c=connection,
        d=DEVICE,
    )


def connected_with_streams(owner: UUID, **streams: Any) -> UUID:
    """A live connection with a complete, recent, supported cursor for the measures and ecg
    streams; keyword arguments override a stream's cursor (``measures={"capability": ...}``)."""
    connection = plant_connection(owner)
    for stream in ("measures", "ecg"):
        plant_cursor(owner, connection, stream, **streams.get(stream, {}))
    return connection


async def ingest(
    env: Env,
    owner: UUID,
    connection: UUID,
    weights: Mapping[date, float] | None = None,
    *,
    pulses: Mapping[date, int] | None = None,
    extra: Sequence[MeasureGroup] = (),
    first_grpid: int = GRPID_BASE,
) -> None:
    """Ingest morning weights (kg) and heart pulses (bpm) as measure groups, then run the
    normalisation and the daily pass as the owner. ``extra`` groups are ingested as given."""
    ctx = make_context(owner)
    groups = list(extra)
    grpid = first_grpid
    for day, kg in (weights or {}).items():
        groups.append(group(grpid, epoch(day), (1, round(kg * 1000), -3, None)))
        grpid += 1
    for day, bpm in (pulses or {}).items():
        groups.append(group(grpid, epoch(day), (11, bpm, 0, None)))
        grpid += 1

    @asynccontextmanager
    async def opener() -> AsyncIterator[AsyncSession]:
        async with user_session(env.factory, ctx) as db:
            yield db

    async with user_session(env.factory, ctx) as db:
        for item in groups:
            await upsert_measure_group(db, owner, connection, item, ZONE, NOW)
    await normalise_owner(opener, owner, now=NOW)
    await daily_owner(opener, owner)


def days_back(count: int, *, skip: Sequence[int] = ()) -> list[date]:
    """``count`` days ending yesterday, oldest first, without the offsets in ``skip``."""
    return [TODAY - timedelta(days=i) for i in range(count, 0, -1) if i not in skip]


@pytest.fixture(name="env")
async def env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    async with running_env(app_database_url) as running:
        running.app.dependency_overrides[view_now] = lambda: NOW
        yield running


async def user(env: Env, email: str) -> tuple[Browser, UUID]:
    browser = await signed_in(env, email)
    found = await env.scalar("SELECT id FROM users WHERE email_normalized = :e", e=email)
    assert isinstance(found, UUID)
    return browser, found
