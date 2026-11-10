"""The worker's registry, target check, schedule and the reconcile and recovery handlers (#164).

Synthetic ids, filler key bytes (``\\x11``, labelled, not key material from a file) and fakes
only: no network, no real account, no webhook secret from anywhere but these tests.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from jobcore import (  # noqa: F401 - fixtures
    JobsEnv,
    jenv_fixture,
    registry_for,
    run_next,
    settings_for,
)
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.ai.chat_model import CHAT_KIND, ChatInput
from ownvitals.crypto.envelope import WebhookSeed
from ownvitals.crypto.keks import KeyUnavailable
from ownvitals.db import create_session_factory
from ownvitals.derived.daily_job import KIND as DAILY_KIND
from ownvitals.derived.daily_job import DailyInput
from ownvitals.derived.ecg_recompute import KIND as RECOMPUTE_KIND
from ownvitals.derived.ecg_recompute import RecomputeInput
from ownvitals.derived.evaluate_job import KIND as EVALUATE_KIND
from ownvitals.derived.evaluate_job import EvaluateInput
from ownvitals.derived.normalize_job import KIND as NORMALIZE_KIND
from ownvitals.derived.normalize_job import NormalizeInput
from ownvitals.derived.scores_job import KIND as SCORES_KIND
from ownvitals.derived.scores_job import ScoresInput
from ownvitals.jobs import KINDS, Complete, JobFailure, JobRun
from ownvitals.jobs.runner import RunConfig
from ownvitals.jobs.withings import INPUT_MODELS, NO_TARGETS, ConnectInput
from ownvitals.notify.findings_job import KIND as NOTIFY_KIND
from ownvitals.notify.findings_job import NotifyInput
from ownvitals.reports.doctor import KIND as DOCTOR_KIND
from ownvitals.reports.doctor_input import DoctorInput
from ownvitals.reports.export import KIND as EXPORT_KIND
from ownvitals.reports.export import ExportInput
from ownvitals.withings.disconnect import DISCONNECT_KIND, DisconnectInput
from ownvitals.withings.subscriptions import callback_url, derive_secret
from ownvitals.withings.tokens import RefreshResult
from ownvitals.withings.webhooks import WebhookTargets
from ownvitals.worker import kinds as worker_kinds
from ownvitals.worker.kinds import (
    SCHEDULES,
    WORKER_TARGETS,
    current_callback,
    run_reconcile,
    run_token_recover,
    worker_registry,
)
from ownvitals.worker.loop import Worker, create_worker_engine

SEED_BYTES = b"\x11" * 32
BASE = "https://app.example.test"
WITHINGS_KINDS = [
    "ai_chat_turn",  # #208: the chat assistant's turn, queued by POST .../messages
    "derive_daily",  # #186: the metric_daily pass, queued by derive_normalize
    "derive_evaluate",  # #187: the rule evaluation, queued by derive_daily
    "derive_normalize",  # #185: the observations pass, queued after the measures streams
    "derive_scores",  # #188: the scores pass, queued by derive_evaluate
    "doctor_pdf_build",  # issue 210: the doctor PDF, queued by POST /api/reports/doctor
    "ecg_fetch",
    "ecg_recompute",  # #191: the estimated recompute, queued by an ecg pass and the nightly sweep
    "export_build",  # #211: the user's JSON/CSV export, queued by POST /api/exports
    "notify_findings",  # #195: the finding notifications, queued by evaluate and recompute
    "token_refresh_recover",
    "withings_backfill",
    "withings_connect",
    "withings_disconnect",
    "withings_poll",
    "withings_reconcile",
    "withings_sync",
]


# ---------------------------------------------------------------------------- the registry


def test_the_worker_registry_holds_the_withings_kinds_and_the_api_none() -> None:
    registry = worker_registry()
    assert registry.names() == WITHINGS_KINDS
    for kind in registry:
        # The disconnect revoke (#163) takes its own input: ids of the row and the session.
        own = {
            DISCONNECT_KIND: DisconnectInput,
            NORMALIZE_KIND: NormalizeInput,
            DAILY_KIND: DailyInput,
            EVALUATE_KIND: EvaluateInput,
            SCORES_KIND: ScoresInput,
            RECOMPUTE_KIND: RecomputeInput,
            NOTIFY_KIND: NotifyInput,
            EXPORT_KIND: ExportInput,
            DOCTOR_KIND: DoctorInput,
            CHAT_KIND: ChatInput,  # #208: the conversation and the message, ids only
        }
        expected = own[kind.name] if kind.name in own else INPUT_MODELS[kind.name]
        assert kind.input_model is expected
    assert worker_registry() is not registry  # a new one each time; nothing shared
    assert KINDS.names() == []  # the API's registry: POST /api/jobs still refuses every kind


def test_only_registered_kinds_are_scheduled_and_the_targets_reach_the_runs() -> None:
    settings = settings_for("postgresql+psycopg://ownvitals_app@127.0.0.1:1/none")
    engine = create_worker_engine(settings)  # connects lazily; nothing connects here
    plain = Worker(settings, engine, registry=registry_for(_done), schedules=SCHEDULES)
    assert plain.schedules == {}
    assert plain.config.targets is NO_TARGETS
    wired = Worker(
        settings, engine, registry=worker_registry(), targets=WORKER_TARGETS, schedules=SCHEDULES
    )
    assert wired.schedules == {"withings_poll": 21_600, "withings_reconcile": 86_400}
    assert wired.config.targets is WORKER_TARGETS
    assert isinstance(WORKER_TARGETS, WebhookTargets)


async def _done(job_run: JobRun) -> Complete:
    return Complete()


@pytest.mark.usefixtures("restore_logging", "queue_schema")
def test_the_process_entry_wires_the_registry_targets_and_schedule(
    monkeypatch: pytest.MonkeyPatch, app_database_url: str, secrets_dir: Path
) -> None:
    from ownvitals.egress import core
    from ownvitals.worker.__main__ import main

    seen: dict[str, Any] = {}

    async def capture(*_args: object, **kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(core, "_worker_list", None)
    monkeypatch.setenv("OWNVITALS_DATABASE_URL", app_database_url)
    monkeypatch.setattr("ownvitals.worker.__main__.run_worker", capture)
    monkeypatch.setattr(
        "ownvitals.worker.__main__.install_egress_hook", lambda *_a, **_k: None
    )  # the process hook would stay installed in the test process
    assert main() == 0
    assert seen["registry"].names() == WITHINGS_KINDS
    assert seen["targets"] is WORKER_TARGETS
    assert seen["schedules"] is SCHEDULES
    assert seen["accounts"].__class__.__name__ == "AuthAccounts"


# ---------------------------------------------------------------------------- the target check

SECRET_ID = UUID("00000000-0000-4000-8000-000000000164")


@pytest.fixture
def app_row(admin: Connection) -> Iterator[int]:
    """A synthetic app row; the ciphertext columns are labelled filler."""
    app_id = admin.execute(
        text(
            "INSERT INTO withings_apps (client_id, callback_base, client_secret, "
            "client_secret_nonce, client_wrapped_dek, client_dek_nonce, client_key_id) "
            "VALUES ('synthetic-client', :base, :secret, :nonce, :wrapped, :nonce, "
            "'kek-synthetic') RETURNING id"
        ),
        {"base": BASE, "secret": b"\x01" * 16, "nonce": b"\x02" * 12, "wrapped": b"\x03" * 48},
    ).scalar_one()
    try:
        yield int(app_id)
    finally:
        admin.execute(text("DELETE FROM withings_apps WHERE id = :id"), {"id": app_id})


def _entries(admin: Connection, app_id: int, *items: dict[str, Any]) -> None:
    admin.execute(
        text("UPDATE withings_apps SET webhook_secrets = CAST(:e AS jsonb) WHERE id = :id"),
        {"e": json.dumps(list(items)), "id": app_id},
    )


def _entry(target: UUID, **fields: Any) -> dict[str, Any]:
    opened = datetime.now(UTC) - timedelta(hours=1)
    return {
        "secret_id": str(target),
        "seed_id": "seed-1",
        "pepper_id": "pep-1",
        "hmac": "11" * 32,
        "active_from": opened.isoformat(),
        "retire_at": None,
        **fields,
    }


def _with_targets(env: JobsEnv) -> RunConfig:
    return RunConfig(
        lease_seconds=env.settings.job_lease_seconds, accounts=env.accounts, targets=WORKER_TARGETS
    )


@pytest.mark.parametrize(
    ("fields", "runs"),
    [
        ({}, True),
        ({"retire_at": datetime.now(UTC).isoformat()}, False),
        ({"active_from": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}, False),
        ({"secret_id": str(uuid4())}, False),  # another entry: the target is unknown
    ],
)
async def test_a_targeted_job_runs_only_while_its_entry_is_open(
    jenv: JobsEnv, app_row: int, fields: dict[str, Any], runs: bool
) -> None:
    _entries(jenv.admin, app_row, _entry(SECRET_ID, **fields))
    _, job_id = jenv.job(target_secret_id=SECRET_ID)
    ran: list[UUID] = []

    async def run(job_run: JobRun) -> Complete:
        ran.append(job_run.lease.job_id)
        return Complete()

    await run_next(jenv, registry_for(run), config=_with_targets(jenv))
    assert jenv.row(job_id).state == ("completed" if runs else "cancelled")
    assert ran == ([job_id] if runs else [])


# ---------------------------------------------------------------------------- current callback


class _StubRing:
    def __init__(self, seed: WebhookSeed | None) -> None:
        self.seed = seed
        self.asked: list[str] = []

    async def seed_for(self, db: object, seed_id: str) -> WebhookSeed:
        self.asked.append(seed_id)
        if self.seed is None or seed_id != self.seed.key_id:
            raise KeyUnavailable
        return self.seed


class _SessionJob:
    """Only what ``current_callback`` uses: ``fenced()`` with a ``db``. A plain app-role
    session stands in for the fenced one (``withings_apps`` is not an owned table)."""

    def __init__(self, factory: Any) -> None:
        self.factory = factory

    @contextlib.asynccontextmanager
    async def fenced(self) -> AsyncIterator[Any]:
        async with self.factory() as session, session.begin():

            class _Fs:
                db = session

            yield _Fs()


async def test_the_current_callback_is_derived_from_the_newest_open_entry(
    jenv: JobsEnv, app_row: int
) -> None:
    older, newest, future, retired = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    _entries(
        jenv.admin,
        app_row,
        _entry(older, active_from=(now - timedelta(days=3)).isoformat()),
        _entry(newest, active_from=(now - timedelta(minutes=5)).isoformat()),
        _entry(future, active_from=(now + timedelta(minutes=5)).isoformat()),
        _entry(retired, retire_at=now.isoformat()),
    )
    ring = _StubRing(WebhookSeed("seed-1", SEED_BYTES))
    job: Any = _SessionJob(create_session_factory(jenv.engine))
    base, url = await current_callback(job, ring, app_row)  # type: ignore[arg-type]
    assert base == BASE
    assert url == callback_url(BASE, derive_secret(SEED_BYTES, app_row, newest))
    assert ring.asked == ["seed-1"]


async def test_no_open_entry_or_no_seed_means_no_repair(
    jenv: JobsEnv, app_row: int, caplog: pytest.LogCaptureFixture
) -> None:
    job: Any = _SessionJob(create_session_factory(jenv.engine))
    ring = _StubRing(WebhookSeed("seed-1", SEED_BYTES))
    assert await current_callback(job, ring, app_row) == (BASE, None)  # type: ignore[arg-type]
    assert ring.asked == []
    _entries(jenv.admin, app_row, _entry(SECRET_ID, seed_id="seed-2"))
    with caplog.at_level("WARNING", logger="ownvitals.worker.kinds"):
        assert await current_callback(job, ring, app_row) == (BASE, None)  # type: ignore[arg-type]
    assert [r.getMessage() for r in caplog.records] == ["webhook seed unavailable"]
    assert await current_callback(job, ring, app_row + 1000) == (None, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------- the handlers


class _Job:
    """A stand-in ``JobRun`` for the handlers' wiring; the work itself is faked."""

    def __init__(self, parsed: object) -> None:
        self.input = parsed
        self.factory = object()
        self.ctx = object()
        self.lease = object()
        self.calls: list[float] = []

    @contextlib.asynccontextmanager
    async def provider_call(self, deadline: float) -> AsyncIterator[None]:
        self.calls.append(deadline)
        yield


class _Client:
    @classmethod
    @contextlib.asynccontextmanager
    async def open(cls, app: object, **_options: object) -> AsyncIterator[str]:
        yield f"client-for-{app}"


@pytest.fixture
def faked(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def materials(job: Any, connection_id: UUID) -> tuple[Any, str]:
        seen["materials"] = connection_id

        class _Account:
            app_id = 7

        return _Account(), "app"

    async def ring(job: Any) -> str:
        return "ring"

    monkeypatch.setattr(worker_kinds, "open_materials", materials)
    monkeypatch.setattr(worker_kinds, "_ring", ring)
    monkeypatch.setattr(worker_kinds, "WithingsClient", _Client)
    return seen


async def test_reconcile_passes_the_current_callback_to_the_nightly_run(
    monkeypatch: pytest.MonkeyPatch, faked: dict[str, Any]
) -> None:
    async def callback(job: Any, ring: Any, app_id: int) -> tuple[str, str]:
        assert (ring, app_id) == ("ring", 7)
        return BASE, BASE + "/api/webhooks/withings/synthetic-marker"

    async def nightly(job: Any, account: Any, client: Any, **kwargs: Any) -> tuple[list, None]:
        faked["nightly"] = (client, kwargs)
        return [], None

    queued: list[tuple[object, UUID, str]] = []

    async def normalize(factory: object, connection_id: UUID, origin: str) -> None:
        queued.append((factory, connection_id, origin))

    async def recompute(factory: object, connection_id: UUID) -> None:
        queued.append((factory, connection_id, "sweep"))

    monkeypatch.setattr(worker_kinds, "current_callback", callback)
    monkeypatch.setattr(worker_kinds, "execute_nightly", nightly)
    monkeypatch.setattr(worker_kinds, "enqueue_normalize", normalize)
    monkeypatch.setattr(worker_kinds, "enqueue_recompute", recompute)
    connection = uuid4()
    job = _Job(ConnectInput(connection_id=connection))
    outcome = await run_reconcile(job)  # type: ignore[arg-type]
    assert isinstance(outcome, Complete)
    # The nightly pass re-derives, and queues the estimated sweep (ADR 0006 section 15).
    assert queued == [(job.factory, connection, "live"), (job.factory, connection, "sweep")]
    assert faked["materials"] == connection
    assert faked["nightly"] == (
        "client-for-app",
        {"callback_base": BASE, "current_url": BASE + "/api/webhooks/withings/synthetic-marker"},
    )


@pytest.mark.parametrize(
    ("outcome", "category"),
    [
        ("refreshed", None),
        ("current", None),
        ("needs_reconnect", "needs_reconnect"),
        ("not_connected", "not_connected"),
        ("recover", "internal"),
        ("failed", "internal"),
    ],
)
async def test_recovery_maps_each_outcome(
    monkeypatch: pytest.MonkeyPatch, faked: dict[str, Any], outcome: str, category: str | None
) -> None:
    connection = uuid4()
    job = _Job(ConnectInput(connection_id=connection))

    async def recover(
        factory: Any, ring: Any, ctx: Any, connection_id: UUID, client: Any, *, lease: Any
    ) -> RefreshResult:
        assert (factory, ring, ctx, lease) == (job.factory, "ring", job.ctx, job.lease)
        assert (connection_id, client) == (connection, "client-for-app")
        assert job.calls == [worker_kinds.RECOVER_DEADLINE_SECONDS]  # inside the provider call
        return RefreshResult(outcome, 3)

    monkeypatch.setattr(worker_kinds, "recover_refresh", recover)
    if category is None:
        assert isinstance(await run_token_recover(job), Complete)  # type: ignore[arg-type]
    else:
        with pytest.raises(JobFailure) as failed:
            await run_token_recover(job)  # type: ignore[arg-type]
        assert failed.value.category == category


async def test_a_wrong_input_is_refused_before_any_call(faked: dict[str, Any]) -> None:
    for handler in (run_reconcile, run_token_recover):
        with pytest.raises(JobFailure) as failed:
            await handler(_Job(object()))  # type: ignore[arg-type]
        assert failed.value.category == "invalid_input"
    assert "materials" not in faked


# ---------------------------------------------------------------------------- the schedule


async def test_the_loop_schedules_the_poll_and_the_reconciliation_once(jenv: JobsEnv) -> None:
    """The real loop, once its checks pass: one poll and one reconciliation for the connected
    connection, and none for the one that needs a reconnect. The credential columns hold
    labelled filler, so the jobs then fail when they open the grant; only their creation is
    checked here."""
    from jobapi import until
    from jobtools import ensure_user

    jenv.sql("TRUNCATE connections CASCADE")
    rows: dict[str, UUID] = {}
    for status in ("connected", "needs_reconnect"):
        owner, rows[status] = uuid4(), uuid4()
        ensure_user(jenv.admin, owner)
        filler = {
            "secret": b"\x01" * 16 if status == "connected" else None,
            "nonce": b"\x02" * 12 if status == "connected" else None,
            "wrapped": b"\x03" * 48 if status == "connected" else None,
            "key": "kek-synthetic" if status == "connected" else None,
        }
        jenv.sql(
            "INSERT INTO connections (id, owner_id, provider, status, secret, secret_nonce, "
            "wrapped_dek, dek_nonce, key_id) VALUES (:id, :owner, 'withings', :status, "
            ":secret, :nonce, :wrapped, :nonce, :key)",
            id=rows[status],
            owner=owner,
            status=status,
            **filler,
        )
    settings = settings_for(jenv.settings.database_url.get_secret_value(), worker_poll_seconds=0.1)
    engine = create_worker_engine(settings)
    worker = Worker(
        settings, engine, registry=worker_registry(), accounts=jenv.accounts, schedules=SCHEDULES
    )
    stop = asyncio.Event()
    task = asyncio.ensure_future(worker.run(stop))

    def scheduled() -> list[tuple[str, str, str]]:
        return [
            (row.kind, row.origin, row.input["connection_id"])
            for row in jenv.sql("SELECT kind, origin, input FROM jobs ORDER BY lane").all()
        ]

    try:
        await until(lambda: len(scheduled()) == 2, seconds=15)
        await until(lambda: worker.passes > 5)
    finally:
        stop.set()
        await asyncio.wait_for(task, 40)
        await engine.dispose()
    live = str(rows["connected"])
    assert scheduled() == [
        ("withings_poll", "scheduler", live),
        ("withings_reconcile", "scheduler", live),
    ]
