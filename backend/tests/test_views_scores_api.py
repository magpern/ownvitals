"""``GET /api/scores`` in the web-request context (#188, ADR 0012 §5): signed-in synthetic users
over the app role, forced RLS. Auth, the shape, another user's rows, no ``his`` entry without the
owner-only switch even when one is stored, and the stale state. Synthetic data only.
"""

import json
from collections.abc import AsyncIterator
from uuid import UUID

import pytest

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    running_env,
)
from flows import Browser, reason, signed_in, signed_in_owing_code
from ownvitals.analysis.scores.his_table import REGISTERED
from ownvitals.derived.scores_revision import revision_digest
from test_views_findings import run, store_flag

SYNTHETIC = True
DAY = "2026-11-01"


@pytest.fixture(name="env")
async def env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    store_flag(None)
    async with running_env(app_database_url) as running:
        yield running


@pytest.fixture(name="owner_env")
async def owner_env_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    store_flag(None)
    async with running_env(app_database_url, experimental_findings=True) as running:
        yield running


async def _user(env: Env, email: str) -> tuple[Browser, UUID]:
    browser = await signed_in(env, email)
    found = await env.scalar("SELECT id FROM users WHERE email_normalized = :e", e=email)
    assert isinstance(found, UUID)
    return browser, found


def plant_score(
    owner: UUID,
    kind: str,
    *,
    value: float | None = 61.0,
    revision: int = revision_digest((1, 0, 0)),
    status: str = "computed",
) -> None:
    run(
        "INSERT INTO scores (owner_id, kind, day, value, components, algo_version, "
        "input_revision, input_fingerprint) VALUES (:o, :k, :d, :v, CAST(:c AS jsonb), 'x/1', :r, "
        "decode(repeat('00', 32), 'hex'))",
        o=owner,
        k=kind,
        d=DAY,
        v=value,
        c=json.dumps({"status": status}),
        r=revision,
    )


def plant_revision(owner: UUID, revision: int) -> None:
    run(
        "INSERT INTO data_revisions (owner_id, stream, revision) VALUES (:o, 'measures', :r)",
        o=owner,
        r=revision,
    )


def kinds(response: object) -> list[str]:
    return sorted(s["kind"] for s in response.json()["scores"])  # type: ignore[attr-defined]


async def test_anonymous_and_half_signed_in_requests_are_refused(env: Env) -> None:
    response = await Browser(env).get("/api/scores")
    assert (response.status_code, reason(response)) == (401, "not_authenticated")
    half = await signed_in_owing_code(env, "half@example.com")  # #475: owes its TOTP code
    assert (await half.get("/api/scores")).status_code == 403


async def test_a_user_sees_only_their_own_scores_and_the_shape(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    _, bob_id = await _user(env, "bob@example.com")
    plant_score(alice_id, "readiness", value=72.0)
    plant_score(bob_id, "body_path:weight", value=40.0)
    plant_revision(alice_id, 1)
    response = await alice.get("/api/scores")
    assert response.status_code == 200
    (row,) = response.json()["scores"]
    assert (row["kind"], row["day"], row["status"], row["value"]) == (
        "readiness", DAY, "computed", 72.0,
    )  # fmt: skip
    assert (row["tier"], row["stale"]) == ("trend", False)


async def test_his_is_absent_without_the_switch_even_when_stored(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    plant_score(alice_id, "readiness")
    plant_score(alice_id, "his", value=None, status="not_set")
    assert kinds(await alice.get("/api/scores")) == ["readiness"]  # absent, no placeholder


async def test_his_is_listed_with_the_switch(owner_env: Env) -> None:
    alice, alice_id = await _user(owner_env, "alice@example.com")
    plant_score(alice_id, "his", value=None, status="not_set")
    response = await alice.get("/api/scores")
    assert kinds(response) == ["his"]
    (row,) = response.json()["scores"]
    assert (row["tier"], row["value"], row["status"]) == ("experimental", None, "not_set")


async def test_a_row_is_stale_when_the_data_revision_moved_on(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    plant_score(alice_id, "readiness", revision=revision_digest((1, 0, 0)))
    plant_revision(alice_id, 2)  # an ingest or the purge bumped the stream after the pass
    (row,) = (await alice.get("/api/scores")).json()["scores"]
    assert row["stale"] is True


async def test_a_his_row_carries_its_provenance_for_the_panel(owner_env: Env) -> None:
    alice, alice_id = await _user(owner_env, "alice@example.com")
    plant_score(alice_id, "his", value=None, status="not_set")
    run(
        "UPDATE scores SET components = components || CAST(:c AS jsonb) WHERE owner_id = :o",
        o=alice_id,
        c=json.dumps({"table": {"id": "withings_card_v1", "version": "1", "source": "s"}}),
    )
    (row,) = (await alice.get("/api/scores")).json()["scores"]
    assert row["input_revision"] == str(revision_digest((1, 0, 0)))
    assert row["input_fingerprint"] == "00" * 32
    assert row["table_sha"] == REGISTERED["withings_card_v1"]
