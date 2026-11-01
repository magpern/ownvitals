"""The image defaults to prod; the local stack says dev itself and keeps passwords out of URLs.

Reads the Dockerfile and compose.yaml at the repository root as text."""

import re

import pytest

from dbtools import BACKEND_DIR
from ownvitals.config import SECRET_FILE_FIELDS

ROOT = BACKEND_DIR.parent


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> str:
    return (ROOT / "compose.yaml").read_text(encoding="utf-8")


def service_block(compose: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S|\Z)", compose, re.MULTILINE | re.DOTALL)
    assert match, f"no service {name}"
    return match.group(1)


def test_the_image_defaults_to_the_prod_environment(dockerfile: str) -> None:
    runtime = dockerfile[dockerfile.index("AS runtime") :]
    assert re.search(r"^\s+OWNVITALS_ENVIRONMENT=prod\b", runtime, re.MULTILINE)
    assert not re.search(r"OWNVITALS_ENVIRONMENT=(dev|test)\b", dockerfile)


def test_the_compose_stack_sets_dev_explicitly_and_only_dev(compose: str) -> None:
    assert re.findall(r"OWNVITALS_ENVIRONMENT:\s*(\S+)", compose) == ["dev"]
    shared = compose[compose.index("x-app: &app") : compose.index("\nservices:")]
    assert "environment: &app-env" in shared
    assert "OWNVITALS_ENVIRONMENT: dev" in shared
    # Every service that runs the image merges the shared block, environment included.
    for name in ("migrate", "web"):
        block = service_block(compose, name)
        assert "<<: *app" in block, name
        assert "OWNVITALS_ENVIRONMENT" not in block, name  # inherited, not overridden


def test_the_compose_database_urls_carry_no_password(compose: str) -> None:
    urls = re.findall(r"OWNVITALS_\w*DATABASE_URL:\s*(\S+)", compose)
    assert len(urls) >= 2, "the stack sets the app and the migrations URL"
    for url in urls:
        assert re.fullmatch(r"postgresql\+psycopg://\w+@[\w.-]+:\d+/\w+", url), url


def test_the_compose_stack_passes_no_secret_through_the_environment(compose: str) -> None:
    names = set(re.findall(r"^\s+(OWNVITALS_\w+):", compose, re.MULTILINE))
    assert "OWNVITALS_ENVIRONMENT" in names
    assert names.isdisjoint({f"OWNVITALS_{field.upper()}" for field in SECRET_FILE_FIELDS})


PINNED = re.compile(r"[a-z0-9][a-z0-9./-]*(?::[0-9]+)?/?[a-z0-9./_-]*:[\w.-]+@sha256:[0-9a-f]{64}")


def check_dockerfile_pins(dockerfile: str) -> None:
    """Every image the build pulls is pinned as ``name:tag@sha256:<index digest>``: the syntax
    line, each FROM, and nothing pulled through ``--from`` or ``--mount=from`` but an earlier
    stage. A tag alone would let a re-pushed tag change what a release contains."""
    syntax = re.findall(r"^# syntax=(\S+)$", dockerfile, re.MULTILINE)
    assert len(syntax) == 1, "one syntax line"
    assert PINNED.fullmatch(syntax[0]), syntax[0]
    stages: set[str] = set()
    froms = re.findall(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?\s*$", dockerfile, re.MULTILINE | re.I)
    assert len(froms) == len(re.findall(r"^FROM\b", dockerfile, re.MULTILINE | re.I))
    assert froms
    for image, stage in froms:
        assert image in stages or PINNED.fullmatch(image), image
        if stage:
            stages.add(stage)
    for source in re.findall(r"--(?:mount=)?from=([^\s,]+)", dockerfile):
        assert source in stages, f"--from={source} is not an earlier stage"


def test_every_image_the_build_pulls_is_pinned_by_digest(dockerfile: str) -> None:
    check_dockerfile_pins(dockerfile)


@pytest.mark.parametrize(
    ("pattern", "replacement"),
    [
        (r"^(# syntax=\S+?)@sha256:[0-9a-f]{64}$", r"\1"),
        (r"^(FROM ghcr\.io/astral-sh/uv:\S+?)@sha256:[0-9a-f]{64}", r"\1"),
        (r"^(FROM python:\S+?)@sha256:[0-9a-f]{64}", r"\1"),
        (r"^(FROM python:\S+?)@sha256:[0-9a-f]{64}", r"\1@sha256:abc"),
        (r"--mount=from=uv,", "--mount=from=ghcr.io/astral-sh/uv:latest,"),
        (r"^(COPY backend/alembic\.ini)", r"COPY --from=busybox:latest /bin/sh /x\n\1"),
    ],
)
def test_the_pin_check_fails_without_a_digest(
    dockerfile: str, pattern: str, replacement: str
) -> None:
    unpinned = re.sub(pattern, replacement, dockerfile, count=1, flags=re.MULTILINE)
    assert unpinned != dockerfile
    with pytest.raises(AssertionError):
        check_dockerfile_pins(unpinned)
