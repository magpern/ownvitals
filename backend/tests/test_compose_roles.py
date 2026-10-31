"""The local stack connects with the roles of ADR 0002 section 5: migrate as the owner, web as the
app role, and no service but db holds the superuser's password or runs the roles script.

Reads compose.yaml at the repository root as text, like ``test_image.py``."""

import re

import pytest

from dbtools import BACKEND_DIR

ROOT = BACKEND_DIR.parent


@pytest.fixture(scope="module")
def compose() -> str:
    return (ROOT / "compose.yaml").read_text(encoding="utf-8")


def block(compose: str, name: str) -> str:
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S|\Z)", compose, re.MULTILINE | re.DOTALL)
    assert match, f"no {name}"
    return match.group(1)


def secrets_of(compose: str, service: str) -> set[str]:
    """The secrets a service mounts (``- name`` or ``- source: name``)."""
    found = re.search(r"^    secrets:\n((?:      .*\n)+)", block(compose, service), re.MULTILINE)
    if found is None:
        return set()
    return set(re.findall(r"^      - (?:source: )?(\w+)", found.group(1), re.MULTILINE))


def services(compose: str) -> list[str]:
    section = compose[compose.index("\nservices:\n") : compose.index("\nsecrets:\n")]
    return re.findall(r"^  (\w[\w-]*):\n", section, re.MULTILINE)


def test_the_app_and_the_migrations_connect_as_their_roles(compose: str) -> None:
    shared = compose[compose.index("x-app: &app") : compose.index("\nservices:")]
    assert re.search(r"OWNVITALS_DATABASE_URL: postgresql\+psycopg://ownvitals_app@db:", shared)
    assert re.search(
        r"OWNVITALS_MIGRATIONS_DATABASE_URL: postgresql\+psycopg://ownvitals_owner@db:",
        block(compose, "migrate"),
    )
    assert "postgres@" not in compose  # nothing connects as the superuser


def test_each_service_gets_only_its_own_database_password(compose: str) -> None:
    assert secrets_of(compose, "migrate") == {"database_owner_password"}
    web = secrets_of(compose, "web")
    assert "database_password" in web
    assert web.isdisjoint({"postgres_password", "database_owner_password"})
    holders = {
        name for name in services(compose) if "postgres_password" in secrets_of(compose, name)
    }
    assert holders == {"db"}


def test_the_database_creates_the_roles_from_the_password_files(compose: str) -> None:
    db = block(compose, "db")
    assert "./docker/postgres/init:/docker-entrypoint-initdb.d:ro" in db
    assert secrets_of(compose, "db") == {
        "postgres_password",
        "database_owner_password",
        "database_password",
    }
    assert (ROOT / "docker" / "postgres" / "init" / "10-roles.sql").is_file()
