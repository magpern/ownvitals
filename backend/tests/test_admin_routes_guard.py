"""Every admin route refuses non-admins and admins without TOTP (acceptance criterion 1 of #12).

The guard enumerates the routes the app registers: each one under ``/api/admin`` must carry
``admin_gate`` (``whoami`` of ``ownvitals.auth``: ``require_admin``), and each is called as an
anonymous caller, a signed-in non-admin, an admin without TOTP in the 12-hour window and, for
writes, an admin without a step-up in the last 5 minutes. A new admin route that skips the gate,
or whose sample request is missing here, fails the test.
"""

import re
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from pydantic import SecretStr

from admintools import (  # noqa: F401 - fixtures
    AdminEnv,
    admin_schema_fixture,
    aenv_fixture,
    age_session,
    caller,
    plain_env_fixture,
    reason,
)
from authtools import Env, session_cookie
from ownvitals.admin.routes import admin_area_routes, admin_gate, gate_problems
from ownvitals.auth.deps import require_admin
from ownvitals.config import Settings
from ownvitals.web import create_app

# One sample request per admin route: (method, route template) -> (path, JSON body).
SAMPLES: dict[tuple[str, str], tuple[str, Any]] = {
    ("GET", "/api/admin/whoami"): ("/api/admin/whoami", None),
    ("GET", "/api/admin/audit"): ("/api/admin/audit", None),
    ("GET", "/api/admin/jobs"): ("/api/admin/jobs", None),
    ("GET", "/api/admin/settings"): ("/api/admin/settings", None),
    ("PUT", "/api/admin/settings/{key}"): ("/api/admin/settings/banner", {"value": "x"}),
    ("DELETE", "/api/admin/settings/{key}"): ("/api/admin/settings/banner", None),
}
SAFE = {"GET", "HEAD"}


def _app() -> FastAPI:
    return create_app(Settings(environment="test", app_secret_key=SecretStr("k" * 40)))  # pyright: ignore[reportCallIssue]


def _admin_routes(app: FastAPI) -> list[tuple[str, str]]:
    return sorted(
        (method, path)
        for path, route in admin_area_routes(app.routes)
        if isinstance(route, APIRoute)
        for method in route.methods or set()
    )


def test_every_admin_route_carries_the_gate() -> None:
    app = _app()
    assert gate_problems(app.routes, require_admin) == []
    assert _admin_routes(app) == sorted(SAMPLES), "add the new admin route to SAMPLES"


def test_the_guard_finds_a_route_without_the_gate() -> None:
    app = _app()
    rogue = APIRouter(prefix="/api/admin")

    @rogue.get("/rogue")
    async def rogue_route() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {}

    app.include_router(rogue)
    assert [p for p in gate_problems(app.routes, require_admin) if "/rogue" in p]


def test_the_guard_refuses_a_mount_under_the_admin_prefix() -> None:
    app = _app()
    app.mount("/api/admin/static", FastAPI())
    assert any("not an API route" in p for p in gate_problems(app.routes, require_admin))


def test_whoami_is_held_to_require_admin_and_the_rest_to_the_gate() -> None:
    app = _app()
    for path, route in admin_area_routes(app.routes):
        calls: set[Any] = set()
        todo = [route.dependant]
        while todo:
            node = todo.pop()
            calls.add(node.call)
            todo.extend(node.dependencies)
        assert (require_admin if path.endswith("/whoami") else admin_gate) in calls, path


def _gated() -> list[tuple[str, str]]:
    return [key for key in sorted(SAMPLES) if not key[1].endswith("/whoami")]


async def _call(aenv: AdminEnv, who: Any, method: str, template: str) -> Any:
    path, body = SAMPLES[(method, template)]
    if who is None:
        return await aenv.client.request(method, path, json=body)
    return await who.request(method, path, body)


@pytest.mark.parametrize(("method", "template"), _gated())
async def test_anonymous_is_refused_401(aenv: AdminEnv, method: str, template: str) -> None:
    response = await _call(aenv, None, method, template)
    if method in SAFE:
        assert (response.status_code, reason(response)) == (401, "not_authenticated")
    else:  # the auth middleware's Origin/CSRF check answers first for a write
        assert response.status_code in (401, 403), response.text
    assert await aenv.audit_rows() == []


@pytest.mark.parametrize(("method", "template"), _gated())
async def test_a_non_admin_is_refused_403_without_an_audit_row(
    aenv: AdminEnv, method: str, template: str
) -> None:
    user = await caller(aenv, "user@example.com", role="user")
    response = await _call(aenv, user, method, template)
    assert (response.status_code, reason(response)) == (403, "forbidden")
    assert response.headers["cache-control"] == "no-store"
    assert await aenv.audit_rows() == []


@pytest.mark.parametrize(("method", "template"), _gated())
async def test_an_admin_without_totp_is_refused_and_audited(
    aenv: AdminEnv, method: str, template: str
) -> None:
    admin = await caller(aenv, mfa=False)
    response = await _call(aenv, admin, method, template)
    assert (response.status_code, reason(response)) == (403, "mfa_required")
    (row,) = await aenv.audit_rows()
    assert (row.action, row.outcome, row.reason) == ("admin.request", "refused", "mfa_required")
    assert row.params == {"method": method, "route": template}


@pytest.mark.parametrize(("method", "template"), _gated())
async def test_an_admin_whose_totp_is_older_than_12_hours_is_refused(
    aenv: AdminEnv, method: str, template: str
) -> None:
    admin = await caller(aenv)
    await age_session(aenv, admin, mfa="12 hours 1 minute", stepup=None)
    response = await _call(aenv, admin, method, template)
    assert (response.status_code, reason(response)) == (403, "mfa_required")


@pytest.mark.parametrize(("method", "template"), [k for k in _gated() if k[0] not in SAFE])
async def test_an_admin_write_needs_a_step_up_within_5_minutes(
    aenv: AdminEnv, method: str, template: str
) -> None:
    admin = await caller(aenv, stepup=False)
    response = await _call(aenv, admin, method, template)
    assert (response.status_code, reason(response)) == (403, "step_up_required")
    admin = await caller(aenv, "second@example.com")
    await age_session(aenv, admin, mfa=None, stepup="5 minutes 5 seconds")
    response = await _call(aenv, admin, method, template)
    assert (response.status_code, reason(response)) == (403, "step_up_required")
    assert [r.reason for r in await aenv.audit_rows()] == ["step_up_required"] * 2


@pytest.mark.parametrize(("method", "template"), [k for k in _gated() if k[0] in SAFE])
async def test_an_admin_read_needs_no_step_up(aenv: AdminEnv, method: str, template: str) -> None:
    admin = await caller(aenv, stepup=False)
    response = await _call(aenv, admin, method, template)
    assert response.status_code == 200, response.text


async def test_a_body_the_route_cannot_read_still_meets_the_gate_first(aenv: AdminEnv) -> None:
    """A non-admin learns nothing about the body a route takes: the gate answers first."""
    user = await caller(aenv, "user@example.com", role="user")
    csrf = await user.csrf()
    response = await aenv.client.put(
        "/api/admin/settings/banner",
        content=b"{not json",
        headers={
            **session_cookie(user.token),
            "Origin": "https://ownvitals.test",
            "X-CSRF-Token": csrf,
            "Content-Type": "application/json",
        },
    )
    assert (response.status_code, reason(response)) == (403, "forbidden")


async def test_an_oversized_body_is_refused_and_audited(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    response = await admin.put("/api/admin/settings/banner", {"value": "x" * 20_000})
    assert (response.status_code, reason(response)) == (413, "body_too_large")
    (row,) = await aenv.audit_rows()
    assert (row.outcome, row.reason) == ("refused", "body_too_large")


async def test_an_invalid_query_after_the_gate_is_audited(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    response = await admin.get("/api/admin/jobs?days=0")
    assert response.status_code == 422
    (row,) = await aenv.audit_rows()
    assert (row.action, row.reason) == ("admin.request", "invalid_input")


async def test_the_rate_limit_answers_429_and_is_audited(aenv: AdminEnv) -> None:
    admin = await caller(aenv)
    aenv.env.app.state.admin_db.requests.per_admin = 2
    statuses = [(await admin.get("/api/admin/jobs")).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    assert (await aenv.audit_rows())[-1].reason == "rate_limited"


async def test_without_maint_credentials_the_admin_area_answers_503(plain_env: Env) -> None:
    user_id = await plain_env.user("admin@example.com", role="admin")
    issued = await plain_env.login(user_id, mfa=True)
    response = await plain_env.client.get("/api/admin/jobs", headers=session_cookie(issued.token))
    assert (response.status_code, reason(response)) == (503, "admin_unavailable")


def test_refusal_reasons_are_codes() -> None:
    for code in ("forbidden", "mfa_required", "step_up_required", "rate_limited"):
        assert re.fullmatch(r"[a-z][a-z0-9_]{0,59}", code)
