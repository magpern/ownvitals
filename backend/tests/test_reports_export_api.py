"""Requesting and downloading the export (ADR 0013 sections 4 and 5, ADR 0003 section 4; #211) on
a real database, through the API, as the app role under forced RLS. The build runs as the worker
runs it. Synthetic accounts only; no real data.
"""

import logging
from typing import Any
from uuid import UUID

import pytest

from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from contexttools import plant
from ctx import make_context
from exporttools import (  # noqa: F401 - fixture
    admin_row,
    admin_run,
    admin_scalar,
    built_export,
    download,
    exporter,
    open_archive,
    request_export,
    run_build,
    xenv_fixture,
)
from flows import age_reauth, reason
from jobtools import admin_url
from ownvitals.reports import export, export_archive, export_store

SYNTHETIC = True


async def _events(env: Env, kind: str) -> list[dict[str, Any]]:
    async with env.db() as db:
        from sqlalchemy import text

        rows = await db.execute(
            text("SELECT detail FROM auth_events WHERE kind = :k ORDER BY at, id"), {"k": kind}
        )
        return [dict(r.detail) for r in rows]


async def _age_stepup(env: Env) -> None:
    await env.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")


# --- the request ---------------------------------------------------------------------------------


async def test_the_request_needs_recent_authentication(xenv: Env) -> None:
    browser = await exporter(xenv)
    await age_reauth(xenv)
    response = await request_export(browser)
    assert response.status_code == 403
    assert reason(response) == "reauth_required"
    assert admin_scalar("SELECT count(*) FROM exports") == 0


async def test_the_request_needs_a_step_up(xenv: Env) -> None:
    browser = await exporter(xenv)
    await _age_stepup(xenv)
    response = await request_export(browser)
    assert response.status_code == 403
    assert reason(response) == "step_up_required"
    assert admin_scalar("SELECT count(*) FROM jobs") == 0


async def test_the_request_is_accepted_within_both_windows(xenv: Env) -> None:
    browser = await exporter(xenv)
    response = await request_export(browser)
    assert response.status_code == 202, response.text
    export = response.json()["export"]
    assert export["state"] == "building"
    assert set(export) == {
        "id", "state", "created_at", "ready_at", "expires_at", "size", "failure", "largest",
        "attempts_left",
    }  # fmt: skip
    job = admin_row("SELECT kind, origin, input FROM jobs")
    assert (job.kind, job.origin, job.input) == (
        "export_build",
        "user",
        {"export_id": export["id"]},
    )
    events = await _events(xenv, "export_requested")
    assert events == [{"export_id": export["id"], "export_kind": "data"}]


async def test_one_export_builds_at_a_time(xenv: Env) -> None:
    browser = await exporter(xenv)
    assert (await request_export(browser)).status_code == 202
    second = await request_export(browser)
    assert second.status_code == 409
    assert reason(second) == "export_in_progress"
    assert admin_scalar("SELECT count(*) FROM exports") == 1


async def test_three_requests_a_day_then_429(xenv: Env, app_database_url: str) -> None:
    browser = await exporter(xenv)
    for _ in range(3):
        assert (await request_export(browser)).status_code == 202
        assert await run_build(app_database_url) == "completed"
    fourth = await request_export(browser)
    assert fourth.status_code == 429
    assert fourth.headers.get("retry-after")
    assert admin_scalar("SELECT count(*) FROM exports") == 3


async def test_the_list_holds_only_the_callers_exports(xenv: Env) -> None:
    a = await exporter(xenv, "synthetic-a@example.com")
    b = await exporter(xenv, "synthetic-b@example.com")
    assert (await request_export(a)).status_code == 202
    listed = (await b.get("/api/exports")).json()
    assert listed == {"exports": []}
    assert len((await a.get("/api/exports")).json()["exports"]) == 1


# --- the download --------------------------------------------------------------------------------


async def test_a_download_streams_once_and_then_is_refused(
    xenv: Env, app_database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    browser = await exporter(xenv)
    export_id = await built_export(xenv, browser, app_database_url)
    row = admin_row("SELECT state, size, sha256 FROM exports")
    assert row.state == "ready"
    response = await download(xenv, browser, export_id)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="ownvitals-export-'
    )
    assert len(response.content) == row.size
    import hashlib

    assert hashlib.sha256(response.content).digest() == bytes(row.sha256)
    assert "manifest.json" in open_archive(response.content)
    after = admin_row("SELECT state, download_count, download_attempts FROM exports")
    assert (after.state, after.download_count, after.download_attempts) == ("expired", 1, 1)
    assert admin_scalar("SELECT count(*) FROM export_chunks") == 0
    again = await download(xenv, browser, export_id)
    assert again.status_code == 410
    assert reason(again) == "export_expired"
    events = await _events(xenv, "export_downloaded")
    assert events == [
        {"export_id": export_id, "export_kind": "data", "attempt": 1, "completed": True}
    ]
    assert [e["export_id"] for e in await _events(xenv, "export_ready")] == [export_id]
    # No export id in any log line (the log-redaction rule for URLs and ids).
    assert all(export_id not in record.getMessage() for record in caplog.records)


async def test_a_download_needs_a_fresh_step_up(xenv: Env, app_database_url: str) -> None:
    browser = await exporter(xenv)
    export_id = await built_export(xenv, browser, app_database_url)
    await _age_stepup(xenv)
    response = await download(xenv, browser, export_id)
    assert response.status_code == 403
    assert reason(response) == "step_up_required"
    assert admin_scalar("SELECT download_attempts FROM exports") == 0


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", None])
async def test_a_cross_site_download_is_refused_without_counting(
    xenv: Env, app_database_url: str, site: str | None
) -> None:
    browser = await exporter(xenv)
    export_id = await built_export(xenv, browser, app_database_url)
    response = await download(xenv, browser, export_id, site=site)
    assert response.status_code == 403
    assert response.headers["x-ownvitals-reason"] == "download_same_origin_required"
    assert "Start the download from your ownvitals Settings page." in response.text
    assert 'href="/settings/data"' in response.text
    assert admin_scalar("SELECT download_attempts FROM exports") == 0


async def test_another_users_session_gets_404(xenv: Env, app_database_url: str) -> None:
    a = await exporter(xenv, "synthetic-a@example.com")
    b = await exporter(xenv, "synthetic-b@example.com")
    export_id = await built_export(xenv, a, app_database_url)
    response = await download(xenv, b, export_id)
    assert response.status_code == 404
    assert reason(response) == "not_found"
    assert admin_scalar("SELECT download_attempts FROM exports") == 0


async def test_three_attempts_then_refused(xenv: Env, app_database_url: str) -> None:
    browser = await exporter(xenv)
    export_id = await built_export(xenv, browser, app_database_url)
    # Interrupted downloads: the attempt is counted before any byte is sent.
    admin_run("UPDATE exports SET download_attempts = 3")
    response = await download(xenv, browser, export_id)
    assert response.status_code == 410
    assert reason(response) == "export_download_spent"


async def test_the_archive_is_gone_after_its_expiry(xenv: Env, app_database_url: str) -> None:
    browser = await exporter(xenv)
    export_id = await built_export(xenv, browser, app_database_url)
    assert admin_scalar("SELECT count(*) FROM export_chunks") > 0
    admin_run("UPDATE exports SET expires_at = now() - interval '1 second'")
    response = await download(xenv, browser, export_id)
    assert response.status_code == 410
    assert reason(response) == "export_expired"
    assert admin_scalar("SELECT state FROM exports") == "expired"
    assert admin_scalar("SELECT count(*) FROM export_chunks") == 0
    assert [e["export_id"] for e in await _events(xenv, "export_expired")] == [export_id]


async def test_the_list_expires_and_empties_old_archives(xenv: Env, app_database_url: str) -> None:
    browser = await exporter(xenv)
    await built_export(xenv, browser, app_database_url)
    admin_run("UPDATE exports SET expires_at = now() - interval '1 second'")
    listed = (await browser.get("/api/exports")).json()["exports"]
    assert [e["state"] for e in listed] == ["expired"]
    assert admin_scalar("SELECT count(*) FROM export_chunks") == 0
    admin_run("UPDATE exports SET created_at = now() - interval '91 days'")
    assert (await browser.get("/api/exports")).json() == {"exports": []}


async def test_a_build_that_ended_without_an_archive_shows_failed(xenv: Env) -> None:
    browser = await exporter(xenv)
    assert (await request_export(browser)).status_code == 202
    admin_run("UPDATE jobs SET state = 'failed', error_category = 'internal', finished_at = now()")
    listed = (await browser.get("/api/exports")).json()["exports"]
    assert [(e["state"], e["failure"]) for e in listed] == [("failed", "build_failed")]
    # Audited once, when the tidy marks it (ADR 0013 section 5).
    events = await _events(xenv, "export_failed")
    assert [(e["export_id"], e["reason"]) for e in events] == [(listed[0]["id"], "build_failed")]
    assert (await request_export(browser)).status_code == 202  # no longer in progress
    assert len(await _events(xenv, "export_failed")) == 1


async def test_a_build_over_the_limit_fails_and_keeps_nothing(
    xenv: Env, app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser = await exporter(xenv)
    owner = UUID(str(await xenv.scalar("SELECT id FROM users")))
    plant(admin_url(), owner)  # some rows, so the archive passes several small chunks
    assert (await request_export(browser)).status_code == 202
    # Small chunks, so some are stored before the limit trips; the failure must remove them.
    monkeypatch.setattr(export_archive, "CHUNK_MAX_BYTES", 256)
    stored_before: list[int] = []
    original = export._fail

    async def counting(job: Any, export_id: Any, largest: str) -> None:
        stored_before.append(admin_scalar("SELECT count(*) FROM export_chunks"))
        await original(job, export_id, largest)

    monkeypatch.setattr(export, "_fail", counting)
    assert await run_build(app_database_url, export_max_bytes=4096) == "completed"
    assert stored_before
    assert stored_before[0] > 0
    row = admin_row("SELECT state, failure, largest, size FROM exports WHERE state <> 'expired'")
    assert (row.state, row.failure, row.size) == ("failed", "export_too_large", None)
    assert row.largest is not None
    assert admin_scalar("SELECT count(*) FROM export_chunks") == 0
    events = await _events(xenv, "export_failed")
    assert [e["reason"] for e in events] == ["export_too_large"]


async def test_parallel_attempts_give_the_whole_archive_at_most_once(
    xenv: Env, app_database_url: str
) -> None:
    browser = await exporter(xenv)
    export_id = UUID(await built_export(xenv, browser, app_database_url))
    owner = UUID(str(await xenv.scalar("SELECT id FROM users")))
    ctx = make_context(owner)
    first = await export_store.begin_download(xenv.factory, ctx, export_id)
    second = await export_store.begin_download(xenv.factory, ctx, export_id)
    last = first.chunks - 1
    # The earlier attempt is superseded: it gets neither a chunk nor the last one.
    with pytest.raises(LookupError):
        await export_store.read_chunk(xenv.factory, ctx, export_id, 0, first.attempt)
    with pytest.raises(LookupError):
        await export_store.take_last_chunk(xenv.factory, ctx, export_id, last, first.attempt)
    # The latest attempt gets the archive once; after that nobody does.
    assert await export_store.take_last_chunk(xenv.factory, ctx, export_id, last, second.attempt)
    with pytest.raises(LookupError):
        await export_store.take_last_chunk(xenv.factory, ctx, export_id, last, second.attempt)
    row = admin_row("SELECT state, download_count FROM exports")
    assert (row.state, row.download_count) == ("expired", 1)
    assert admin_scalar("SELECT count(*) FROM export_chunks") == 0
