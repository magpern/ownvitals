"""The desktop's QR login stream (ADR 0011 section 5, threat model T89).

Events in order and nothing else in them; 404 for another pre-session's row; the stream ends at
the row's deadline, still works with notifications suppressed, gives way to a newer stream for
the same row, and is bounded per IP and per process; an open stream holds no pooled connection.
Synthetic accounts and codes only."""

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    env_fixture,
    running_env,
)
from ownvitals.auth import qrlogin, qrlogin_phone, qrlogin_stream
from ownvitals.auth.cookies import PRE_COOKIE
from ownvitals.auth.qrlogin_stream import StreamSlots
from qrtools import (
    BASE,
    QrBrowser,
    approve,
    complete,
    frames,
    fresh_code,
    phone,
    scan,
    start,
)


async def until(check, seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not await check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.02)


def subscribed(env: Env, qr_id: str):
    hub = env.app.state.qr_hub

    async def check() -> bool:
        return uuid.UUID(qr_id) in hub._subscriptions  # pyright: ignore[reportPrivateUsage]

    return check


async def events(desktop: QrBrowser, qr_id: str) -> httpx.Response:
    return await desktop.get(f"{BASE}/{qr_id}/events")


async def test_the_events_in_order_and_nothing_else(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    started = await start(desktop)
    stream = asyncio.create_task(events(desktop, started["id"]))
    await until(subscribed(env, started["id"]))
    assert (await scan(mobile, started)).status_code == 200
    assert (await approve(mobile, started["id"], await fresh_code(env))).status_code == 200
    answer = await asyncio.wait_for(stream, 10)
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/event-stream")
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["x-accel-buffering"] == "no"
    assert frames(answer.text) == ["scanned", "approved"]
    assert answer.text == (
        'id: scanned\nevent: scanned\ndata: {"state":"scanned"}\n\n'
        'id: approved\nevent: approved\ndata: {"state":"approved"}\n\n'
    )
    # No detail of the phone and no token in any event.
    phone_ip = mobile.ip or ""
    for secret in (*mobile.cookies.values(), *desktop.cookies.values(), "user@example.com"):
        assert secret not in answer.text
    assert phone_ip not in answer.text
    done = await complete(desktop, started["id"])
    assert done.status_code == 200
    for value in desktop.cookies.values():
        assert value not in answer.text


async def test_deny_ends_the_stream(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    started = await start(desktop)
    assert (await scan(mobile, started)).status_code == 200
    stream = asyncio.create_task(events(desktop, started["id"]))
    await until(subscribed(env, started["id"]))
    assert (await mobile.post(f"{BASE}/{started['id']}/deny")).status_code == 200
    answer = await asyncio.wait_for(stream, 10)
    assert frames(answer.text) == ["scanned", "denied"]
    # The browser saw the end: its reconnect is a 204, so an EventSource stops.
    env.client.cookies.clear()
    again = await env.client.get(
        f"{BASE}/{started['id']}/events",
        headers={**desktop._cookie_header(), "Last-Event-ID": "denied"},  # pyright: ignore[reportPrivateUsage]
    )
    assert (again.status_code, again.text) == (204, "")


async def test_another_pre_session_or_none_is_a_404(env: Env) -> None:
    desktop = QrBrowser(env)
    started = await start(desktop)
    stranger = QrBrowser(env, "203.0.113.70")
    assert (await events(stranger, started["id"])).status_code == 404
    await start(stranger)
    assert (await events(stranger, started["id"])).status_code == 404
    assert (await events(desktop, str(uuid.uuid4()))).status_code == 404
    header = QrBrowser(env)  # the cookie is read from its cookie only
    answer = await env.client.get(
        f"{BASE}/{started['id']}/events?{PRE_COOKIE}={desktop.cookies[PRE_COOKIE]}",
        headers={"X-Ownvitals-Pre": desktop.cookies[PRE_COOKIE]},
    )
    assert answer.status_code == 404
    assert header.cookies == {}


async def test_the_stream_closes_at_the_deadline(env: Env) -> None:
    desktop = QrBrowser(env)
    started = await start(desktop)
    await env.run("UPDATE qr_logins SET scan_deadline = now() + interval '1 second'")
    answer = await asyncio.wait_for(events(desktop, started["id"]), 10)
    assert frames(answer.text) == ["expired"]
    assert await env.scalar("SELECT state FROM qr_logins") == "expired"


async def test_it_works_with_notifications_suppressed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def silent(*_: object) -> None:
        return None

    monkeypatch.setattr(qrlogin, "_notify", silent)
    monkeypatch.setattr(qrlogin_phone, "_notify", silent)
    monkeypatch.setattr(qrlogin_stream, "POLL_SECONDS", 0.2)
    mobile = await phone(env)
    desktop = QrBrowser(env)
    started = await start(desktop)
    stream = asyncio.create_task(events(desktop, started["id"]))
    await until(subscribed(env, started["id"]))
    assert (await scan(mobile, started)).status_code == 200
    await asyncio.sleep(0.6)  # no wake-up: the stream finds it on its next read
    assert (await approve(mobile, started["id"], await fresh_code(env))).status_code == 200
    answer = await asyncio.wait_for(stream, 10)
    assert frames(answer.text) == ["scanned", "approved"]


async def test_a_second_stream_for_the_row_closes_the_first(env: Env) -> None:
    mobile = await phone(env)
    desktop = QrBrowser(env)
    started = await start(desktop)
    first = asyncio.create_task(events(desktop, started["id"]))
    await until(subscribed(env, started["id"]))
    second = asyncio.create_task(events(desktop, started["id"]))
    closed = await asyncio.wait_for(first, 10)  # ends without waiting for the row
    assert closed.status_code == 200
    assert frames(closed.text) == []
    assert not second.done()
    assert (await scan(mobile, started)).status_code == 200
    await mobile.post(f"{BASE}/{started['id']}/deny")
    assert frames((await asyncio.wait_for(second, 10)).text) == ["scanned", "denied"]
    assert env.app.state.qr_streams.open == 0


@contextlib.asynccontextmanager
async def open_streams(env: Env, desktops: list[QrBrowser]) -> AsyncIterator[list[asyncio.Task]]:
    tasks = []
    for desktop in desktops:
        started = await start(desktop)
        tasks.append(asyncio.create_task(events(desktop, started["id"])))
        await until(subscribed(env, started["id"]))
    try:
        yield tasks
    finally:
        env.app.state.qr_hub.close()  # ends every open stream
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_the_sixth_stream_from_one_ip_is_refused(env: Env) -> None:
    ip = "198.51.100.80"
    async with open_streams(env, [QrBrowser(env, ip) for _ in range(5)]):
        sixth = QrBrowser(env, ip)
        started = await start(sixth)
        answer = await events(sixth, started["id"])
        assert answer.status_code == 429
        assert answer.json() == {"detail": {"reason": "too_many_streams"}}
        assert "retry-after" in answer.headers
        # IPv6 counts by /64; another address is not affected (it ends at once: expired).
        other = QrBrowser(env, "198.51.100.81")
        started = await start(other)
        await other.env.run(
            "UPDATE qr_logins SET scan_deadline = now() WHERE id = :i", i=started["id"]
        )
        assert (await events(other, started["id"])).status_code == 200


async def test_the_process_limit(clean_auth: None, app_database_url: str) -> None:
    async with running_env(app_database_url, qr_streams_per_process=2) as env:
        desktops = [QrBrowser(env, f"198.51.100.{90 + i}") for i in range(2)]
        async with open_streams(env, desktops):
            third = QrBrowser(env, "198.51.100.99")
            started = await start(third)
            assert (await events(third, started["id"])).status_code == 429


async def test_open_streams_hold_no_pooled_connection(env: Env) -> None:
    pool = env.app.state.engine.pool
    state = env.app.state

    async def listening() -> bool:
        return state.qr_hub.listening and state.event_hub.listening

    await until(listening)
    before = pool.checkedout()  # the two listeners (jobs and QR) and nothing else
    assert before == 2
    async with open_streams(env, [QrBrowser(env, f"198.51.100.{110 + i}") for i in range(4)]):
        await asyncio.sleep(0.3)
        assert pool.checkedout() == before


def test_slots_count_per_ip_and_process_and_replace_per_row() -> None:
    slots = StreamSlots(per_ip=2, per_process=3)
    row = uuid.uuid4()
    a = slots.acquire(row, "ip-a")
    assert a is not None
    b = slots.acquire(row, "ip-a")  # the same row: replaces a
    assert b is not None
    assert a.superseded
    assert slots.open == 1
    assert slots.acquire(uuid.uuid4(), "ip-a") is not None
    assert slots.acquire(uuid.uuid4(), "ip-a") is None  # two per IP
    assert slots.acquire(uuid.uuid4(), "ip-b") is not None
    assert slots.acquire(uuid.uuid4(), "ip-c") is None  # three per process
    slots.release(b)
    assert slots.acquire(uuid.uuid4(), "ip-c") is not None
