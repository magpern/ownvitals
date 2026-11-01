"""The audit log view (#12): read only, newest first, filtered and paged by a cursor; never the
IP's HMAC; the read is audited once with its filter. Synthetic accounts only."""

import json
from urllib.parse import urlencode

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    admin_schema_fixture,
    aenv_fixture,
    caller,
    reason,
)
from ownvitals.admin.audit import MAX_PARAMS_BYTES, checked_params


async def _seed(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    await admin.put("/api/admin/settings/signup_mode", {"value": "closed"})
    await admin.put("/api/admin/settings/signup_mode", {"value": "open"})
    await admin.get("/api/admin/jobs")


async def test_rows_are_listed_newest_first_without_the_ip_hash(aenv: AdminEnv) -> None:
    await _seed(aenv)
    admin = await caller(aenv, "reader@example.com")
    body = (await admin.get("/api/admin/audit")).json()
    actions = [(r["action"], r["outcome"]) for r in body["rows"]]
    # The read's own row is written first in its transaction, so it is listed too.
    assert actions == [
        ("audit.list", "ok"),
        ("job.list", "ok"),
        ("setting.set", "refused"),
        ("setting.set", "ok"),
    ]
    assert all("ip_hash" not in r for r in body["rows"])
    assert body["next_before"] is None


async def test_filters_by_action_prefix_outcome_and_actor_email(aenv: AdminEnv) -> None:
    await _seed(aenv)
    admin = await caller(aenv, "reader@example.com")
    query = urlencode({"action": "setting.", "outcome": "refused"})
    rows = (await admin.get(f"/api/admin/audit?{query}")).json()["rows"]
    assert [(r["action"], r["reason"]) for r in rows] == [("setting.set", "environment_only")]
    query = urlencode({"actor_email": "ADMIN@example.com", "action": "job.list"})
    rows = (await admin.get(f"/api/admin/audit?{query}")).json()["rows"]
    assert [r["actor_email"] for r in rows] == ["admin@example.com"]


async def test_the_cursor_pages_through_the_log(aenv: AdminEnv) -> None:
    await _seed(aenv)
    admin = await caller(aenv, "reader@example.com")
    first = (await admin.get("/api/admin/audit?limit=2")).json()
    assert len(first["rows"]) == 2
    assert first["next_before"] == first["rows"][-1]["id"]
    second = (await admin.get(f"/api/admin/audit?limit=2&before={first['next_before']}")).json()
    assert second["rows"][0]["id"] < first["rows"][-1]["id"]
    response = await admin.get("/api/admin/audit?before=999999")
    assert (response.status_code, reason(response)) == (404, "not_found")


async def test_an_empty_period_is_refused_and_audited(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    query = urlencode({"since": "2026-11-02T00:00:00Z", "until": "2026-11-01T00:00:00Z"})
    response = await admin.get(f"/api/admin/audit?{query}")
    assert (response.status_code, reason(response)) == (422, "period_invalid")
    (row,) = await aenv.audit_rows()
    assert (row.action, row.outcome, row.reason) == ("admin.request", "refused", "period_invalid")


async def test_the_read_is_audited_with_its_filter(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    await admin.get("/api/admin/audit?outcome=ok&limit=5")
    (row,) = await aenv.audit_rows()
    assert (row.action, row.params) == ("audit.list", {"outcome": "ok", "limit": 5})


async def test_there_is_no_route_that_writes_the_log(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = await admin.request(method, "/api/admin/audit")
        assert response.status_code == 405


def test_the_largest_accepted_filter_fits_its_audit_row() -> None:
    worst = {
        "actor_id": "00000000-0000-0000-0000-000000000000",
        "actor_email": "\u2603" * 53,  # 53 characters of 6 bytes each once escaped
        "action": "a" * 60,
        "target_user_id": "00000000-0000-0000-0000-000000000000",
        "target_kind": "k" * 40,
        "target_id": "\u2603" * 33,
        "outcome": "refused",
        "since": "2026-11-01T00:00:00+00:00",
        "until": "2026-11-02T00:00:00+00:00",
        "before": 2**63 - 1,
        "limit": 100,
    }
    assert len(checked_params("audit.list", worst).encode()) <= MAX_PARAMS_BYTES
    assert json.loads(checked_params("audit.list", worst))["limit"] == 100
