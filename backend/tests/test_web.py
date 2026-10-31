import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NoReturn

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from sqlalchemy.engine import URL, make_url

from ownvitals import __version__, web
from ownvitals.config import Settings
from ownvitals.web import FEATURE_ROUTERS, create_app


def make_app(database_url: str, **overrides: object) -> FastAPI:
    values: dict[str, object] = {"environment": "test", "database_url": database_url, **overrides}
    return create_app(Settings(**values))  # pyright: ignore[reportArgumentType]


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """A client for ``app`` with its lifespan (engine creation and disposal) running."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def make_client(database_url: str, **overrides: object):
    return running(make_app(database_url, **overrides))


@pytest.fixture
async def down_client(unreachable_database_url: str) -> AsyncIterator[httpx.AsyncClient]:
    """A client for an app whose database cannot be reached."""
    async with make_client(unreachable_database_url) as client:
        yield client


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>ownvitals</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log('app');", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the static dir", encoding="utf-8")
    return root


async def test_health_does_not_touch_the_database(unreachable_database_url: str) -> None:
    def no_database(*_: object) -> NoReturn:
        raise AssertionError("/api/health must not touch the database")

    app = make_app(unreachable_database_url)
    async with running(app) as client:
        app.state.session_factory = no_database
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


async def test_ready_is_200_with_a_real_database(database_url: str) -> None:
    async with make_client(database_url) as client:
        response = await client.get("/api/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_ready_uses_database_password_instead_of_the_url_password(
    database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    url = make_url(database_url)
    assert url.password, "the test database URL must carry a password"
    wrong = url.set(password="wrong-" + url.password).render_as_string(hide_password=False)
    with caplog.at_level(logging.WARNING, logger="ownvitals.web"):
        async with make_client(wrong) as client:
            refused = await client.get("/api/ready")
    assert refused.status_code == 503
    assert wrong not in caplog.text
    async with make_client(wrong, database_password=url.password) as client:
        assert (await client.get("/api/ready")).status_code == 200


async def test_ready_is_503_when_the_database_is_unreachable(
    down_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ownvitals.web"):
        response = await down_client.get("/api/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}  # no host, user or error text
    assert "not-a-real-password" not in caplog.text
    assert any(record.getMessage() == "database not ready" for record in caplog.records)


async def test_unknown_api_path_is_a_json_404(down_client: httpx.AsyncClient) -> None:
    response = await down_client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"detail": "Not Found"}


async def test_api_docs_live_under_api_and_are_off_in_prod(
    unreachable_database_url: str,
) -> None:
    async with make_client(unreachable_database_url) as client:
        assert (await client.get("/api/openapi.json")).status_code == 200
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404
    # In prod the password comes from its file (here the database_password setting), not the URL.
    url = make_url(unreachable_database_url)
    passwordless = URL.create(
        url.drivername, username=url.username, host=url.host, port=url.port, database=url.database
    )
    async with make_client(
        passwordless.render_as_string(hide_password=False),
        environment="prod",
        public_base_url="https://ownvitals.example",
        database_password=url.password,
    ) as client:
        assert (await client.get("/api/openapi.json")).status_code == 404
        assert (await client.get("/api/docs")).status_code == 404


async def test_without_static_dir_other_paths_are_404(down_client: httpx.AsyncClient) -> None:
    assert (await down_client.get("/")).status_code == 404
    assert (await down_client.get("/dashboard/1")).status_code == 404


async def test_serves_static_files_and_the_spa_fallback(
    unreachable_database_url: str, static_dir: Path
) -> None:
    index = (static_dir / "index.html").read_text(encoding="utf-8")
    async with make_client(unreachable_database_url, static_dir=static_dir) as client:
        asset = await client.get("/assets/app.js")
        assert asset.status_code == 200
        assert asset.text == "console.log('app');"

        for path in ("/", "/dashboard/42", "/assets/missing.js", "/index.html"):
            page = await client.get(path)
            assert page.status_code == 200, path
            assert page.text == index, path
            assert page.headers["content-type"].startswith("text/html"), path


async def test_api_paths_are_never_answered_by_the_spa_fallback(
    unreachable_database_url: str, static_dir: Path
) -> None:
    async with make_client(unreachable_database_url, static_dir=static_dir) as client:
        for path in ("/api", "/api/", "/api/nope", "/api/health/extra"):
            response = await client.get(path)
            assert response.status_code == 404, path
            assert response.headers["content-type"] == "application/json", path
        assert (await client.get("/api/health")).json()["status"] == "ok"


async def test_static_files_cannot_escape_the_static_dir(
    unreachable_database_url: str, static_dir: Path
) -> None:
    index = (static_dir / "index.html").read_text(encoding="utf-8")
    async with make_client(unreachable_database_url, static_dir=static_dir) as client:
        for path in ("/%2e%2e/secret.txt", "/..%2fsecret.txt", "/assets/..%2f..%2fsecret.txt"):
            response = await client.get(path)
            assert "outside the static dir" not in response.text, path
            assert response.text == index, path


async def test_a_missing_static_dir_is_logged_and_ignored(
    unreachable_database_url: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="ownvitals.web"):
        app = make_app(unreachable_database_url, static_dir=tmp_path / "missing")
    assert any("static_dir does not exist" in record.getMessage() for record in caplog.records)
    async with running(app) as client:
        assert (await client.get("/api/health")).status_code == 200
        assert (await client.get("/")).status_code == 404


async def test_static_dir_without_index_html_is_a_404_not_an_error(
    unreachable_database_url: str, tmp_path: Path
) -> None:
    async with make_client(unreachable_database_url, static_dir=tmp_path) as client:
        assert (await client.get("/dashboard/1")).status_code == 404


# --- the feature packages' routers -------------------------------------------------------------


def test_every_feature_package_has_a_router_that_the_app_includes() -> None:
    from ownvitals.admin.routes import router as admin
    from ownvitals.auth.routes import router as auth
    from ownvitals.connections.routes import router as connections
    from ownvitals.flags.routes import router as flags
    from ownvitals.jobs.routes import router as jobs

    routers = (auth, connections, flags, jobs, admin)
    assert all(isinstance(router, APIRouter) for router in routers)
    assert {id(router) for router in routers} == {id(router) for router in FEATURE_ROUTERS}


async def test_the_app_serves_the_routes_of_its_feature_routers(
    unreachable_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The stubs are empty, so prove the mount with a router that has a route, added the way an
    # issue will add one.
    router = APIRouter()

    @router.get("/api/_stub-probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    monkeypatch.setattr(web, "FEATURE_ROUTERS", (*FEATURE_ROUTERS, router))
    async with make_client(unreachable_database_url) as client:
        response = await client.get("/api/_stub-probe")
    assert response.json() == {"ok": True}


# --- the security headers ----------------------------------------------------------------------

CSP_EXPECTED = {
    "default-src": ["'self'"],
    "script-src": ["'self'"],
    "style-src": ["'self'"],
    "img-src": ["'self'"],
    "font-src": ["'self'"],
    "connect-src": ["'self'"],
    "media-src": ["'none'"],
    "object-src": ["'none'"],
    "frame-src": ["'none'"],
    "worker-src": ["'none'"],
    "manifest-src": ["'self'"],
    "base-uri": ["'none'"],
    "form-action": ["'self'"],
    "frame-ancestors": ["'none'"],
}


def csp_of(response: httpx.Response) -> dict[str, list[str]]:
    assert len(response.headers.get_list("content-security-policy")) == 1  # one policy, no merge
    directives: dict[str, list[str]] = {}
    for part in response.headers["content-security-policy"].split(";"):
        name, *values = part.split()
        assert name not in directives, name
        directives[name] = values
    return directives


def assert_secure_headers(response: httpx.Response, path: str) -> None:
    assert csp_of(response) == CSP_EXPECTED, path
    assert response.headers.get_list("referrer-policy") == ["no-referrer"], path
    assert response.headers.get_list("x-content-type-options") == ["nosniff"], path


async def test_every_response_carries_the_csp_and_no_referrer(
    unreachable_database_url: str, static_dir: Path
) -> None:
    """The SPA page, its files and the fallback, and the API (200, 404 and 503) all carry the
    one policy: everything from our own origin, nothing inline, no framing, no referrer, and no
    type sniffing."""
    async with make_client(unreachable_database_url, static_dir=static_dir) as client:
        for path in ("/", "/dashboard/42", "/assets/app.js", "/index.html"):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert_secure_headers(response, path)
        for path, status in (("/api/health", 200), ("/api/ready", 503), ("/api/nope", 404)):
            response = await client.get(path)
            assert response.status_code == status, path
            assert_secure_headers(response, path)


def test_the_policy_names_no_other_host_and_nothing_unsafe() -> None:
    from ownvitals.headers import CONTENT_SECURITY_POLICY

    assert "unsafe" not in CONTENT_SECURITY_POLICY
    assert "data:" not in CONTENT_SECURITY_POLICY
    assert "*" not in CONTENT_SECURITY_POLICY
    assert "://" not in CONTENT_SECURITY_POLICY  # 'self' only: no host at all
