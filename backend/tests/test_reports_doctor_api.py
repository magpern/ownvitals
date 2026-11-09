"""Requesting, building and downloading the doctor PDF (ADR 0013 sections 4 and 6; issue 210) on
a real database, through the API, as the app role under forced RLS. The build runs as the worker
runs it. Synthetic accounts only; no real data.
"""

import asyncio
import hashlib
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text

import pdftools
from authtools import Env, auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from contexttools import plant
from exporttools import (  # noqa: F401 - fixture
    admin_row,
    admin_run,
    admin_scalar,
    cookies,
    exporter,
    xenv_fixture,
)
from flows import Browser, age_reauth, reason
from jobcore import settings_for
from jobtools import admin_url
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.db import create_session_factory
from ownvitals.jobs import KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.reports.doctor import KIND, register_doctor_kind
from ownvitals.worker.loop import create_worker_engine

SYNTHETIC = True
START, END = date(2026, 1, 1), date(2026, 3, 31)
CHOICES: dict[str, Any] = {
    "start": START.isoformat(),
    "end": END.isoformat(),
    "metrics": ["weight"],
}


async def request(browser: Browser, **changes: Any) -> httpx.Response:
    return await browser.post("/api/reports/doctor", json={**CHOICES, **changes})


async def build(app_database_url: str, **settings: Any) -> str:
    """Claim one queued ``doctor_pdf_build`` job and run it as the worker does."""
    worker_settings = settings_for(app_database_url, **settings)
    engine = create_worker_engine(worker_settings, pool_size=4)
    try:
        factory = create_session_factory(engine)
        registry = KindRegistry()
        register_doctor_kind(registry, worker_settings)
        claimed = await queue.claim(factory, "synthetic-doctor-worker", 60, [KIND])
        assert claimed is not None
        execution = Execution(claimed)
        config = RunConfig(lease_seconds=60, accounts=ACCOUNTS)
        await execute(execution, factory, registry, config, asyncio.Event())
        return str(execution.result)
    finally:
        await engine.dispose()


async def download(
    env: Env, browser: Browser, export_id: str, *, site: str | None = "same-origin"
) -> httpx.Response:
    headers = cookies(browser)
    if site is not None:
        headers["Sec-Fetch-Site"] = site
    env.client.cookies.clear()
    return await env.client.get(f"/api/reports/doctor/{export_id}/file", headers=headers)


async def built(env: Env, browser: Browser, url: str, **changes: Any) -> str:
    response = await request(browser, **changes)
    assert response.status_code == 202, response.text
    assert await build(url) == "completed"
    return str(response.json()["export"]["id"])


async def events(env: Env, kind: str) -> list[dict[str, Any]]:
    async with env.db() as db:
        rows = await db.execute(
            text("SELECT detail FROM auth_events WHERE kind = :k ORDER BY at, id"), {"k": kind}
        )
        return [dict(r.detail) for r in rows]


def seed(user: UUID) -> None:
    """Planted rows of every kind, then weight days, a typed-in day and findings of every tier."""
    plant(admin_url(), user)
    admin_run("UPDATE measure_groups SET attrib = 2 WHERE grpid = 1")
    for i in range(60):
        day = START + timedelta(days=i)
        admin_run(
            "INSERT INTO metric_daily (owner_id, metric_key, day, method, value, mean, min, max, "
            "mad, n, source_groups, algo_version, input_revision, input_fingerprint) "
            "VALUES (:o, 'weight', :d, 'median', :v, :v, :v, :v, 0.5, 1, "
            "CASE WHEN :i = 5 THEN ARRAY[1]::bigint[] ELSE ARRAY[900 + :i]::bigint[] END, "
            "'synthetic/1', 0, decode(repeat('ab', 32), 'hex'))",
            o=user,
            d=day,
            v=70 + i % 4,
            i=i,
        )
    for rule, tier in (("E1", "device"), ("E5", "experimental"), ("C1", "trend")):
        admin_run(
            "INSERT INTO findings (owner_id, rule_id, rule_version, tier, severity, confidence, "
            "origin, subject, window_start, window_end, evidence, input_revision, "
            "input_fingerprint) VALUES (:o, :r, 'synthetic/1', :t, 'attention', "
            "CASE WHEN :t = 'device' THEN 'high' ELSE 'medium' END, "
            "'live', 'x', '2026-02-01', '2026-02-01', '{}', 99, decode(repeat('cd', 32), 'hex'))",
            o=user,
            r=rule,
            t=tier,
        )


# --- the request ---------------------------------------------------------------------------------


async def test_the_request_needs_a_step_up(xenv: Env) -> None:
    browser = await exporter(xenv)
    await xenv.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")
    response = await request(browser)
    assert response.status_code == 403
    assert reason(response) == "step_up_required"
    assert admin_scalar("SELECT count(*) FROM jobs") == 0


async def test_the_request_needs_no_recent_authentication_beyond_the_step_up(xenv: Env) -> None:
    browser = await exporter(xenv)
    await age_reauth(xenv)
    assert (await request(browser)).status_code == 202


async def test_the_job_input_holds_the_choices_and_no_name_or_email(xenv: Env) -> None:
    browser = await exporter(xenv, "synthetic-doctor@example.com")
    response = await request(browser)
    assert response.status_code == 202, response.text
    assert response.json()["export"]["state"] == "building"
    job = admin_row("SELECT kind, origin, input FROM jobs")
    assert job.kind == KIND
    assert set(job.input) == {"start", "end", "metrics", "recordings", "export_id"}
    assert "synthetic-doctor" not in str(job.input)
    assert admin_scalar("SELECT kind FROM exports WHERE kind = 'doctor_pdf'") == "doctor_pdf"
    assert [e["export_kind"] for e in await events(xenv, "doctor_pdf_requested")] == ["doctor_pdf"]


async def test_the_request_refuses_a_name_a_long_period_and_unknown_metrics(xenv: Env) -> None:
    browser = await exporter(xenv)
    for changes in (
        {"name": "A Name"},
        {"end": "2027-06-01"},
        {"metrics": ["nope"]},
        {"metrics": []},
    ):
        assert (await request(browser, **changes)).status_code == 422
    assert admin_scalar("SELECT count(*) FROM jobs") == 0


async def test_one_doctor_report_builds_at_a_time(xenv: Env) -> None:
    browser = await exporter(xenv)
    assert (await request(browser)).status_code == 202
    second = await request(browser)
    assert second.status_code == 409
    assert reason(second) == "export_in_progress"


async def test_ten_requests_a_day_then_429(xenv: Env, app_database_url: str) -> None:
    browser = await exporter(xenv)
    for _ in range(10):
        assert (await request(browser)).status_code == 202
        assert await build(app_database_url) == "completed"
    eleventh = await request(browser)
    assert eleventh.status_code == 429
    assert eleventh.headers.get("retry-after")


async def test_the_data_export_and_the_doctor_pdf_do_not_share_the_in_progress_rule(
    xenv: Env,
) -> None:
    browser = await exporter(xenv)
    assert (await browser.post("/api/exports")).status_code == 202
    assert (await request(browser)).status_code == 202
    listed = (await browser.get("/api/reports/doctor")).json()["exports"]
    assert len(listed) == 1


# --- the build and the download -------------------------------------------------------------------


async def test_the_pdf_holds_the_chosen_data_and_nothing_experimental(
    xenv: Env, app_database_url: str
) -> None:
    browser = await exporter(xenv)
    user = UUID(str(admin_scalar("SELECT id FROM users")))
    seed(user)
    ecg = str(admin_scalar("SELECT id FROM ecg_recordings"))
    export_id = await built(xenv, browser, app_database_url, recordings=[ecg])
    response = await download(xenv, browser, export_id)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="ownvitals-health-report-'
    )
    assert response.headers["cache-control"] == "no-store"
    row = admin_row(
        "SELECT state, size, sha256, download_count FROM exports WHERE kind = 'doctor_pdf'"
    )
    assert len(response.content) == row.size
    assert hashlib.sha256(response.content).digest() == bytes(row.sha256)
    assert response.content.startswith(b"%PDF")
    shown = pdftools.text(response.content)
    assert "Health data report" in shown
    assert "Weight" in shown
    assert "Withings classified an ECG as positive for atrial fibrillation." in shown
    assert "no signal stored" in shown  # the planted recording has no signal
    assert "E5" not in shown
    assert "C1" not in shown
    for word in ("estimate", "experimental", "trend", "score"):
        assert word not in shown.lower()
    for page in pdftools.streams(response.content):
        assert "ownvitals is not a medical device" in page
    assert [e["export_kind"] for e in await events(xenv, "export_ready")] == ["doctor_pdf"]


async def test_a_flag_on_account_still_gets_no_estimate_or_experimental_finding(
    xenv: Env, app_database_url: str
) -> None:
    browser = await exporter(xenv)
    user = UUID(str(admin_scalar("SELECT id FROM users")))
    seed(user)
    admin_run(
        "INSERT INTO app_settings (flag, value) VALUES ('experimental_findings', '\"on\"') "
        "ON CONFLICT (flag) DO UPDATE SET value = '\"on\"'"
    )
    response = await request(browser)
    assert response.status_code == 202
    assert await build(app_database_url, experimental_findings=True) == "completed"
    export_id = response.json()["export"]["id"]
    shown = pdftools.text((await download(xenv, browser, export_id)).content)
    assert "E5" not in shown
    assert "C1" not in shown
    assert "estimate" not in shown.lower()


async def test_a_download_is_once_and_audited(xenv: Env, app_database_url: str) -> None:
    browser = await exporter(xenv)
    export_id = await built(xenv, browser, app_database_url)
    assert (await download(xenv, browser, export_id)).status_code == 200
    after = admin_row(
        "SELECT state, download_count, download_attempts FROM exports WHERE kind = 'doctor_pdf'"
    )
    assert (after.state, after.download_count, after.download_attempts) == ("expired", 1, 1)
    assert admin_scalar("SELECT count(*) FROM export_chunks") == 0
    again = await download(xenv, browser, export_id)
    assert again.status_code == 410
    assert reason(again) == "export_expired"
    assert await events(xenv, "doctor_pdf_downloaded") == [
        {"export_id": export_id, "export_kind": "doctor_pdf", "attempt": 1, "completed": True}
    ]


async def test_a_download_needs_a_step_up_and_a_same_origin_request(
    xenv: Env, app_database_url: str
) -> None:
    browser = await exporter(xenv)
    export_id = await built(xenv, browser, app_database_url)
    for site in ("cross-site", "same-site", "none", None):
        refused = await download(xenv, browser, export_id, site=site)
        assert refused.status_code == 403
        assert refused.headers["x-ownvitals-reason"] == "download_same_origin_required"
    await xenv.run("UPDATE sessions SET stepup_at = now() - interval '11 minutes'")
    stale = await download(xenv, browser, export_id)
    assert stale.status_code == 403
    assert reason(stale) == "step_up_required"
    assert admin_scalar("SELECT download_attempts FROM exports WHERE kind = 'doctor_pdf'") == 0


async def test_another_user_can_neither_list_nor_fetch_it(xenv: Env, app_database_url: str) -> None:
    a = await exporter(xenv, "synthetic-a@example.com")
    b = await exporter(xenv, "synthetic-b@example.com")
    export_id = await built(xenv, a, app_database_url)
    response = await download(xenv, b, export_id)
    assert response.status_code == 404
    assert reason(response) == "not_found"
    assert (await b.get("/api/reports/doctor")).json() == {"exports": []}
    assert admin_scalar("SELECT download_attempts FROM exports WHERE kind = 'doctor_pdf'") == 0


async def test_the_data_export_download_does_not_serve_a_doctor_report(
    xenv: Env, app_database_url: str
) -> None:
    browser = await exporter(xenv)
    export_id = await built(xenv, browser, app_database_url)
    headers = {**cookies(browser), "Sec-Fetch-Site": "same-origin"}
    xenv.client.cookies.clear()
    response = await xenv.client.get(f"/api/exports/{export_id}/file", headers=headers)
    assert response.status_code == 404
