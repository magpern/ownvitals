"""The three handlers that can write ECG rows each run `after_ecg` (#307). No database: the
Withings calls and the follow-ups are faked (synthetic ids only)."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from ownvitals.jobs.withings import StreamInput
from ownvitals.withings import sync_kinds


class _Client:
    @classmethod
    @asynccontextmanager
    async def open(cls, app: object, **_options: object) -> AsyncIterator[str]:
        yield "client"


class _Job:
    def __init__(self, parsed: object) -> None:
        self.input = parsed
        self.factory = "factory"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, str]]:
    seen: list[tuple[uuid.UUID, str]] = []

    async def remember(job: object, connection_id: uuid.UUID, origin: str) -> None:
        seen.append((connection_id, origin))

    async def materials(job: object, connection_id: uuid.UUID) -> tuple[str, str]:
        return "account", "app"

    async def done(*args: object, **kwargs: object) -> str:
        return "supported"

    async def no_normalize(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(sync_kinds, "after_ecg", remember)
    monkeypatch.setattr(sync_kinds, "open_materials", materials)
    monkeypatch.setattr(sync_kinds, "WithingsClient", _Client)
    monkeypatch.setattr(sync_kinds, "execute_backfill", done)
    monkeypatch.setattr(sync_kinds, "execute_sync", done)
    monkeypatch.setattr(sync_kinds, "enqueue_normalize", no_normalize)
    return seen


async def test_the_ecg_backfill_and_sync_run_the_follow_ups(
    calls: list[tuple[uuid.UUID, str]],
) -> None:
    connection = uuid.uuid4()
    parsed = StreamInput(connection_id=connection, stream="ecg")
    await sync_kinds.run_backfill(_Job(parsed))  # type: ignore[arg-type]
    await sync_kinds.run_sync_kind(_Job(parsed))  # type: ignore[arg-type]
    assert calls == [(connection, "backfill"), (connection, "live")]


async def test_the_ecg_fetch_runs_the_follow_ups(calls: list[tuple[uuid.UUID, str]]) -> None:
    connection = uuid.uuid4()
    parsed = StreamInput(connection_id=connection, stream="ecg")
    await sync_kinds.run_ecg_fetch(_Job(parsed))  # type: ignore[arg-type]
    assert calls == [(connection, "live")]


async def test_the_other_streams_do_not(calls: list[tuple[uuid.UUID, str]]) -> None:
    parsed = StreamInput(connection_id=uuid.uuid4(), stream="measures")
    await sync_kinds.run_backfill(_Job(parsed))  # type: ignore[arg-type]
    await sync_kinds.run_sync_kind(_Job(parsed))  # type: ignore[arg-type]
    assert calls == []
