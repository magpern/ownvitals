"""Helpers for the export tests (#211): a signed-in browser that may export, the build job run by
the worker's runner, the download, and the archive opened in memory. Synthetic data only."""

import asyncio
import io
import json
import zipfile
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from authtools import Env, running_env
from flows import Browser, signed_in
from jobcore import settings_for
from jobtools import admin_url, engine_for
from ownvitals.auth.owners import ACCOUNTS
from ownvitals.db import create_session_factory
from ownvitals.jobs import KindRegistry, queue
from ownvitals.jobs.runner import Execution, RunConfig, execute
from ownvitals.reports.export import KIND, register_export_kind
from ownvitals.worker.loop import create_worker_engine

SYNTHETIC = True


@pytest.fixture(name="xenv")
async def xenv_fixture(clean_auth: None, app_database_url: str) -> AsyncIterator[Env]:
    async with running_env(app_database_url) as running:
        yield running


def cookies(browser: Browser) -> dict[str, str]:
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in browser.cookies.items())}


async def exporter(env: Env, email: str = "synthetic-export@example.com") -> Browser:
    """Signed in by password and TOTP just now: recent authentication and a step-up."""
    return await signed_in(env, email)


async def request_export(browser: Browser) -> httpx.Response:
    return await browser.post("/api/exports")


async def run_build(app_database_url: str, **settings: Any) -> str:
    """Claim one queued ``export_build`` job and run it as the worker does; its result."""
    worker_settings = settings_for(app_database_url, **settings)
    engine = create_worker_engine(worker_settings, pool_size=4)
    try:
        factory = create_session_factory(engine)
        registry = KindRegistry()
        register_export_kind(registry, worker_settings)
        claimed = await queue.claim(factory, "synthetic-export-worker", 60, [KIND])
        assert claimed is not None
        execution = Execution(claimed)
        await execute(
            execution,
            factory,
            registry,
            RunConfig(lease_seconds=60, accounts=ACCOUNTS),
            asyncio.Event(),
        )
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
    return await env.client.get(f"/api/exports/{export_id}/file", headers=headers)


def open_archive(body: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert archive.testzip() is None
        return {name: archive.read(name) for name in archive.namelist()}


def manifest(files: dict[str, bytes]) -> dict[str, Any]:
    return json.loads(files["manifest.json"])


async def built_export(env: Env, browser: Browser, app_database_url: str) -> str:
    """Request an export and build it; the export's id."""
    response = await request_export(browser)
    assert response.status_code == 202, response.text
    export_id = response.json()["export"]["id"]
    assert await run_build(app_database_url) == "completed"
    return export_id


def admin_scalar(sql: str, **params: Any) -> Any:
    """As the superuser (owned tables are invisible to a session without a user)."""
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        return conn.execute(text(sql), params).scalar_one_or_none()


def admin_row(sql: str, **params: Any) -> Any:
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        return conn.execute(text(sql), params).one()


def admin_run(sql: str, **params: Any) -> None:
    with engine_for(admin_url()) as engine, engine.begin() as conn:
        conn.execute(text(sql), params)
