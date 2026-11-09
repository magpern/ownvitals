"""PLAN §4.3 refresh, against the fake. Synthetic tokens only."""

import asyncio
import urllib.request
from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from ownvitals import egress
from ownvitals.connections.providers.withings import commit_refresh, open_withings, store_grant
from ownvitals.connections.repository import disconnect
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.egress import core
from ownvitals.withings import client as client_module
from ownvitals.withings import metrics as wm
from ownvitals.withings.apps import create_app
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.tokens import recover_refresh, refresh_tokens
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingswire import STATUS_BAD_REFRESH_TOKEN

pytestmark = pytest.mark.usefixtures("auth_schema", "clean_auth")
CLIENT = "synthetic-client"
SECRET = "synthetic-secret"
BASE = "https://app.example.test"


class Boom(Exception):
    """A crash injected between two steps of the refresh."""


@pytest.fixture(autouse=True)
def _worker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    egress.install_worker_allowlist(ai_enabled=False)


@pytest.fixture
async def owner(owner_database_url: str):
    engine = create_async_engine(owner_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _tokens(fake: FakeWithings) -> tuple[int, dict[str, object]]:
    fake.add_app(CLIENT, SECRET)
    user = fake.add_user()
    return user.userid, fake.tokens_for(CLIENT, user.userid)


async def _grant(
    env: Env, owner: object, email: str
) -> tuple[UserContext, UUID, FakeWithings, int]:
    fake = FakeWithings(seed=4)
    userid, body = _tokens(fake)
    app_id = await create_app(owner, env.factory, env.app.state.keyring, CLIENT, BASE, SECRET)  # type: ignore[arg-type]
    ctx = make_context(await env.user(email))
    stored = await store_grant(
        env.factory,
        env.app.state.keyring,
        ctx,
        app_id,
        userid,
        str(body["scope"]),
        str(body["access_token"]),
        str(body["refresh_token"]),
        expires_in=int(body["expires_in"]),  # type: ignore[arg-type]
    )
    return ctx, stored.id, fake, userid


def _calls(fake: FakeWithings) -> int:
    return sum(1 for entry in fake.log if entry.endpoint == "v2/oauth2:requesttoken")


async def test_a_crash_after_the_response_recovers_once(env: Env, owner: object) -> None:
    ctx, connection_id, fake, userid = await _grant(env, owner, "crash-response@example.com")
    transport = FakeWithingsTransport(fake)

    def boom() -> None:
        raise Boom

    async with WithingsClient.open(WithingsApp(CLIENT, SECRET), inner=transport) as client:
        with pytest.raises(Boom):
            await refresh_tokens(
                env.factory, env.app.state.keyring, ctx, connection_id, client, after_response=boom
            )
        assert _calls(fake) == 1
        recovered = await recover_refresh(
            env.factory, env.app.state.keyring, ctx, connection_id, client
        )
        again = await recover_refresh(
            env.factory, env.app.state.keyring, ctx, connection_id, client
        )
    assert recovered.outcome == "refreshed"
    assert again.outcome == "current"
    assert again.generation == recovered.generation == 2
    assert _calls(fake) == 2
    opened = await open_withings(env.factory, env.app.state.keyring, ctx, connection_id)
    assert opened.access_token == fake.grant(CLIENT, userid).access
    assert opened.intent is None


async def test_a_crash_after_commit_keeps_the_new_generation(env: Env, owner: object) -> None:
    ctx, connection_id, fake, userid = await _grant(env, owner, "crash-commit@example.com")

    def boom() -> None:
        raise Boom

    async with WithingsClient.open(
        WithingsApp(CLIENT, SECRET), inner=FakeWithingsTransport(fake)
    ) as client:
        with pytest.raises(Boom):
            await refresh_tokens(
                env.factory, env.app.state.keyring, ctx, connection_id, client, after_commit=boom
            )
    opened = await open_withings(env.factory, env.app.state.keyring, ctx, connection_id)
    grant = fake.grant(CLIENT, userid)
    assert opened.generation == 2
    assert opened.access_token == grant.access
    assert grant.access_used is False
    assert _calls(fake) == 1
    again = await open_withings(env.factory, env.app.state.keyring, ctx, connection_id)
    assert again.generation == 2
    assert again.access_token == opened.access_token


async def test_two_workers_perform_one_refresh(env: Env, owner: object) -> None:
    ctx, connection_id, fake, _userid = await _grant(env, owner, "race@example.com")
    fake.inject("delay", at=1, endpoint="v2/oauth2:requesttoken", seconds=0.2)
    transport = FakeWithingsTransport(fake, sleep=asyncio.sleep)
    async with WithingsClient.open(WithingsApp(CLIENT, SECRET), inner=transport) as client:
        first, second = await asyncio.gather(
            refresh_tokens(env.factory, env.app.state.keyring, ctx, connection_id, client),
            refresh_tokens(env.factory, env.app.state.keyring, ctx, connection_id, client),
        )
    assert _calls(fake) == 1
    assert {first.outcome, second.outcome} == {"refreshed", "current"}
    assert first.generation == second.generation == 2


async def test_a_stale_intent_needs_reconnect(env: Env, owner: object) -> None:
    ctx, connection_id, fake, _userid = await _grant(env, owner, "stale@example.com")
    async with user_session(env.factory, ctx) as db:
        await db.execute(
            text(
                "UPDATE connections SET refresh_in_flight = token_generation, "
                "refresh_intent_at = now() - interval '8 hours 1 second' WHERE id = :id"
            ),
            {"id": connection_id},
        )
    async with WithingsClient.open(
        WithingsApp(CLIENT, SECRET), inner=FakeWithingsTransport(fake)
    ) as client:
        result = await recover_refresh(
            env.factory, env.app.state.keyring, ctx, connection_id, client
        )
    assert result.outcome == "needs_reconnect"
    assert _calls(fake) == 0
    async with user_session(env.factory, ctx) as db:
        status, error, kept = (
            await db.execute(
                text(
                    "SELECT status, last_error, secret IS NOT NULL FROM connections WHERE id = :id"
                ),
                {"id": connection_id},
            )
        ).one()
    assert (status, error, kept) == ("needs_reconnect", "needs_reconnect", True)


async def test_an_oversized_refresh_body_stays_recoverable(
    env: Env, owner: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request was sent and the body discarded. Leave the intent; return no access token."""
    ctx, connection_id, fake, userid = await _grant(env, owner, "oversized-refresh@example.com")
    cap = client_module.MAX_RESPONSE_BYTES
    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 64)
    failures = wm.token_refresh.get(result="failure")
    recoveries = wm.token_refresh.get(result="recovered")
    async with WithingsClient.open(
        WithingsApp(CLIENT, SECRET), inner=FakeWithingsTransport(fake)
    ) as client:
        result = await refresh_tokens(
            env.factory, env.app.state.keyring, ctx, connection_id, client
        )
        assert _calls(fake) == 1
        assert result.outcome == "recover"
        # #167: the refresh metrics move through the real refresh and recovery.
        assert wm.token_refresh.get(result="failure") == failures + 1
        assert wm.token_refresh.get(result="recovered") == recoveries
        assert result.access_token == ""
        assert result.access_token != fake.grant(CLIENT, userid).access
        opened = await open_withings(env.factory, env.app.state.keyring, ctx, connection_id)
        assert opened.intent == opened.generation == 1
        monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", cap)
        recovered = await recover_refresh(
            env.factory, env.app.state.keyring, ctx, connection_id, client
        )
    assert recovered.outcome == "refreshed"
    assert recovered.access_token == fake.grant(CLIENT, userid).access
    assert wm.token_refresh.get(result="recovered") == recoveries + 1
    assert wm.token_refresh.get(result="failure") == failures + 1
    assert _calls(fake) == 2
    stored = await open_withings(env.factory, env.app.state.keyring, ctx, connection_id)
    assert stored.intent is None
    assert stored.access_token == recovered.access_token


async def test_an_older_generation_cannot_overwrite(env: Env, owner: object) -> None:
    ctx, connection_id, _fake, _userid = await _grant(env, owner, "cas@example.com")
    ring = env.app.state.keyring
    stale = await commit_refresh(
        env.factory,
        ring,
        ctx,
        connection_id,
        0,
        "synthetic-access-token-older-aaaa",
        "synthetic-refresh-token-older-bbb",
        10800,
    )
    assert stale is False
    fresh = await commit_refresh(
        env.factory,
        ring,
        ctx,
        connection_id,
        1,
        "synthetic-access-token-newer-aaaa",
        "synthetic-refresh-token-newer-bbb",
        10800,
    )
    assert fresh is True
    opened = await open_withings(env.factory, ring, ctx, connection_id)
    assert opened.generation == 2
    assert opened.access_token == "synthetic-access-token-newer-aaaa"
    rejected = await commit_refresh(
        env.factory,
        ring,
        ctx,
        connection_id,
        1,
        "synthetic-access-token-stale-aaaa",
        "synthetic-refresh-token-stale-bbb",
        10800,
    )
    assert rejected is False
    opened = await open_withings(env.factory, ring, ctx, connection_id)
    assert opened.generation == 2
    assert opened.access_token == "synthetic-access-token-newer-aaaa"


# ------------------------------------------------- a disconnect or a reconnect during the call
# The refresh lock does not cover a disconnect or a reconnect (``store_grant``), and neither one
# raises the generation in a way the refresh can see before its compare-and-swap. These run the
# other write while ``requesttoken`` is out (#279).

WINNER_ACCESS = "synthetic-access-token-winner-aaaa"
WINNER_REFRESH = "synthetic-refresh-token-winner-bbb"


class _During(FakeWithingsTransport):
    """Runs ``hook`` once, when the ``requesttoken`` request arrives, before the fake answers."""

    def __init__(self, fake: FakeWithings, hook: Callable[[], Awaitable[None]]) -> None:
        super().__init__(fake)
        self._hook: Callable[[], Awaitable[None]] | None = hook

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._hook is not None and b"action=requesttoken" in await request.aread():
            hook, self._hook = self._hook, None
            await hook()
        return await super().handle_async_request(request)


async def _row(env: Env, ctx: UserContext, connection_id: UUID) -> tuple[str, bool, str | None]:
    async with user_session(env.factory, ctx) as db:
        row = (
            await db.execute(
                text("SELECT status, secret IS NULL, last_error FROM connections WHERE id = :i"),
                {"i": connection_id},
            )
        ).one()
    return str(row[0]), bool(row[1]), row[2]


async def test_a_disconnect_during_the_refresh_is_not_undone(env: Env, owner: object) -> None:
    ctx, connection_id, fake, _userid = await _grant(env, owner, "race-disconnect@example.com")

    async def hook() -> None:
        assert await disconnect(env.factory, ctx, "withings", connection_id=connection_id)

    async with WithingsClient.open(
        WithingsApp(CLIENT, SECRET), inner=_During(fake, hook)
    ) as client:
        result = await refresh_tokens(
            env.factory, env.app.state.keyring, ctx, connection_id, client
        )
    assert _calls(fake) == 1
    assert result.outcome == "not_connected"
    assert result.access_token == ""
    assert await _row(env, ctx, connection_id) == ("disconnected", True, None)


async def test_a_revoke_during_the_refresh_does_not_flag_the_disconnected_row(
    env: Env, owner: object
) -> None:
    ctx, connection_id, fake, userid = await _grant(env, owner, "race-revoke@example.com")

    async def hook() -> None:
        # disconnect_withings revokes the grant upstream, then clears the row
        fake._drop_tokens(fake._grants.pop((CLIENT, userid)))
        assert await disconnect(env.factory, ctx, "withings", connection_id=connection_id)

    async with WithingsClient.open(
        WithingsApp(CLIENT, SECRET), inner=_During(fake, hook)
    ) as client:
        result = await refresh_tokens(
            env.factory, env.app.state.keyring, ctx, connection_id, client
        )
    assert result.outcome == "not_connected"
    assert await _row(env, ctx, connection_id) == ("disconnected", True, None)
    refused = [e for e in fake.log if e.endpoint == "v2/oauth2:requesttoken"]
    assert [e.api_status for e in refused] == [STATUS_BAD_REFRESH_TOKEN]  # TokenInvalid: give-up


async def test_a_reconnect_during_the_refresh_wins_and_its_token_is_returned(
    env: Env, owner: object
) -> None:
    ctx, connection_id, fake, userid = await _grant(env, owner, "race-reconnect@example.com")
    ring = env.app.state.keyring
    before = await open_withings(env.factory, ring, ctx, connection_id)

    async def hook() -> None:
        stored = await store_grant(
            env.factory,
            ring,
            ctx,
            int(before.public["app_id"]),
            userid,
            "user.metrics",
            WINNER_ACCESS,
            WINNER_REFRESH,
            expires_in=10800,
        )
        assert (stored.id, stored.generation) == (connection_id, 2)

    async with WithingsClient.open(
        WithingsApp(CLIENT, SECRET), inner=_During(fake, hook)
    ) as client:
        result = await refresh_tokens(env.factory, ring, ctx, connection_id, client)
    assert _calls(fake) == 1
    assert result.outcome == "current"
    assert result.generation == 2
    assert result.access_token == WINNER_ACCESS
    opened = await open_withings(env.factory, ring, ctx, connection_id)
    assert (opened.generation, opened.access_token) == (2, WINNER_ACCESS)
    assert opened.refresh_token == WINNER_REFRESH
    assert opened.intent is None
