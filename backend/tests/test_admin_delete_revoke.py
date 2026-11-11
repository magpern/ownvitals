"""The admin account delete revokes the Withings grant first (#365, PLAN section 4.3, ADR 0002
section 3 as amended 2026-11-10).

``DELETE /api/admin/users/{id}`` queues a ``withings_admin_revoke`` job through
``admin_account_revoke`` (an audited admin transaction) and ``queue_enqueue_admin_revoke``,
waits for it, then deletes the account whatever the outcome and records it on the
``user.delete`` row. The revoke runs in the job (here an in-process worker over the fake), never
in web. Every account, token, session and Withings account below is synthetic.
"""

import asyncio
import contextlib
import json
import urllib.request
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    admin_schema_fixture,
    aenv_fixture,
    caller,
    reason,
)
from authtools import Env
from jobtools import admin_url, engine_for
from ownvitals.admin import user_revoke
from ownvitals.auth.admin_revoke_job import (
    ADMIN_REVOKE_KIND,
    AdminRevokeInput,
    admin_revoke_kind,
    admin_stepped_up,
)
from ownvitals.auth.disconnect_job import JOB_SLACK
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.auth.session_model import ADMIN_MFA_WINDOW, ADMIN_STEPUP_WINDOW
from ownvitals.egress import core, install_worker_allowlist
from ownvitals.jobs import KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.withings.errors import RetryBudget
from test_withings_disconnect import Linked, _asleep, _linked
from withingswire import Failure

_CREDENTIALS = "SELECT count(*) FROM connections WHERE owner_id = :o AND secret IS NOT NULL"
_REVOKE = text("SELECT outcome, job_id FROM public.admin_account_revoke(:t, :e, CAST(:s AS uuid))")
_AUDIT = text(
    "INSERT INTO admin_audit (actor_id, actor_email, action, target_user_id, params, outcome, "
    "request_id, ip_hash) VALUES (:actor, 'synthetic-admin@example.com', 'user.revoke', :target, "
    "CAST('{}' AS jsonb), 'ok', 'synthetic-request', :ip_hash)"
)


@pytest.fixture(autouse=True)
def _web(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "_opener", None)
    monkeypatch.setattr(core, "_worker_list", None)
    monkeypatch.setattr(user_revoke, "ADMIN_REVOKE_WAIT_SECONDS", 15.0)
    monkeypatch.setattr(user_revoke, "_POLL_SECONDS", 0.05)


@pytest.fixture
async def owner(owner_database_url: str) -> AsyncIterator[object]:
    engine = create_async_engine(owner_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def linked(aenv: AdminEnv, owner: object) -> AsyncIterator[Linked]:
    """A synthetic target with a live Withings connection on the fake; its app row is removed
    afterwards (``withings_apps`` is a system table the account fixtures keep)."""
    found = await _linked(aenv.env, owner)
    found.fake.log.clear()
    try:
        yield found
    finally:
        await aenv.superuser("DELETE FROM connections WHERE app_id = :a", a=found.app_id)
        await aenv.superuser("DELETE FROM withings_apps WHERE id = :a", a=found.app_id)


@contextlib.asynccontextmanager
async def worker(env: Env, linked: Linked) -> AsyncIterator[None]:
    """An in-process worker that runs only ``withings_admin_revoke``, over the fake."""
    install_worker_allowlist(ai_enabled=False)
    registry = KindRegistry()
    registry.register(
        admin_revoke_kind(
            ring=env.app.state.keyring,
            inner=linked.inner,
            budget=lambda: RetryBudget(retries=2),
            sleep=_asleep,
        )
    )
    stop = asyncio.Event()
    config = RunConfig(lease_seconds=60, accounts=ACCOUNTS)

    async def loop() -> None:
        while not stop.is_set():
            row = await queue.claim(env.factory, "synthetic-worker", 60, registry.names())
            if row is None:
                await asyncio.sleep(0.05)
                continue
            await execute(Execution(row), env.factory, registry, config, asyncio.Event())

    task = asyncio.ensure_future(loop())
    try:
        yield
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


async def _delete(admin: Any, target: UUID, email: str = "disconnect@example.com") -> Any:
    return await admin.request("DELETE", f"/api/admin/users/{target}", {"email": email})


async def _trail(aenv: AdminEnv) -> list[tuple[str, str, Any]]:
    """The Users view's audit rows (the app's ``withings_app.create`` row left out)."""
    rows = await aenv.audit_rows()
    return [(r.action, r.outcome, r.params) for r in rows if r.action.startswith("user.")]


def _revokes(linked: Linked) -> list[str]:
    return [e.endpoint for e in linked.fake.log if e.endpoint.endswith("revoke")]


# --- the three outcomes: revoked, revoke failed, worker slow -----------------------------------


async def test_the_admin_delete_revokes_in_the_worker_before_the_account_goes(
    aenv: AdminEnv, linked: Linked
) -> None:
    admin = await caller(aenv)
    async with worker(aenv.env, linked):
        response = await _delete(admin, linked.ctx.user_id)
    assert response.status_code == 204, response.text
    assert _revokes(linked) == ["notify:revoke", "notify:revoke", "v2/oauth2:revoke"]
    target = linked.ctx.user_id
    assert await aenv.env.scalar("SELECT count(*) FROM users WHERE id = :u", u=target) == 0
    rows = await aenv.superuser("SELECT count(*) FROM connections WHERE owner_id = :o", o=target)
    assert rows.scalar_one() == 0
    assert await _trail(aenv) == [
        ("user.revoke", "ok", {}),
        ("user.revoke_wait", "ok", {}),
        ("user.delete", "ok", {"revoke": "revoked"}),
    ]
    assert linked.access not in response.text


async def test_a_failed_revoke_is_recorded_and_the_account_still_goes(
    aenv: AdminEnv, linked: Linked
) -> None:
    admin = await caller(aenv)
    linked.fake.failures.append(
        Failure(kind="http", at=frozenset({1}), endpoint="v2/oauth2:revoke", http_status=400)
    )
    async with worker(aenv.env, linked):
        response = await _delete(admin, linked.ctx.user_id)
    assert response.status_code == 204, response.text
    assert "v2/oauth2:revoke" in _revokes(linked)
    target = linked.ctx.user_id
    assert await aenv.env.scalar("SELECT count(*) FROM users WHERE id = :u", u=target) == 0
    assert (await _trail(aenv))[-1] == ("user.delete", "ok", {"revoke": "failed"})


async def test_a_slow_worker_does_not_hold_the_delete_and_web_calls_nothing(
    aenv: AdminEnv, linked: Linked, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No worker runs the kind: the delete waits its bound, makes no provider call, deletes the
    account (the credentials and the queued job go with it) and records the revoke as failed."""
    monkeypatch.setattr(user_revoke, "ADMIN_REVOKE_WAIT_SECONDS", 0.3)
    admin = await caller(aenv)
    target = linked.ctx.user_id
    response = await _delete(admin, target)
    assert response.status_code == 204, response.text
    assert linked.fake.log == []
    assert await aenv.env.scalar("SELECT count(*) FROM users WHERE id = :u", u=target) == 0
    jobs = await aenv.superuser("SELECT count(*) FROM jobs WHERE owner_id = :o", o=target)
    assert jobs.scalar_one() == 0
    assert await _trail(aenv) == [
        ("user.revoke", "ok", {}),
        ("user.revoke_wait", "ok", {}),
        ("user.delete", "ok", {"revoke": "failed"}),
    ]


# --- no connection, and a refused delete -------------------------------------------------------


async def test_without_a_live_connection_nothing_is_queued_or_awaited(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    target = await aenv.env.user("sam@example.com")
    response = await _delete(admin, target, "sam@example.com")
    assert response.status_code == 204, response.text
    assert await _trail(aenv) == [
        ("user.revoke", "ok", {}),
        ("user.delete", "ok", {"revoke": "none"}),
    ]


async def test_a_refused_delete_queues_no_revoke(aenv: AdminEnv, linked: Linked) -> None:
    """The delete's checks run before anything is queued: a wrong address or an admin target
    leaves no job, one ``refused`` row and the credentials."""
    admin = await caller(aenv)
    target = linked.ctx.user_id
    mismatch = await _delete(admin, target, "other@example.com")
    assert (mismatch.status_code, reason(mismatch)) == (409, "email_mismatch")
    own = await _delete(admin, admin.user_id, "admin@example.com")
    assert (own.status_code, reason(own)) == (409, "target_is_admin")
    jobs = await aenv.superuser("SELECT count(*) FROM jobs WHERE kind = :k", k=ADMIN_REVOKE_KIND)
    assert jobs.scalar_one() == 0
    assert (await aenv.superuser(_CREDENTIALS, o=target)).scalar_one() == 1
    rows = [(r.action, r.outcome, r.reason) for r in await aenv.audit_rows()]
    rows = [row for row in rows if row[0].startswith("user.")]
    assert rows == [
        ("user.revoke", "refused", "email_mismatch"),
        ("user.revoke", "refused", "target_is_admin"),
    ]


# --- the definers -------------------------------------------------------------------------------


async def test_the_definer_queues_one_job_naming_the_audit_rows_admin(
    aenv: AdminEnv, linked: Linked
) -> None:
    """The job's admin is the audit row's actor, never a caller's id; one attempt, lane 1,
    origin ``operator``; nothing for an account without a live connection."""
    actor = await aenv.env.user("admin@example.com", role="admin")
    plain = await aenv.env.user("plain@example.com")
    session = uuid4()
    target = linked.ctx.user_id
    with engine_for(aenv.maint_url) as engine, engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ WRITE"))
        for who in (target, plain):
            connection.execute(_AUDIT, {"actor": actor, "target": who, "ip_hash": bytes(32)})
        queued = connection.execute(
            _REVOKE, {"t": target, "e": "disconnect@example.com", "s": str(session)}
        ).one()
        empty = connection.execute(
            _REVOKE, {"t": plain, "e": "plain@example.com", "s": str(session)}
        ).one()
    assert (queued.outcome, empty.outcome, empty.job_id) == ("ok", "ok", None)
    job = (
        await aenv.superuser(
            "SELECT owner_id, kind, origin, lane, state, max_attempts, input FROM jobs "
            "WHERE id = :j",
            j=queued.job_id,
        )
    ).one()
    assert (job.owner_id, job.kind, job.origin, job.lane, job.state, job.max_attempts) == (
        target,
        ADMIN_REVOKE_KIND,
        "operator",
        1,
        "queued",
        1,
    )
    assert job.input == {"admin_id": str(actor), "session_id": str(session)}
    AdminRevokeInput.model_validate(job.input)
    assert linked.fake.log == []


async def test_no_runtime_role_may_queue_an_admin_revoke(
    aenv: AdminEnv, app_database_url: str, linked: Linked
) -> None:
    enqueue = text("SELECT public.queue_enqueue_admin_revoke(:o, :a, :s)")
    params = {"o": linked.ctx.user_id, "a": uuid4(), "s": uuid4()}
    for url in (app_database_url, aenv.maint_url):
        with (
            engine_for(url) as engine,
            engine.connect() as connection,
            pytest.raises(ProgrammingError, match="permission denied"),
        ):
            connection.execute(enqueue, params)
    with engine_for(admin_url()) as engine, engine.connect() as connection:
        # Even its one caller's role: an admin naming the owner is refused.
        connection.execute(text("SET LOCAL ROLE ownvitals_admin_accounts"))
        same = {**params, "a": linked.ctx.user_id}
        with pytest.raises(DBAPIError, match="invalid enqueue"):
            connection.execute(enqueue, same)
        connection.rollback()


# --- the job's re-check of the admin's session ---------------------------------------------------


async def _session(aenv: AdminEnv, user_id: UUID) -> UUID:
    issued = await aenv.env.full_login(user_id)
    await aenv.env.run("UPDATE sessions SET stepup_at = now() WHERE id = :i", i=issued.id)
    return issued.id


async def test_the_job_checks_the_admins_live_step_up(aenv: AdminEnv) -> None:
    admin = await aenv.env.user("admin@example.com", role="admin")
    other = await aenv.env.user("other-admin@example.com", role="admin")
    plain = await aenv.env.user("plain@example.com")
    session = await _session(aenv, admin)
    factory = aenv.env.factory
    assert await admin_stepped_up(factory, admin, session)
    assert not await admin_stepped_up(factory, other, session)  # another admin's session
    plain_session = await _session(aenv, plain)
    assert not await admin_stepped_up(factory, plain, plain_session)  # not an admin
    stale = f"{(ADMIN_STEPUP_WINDOW + JOB_SLACK * 2).total_seconds()} seconds"
    old_mfa = f"{(ADMIN_MFA_WINDOW + JOB_SLACK * 2).total_seconds()} seconds"
    for change, args in (
        (
            "UPDATE sessions SET stepup_at = now() - CAST(:a AS interval) WHERE id = :i",
            {"a": stale},
        ),
        # a future proof is none
        ("UPDATE sessions SET stepup_at = now() + interval '1 minute' WHERE id = :i", {}),
        ("UPDATE sessions SET stepup_at = NULL WHERE id = :i", {}),
        ("UPDATE sessions SET mfa_at = NULL WHERE id = :i", {}),
        ("UPDATE sessions SET mfa_at = now() - CAST(:a AS interval) WHERE id = :i", {"a": old_mfa}),
        ("UPDATE sessions SET expires_at = now() - interval '1 second' WHERE id = :i", {}),
    ):
        fresh = await _session(aenv, admin)
        await aenv.env.run(change, i=fresh, **args)
        assert not await admin_stepped_up(factory, admin, fresh), change
    await aenv.env.run("UPDATE users SET disabled_at = now() WHERE id = :u", u=admin)
    assert not await admin_stepped_up(factory, admin, session)  # a disabled admin


async def _settled(aenv: AdminEnv, owner_id: UUID) -> Any:
    """The owner's one job once it left ``queued`` and ``running`` (at most 10 s)."""
    query = "SELECT state, error_category FROM jobs WHERE owner_id = :o"
    for _ in range(200):
        row = (await aenv.superuser(query, o=owner_id)).one()
        if row.state not in ("queued", "running"):
            return row
        await asyncio.sleep(0.05)
    raise AssertionError("the job did not finish")


async def test_a_job_without_the_admins_step_up_revokes_nothing(
    aenv: AdminEnv, linked: Linked
) -> None:
    """A queued job whose session is not a stepped-up admin's (here the target's own) fails
    ``invalid_input`` before any provider call, and the credentials stay."""
    target = linked.ctx.user_id
    session = await _session(aenv, target)
    body = json.dumps({"admin_id": str(target), "session_id": str(session)})
    await aenv.superuser(
        "INSERT INTO jobs (owner_id, kind, origin, lane, state, input, max_attempts, next_seq) "
        "VALUES (:o, :k, 'operator', 1, 'queued', CAST(:i AS jsonb), 1, 1)",
        o=target,
        k=ADMIN_REVOKE_KIND,
        i=body,
    )
    async with worker(aenv.env, linked):
        state = await _settled(aenv, target)
    assert (state.state, state.error_category) == ("failed", "invalid_input")
    assert _revokes(linked) == []
    assert (await aenv.superuser(_CREDENTIALS, o=target)).scalar_one() == 1
