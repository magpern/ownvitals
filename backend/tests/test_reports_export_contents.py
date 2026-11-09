"""What the export holds (ADR 0013 sections 1 and 2, ADR 0006 sections 9 and 11; #211), built by
the worker's runner in the requesting user's context and downloaded through the API.

- **Isolation**: two synthetic users with a row in every owned table (``contexttools.plant``)
  and their own ``users`` and ``auth_events`` rows; user A's archive holds none of B's row ids,
  B's id or B's email, and every row in it is A's.
- **The allowlist**: every allowlisted column of every exported table is in its JSON objects and
  its CSV header, no column outside it is, and no planted secret (password hash, the sealed
  token's key id, the filler of every hash column, a session's token hash) appears in any file.
- **Tiers**: without the experimental switch the experimental findings and the estimates are
  absent and ``manifest.json`` lists them as withheld; with it both are in, the estimates in
  their own labelled file. Every finding the read function gives the ``export`` consumer is in,
  a hidden one with ``hidden_gate_failed``.

Synthetic accounts only; no real data.
"""

import base64
import csv
import io
import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from authtools import Env, auth_schema_fixture, clean_auth_fixture, running_env  # noqa: F401
from contexttools import ROWS, SYNTHETIC_HASH, checked, plant
from exporttools import (  # noqa: F401 - fixture
    admin_run,
    built_export,
    download,
    exporter,
    manifest,
    open_archive,
    request_export,
    run_build,
    xenv_fixture,
)
from jobtools import admin_url, engine_for
from ownvitals.auth.exports import ACCOUNT_COLUMNS
from ownvitals.reports.export_allowlist import (
    ESTIMATES_LABEL,
    GENERIC,
    NOT_EXPORTED_COLUMNS,
    SPECIAL_COLUMNS,
    WITHHELD_REASON,
)
from test_views_findings import plant_finding, plant_group, plant_recording
from viewtools import connected_with_streams

SYNTHETIC = True
# Planted in the connections' provider bookkeeping (``public``), which is not exported.
SECRET_KEY_ID = "synthetic-secret-marker-0001"


def _owned_ids(owner: UUID) -> set[str]:
    """Every row id of ``owner`` in every owned table (as the superuser)."""
    found: set[str] = set()
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        for table in ROWS:
            ids = conn.execute(
                text(f"SELECT id::text FROM public.{checked(table)} WHERE owner_id = :o"),  # noqa: S608
                {"o": owner},
            ).scalars()
            found.update(ids)
    return found


async def _archive(env: Env, browser: Any, app_database_url: str) -> dict[str, bytes]:
    export_id = await built_export(env, browser, app_database_url)
    response = await download(env, browser, export_id)
    assert response.status_code == 200, response.text
    return open_archive(response.content)


def _json(files: dict[str, bytes], stem: str) -> list[dict[str, Any]]:
    return json.loads(files[f"data/{stem}.json"])


def _csv(files: dict[str, bytes], stem: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(files[f"csv/{stem}.csv"].decode())))


async def _two(env: Env, app_database_url: str) -> tuple[Any, UUID, Any, UUID]:
    a = await exporter(env, "synthetic-export-a@example.com")
    b = await exporter(env, "synthetic-export-b@example.com")
    a_id = UUID(
        str(await env.scalar("SELECT id FROM users WHERE email LIKE 'synthetic-export-a%'"))
    )
    b_id = UUID(
        str(await env.scalar("SELECT id FROM users WHERE email LIKE 'synthetic-export-b%'"))
    )
    for owner in (a_id, b_id):
        plant(admin_url(), owner)
        admin_run(
            "UPDATE connections SET "
            "public = public || jsonb_build_object('synthetic_marker', CAST(:k AS text)) "
            "WHERE owner_id = :o",
            k=SECRET_KEY_ID,
            o=owner,
        )
    return a, a_id, b, b_id


async def test_user_a_export_holds_no_row_of_user_b(xenv: Env, app_database_url: str) -> None:
    a, a_id, b, b_id = await _two(xenv, app_database_url)
    b_ids = _owned_ids(b_id)
    assert len(b_ids) >= len(ROWS)
    files = await _archive(xenv, a, app_database_url)
    blob = b"\n".join(files.values()).decode("utf-8")
    for value in (*b_ids, str(b_id), "synthetic-export-b@example.com"):
        assert value not in blob, value
    assert manifest(files)["account_id"] == str(a_id)
    assert _json(files, "account")[0]["email"] == "synthetic-export-a@example.com"
    # Only A's security events (they hold no id, so they are counted against A's own rows,
    # without the two the build and the download wrote after the archive was made).
    a_events = await xenv.scalar(
        "SELECT count(*) FROM auth_events WHERE user_id = :u "
        "AND kind NOT IN ('export_ready', 'export_downloaded')",
        u=a_id,
    )
    b_events = await xenv.scalar("SELECT count(*) FROM auth_events WHERE user_id = :u", u=b_id)
    assert a_events
    assert b_events
    assert len(_json(files, "auth_events")) == a_events
    # Every exported row is A's: A's own planted ids are there, table by table.
    a_ids = _owned_ids(a_id)
    for spec in GENERIC:
        for row in _json(files, spec.file):
            assert row["id"] in a_ids, spec.file
    # The other way round, too.
    files_b = await _archive(xenv, b, app_database_url)
    blob_b = b"\n".join(files_b.values()).decode("utf-8")
    for value in (*(a_ids - {str(a_id)}), str(a_id), "synthetic-export-a@example.com"):
        assert value not in blob_b, value


async def test_the_archive_holds_exactly_the_allowlisted_columns(
    xenv: Env, app_database_url: str
) -> None:
    a, _, _, _ = await _two(xenv, app_database_url)
    files = await _archive(xenv, a, app_database_url)
    listed = {entry["path"] for entry in manifest(files)["files"]}
    assert listed | {"manifest.json"} == set(files)
    for entry in manifest(files)["files"]:
        import hashlib

        assert hashlib.sha256(files[entry["path"]]).hexdigest() == entry["sha256"]
    for spec in GENERIC:
        rows = _json(files, spec.file)
        assert rows, spec.file  # the planted row
        columns = set(spec.columns) | ({"value"} if spec.real_value else set())
        assert all(set(row) == columns for row in rows), spec.file
        header = _csv(files, spec.file)[0]
        assert set(header) == columns, spec.file
        assert not set(header) & set(NOT_EXPORTED_COLUMNS[spec.table]), spec.file
    recordings = _json(files, "ecg_recordings")
    assert recordings
    assert set(recordings[0]) == set(SPECIAL_COLUMNS["ecg_recordings"])
    assert set(_json(files, "account")[0]) == set(ACCOUNT_COLUMNS["users"])
    events = _json(files, "auth_events")
    assert events
    assert all(set(e) == set(ACCOUNT_COLUMNS["auth_events"]) for e in events)
    blob = b"\n".join(files.values()).decode("utf-8")
    for secret in (
        SECRET_KEY_ID,
        "$argon2",  # the password hash
        SYNTHETIC_HASH.hex(),
        base64.b64encode(SYNTHETIC_HASH).decode(),
        "input_fingerprint",
        "payload_hash",
        "password_hash",
        "token_hash",
    ):
        assert secret not in blob, secret
    session_hash = await xenv.scalar("SELECT token_hash FROM sessions LIMIT 1")
    assert isinstance(session_hash, bytes)
    assert session_hash.hex() not in blob
    assert base64.b64encode(session_hash).decode() not in blob


# --- tiers -------------------------------------------------------------------------------------


@pytest.fixture(name="switch")
def switch_fixture(request: pytest.FixtureRequest) -> bool:
    return bool(request.param)


@pytest.fixture(name="tier_env")
async def tier_env_fixture(
    clean_auth: None, app_database_url: str, switch: bool
) -> AsyncIterator[Env]:
    admin_run("DELETE FROM app_settings WHERE flag = 'experimental_findings'")
    async with running_env(app_database_url, experimental_findings=switch) as running:
        yield running


def _plant_tiers(owner: UUID) -> dict[str, UUID]:
    connection = connected_with_streams(owner)
    hidden_group = 900_001
    plant_group(owner, connection, hidden_group, minutes=0)
    plant_recording(owner, connection, hidden_group, minutes=0, result="fail")
    # A stored HIS (experimental) and a readiness score (trend): the HIS stays stored after the
    # switch goes off until the next score pass, so the export must filter it itself.
    for kind in ("his", "readiness"):
        admin_run(
            "INSERT INTO scores (owner_id, kind, day, value, components, algo_version, "
            "input_revision, input_fingerprint) VALUES (:o, :k, DATE '2026-11-01', 50, "
            "CAST('{\"status\": \"computed\"}' AS jsonb), 'synthetic/1', 0, :h)",
            o=owner,
            k=kind,
            h=SYNTHETIC_HASH,
        )
    return {
        "C1": plant_finding(owner, "C1", tier="trend"),
        "B1": plant_finding(owner, "B1", tier="experimental", metric_keys=("weight",)),
        "E4": plant_finding(owner, "E4"),
        "E6": plant_finding(owner, "E6", [hidden_group]),
    }


@pytest.mark.parametrize("switch", [False, True], indirect=True)
async def test_experimental_items_follow_the_switch_and_hidden_ones_are_marked(
    tier_env: Env, app_database_url: str, switch: bool
) -> None:
    browser = await exporter(tier_env)
    owner = UUID(str(await tier_env.scalar("SELECT id FROM users")))
    planted = _plant_tiers(owner)
    export_id = (await request_export(browser)).json()["export"]["id"]
    assert await run_build(app_database_url, experimental_findings=switch) == "completed"
    response = await download(tier_env, browser, export_id)
    assert response.status_code == 200, response.text
    files = open_archive(response.content)
    found = {row["id"]: row for row in _json(files, "findings")}
    states = {
        rule: found[str(i)]["overlay_state"] for rule, i in planted.items() if str(i) in found
    }
    shown = manifest(files)
    kinds = sorted(row["kind"] for row in _json(files, "scores"))
    csv_kinds = sorted(row[1] for row in _csv(files, "scores")[1:])
    assert csv_kinds == kinds
    if switch:
        assert kinds == ["his", "readiness"]
        assert set(states) == {"C1", "B1", "E4", "E6"}
        assert shown["withheld"] == []
        estimates = _json(files, "ecg_estimates")
        assert estimates
        assert all(row["source"] == ESTIMATES_LABEL for row in estimates)
        entry = next(f for f in shown["files"] if f["path"] == "data/ecg_estimates.json")
        assert entry["label"] == ESTIMATES_LABEL
    else:
        assert set(states) == {"C1", "E6"}  # absent, not redacted
        assert kinds == ["readiness"]  # the stored HIS is absent without the switch
        assert shown["withheld"] == [
            {"item": "ecg_estimates", "reason": WITHHELD_REASON},
            {"item": "experimental_findings", "reason": WITHHELD_REASON},
            {"item": "experimental_scores", "reason": WITHHELD_REASON},
        ]
        assert "data/ecg_estimates.json" not in files
    assert states["C1"] == "shown"
    assert states["E6"] == "hidden_gate_failed"  # hidden on screen, present in the export
