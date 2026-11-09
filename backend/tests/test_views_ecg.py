"""``/api/ecg`` in the web-request context: signed-in synthetic users over the app role, forced
RLS. Another user's recording answers 404, Withings' values and our estimate stay apart, the PDF
never carries an estimate even with the owner switch on, and the signal is never logged.
Synthetic data only.
"""

import json
import logging
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    Env,
    auth_schema_fixture,
    clean_auth_fixture,
    running_env,
)
from flows import Browser, reason, signed_in
from ownvitals.ecg.validate_cli import input_fingerprint
from ownvitals.ingest.codec import frame_signal
from pdftools import text as pdf_text
from test_views_findings import (
    BASE,
    DEVICE,
    RATE,
    _conn,
    estimate,
    plant_group,
    plant_user,
    run,
    store_flag,
)

SYNTHETIC = True
MARKER = 31337  # a sample value that must never reach a log line
LEADS = [[MARKER + (i % 7) for i in range(RATE * 2)]]


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


def plant(owner: UUID, connection: UUID, signalid: int, *, minutes: int = 0, signal: bool = True):
    """A synthetic recording with Withings' own values and a current synthetic estimate."""
    frame, digest = frame_signal(LEADS) if signal else (None, None)
    est = estimate(signalid, "pass", input_fingerprint=input_fingerprint(signalid, digest, RATE))
    with _conn() as engine, engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO ecg_recordings (owner_id, connection_id, signalid, recorded_at, "
                "source_tz, model_id, afib, afib_details, heart_rate, sampling_hz, wearposition, "
                "signal, payload_hash, reported, estimated) VALUES (:o, :c, :s, :at, "
                "'Europe/Stockholm', 91, 1, CAST(:d AS jsonb), 64, :r, 2, :sig, :h, "
                "CAST(:rep AS jsonb), CAST(:est AS jsonb)) RETURNING id"
            ),
            {
                "o": owner,
                "c": connection,
                "s": signalid,
                "at": BASE.replace(minute=minutes),
                "d": json.dumps({"code": 1}),
                "r": RATE,
                "sig": frame,
                "h": digest,
                "rep": json.dumps({"deviceid": DEVICE}),
                "est": json.dumps(est),
            },
        ).scalar_one()


async def test_anonymous_and_half_signed_in_requests_are_refused(env: Env) -> None:
    anonymous = Browser(env)
    response = await anonymous.get("/api/ecg")
    assert (response.status_code, reason(response)) == (401, "not_authenticated")
    half = await signed_in(env, "half@example.com", second_factor=False)
    assert (await half.get(f"/api/ecg/{uuid4()}")).status_code == 403
    assert (await half.get(f"/api/ecg/{uuid4()}.pdf")).status_code == 403


async def test_a_user_sees_only_their_own_recordings_and_the_signal(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    bob, bob_id = await _user(env, "bob@example.com")
    mine = plant(alice_id, plant_user(alice_id), 1)
    theirs = plant(bob_id, plant_user(bob_id), 2)
    listed = await alice.get("/api/ecg")
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    assert [r["id"] for r in listed.json()["recordings"]] == [str(mine)]
    assert [r["id"] for r in (await bob.get("/api/ecg")).json()["recordings"]] == [str(theirs)]
    for path in (f"/api/ecg/{theirs}", f"/api/ecg/{theirs}.pdf", f"/api/ecg/{uuid4()}"):
        assert (await alice.get(path)).status_code == 404
    body = (await alice.get(f"/api/ecg/{mine}")).json()
    assert body["signal"]["leads"] == LEADS
    assert body["signal"]["unit"] == "uV"
    assert body["signal"]["sampling_hz"] == RATE
    assert body["signal"]["lead_labels"] == ["lead unverified"]
    assert body["recording"]["classification"]["source"] == "withings"
    assert body["recording"]["heart_rate_bpm"] == 64
    assert body["recording"]["source_tz"] == "Europe/Stockholm"
    assert body["wear_position"] == 2


async def test_withings_intervals_are_linked_to_the_recording(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    connection = plant_user(alice_id)
    plant_group(alice_id, connection, 11, minutes=0, types=(135, 138))
    run("UPDATE measures SET value_raw = 95 WHERE type = 135 AND owner_id = :o", o=alice_id)
    plant(alice_id, connection, 1)
    (row,) = (await alice.get("/api/ecg")).json()["recordings"]
    shown = {i["key"]: i for i in row["intervals"]}
    assert shown["qrs_duration"]["value"] == 95
    assert shown["qrs_duration"]["source"] == "withings"
    assert "pr_interval" not in shown  # not reported: absent, never zero


async def test_the_estimate_is_absent_without_the_flag(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    mine = plant(alice_id, plant_user(alice_id), 1)
    off = (await alice.get(f"/api/ecg/{mine}")).json()
    assert off["ownvitals_estimate"] is None
    assert "hr_bpm" not in json.dumps(off["recording"])


async def test_the_estimate_shows_in_its_own_section_with_the_flag(owner_env: Env) -> None:
    alice, alice_id = await _user(owner_env, "alice@example.com")
    mine = plant(alice_id, plant_user(alice_id), 1)
    on = (await alice.get(f"/api/ecg/{mine}")).json()
    assert on["ownvitals_estimate"]["source"] == "ownvitals_estimate"
    assert on["ownvitals_estimate"]["label_key"] == "ecg.estimate.label"
    assert "ownvitals_estimate" not in on["recording"]


async def test_the_pdf_never_carries_an_estimate_even_with_the_flag(owner_env: Env) -> None:
    alice, alice_id = await _user(owner_env, "alice@example.com")
    mine = plant(alice_id, plant_user(alice_id), 1)
    response = await alice.get(f"/api/ecg/{mine}.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF")
    shown = pdf_text(response.content)
    joined = shown.lower()
    assert "not a medical device" in joined
    assert "61.5" not in joined  # the synthetic estimate's heart rate
    assert "estimat" not in joined
    assert "ownvitals estimate" not in joined


async def test_a_recording_without_a_signal_has_no_pdf(env: Env) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    mine = plant(alice_id, plant_user(alice_id), 1, signal=False)
    detail = (await alice.get(f"/api/ecg/{mine}")).json()
    assert detail["signal"] is None
    assert detail["recording"]["has_signal"] is False
    assert (await alice.get(f"/api/ecg/{mine}.pdf")).status_code == 404


async def test_paging_validation(env: Env) -> None:
    alice, _ = await _user(env, "alice@example.com")
    assert (await alice.get("/api/ecg?limit=0")).status_code == 422
    assert (await alice.get("/api/ecg?offset=-1")).status_code == 422
    assert (await alice.get("/api/ecg/not-a-uuid")).status_code == 422


async def test_the_signal_is_never_logged(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    alice, alice_id = await _user(env, "alice@example.com")
    mine = plant(alice_id, plant_user(alice_id), 1)
    with caplog.at_level(logging.DEBUG):
        await alice.get("/api/ecg")
        await alice.get(f"/api/ecg/{mine}")
        await alice.get(f"/api/ecg/{mine}.pdf")
    assert str(MARKER) not in caplog.text
