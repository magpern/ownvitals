"""The admin Jobs view (#12): counts per state and recent failures by kind and category, read
through maint's column grant; aggregates only, never an owner, input or key. Synthetic jobs."""

import uuid

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    admin_schema_fixture,
    aenv_fixture,
    caller,
)


async def _job(aenv: AdminEnv, owner: uuid.UUID, state: str, **extra: str) -> None:
    columns = {
        "owner_id": owner,
        "kind": "test_kind",
        "state": state,
        "input": '{"synthetic": true, "secret_marker": "not-for-admins"}',
        "idempotency_key": uuid.uuid4().hex,
        "request_hash": bytes(32),
        **extra,
    }
    if state == "failed":
        columns["error_category"] = columns.get("error_category", "internal")
    if state in ("completed", "failed", "cancelled"):
        columns["finished_at"] = "now"
    names = ", ".join(columns)
    values = ", ".join(
        "now()" if v == "now" else f"CAST(:{k} AS jsonb)" if k == "input" else f":{k}"
        for k, v in columns.items()
    )
    params = {k: v for k, v in columns.items() if v != "now"}
    await aenv.superuser(f"INSERT INTO jobs ({names}) VALUES ({values})", **params)  # noqa: S608


async def test_counts_per_state_and_failures_by_category(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    owner = await aenv.env.user("user@example.com")
    for state in ("queued", "queued", "completed", "failed"):
        await _job(aenv, owner, state)
    await _job(aenv, owner, "failed", error_category="rate_limited")
    response = await admin.get("/api/admin/jobs?days=7")
    assert response.status_code == 200
    body = response.json()
    assert {s["state"]: s["count"] for s in body["states"]} == {
        "completed": 1,
        "failed": 2,
        "queued": 2,
    }
    assert sorted((f["category"], f["count"]) for f in body["failures"]) == [
        ("internal", 1),
        ("rate_limited", 1),
    ]
    assert "not-for-admins" not in response.text
    assert str(owner) not in response.text
    (row,) = await aenv.audit_rows()
    assert (row.action, row.params) == ("job.list", {"days": 7})
