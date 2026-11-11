"""The public site's serving rules (ADR 0016): containment, exact noindex, cache, no cookies.

Synthetic files only; the database is unreachable (the public routes never touch it).
"""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from ownvitals.config import Settings
from ownvitals.public_site import PUBLIC_PATHS, public_relative_file
from ownvitals.web import create_app

SHELL = "<!doctype html><title>app shell</title>"
HOME = "<!doctype html><title>synthetic public home</title>"
FEATURES = "<!doctype html><title>synthetic public features</title>"
SECRET = "outside the public dir"


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    static, public = tmp_path / "dist", tmp_path / "dist-public"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(SHELL, encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log('app');", encoding="utf-8")
    (public / "features").mkdir(parents=True)
    (public / "sv" / "features").mkdir(parents=True)
    (public / "index.html").write_text(HOME, encoding="utf-8")
    (public / "features" / "index.html").write_text(FEATURES, encoding="utf-8")
    (public / "robots.txt").write_text("User-agent: *\nDisallow: /api/\n", encoding="utf-8")
    (public / "sitemap.xml").write_text("<urlset/>", encoding="utf-8")
    (public / "internal.html").write_text("not listed", encoding="utf-8")
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    return static, public


@pytest.fixture
async def client(
    dirs: tuple[Path, Path], unreachable_database_url: str
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        environment="test",
        database_url=unreachable_database_url,  # pyright: ignore[reportArgumentType]
        static_dir=dirs[0],
        public_dir=dirs[1],
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def test_the_path_set_is_exact() -> None:
    assert public_relative_file("") is not None
    assert public_relative_file("sv") is not None
    assert public_relative_file("features/") is None  # one trailing-slash form only
    assert public_relative_file("features/x") is None
    assert public_relative_file("internal.html") is None
    assert public_relative_file("../features") is None
    assert "features" in PUBLIC_PATHS
    assert "sv/features" in PUBLIC_PATHS


async def test_listed_paths_are_served_from_the_public_dir(client: httpx.AsyncClient) -> None:
    for path, body in (("/", HOME), ("/features", FEATURES)):
        response = await client.get(path)
        assert response.text == body, path
        assert "x-robots-tag" not in response.headers, path
        assert response.headers["cache-control"] == "public, max-age=300", path
        assert "set-cookie" not in response.headers, path
        assert "content-security-policy" in response.headers, path
    robots = await client.get("/robots.txt")
    assert robots.headers["content-type"].startswith("text/plain")
    assert "Disallow: /api/" in robots.text
    assert (await client.get("/sitemap.xml")).status_code == 200
    head = await client.head("/features")
    assert head.status_code == 200
    assert "x-robots-tag" not in head.headers


async def test_everything_else_is_noindex_and_not_public_cached(
    client: httpx.AsyncClient,
) -> None:
    # Listing "/" exempts nothing else: the shell, the app files, the API and errors.
    for path in (
        "/x",
        "/dashboard/1",
        "/assets/app.js",
        "/internal.html",
        "/features/",
        "/api/health",
        "/api/nope",
    ):
        response = await client.get(path)
        assert response.headers["x-robots-tag"] == "noindex, nofollow", path
        assert "public" not in response.headers.get("cache-control", ""), path


async def test_a_listed_path_that_falls_through_is_noindex(client: httpx.AsyncClient) -> None:
    # "/privacy" is listed but its page is not built (the legal flag is off): the shell answers.
    for path in ("/privacy", "/sv/features", "/sv"):
        response = await client.get(path)
        assert response.text == SHELL, path
        assert response.headers["x-robots-tag"] == "noindex, nofollow", path
        assert "public" not in response.headers.get("cache-control", ""), path


async def test_traversal_and_nul_never_reach_the_public_dir(client: httpx.AsyncClient) -> None:
    for path in (
        "/..%2fsecret.txt",
        "/%2e%2e/secret.txt",
        "/sv/..%2f..%2fsecret.txt",
        "/features/..%2f..%2fsecret.txt",
        "/sv%2f..%2f..%2fsecret.txt",
        "/%2e%2e%2fdist-public%2findex.html",
        "/features%00",
        "/%00",
        "/robots.txt%00",
    ):
        response = await client.get(path)
        assert SECRET not in response.text, path
        assert response.text != HOME, path
        assert response.headers["x-robots-tag"] == "noindex, nofollow", path


async def test_a_symlink_out_of_the_public_dir_is_not_served(
    dirs: tuple[Path, Path], client: httpx.AsyncClient, tmp_path: Path
) -> None:
    public = dirs[1]
    (public / "contact").mkdir()
    link = public / "contact" / "index.html"
    try:
        link.symlink_to(tmp_path / "secret.txt")
    except OSError:  # symlinks not permitted on this machine
        pytest.skip("cannot create symlinks")
    (public / "terms").symlink_to(tmp_path)  # a directory link whose index.html is outside
    (tmp_path / "index.html").write_text(SECRET, encoding="utf-8")
    for path in ("/contact", "/terms"):
        response = await client.get(path)
        assert SECRET not in response.text, path
        assert response.text == SHELL, path
        assert response.headers["x-robots-tag"] == "noindex, nofollow", path


async def test_public_files_only_without_static_dir_is_not_served(
    dirs: tuple[Path, Path], unreachable_database_url: str
) -> None:
    app = create_app(
        Settings(
            environment="test",
            database_url=unreachable_database_url,  # pyright: ignore[reportArgumentType]
            public_dir=dirs[1],
        )
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get("/")
    assert response.status_code == 404
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


async def test_a_listed_path_that_is_a_directory_is_not_served(
    dirs: tuple[Path, Path], client: httpx.AsyncClient
) -> None:
    public = dirs[1]
    (public / "contact" / "index.html").mkdir(parents=True)  # index.html is a directory
    (public / "robots.txt").unlink()
    (public / "robots.txt").mkdir()
    for path in ("/contact", "/robots.txt"):
        response = await client.get(path)
        assert response.status_code == 200, path
        assert response.text == SHELL, path
        assert response.headers["x-robots-tag"] == "noindex, nofollow", path
