"""The jobs API: create, get and cancel (ADR 0005 section 2), through the real app on the app role
under RLS, with the test-only kind and the test-only issuer (``jobapi``). No provider is contacted;
synthetic users and inputs only."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text

from jobapi import (  # noqa: F401 - fixtures
    UNKNOWN,
    Api,
    Browser,
    api_fixture,
    bare_app_fixture,
    new_key,
    plant_running,
    until_waiting_on_the_owner_lock,
)
from jobcore import (
    JobsEnv,
    jenv_fixture,  # noqa: F401 - fixtures
)
from jobtools import admin_fixture, engine_for, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.db.queue_locks import JOB_CREATION
from ownvitals.jobs.routes import BODY_MAX_BYTES, canonical_json, jsonb_size
from ownvitals.jobs.sentences import SENTENCES

SHOWN = {
    "id",
    "kind",
    "state",
    "cancel_requested",
    "error_category",
    "error_message",
    "attempt",
    "max_attempts",
    "created_at",
    "updated_at",
    "finished_at",
}  # no input, key, hash, lease, token or worker name


def count(env: JobsEnv, owner: UUID | None = None) -> int:
    if owner is None:
        return env.sql("SELECT count(*) FROM jobs").scalar_one()
    return env.sql("SELECT count(*) FROM jobs WHERE owner_id = :o", o=owner).scalar_one()


async def test_create_queues_a_job_with_its_event(jenv: JobsEnv, api: Api) -> None:
    alice = api.browser()
    response = await alice.create(note="hills")
    assert response.status_code == 201, response.text
    shown = response.json()
    assert set(shown) == SHOWN
    assert (shown["state"], shown["kind"], shown["cancel_requested"], shown["attempt"]) == (
        "queued",
        "test_kind",
        False,
        0,
    )
    assert (shown["error_category"], shown["error_message"]) == (None, None)
    job_id = UUID(shown["id"])
    row = jenv.row(job_id)
    assert (row.owner_id, row.attempt, row.next_seq, row.lease_version) == (alice.user_id, 0, 1, 0)
    assert row.input == {"note": "hills"}
    assert jenv.events(job_id) == [(1, "queued", {}, 0)]
    got = await alice.get(f"/api/jobs/{job_id}")
    assert (got.status_code, got.json()) == (200, shown)


async def test_the_input_is_validated_by_the_kind_and_stored_canonically(
    jenv: JobsEnv, api: Api
) -> None:
    alice = api.browser()
    response = await alice.create()  # the model's default
    assert response.status_code == 201, response.text
    row = jenv.row(UUID(response.json()["id"]))
    assert row.input == {"note": "fine"}
    body = {"kind": "test_kind", "input": row.input}
    assert bytes(row.request_hash) == hashlib.sha256(canonical_json(body)).digest()


async def test_idempotency_same_key_same_body_other_body_and_the_limit(
    jenv: JobsEnv, api: Api
) -> None:
    alice = api.browser()
    key = new_key()
    first = await alice.create(key, note="a")
    replay = await alice.create(key, note="a")
    assert (first.status_code, replay.status_code) == (201, 200)
    assert replay.json() == first.json()
    other = await alice.create(key, note="b")
    assert (other.status_code, other.json()) == (
        409,
        {"detail": {"reason": "idempotency_mismatch"}},
    )
    second = await alice.create(new_key(), note="a")
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]
    third = await alice.create(new_key(), note="a")  # the per-user limit (2) is reached
    assert (third.status_code, third.json()) == (429, {"detail": {"reason": "busy", "limit": 2}})
    assert count(jenv, alice.user_id) == 2
    # A replay still answers once the limit is reached, and writes nothing.
    assert (await alice.create(key, note="a")).status_code == 200
    assert count(jenv, alice.user_id) == 2
    # Another user has a limit of their own and may use the same key.
    bob = api.browser()
    assert (await bob.create(key, note="a")).status_code == 201


async def test_concurrent_creates_with_one_key_make_one_job(jenv: JobsEnv, api: Api) -> None:
    alice = api.browser()
    key = new_key()
    responses = await asyncio.gather(*(alice.create(key) for _ in range(5)))
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 200, 201]
    assert len({r.json()["id"] for r in responses}) == 1
    assert count(jenv, alice.user_id) == 1


async def test_concurrent_creates_never_pass_the_limit(jenv: JobsEnv, api: Api) -> None:
    alice = api.browser()
    responses = await asyncio.gather(*(alice.create() for _ in range(6)))
    assert sorted(r.status_code for r in responses) == [201, 201, 429, 429, 429, 429]
    assert count(jenv, alice.user_id) == 2


async def test_create_counts_the_limit_only_after_the_owner_lock(
    jenv: JobsEnv, api: Api, database_url: str
) -> None:
    """While another transaction holds the owner's job-creation lock (ADR 0005 section 9), a
    create waits; a job that transaction commits is then counted."""
    alice = api.browser()
    plant_running(jenv, alice.user_id)
    with engine_for(database_url) as engine, engine.connect() as holder:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:owner))"),
            {"ns": JOB_CREATION, "owner": str(alice.user_id)},
        )
        task = asyncio.create_task(alice.create())
        await until_waiting_on_the_owner_lock(jenv)
        assert not task.done()
        plant_running(jenv, alice.user_id)  # the limit (2) is now reached
        holder.commit()
        response = await task
    assert (response.status_code, response.json()) == (
        429,
        {"detail": {"reason": "busy", "limit": 2}},
    )


async def test_header_and_body_refusals_echo_nothing(jenv: JobsEnv, api: Api) -> None:
    alice = api.browser()
    body = alice.body()
    missing = await alice.post("/api/jobs", body)
    assert (missing.status_code, missing.json()["detail"]["reason"]) == (
        400,
        "idempotency_key_missing",
    )
    for bad in ("short", "x" * 65, "has space in it!!", "dots.are.not.allowed.here"):
        response = await alice.post("/api/jobs", body, {"Idempotency-Key": bad})
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "idempotency_key_invalid", bad
    unknown = await alice.post(
        "/api/jobs", {**body, "kind": "nope"}, {"Idempotency-Key": new_key()}
    )
    assert (unknown.status_code, unknown.json()) == (422, {"detail": {"reason": "unknown_kind"}})
    planted = "PLANTED-VALUE-iiiiiiiiiiiiiiii"
    for broken in (
        {**body, "extra": planted},
        {"kind": "test_kind", "input": {"note": 7, "zzz": planted}},
        {"kind": "test_kind", "input": planted},
        {"kind": planted, "input": {}},
        {"kind": "test_kind"},
        [planted],
    ):
        response = await alice.post("/api/jobs", broken, {"Idempotency-Key": new_key()})
        assert response.status_code == 422, response.text
        assert planted not in response.text
        assert response.json()["detail"]["reason"] == "invalid_input"
    # An extra key's name is the client's text too: its error points at the parent.
    secret_key = "PLANTED-KEY-kkkkkkkkkkkkkkkk"
    for broken in (
        {**body, secret_key: 1},
        {"kind": "test_kind", "input": {secret_key: 1}},  # the kind's model
    ):
        response = await alice.post("/api/jobs", broken, {"Idempotency-Key": new_key()})
        assert response.status_code == 422, response.text
        assert secret_key not in response.text
        errors = response.json()["detail"]["errors"]
        assert any(e["type"] == "extra_forbidden" for e in errors)
    not_json = await alice.client.post(
        "/api/jobs",
        content=b"{not json " + planted.encode(),
        headers=alice.headers({"Idempotency-Key": new_key(), "Content-Type": "application/json"}),
    )
    assert not_json.status_code == 422
    assert planted not in not_json.text
    assert count(jenv) == 0


async def test_an_input_over_16_kib_is_too_large(jenv: JobsEnv, api: Api) -> None:
    alice = api.browser()
    response = await alice.create(note="x" * 16400)
    assert (response.status_code, response.json()) == (422, {"detail": {"reason": "too_large"}})
    assert count(jenv) == 0


async def test_malformed_ids_answer_like_unknown_ones(jenv: JobsEnv, api: Api) -> None:
    alice = api.browser()
    planted = "PLANTED-VALUE-pppppppppppppppp"
    for response in (
        await alice.get(f"/api/jobs/{planted}"),
        await alice.post(f"/api/jobs/{planted}/cancel"),
        await alice.get(f"/api/jobs/{UNKNOWN}"),
        await alice.post(f"/api/jobs/{UNKNOWN}/cancel"),
    ):
        assert (response.status_code, response.json()) == (
            404,
            {"detail": {"reason": "not_found"}},
        )
        assert planted not in response.text


async def test_another_users_job_answers_exactly_like_an_unknown_id(
    jenv: JobsEnv, api: Api
) -> None:
    alice, bob = api.browser(), api.browser()
    bobs_job = (await bob.create()).json()["id"]
    before = jenv.row(UUID(bobs_job))
    for theirs, nothing in (
        (await alice.get(f"/api/jobs/{bobs_job}"), await alice.get(f"/api/jobs/{UNKNOWN}")),
        (
            await alice.post(f"/api/jobs/{bobs_job}/cancel"),
            await alice.post(f"/api/jobs/{UNKNOWN}/cancel"),
        ),
        (
            await alice.get(f"/api/jobs/{bobs_job}/events"),
            await alice.get(f"/api/jobs/{UNKNOWN}/events"),
        ),
    ):
        assert (theirs.status_code, theirs.text) == (nothing.status_code, nothing.text)
        assert theirs.json() == {"detail": {"reason": "not_found"}}
    after = jenv.row(UUID(bobs_job))
    assert (after.state, after.cancel_requested_at, after.next_seq) == ("queued", None, 1)
    assert before.updated_at == after.updated_at


async def test_cancel_queued_running_and_ended_jobs(jenv: JobsEnv, api: Api) -> None:
    alice = api.browser()
    queued = (await alice.create()).json()["id"]
    done = await alice.post(f"/api/jobs/{queued}/cancel")
    again = await alice.post(f"/api/jobs/{queued}/cancel")
    assert (done.status_code, done.json()["state"]) == (200, "cancelled")
    assert done.json()["finished_at"] is not None
    assert (again.status_code, again.json()["state"]) == (200, "cancelled")
    assert jenv.kinds(UUID(queued)) == ["queued", "cancelled"]

    running = plant_running(jenv, alice.user_id)
    requested = await alice.post(f"/api/jobs/{running}/cancel")
    assert requested.status_code == 200
    assert (requested.json()["state"], requested.json()["cancel_requested"]) == ("running", True)
    assert (await alice.post(f"/api/jobs/{running}/cancel")).status_code == 200
    assert jenv.kinds(running) == ["cancel_requested"]  # no second event
    assert jenv.events(running)[0][3] == 0  # a user event carries lease_version 0

    jenv.sql(
        "UPDATE jobs SET state = 'completed', finished_at = now(), leased_by = NULL, "
        "lease_expires_at = NULL, heartbeat_at = NULL WHERE id = :i",
        i=running,
    )
    ended = await alice.post(f"/api/jobs/{running}/cancel")
    assert (ended.status_code, ended.json()) == (409, {"detail": {"reason": "not_cancellable"}})


async def test_a_failed_job_shows_its_category_and_one_plain_sentence(
    jenv: JobsEnv, api: Api
) -> None:
    alice = api.browser()
    _, job_id = jenv.job(
        alice.user_id,
        state="failed",
        error_category="upstream_unavailable",
        finished_at=datetime.now(UTC),
    )
    shown = (await alice.get(f"/api/jobs/{job_id}")).json()
    assert shown["error_category"] == "upstream_unavailable"
    assert shown["error_message"] == SENTENCES["upstream_unavailable"]


async def test_a_body_over_the_cap_is_refused_before_it_is_read(jenv: JobsEnv, api: Api) -> None:
    alice = api.browser()
    huge = alice.body(note="x" * BODY_MAX_BYTES)
    response = await alice.post("/api/jobs", huge, {"Idempotency-Key": new_key()})
    assert (response.status_code, response.json()) == (413, {"detail": {"reason": "too_large"}})
    assert count(jenv) == 0


@pytest.mark.parametrize("oversized", [False, True])
async def test_a_chunked_body_without_a_length_is_capped_while_it_is_read(
    jenv: JobsEnv, api: Api, oversized: bool
) -> None:
    alice = api.browser()
    note = "x" * BODY_MAX_BYTES if oversized else "hills"
    payload = canonical_json(alice.body(note=note))

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(payload), 4096):
            yield payload[start : start + 4096]

    headers = alice.headers({"Idempotency-Key": new_key(), "Content-Type": "application/json"})
    response = await alice.client.post("/api/jobs", headers=headers, content=chunks())
    assert "content-length" not in response.request.headers
    assert response.request.headers["transfer-encoding"] == "chunked"
    if oversized:
        assert (response.status_code, response.json()) == (413, {"detail": {"reason": "too_large"}})
        assert count(jenv) == 0
    else:
        assert response.status_code == 201, response.text


def test_jsonb_size_measures_as_jsonb_prints() -> None:
    value = {"a": [1, 2], "b": "å"}
    assert jsonb_size(value) == len('{"a": [1, 2], "b": "å"}'.encode())
    assert canonical_json(value) == json.dumps(value, sort_keys=True, separators=(",", ":"),
                                               ensure_ascii=False).encode()  # fmt: skip


async def test_without_a_session_every_route_answers_401(
    jenv: JobsEnv, bare_app: httpx.AsyncClient
) -> None:
    """The app as shipped (#10 replaced #77's fail-closed seam with ``ownvitals.auth``): without
    a session nobody is signed in, whatever is sent."""
    _, job_id = jenv.job()
    key = {"Idempotency-Key": new_key(), "X-Test-User": str(jenv.row(job_id).owner_id)}
    for response in (
        await bare_app.post("/api/jobs", json=Browser.body(), headers=key),
        await bare_app.get(f"/api/jobs/{job_id}", headers=key),
        await bare_app.post(f"/api/jobs/{job_id}/cancel", headers=key),
        await bare_app.get(f"/api/jobs/{job_id}/events", headers=key),
    ):
        assert (response.status_code, response.json()) == (
            401,
            {"detail": {"reason": "not_authenticated"}},
        )
    assert jenv.row(job_id).cancel_requested_at is None
    assert count(jenv) == 1


async def test_the_shipped_registry_accepts_no_kind(jenv: JobsEnv, api: Api) -> None:
    """Pins the other default: #9 registers no kind, so the API creates nothing."""
    from ownvitals.jobs.routes import job_kinds

    del api.app.dependency_overrides[job_kinds]
    response = await api.browser().create()
    assert (response.status_code, response.json()) == (422, {"detail": {"reason": "unknown_kind"}})
    assert count(jenv) == 0
