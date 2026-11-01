"""The SSE stream and ``GET /api/jobs/{id}/events`` (ADR 0005 section 7): replay and
``Last-Event-ID``, live wake-up, polling, keepalive and limits, the session probe, and that only
the owner's events leave, and only what their models name. Synthetic data only."""

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import pytest
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ctx import make_context
from jobapi import Api, api_fixture, until  # noqa: F401 - fixtures
from jobcore import (
    JobsEnv,
    claim,
    jenv_fixture,  # noqa: F401 - fixtures
)
from jobtools import admin_fixture, new_job, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.db import user_session
from ownvitals.jobs import fenced_session, stream, transitions
from ownvitals.jobs.deps import stream_session_probe
from ownvitals.jobs.events_routes import MAX_STREAMS_PER_USER
from ownvitals.jobs.hub import EventHub
from ownvitals.jobs.issuer import issue
from ownvitals.jobs.progress import read_page

SECRET = "synthetic-secret-value-9c1e"  # planted to prove it never leaves


def add_event(
    admin: Connection,
    owner: UUID,
    job_id: UUID,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    raw: str | None = None,
) -> int:
    """The next event of the job, written as the superuser (any payload, even off-model)."""
    seq = admin.execute(
        text("UPDATE jobs SET next_seq = next_seq + 1 WHERE id = :j RETURNING next_seq"),
        {"j": job_id},
    ).scalar_one()
    admin.execute(
        text(
            "INSERT INTO job_events (owner_id, job_id, seq, kind, payload, lease_version) "
            "VALUES (:o, :j, :s, :k, CAST(:p AS jsonb), 0)"
        ),
        {"o": owner, "j": job_id, "s": seq, "k": kind, "p": raw or json.dumps(payload or {})},
    )
    return seq


def seeded(admin: Connection, owner: UUID) -> UUID:
    """A job of ``owner`` with three events: queued, claimed, progress."""
    job_id = new_job(admin, owner)
    add_event(admin, owner, job_id, "queued")
    add_event(admin, owner, job_id, "claimed", {"attempt": 1})
    add_event(admin, owner, job_id, "progress", {"done": 1, "total": 4})
    return job_id


def finish(admin: Connection, job_id: UUID, state: str = "completed") -> None:
    admin.execute(
        text("UPDATE jobs SET state = :s, finished_at = now() WHERE id = :j"),
        {"s": state, "j": job_id},
    )


async def signed_in() -> bool:
    return True


async def collect(
    api: Api,
    owner: UUID,
    job_id: UUID,
    after: int = 0,
    *,
    hub: EventHub | None = None,
    still_signed_in: Callable[[], Awaitable[bool]] = signed_in,
) -> list[str]:
    state = api.app.state
    return [
        chunk
        async for chunk in stream.event_stream(
            state.session_factory,
            hub or state.event_hub,
            make_context(owner),
            job_id,
            after,
            still_signed_in=still_signed_in,
        )
    ]


def chunks(body: str) -> list[str]:
    return [part + "\n\n" for part in body.split("\n\n") if part]


def parse(frames: list[str]) -> list[tuple[int, str, dict[str, Any]]]:
    out: list[tuple[int, str, dict[str, Any]]] = []
    for item in frames:
        if item.startswith(":"):
            continue
        fields = dict(line.split(": ", 1) for line in item.strip().split("\n"))
        out.append((int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return out


async def test_the_stream_replays_after_the_last_id_and_ends_on_the_terminal_event(
    jenv: JobsEnv, api: Api
) -> None:
    owner = uuid.uuid4()
    job_id = seeded(jenv.admin, owner)
    add_event(jenv.admin, owner, job_id, "completed")
    finish(jenv.admin, job_id)
    full = parse(await collect(api, owner, job_id))
    assert [(s, k) for s, k, _ in full] == [
        (1, "queued"),
        (2, "claimed"),
        (3, "progress"),
        (4, "completed"),
    ]
    assert full[1][2] == {"attempt": 1}
    assert full[2][2] == {"done": 1, "total": 4}
    assert [s for s, _, _ in parse(await collect(api, owner, job_id, after=2))] == [3, 4]
    assert await collect(api, owner, job_id, after=4) == []  # a reconnect after the end
    assert await collect(api, owner, job_id, after=99) == []


async def test_a_long_backlog_is_sent_in_pages_in_order(
    jenv: JobsEnv, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stream, "PAGE_SIZE", 2)
    owner = uuid.uuid4()
    job_id = new_job(jenv.admin, owner)
    for done in range(5):
        add_event(jenv.admin, owner, job_id, "progress", {"done": done, "total": 5})
    add_event(jenv.admin, owner, job_id, "completed")
    finish(jenv.admin, job_id)
    assert [s for s, _, _ in parse(await collect(api, owner, job_id))] == [1, 2, 3, 4, 5, 6]


def count_reads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the stream's reads of the database: a second read means it is now waiting."""
    reads: list[int] = []
    real = stream.read_page

    async def counting(*args: Any, **kwargs: Any) -> Any:
        reads.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(stream, "read_page", counting)
    return reads


async def test_a_live_event_reaches_the_stream_by_notification_not_by_polling(
    jenv: JobsEnv, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, job_id = jenv.job()
    execution = await claim(jenv)
    lease = (await issue(jenv.factory, execution.claimed, jenv.accounts)).lease
    hub: EventHub = api.app.state.event_hub
    await until(lambda: hub.listening)  # so the stream waits for a wake-up, 15 s per round
    reads = count_reads(monkeypatch)
    received: list[str] = []

    async def consume() -> None:
        async for item in stream.event_stream(
            api.app.state.session_factory, hub, make_context(owner), job_id, 0,
            still_signed_in=signed_in,
        ):  # fmt: skip
            received.append(item)

    task = asyncio.ensure_future(consume())
    try:
        await until(lambda: len(received) == 1)  # caught up: the next thing it does is wait
        assert len(reads) == 1
        assert [k for _, k, _ in parse(received)] == ["claimed"]
        async with fenced_session(jenv.factory, lease) as fs:
            await fs.event("progress", {"done": 1, "total": 2})
        await until(lambda: len(received) == 2, 3)  # far sooner than keepalive or polling
        assert [k for _, k, _ in parse(received)] == ["claimed", "progress"]
        async with fenced_session(jenv.factory, lease) as fs:
            await transitions.complete(fs)
        await asyncio.wait_for(task, 5)
        assert [k for _, k, _ in parse(received)][-1] == "completed"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_with_the_listener_down_the_stream_polls(
    jenv: JobsEnv, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stream, "POLL_SECONDS", 0.1)
    hub = EventHub(api.app.state.engine)  # never started: not listening
    assert not hub.listening
    owner = uuid.uuid4()
    job_id = new_job(jenv.admin, owner)
    reads = count_reads(monkeypatch)
    task = asyncio.ensure_future(collect(api, owner, job_id, hub=hub))
    try:
        # Nothing wakes the stream, and keepalive is 15 s away: only polling reads again.
        await until(lambda: len(reads) >= 3, 3)
        add_event(jenv.admin, owner, job_id, "completed")
        finish(jenv.admin, job_id)
        assert [k for _, k, _ in parse(await asyncio.wait_for(task, 5))] == ["completed"]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_keepalive_comments_and_the_stream_time_limit(
    jenv: JobsEnv, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stream, "KEEPALIVE_SECONDS", 0.1)
    monkeypatch.setattr(stream, "MAX_STREAM_SECONDS", 0.6)
    owner = uuid.uuid4()
    job_id = new_job(jenv.admin, owner)  # nothing happens
    frames = await asyncio.wait_for(collect(api, owner, job_id), 10)
    assert frames
    assert set(frames) == {stream.KEEPALIVE_FRAME}


async def test_the_stream_ends_when_the_session_does(
    jenv: JobsEnv, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stream, "KEEPALIVE_SECONDS", 0.1)
    owner = uuid.uuid4()
    job_id = new_job(jenv.admin, owner)

    async def signed_out() -> bool:
        return False

    frames = await asyncio.wait_for(collect(api, owner, job_id, still_signed_in=signed_out), 10)
    assert set(frames) <= {stream.KEEPALIVE_FRAME}  # it ended (at most one comment first)


async def test_without_a_session_cookie_the_probe_ends_the_stream(
    jenv: JobsEnv, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#10's probe (``ownvitals.auth.deps.session_probe``) without a session cookie says no, so
    an open stream ends at its first check, also through the endpoint (only the user override
    is in place there). ``test_auth_session_probe.py`` covers a real session."""
    request = Request({"type": "http", "headers": [], "app": api.app})
    probe_for = stream_session_probe(request)
    assert await probe_for(uuid.uuid4())() is False
    monkeypatch.setattr(stream, "KEEPALIVE_SECONDS", 0.1)
    browser = api.browser()
    job_id = seeded(jenv.admin, browser.user_id)  # not over: the stream would wait for more
    response = await asyncio.wait_for(browser.get(f"/api/jobs/{job_id}/events"), 10)
    assert response.status_code == 200
    assert [k for _, k, _ in parse(chunks(response.text))] == ["queued", "claimed", "progress"]


async def test_a_stream_never_serves_another_users_job(jenv: JobsEnv, api: Api) -> None:
    owner, other = uuid.uuid4(), uuid.uuid4()
    job_id = seeded(jenv.admin, owner)
    assert await collect(api, other, job_id) == []


async def test_a_stored_payload_off_its_model_never_leaves(jenv: JobsEnv, api: Api) -> None:
    owner = uuid.uuid4()
    job_id = new_job(jenv.admin, owner)
    add_event(jenv.admin, owner, job_id, "progress", {"done": 1, "total": 2, "token": SECRET})
    add_event(jenv.admin, owner, job_id, "claimed", {"attempt": 1, "worker": SECRET})
    add_event(jenv.admin, owner, job_id, "failed", {"category": "internal", "detail": SECRET})
    frames = await collect(api, owner, job_id)  # failed is terminal: the stream ends
    assert SECRET not in "".join(frames)
    assert [p for _, _, p in parse(frames)] == [{}, {}, {}]


@pytest.mark.parametrize("stored", ["5", '"text"', "[1]", "true"])
async def test_a_stored_scalar_payload_is_sent_empty_and_the_stream_goes_on(
    jenv: JobsEnv, api: Api, stored: str
) -> None:
    owner = uuid.uuid4()
    job_id = new_job(jenv.admin, owner)
    add_event(jenv.admin, owner, job_id, "progress", raw=stored)
    add_event(jenv.admin, owner, job_id, "cancelled")
    frames = await collect(api, owner, job_id)
    assert [(k, p) for _, k, p in parse(frames)] == [("progress", {}), ("cancelled", {})]


async def test_closing_the_hub_ends_every_open_stream(jenv: JobsEnv, api: Api) -> None:
    """Shutdown (``EventHub.close``) ends a stream that would otherwise wait."""
    owner = uuid.uuid4()
    job_id = seeded(jenv.admin, owner)  # not over: the stream would wait for more
    hub: EventHub = api.app.state.event_hub
    task = asyncio.ensure_future(collect(api, owner, job_id))
    await until(lambda: job_id in hub._subscriptions)  # pyright: ignore[reportPrivateUsage]
    hub.close()
    frames = await asyncio.wait_for(task, 5)
    assert [k for _, k, _ in parse(frames)] == ["queued", "claimed", "progress"]
    assert await asyncio.wait_for(collect(api, owner, job_id, after=3), 5) == []  # ends at once


# --- the endpoint -----------------------------------------------------------------------------


async def test_foreign_unknown_and_malformed_ids_are_the_same_404(jenv: JobsEnv, api: Api) -> None:
    owner = uuid.uuid4()
    job_id = seeded(jenv.admin, owner)
    add_event(jenv.admin, owner, job_id, "completed")
    finish(jenv.admin, job_id)
    other = api.browser()
    bodies = set()
    for raw in (str(job_id), str(uuid.uuid4()), "not-a-uuid", "1"):
        response = await other.get(f"/api/jobs/{raw}/events")
        assert response.status_code == 404
        assert response.headers["content-type"] == "application/json"
        bodies.add(response.text)
    assert len(bodies) == 1
    assert "completed" not in bodies.pop()
    response = await other.client.get(
        f"/api/jobs/{job_id}/events?after=0", headers=other.headers({"Last-Event-ID": "0"})
    )
    assert response.status_code == 404  # not even with a Last-Event-ID or ?after=


async def test_the_endpoint_replays_the_owners_events_and_resumes(jenv: JobsEnv, api: Api) -> None:
    browser = api.browser()
    owner = browser.user_id
    job_id = seeded(jenv.admin, owner)
    add_event(jenv.admin, owner, job_id, "completed")
    finish(jenv.admin, job_id)
    url = f"/api/jobs/{job_id}/events"
    response = await browser.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text.startswith("id: 1\nevent: queued\ndata: {}\n\n")
    assert response.text.endswith("id: 4\nevent: completed\ndata: {}\n\n")
    resumed = await browser.client.get(url, headers=browser.headers({"Last-Event-ID": "3"}))
    assert [s for s, _, _ in parse(chunks(resumed.text))] == [4]
    first = await browser.get(url + "?after=2")
    assert first.text.startswith("id: 3\n")
    both = await browser.client.get(
        url + "?after=0", headers=browser.headers({"Last-Event-ID": "3"})
    )
    assert both.text.startswith("id: 4\n")  # the header wins
    # After the end, a reconnect is a 204: an EventSource stops reconnecting.
    over = await browser.client.get(url, headers=browser.headers({"Last-Event-ID": "4"}))
    assert (over.status_code, over.text) == (204, "")
    assert (await browser.get(url + "?after=99")).status_code == 204


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5", "99999999999999999999", " 1"])
async def test_a_malformed_last_event_id_is_a_400(jenv: JobsEnv, api: Api, bad: str) -> None:
    browser = api.browser()
    job_id = seeded(jenv.admin, browser.user_id)
    response = await browser.client.get(
        f"/api/jobs/{job_id}/events", headers=browser.headers({"Last-Event-ID": bad})
    )
    assert response.status_code == 400
    assert response.json() == {"detail": {"reason": "bad_last_event_id"}}


async def test_at_most_five_open_streams_per_user(jenv: JobsEnv, api: Api) -> None:
    browser = api.browser()
    owner = browser.user_id
    job_id = seeded(jenv.admin, owner)
    add_event(jenv.admin, owner, job_id, "completed")
    finish(jenv.admin, job_id)
    limiter = api.app.state.stream_limiter
    held = [limiter.acquire(owner) for _ in range(MAX_STREAMS_PER_USER)]
    assert all(held)
    url = f"/api/jobs/{job_id}/events"
    response = await browser.get(url)
    assert response.status_code == 429
    assert response.json() == {"detail": {"reason": "too_many_streams"}}
    assert "retry-after" in response.headers
    # a foreign or unknown id is a 404 even at the limit: the limit never confirms an id
    assert (await browser.get(f"/api/jobs/{uuid.uuid4()}/events")).status_code == 404
    other = api.browser()  # another user is not affected
    other_job = seeded(jenv.admin, other.user_id)
    add_event(jenv.admin, other.user_id, other_job, "completed")
    finish(jenv.admin, other_job)
    assert (await other.get(f"/api/jobs/{other_job}/events")).status_code == 200
    limiter.release(owner, held[0])
    assert (await browser.get(url)).status_code == 200
    # a finished stream gave its slot back
    assert limiter.acquire(owner) is not None  # four are still held, so one is free
    assert limiter.acquire(owner) is None


async def test_the_endpoint_never_shows_what_a_payload_model_does_not_name(
    jenv: JobsEnv, api: Api
) -> None:
    browser = api.browser()
    owner = browser.user_id
    job_id = new_job(jenv.admin, owner)
    add_event(jenv.admin, owner, job_id, "progress", {"done": 1, "total": 1, "token": SECRET})
    add_event(jenv.admin, owner, job_id, "completed", {"x": SECRET})
    finish(jenv.admin, job_id)
    response = await browser.get(f"/api/jobs/{job_id}/events")
    assert response.status_code == 200
    assert SECRET not in response.text
    assert [p for _, _, p in parse(chunks(response.text))] == [{}, {}]  # off-model: sent empty


async def test_rls_alone_hides_another_owners_job_and_events(jenv: JobsEnv) -> None:
    """The reads of ``progress`` without their ``owner_id`` filter, in the other owner's user
    session, still see nothing: RLS refuses on its own (and the same statements see the rows in
    the owner's session, so the query itself works)."""
    owner, job_id = jenv.job()
    other = uuid.uuid4()
    add_event(jenv.admin, owner, job_id, "queued")
    assert await read_page(jenv.factory, make_context(other), job_id, 0, 10) is None
    page = await read_page(jenv.factory, make_context(owner), job_id, 0, 10)
    assert page is not None
    assert [e.seq for e in page.events] == [1]
    unfiltered = (
        text("SELECT state FROM jobs WHERE id = :job_id"),
        text("SELECT seq FROM job_events WHERE job_id = :job_id"),
    )
    seen: dict[UUID, list[int]] = {}
    for who in (other, owner):
        async with user_session(jenv.factory, make_context(who)) as db:
            seen[who] = [len((await db.execute(q, {"job_id": job_id})).all()) for q in unfiltered]
    assert seen == {other: [0, 0], owner: [1, 1]}
