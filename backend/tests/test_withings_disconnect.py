"""Disconnect revokes upstream and drops credentials. Synthetic fixtures and the fake only."""

import logging
import re
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from authtools import Env, auth_schema_fixture, clean_auth_fixture, env_fixture  # noqa: F401
from ctx import make_context
from ownvitals.connections.providers.withings import GrantRefused, store_grant
from ownvitals.context import UserContext
from ownvitals.egress import core, install_worker_allowlist
from ownvitals.withings.apps import create_app
from ownvitals.withings.disconnect import DisconnectRefused, disconnect_withings
from ownvitals.withings.errors import RetryBudget
from ownvitals.withings.oauth import SessionProof, StateStore, callback, start
from withingscore import FakeWithings
from withingsfake import FakeWithingsTransport
from withingswire import MASKED, Failure, Request, Subscription

pytestmark = pytest.mark.usefixtures("auth_schema", "clean_auth")
CLIENT = "synthetic-client"
SECRET = "synthetic-secret"
BASE = "https://app.example.test"
REDIRECT = BASE + "/api/oauth/withings/callback"
CURRENT = BASE + "/api/webhooks/withings/current-synthetic"
OLD = BASE + "/api/webhooks/withings/old-marker"
FOREIGN = "https://other.example.test/api/webhooks/withings/foreign-marker"
_NAME = re.compile(r"[a-z_]+")


@pytest.fixture(autouse=True)
def _web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)


@pytest.fixture
async def owner(owner_database_url: str):
    engine = create_async_engine(owner_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@dataclass
class Linked:
    ctx: UserContext
    connection_id: UUID
    fake: FakeWithings
    inner: FakeWithingsTransport
    access: str
    refresh: str
    app_id: int
    userid: int


def _fake() -> FakeWithings:
    fake = FakeWithings(seed=27)
    fake.add_app(CLIENT, SECRET, (REDIRECT,))
    fake.add_user()
    return fake


async def _app(env: Env, owner: object) -> int:
    return await create_app(owner, env.factory, env.app.state.keyring, CLIENT, BASE, SECRET)  # type: ignore[arg-type]


def _proof() -> SessionProof:
    return SessionProof(uuid4(), True)


async def _code(fake: FakeWithings, url: str) -> str:
    page = fake.handle(Request("GET", url))
    return parse_qs(urlsplit(page.headers["location"]).query)["code"][0]


async def _linked(env: Env, owner: object) -> Linked:
    app_id = await _app(env, owner)
    ring = env.app.state.keyring
    fake = _fake()
    ctx = make_context(await env.user("disconnect@example.com"))
    store = StateStore()
    proof = _proof()
    inner = FakeWithingsTransport(fake)
    started = await start(env.factory, ring, ctx, proof, app_id, store=store)
    stored = await callback(
        env.factory,
        ring,
        ctx,
        proof,
        await _code(fake, started.authorize_url),
        started.state,
        store=store,
        inner=inner,
    )
    install_worker_allowlist(ai_enabled=False)
    userid = next(iter(fake.users))
    grant = fake.grant(CLIENT, userid)
    fake.subscriptions.extend(
        (
            Subscription(CLIENT, userid, 1, CURRENT),
            Subscription(CLIENT, userid, 44, OLD),
            Subscription(CLIENT, userid, 1, FOREIGN),
        )
    )
    return Linked(ctx, stored.id, fake, inner, grant.access, grant.refresh, app_id, userid)


def _scalar(url: str, sql: str, **params: object) -> object:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar_one()
    finally:
        engine.dispose()


def _run(url: str, sql: str, **params: object) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params)
    finally:
        engine.dispose()


def _hits(url: str, needle: str) -> list[str]:
    """Columns whose bytes contain ``needle``. A superuser sees every row."""
    engine = create_engine(url)
    found: list[str] = []
    try:
        with engine.connect() as conn:
            columns = conn.execute(
                text(
                    "SELECT c.relname, a.attname, t.typname "
                    "FROM pg_attribute a "
                    "JOIN pg_class c ON c.oid = a.attrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_type t ON t.oid = a.atttypid "
                    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                    "AND a.attnum > 0 AND NOT a.attisdropped "
                    "AND t.typname IN ('text', 'varchar', 'json', 'jsonb', 'bytea')"
                )
            ).all()
            for table, column, kind in columns:
                if _NAME.fullmatch(table) is None or _NAME.fullmatch(column) is None:
                    continue
                if kind == "bytea":
                    sql = (
                        f"SELECT count(*) FROM public.{table} "  # noqa: S608 - names are catalog identifiers
                        f"WHERE {column} IS NOT NULL AND position(:n IN {column}) > 0"
                    )
                    params: dict[str, object] = {"n": needle.encode()}
                else:
                    sql = (
                        f"SELECT count(*) FROM public.{table} "  # noqa: S608
                        f"WHERE {column} IS NOT NULL AND strpos({column}::text, :n) > 0"
                    )
                    params = {"n": needle}
                if conn.execute(text(sql), params).scalar_one():
                    found.append(f"{table}.{column}")
        return found
    finally:
        engine.dispose()


async def _asleep(_: float) -> None:
    return None


async def test_disconnect_revokes_and_keeps_other_owners_rows(
    env: Env, owner: object, database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    linked = await _linked(env, owner)
    other = make_context(await env.user("other-disconnect@example.com"))
    await store_grant(
        env.factory,
        env.app.state.keyring,
        other,
        linked.app_id,
        2002,
        "user.metrics",
        "synthetic-access-token-bbb",
        "synthetic-refresh-token-bbb",
        expires_in=10800,
    )
    _run(
        database_url,
        "INSERT INTO devices (owner_id, connection_id, hash_deviceid) "
        "VALUES (:o, :c, 'synthetic-device')",
        o=linked.ctx.user_id,
        c=linked.connection_id,
    )
    other_connection = _scalar(
        database_url, "SELECT id FROM connections WHERE owner_id = :o", o=other.user_id
    )
    _run(
        database_url,
        "INSERT INTO devices (owner_id, connection_id, hash_deviceid) "
        "VALUES (:o, :c, 'synthetic-device')",
        o=other.user_id,
        c=other_connection,
    )
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    result = await disconnect_withings(
        env.factory,
        env.app.state.keyring,
        linked.ctx,
        linked.connection_id,
        stepped_up=True,
        inner=linked.inner,
    )
    assert result.cleared is True
    assert result.subscriptions_revoked == 2
    assert result.token_revoked is True
    assert result.revoke_failed is False
    assert result.removal is None
    endpoints = [entry.endpoint for entry in linked.fake.log]
    assert endpoints.count("notify:revoke") == 2
    assert "v2/oauth2:revoke" in endpoints
    for entry in linked.fake.log:
        assert set(entry.query_names).isdisjoint(MASKED)
    assert "old-marker" not in caplog.text
    assert "foreign-marker" not in caplog.text
    assert linked.access not in caplog.text
    assert linked.refresh not in caplog.text
    assert linked.access not in repr(result)
    status = _scalar(
        database_url,
        "SELECT status FROM connections WHERE owner_id = :o",
        o=linked.ctx.user_id,
    )
    assert status == "disconnected"
    assert (
        _scalar(
            database_url,
            "SELECT count(*) FROM connections WHERE owner_id = :o "
            "AND (secret IS NOT NULL OR secret_nonce IS NOT NULL OR wrapped_dek IS NOT NULL "
            "OR dek_nonce IS NOT NULL OR key_id IS NOT NULL)",
            o=linked.ctx.user_id,
        )
        == 0
    )
    assert _hits(database_url, linked.access) == []
    assert _hits(database_url, linked.refresh) == []
    assert (
        _scalar(
            database_url, "SELECT count(*) FROM devices WHERE owner_id = :o", o=linked.ctx.user_id
        )
        == 1
    )
    assert (
        _scalar(database_url, "SELECT status FROM connections WHERE owner_id = :o", o=other.user_id)
        == "connected"
    )
    assert (
        _scalar(
            database_url,
            "SELECT count(*) FROM connections WHERE owner_id = :o AND secret IS NOT NULL",
            o=other.user_id,
        )
        == 1
    )
    assert (
        _scalar(database_url, "SELECT count(*) FROM devices WHERE owner_id = :o", o=other.user_id)
        == 1
    )


async def test_a_transient_failure_is_retried_then_both_revokes_run(
    env: Env, owner: object
) -> None:
    linked = await _linked(env, owner)
    linked.fake.failures.append(
        Failure(kind="http", at=frozenset({1}), endpoint="notify:list", http_status=503)
    )
    result = await disconnect_withings(
        env.factory,
        env.app.state.keyring,
        linked.ctx,
        linked.connection_id,
        stepped_up=True,
        inner=linked.inner,
        budget=RetryBudget(retries=1, max_delay=0),
        sleep=_asleep,
    )
    endpoints = [entry.endpoint for entry in linked.fake.log]
    assert endpoints.count("notify:list") == 2
    assert endpoints.count("notify:revoke") == 2
    assert "v2/oauth2:revoke" in endpoints
    assert result.token_revoked is True
    assert result.revoke_failed is False


async def test_a_failed_revoke_still_removes_secrets(
    env: Env, owner: object, database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    linked = await _linked(env, owner)
    linked.fake.failures.append(
        Failure(kind="http", at=frozenset({1}), endpoint="notify:list", http_status=400)
    )
    caplog.clear()
    caplog.set_level(logging.DEBUG)
    result = await disconnect_withings(
        env.factory,
        env.app.state.keyring,
        linked.ctx,
        linked.connection_id,
        stepped_up=True,
        inner=linked.inner,
    )
    endpoints = [entry.endpoint for entry in linked.fake.log]
    assert "notify:revoke" not in endpoints
    assert "v2/oauth2:revoke" in endpoints
    assert result.revoke_failed is True
    assert result.cleared is True
    assert result.removal is not None
    assert linked.access not in result.removal
    assert "old-marker" not in result.removal
    assert linked.access not in caplog.text
    assert (
        _scalar(
            database_url,
            "SELECT count(*) FROM connections WHERE owner_id = :o AND secret IS NOT NULL",
            o=linked.ctx.user_id,
        )
        == 0
    )


async def test_disconnect_requires_step_up(env: Env, owner: object, database_url: str) -> None:
    linked = await _linked(env, owner)
    before = len(linked.fake.log)
    with pytest.raises(DisconnectRefused, match="step_up_required") as caught:
        await disconnect_withings(
            env.factory,
            env.app.state.keyring,
            linked.ctx,
            linked.connection_id,
            stepped_up=False,
            inner=linked.inner,
        )
    assert caught.value.reason == "step_up_required"
    assert len(linked.fake.log) == before
    assert (
        _scalar(
            database_url, "SELECT status FROM connections WHERE owner_id = :o", o=linked.ctx.user_id
        )
        == "connected"
    )


async def test_a_foreign_connection_is_left_alone(env: Env, database_url: str) -> None:
    owner_a = make_context(await env.user("row-a@example.com"))
    owner_b = make_context(await env.user("row-b@example.com"))
    ring = env.app.state.keyring
    stored = await store_grant(
        env.factory,
        ring,
        owner_a,
        7,
        3003,
        "user.metrics",
        "synthetic-access-token-aaa",
        "synthetic-refresh-token-aaa",
        expires_in=10800,
    )
    with pytest.raises(DisconnectRefused, match="not_connected"):
        await disconnect_withings(env.factory, ring, owner_b, stored.id, stepped_up=True)
    assert (
        _scalar(database_url, "SELECT status FROM connections WHERE id = :i", i=stored.id)
        == "connected"
    )
    assert (
        _scalar(
            database_url,
            "SELECT count(*) FROM connections WHERE id = :i AND secret IS NOT NULL",
            i=stored.id,
        )
        == 1
    )


async def test_a_different_withings_account_is_refused_while_connected(env: Env) -> None:
    ctx = make_context(await env.user("one-account@example.com"))
    ring = env.app.state.keyring
    await store_grant(
        env.factory,
        ring,
        ctx,
        7,
        4004,
        "user.metrics",
        "synthetic-access-token-one",
        "synthetic-refresh-token-one",
        expires_in=10800,
    )
    with pytest.raises(GrantRefused, match="other_withings_account") as caught:
        await store_grant(
            env.factory,
            ring,
            ctx,
            7,
            4005,
            "user.metrics",
            "synthetic-access-token-two",
            "synthetic-refresh-token-two",
            expires_in=10800,
        )
    assert caught.value.reason == "other_withings_account"
