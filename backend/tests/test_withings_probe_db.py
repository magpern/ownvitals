"""The probe command end to end (#29): the stored grant, the owner lookup, the rate limiter and
the refresh path, over the migrated database and the fake. Synthetic data and tokens only."""

import io
import json
import re
import urllib.request
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from ownvitals import egress
from ownvitals.connections.providers.withings import open_withings, store_grant
from ownvitals.egress import core
from ownvitals.withings import probe as probe_module
from ownvitals.withings.apps import create_app
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.probe import probe
from ownvitals.withings.tokens import refresh_tokens
from test_withings_probe import NEEDLES, PLANTED_COMMENT, PLANTED_URL, _dataset
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingswire import ACCESS_TTL, Subscription

pytestmark = pytest.mark.usefixtures("auth_schema", "clean_auth")
CLIENT = "synthetic-client"
SECRET = "synthetic-secret"
BASE = "https://app.example.test"


@pytest.fixture(autouse=True)
def _worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=False)


@pytest.fixture(autouse=True)
async def _fresh_bucket(auth_schema: None, database_url: str):
    """The rate tables outlive ``clean_auth``: every test starts with a full, unpaused bucket."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM withings_rate_demand"))
            await conn.execute(
                text("UPDATE withings_rate_slots SET lease_id = NULL, lease_until = NULL")
            )
            await conn.execute(
                text("UPDATE withings_rate_buckets SET remaining = budget, paused_until = NULL")
            )
    finally:
        await engine.dispose()


@pytest.fixture
async def owner(owner_database_url: str):
    engine = create_async_engine(owner_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _connect(env: Env, owner: object, fake: FakeWithings, email: str) -> tuple[UUID, str]:
    """One account with a stored Withings grant over the planted dataset."""
    if CLIENT not in fake.apps:
        fake.add_app(CLIENT, SECRET)
    user = fake.add_user(dataset=_dataset())
    fake.subscriptions.append(Subscription(CLIENT, user.userid, 1, PLANTED_URL, PLANTED_COMMENT))
    body = fake.tokens_for(CLIENT, user.userid)
    ring = env.app.state.keyring
    async with env.db() as db:
        found = (await db.execute(text("SELECT id FROM withings_apps"))).first()
    app_id = (
        int(found.id)
        if found is not None
        else await create_app(owner, env.factory, ring, CLIENT, BASE, SECRET)  # type: ignore[arg-type]
    )
    ctx = make_context(await env.user(email))
    stored = await store_grant(
        env.factory,
        ring,
        ctx,
        app_id,
        user.userid,
        str(body["scope"]),
        str(body["access_token"]),
        str(body["refresh_token"]),
        expires_in=int(body["expires_in"]),  # type: ignore[arg-type]
    )
    return stored.id, str(body["access_token"])


async def _run(
    env: Env, fake: FakeWithings, connection: UUID, **options: object
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = await probe(
        env.settings,
        connection,
        out,
        err,
        inner=FakeWithingsTransport(fake),
        **options,  # type: ignore[arg-type]
    )
    return code, out.getvalue(), err.getvalue()


def _assert_shapes_only(output: str, connection: UUID, *secrets: str) -> None:
    output = output.replace(str(connection), "<connection>")  # the one id the output holds
    leaked = sorted(n for n in (*NEEDLES, *secrets, PLANTED_URL, PLANTED_COMMENT) if n in output)
    assert leaked == [], leaked
    assert not re.search(r"\d{9,}", output.replace("-", " "))  # no timestamp, grpid or user id


async def test_the_probe_prints_shapes_only_in_both_formats(env: Env, owner: object) -> None:
    fake = FakeWithings(seed=29)
    connection, access = await _connect(env, owner, fake, "probe-shapes@example.com")
    code, text_out, err = await _run(env, fake, connection)
    assert (code, err) == (0, "")
    assert f"connection {connection}" in text_out
    assert "outcome complete" in text_out
    assert "capability ecg supported" in text_out
    _assert_shapes_only(text_out, connection, access)
    code, json_out, err = await _run(env, fake, connection, as_json=True)
    assert (code, err) == (0, "")
    data = json.loads(json_out)
    assert data["connection"] == str(connection)
    assert data["notify"]["subscriptions"] == 1
    _assert_shapes_only(json_out, connection, access)


async def test_every_call_takes_a_rate_token_and_frees_its_slot(
    env: Env, owner: object, database_url: str
) -> None:
    fake = FakeWithings(seed=29)
    connection, _ = await _connect(env, owner, fake, "probe-rate@example.com")
    code, _, _ = await _run(env, fake, connection)
    assert code == 0
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as conn:
            remaining = (
                await conn.execute(text("SELECT remaining FROM withings_rate_buckets"))
            ).scalar_one()
            held = (
                await conn.execute(
                    text("SELECT count(*) FROM withings_rate_slots WHERE lease_id IS NOT NULL")
                )
            ).scalar_one()
    finally:
        await engine.dispose()
    assert held == 0
    assert remaining <= 100 - fake.request_count + 2  # a refill of a second or two at most


async def test_a_paused_bucket_stops_the_probe_before_any_call(
    env: Env, owner: object, database_url: str
) -> None:
    fake = FakeWithings(seed=29)
    connection, _ = await _connect(env, owner, fake, "probe-paused@example.com")
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT withings_rate_pause(id, 900) FROM withings_apps"))
    finally:
        await engine.dispose()
    waited: list[float] = []

    async def sleep(seconds: float) -> None:
        waited.append(seconds)

    code, out, _ = await _run(env, fake, connection, sleep=sleep)
    assert code == 1
    assert "outcome stopped rate_limited" in out
    assert fake.request_count == 0
    assert sum(waited) <= 120


async def test_an_expired_access_token_is_refreshed_through_the_crash_safe_path(
    env: Env, owner: object
) -> None:
    fake = FakeWithings(seed=29)
    connection, access = await _connect(env, owner, fake, "probe-refresh@example.com")
    fake.advance(ACCESS_TTL + 60)
    code, out, _ = await _run(env, fake, connection)
    assert code == 0
    assert "grant refreshed" in out
    assert "outcome complete" in out
    ctx = make_context(await _user_id(env, "probe-refresh@example.com"))
    opened = await open_withings(env.factory, env.app.state.keyring, ctx, connection)
    assert opened.generation == 2
    assert opened.access_token != access
    _assert_shapes_only(out, connection, access, opened.access_token, opened.refresh_token)


async def _user_id(env: Env, email: str) -> UUID:
    async with env.db() as db:
        row = (await db.execute(text("SELECT id FROM users WHERE email = :e"), {"e": email})).one()
    return row.id


async def test_the_owner_is_found_among_several_accounts(env: Env, owner: object) -> None:
    fake = FakeWithings(seed=29)
    await _connect(env, owner, fake, "probe-first@example.com")
    second, _ = await _connect(env, owner, fake, "probe-second@example.com")
    code, out, _ = await _run(env, fake, second)
    assert code == 0
    assert f"connection {second}" in out


async def test_an_unknown_connection_prints_nothing_but_the_refusal(
    env: Env, owner: object
) -> None:
    fake = FakeWithings(seed=29)
    await _connect(env, owner, fake, "probe-unknown@example.com")
    code, out, err = await _run(env, fake, uuid4())
    assert (code, out) == (1, "")
    assert err == "error: no live Withings connection has this id\n"
    assert fake.request_count == 0


LEAK = "synthetic-leak-987654321-s3cr3t"


@pytest.mark.parametrize("where", ["open_withings", "collect"])
async def test_a_failure_prints_the_exception_type_only(
    env: Env, owner: object, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    fake = FakeWithings(seed=29)
    connection, _ = await _connect(env, owner, fake, f"probe-fail-{where}@example.com")

    async def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(LEAK)

    monkeypatch.setattr(probe_module, where, boom)
    code, out, err = await _run(env, fake, connection)
    assert (code, out, err) == (1, "", "error: RuntimeError\n")


async def test_a_grant_the_worker_rotated_meanwhile_is_used_not_rotated_again(
    env: Env, owner: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeWithings(seed=29)
    connection, _ = await _connect(env, owner, fake, "probe-rotated@example.com")
    fake.advance(ACCESS_TTL + 60)  # the stored access token is expired
    opened: list[int] = []

    async def open_then_rotate(factory: Any, ring: Any, ctx: Any, connection_id: UUID) -> Any:
        view = await open_withings(factory, ring, ctx, connection_id)
        if not opened:  # the worker refreshes right after the probe read the grant
            async with WithingsClient.open(
                WithingsApp(CLIENT, SECRET), inner=FakeWithingsTransport(fake)
            ) as client:
                assert (
                    await refresh_tokens(factory, ring, ctx, connection_id, client)
                ).outcome == ("refreshed")
        opened.append(view.generation)
        return view

    monkeypatch.setattr(probe_module, "open_withings", open_then_rotate)
    code, out, _ = await _run(env, fake, connection)
    assert code == 0
    assert "grant current" in out
    assert opened == [1, 2]
    assert sum(1 for e in fake.log if e.endpoint == "v2/oauth2:requesttoken") == 1
