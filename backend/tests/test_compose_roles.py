"""The local stack connects with the roles of ADR 0002 section 5: migrate as the owner, web and the
worker as the app role, and no service but db and db-functions holds the superuser's password.
Only db runs the roles script; db-functions runs the job queue's script (ADR 0005 section 3), a
one-shot whose image and command are pinned here.

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


DB_FUNCTIONS_SCRIPT = ROOT / "docker" / "postgres" / "queue" / "db-functions.sh"


def check_database_passwords(compose: str) -> None:
    assert secrets_of(compose, "migrate") == {"database_owner_password"}
    for app in ("web", "worker"):
        mounted = secrets_of(compose, app)
        assert "database_password" in mounted, app
        assert mounted.isdisjoint({"postgres_password", "database_owner_password"}), app
    # The queue's one-shot: the superuser's password and nothing else (no app or owner password).
    assert secrets_of(compose, "db-functions") == {"postgres_password"}
    holders = {
        name for name in services(compose) if "postgres_password" in secrets_of(compose, name)
    }
    assert holders == {"db", "db-functions"}


def test_each_service_gets_only_its_own_database_password(compose: str) -> None:
    check_database_passwords(compose)


def _add_secret(compose: str, service: str, secret: str) -> str:
    body = block(compose, service)
    changed = body.replace("    secrets:\n", f"    secrets:\n      - {secret}\n", 1)
    assert changed != body
    return compose.replace(body, changed, 1)


@pytest.mark.parametrize(
    ("service", "secret"),
    [
        ("worker", "postgres_password"),
        ("web", "postgres_password"),
        ("worker", "database_owner_password"),
        ("db-functions", "database_password"),
        ("db-functions", "database_owner_password"),
    ],
)
def test_the_password_check_fails_on_a_wrong_mount(compose: str, service: str, secret: str) -> None:
    with pytest.raises(AssertionError):
        check_database_passwords(_add_secret(compose, service, secret))


def test_db_functions_is_a_pinned_one_shot_after_the_migrations(compose: str) -> None:
    """A change to the one-shot's image or command would run as the superuser: both are pinned.
    It runs once per start, after the migrations only; web and the worker wait for it, since
    their queue check refuses to start without the queue's functions."""
    one_shot = block(compose, "db-functions")
    image = re.search(r"^    image: (\S+)$", one_shot, re.MULTILINE)
    assert image is not None
    assert image.group(1) == "postgres:17.11"
    assert f"    image: {image.group(1)}\n" in block(compose, "db")  # the database's own image
    assert '    restart: "no"\n' in one_shot
    assert "    user: postgres\n" in one_shot
    assert '    entrypoint: ["sh", "/queue/db-functions.sh"]\n' in one_shot
    for absent in ("command:", "environment:", "ports:", "<<:"):
        assert absent not in one_shot, absent
    assert "      - ./docker/postgres/queue:/queue:ro\n" in one_shot
    depends = one_shot.split("    depends_on:\n")[1]
    assert re.findall(r"^      (\S+):$", depends, re.MULTILINE) == ["migrate"]
    for waiting in ("web", "worker"):
        assert re.search(
            r"depends_on:\n      db-functions:\n        condition: service_completed_successfully",
            block(compose, waiting),
        ), waiting


def test_the_superuser_password_never_reaches_the_environment_or_a_command_line(
    compose: str,
) -> None:
    assert "PGPASSWORD" not in compose
    command_lines = re.findall(r"^\s+(?:environment|command|entrypoint):.*$", compose, re.M)
    assert command_lines
    assert all("postgres_password" not in line for line in command_lines)
    # A tmpfs for the PGPASSFILE, private to the postgres user (uid 999 in the image).
    assert "      - /run/pgpass:mode=0700,uid=999,gid=999\n" in block(compose, "db-functions")
    script = DB_FUNCTIONS_SCRIPT.read_text(encoding="utf-8")
    assert "PGPASSWORD" not in script
    assert "postgres@" not in script  # by host, user and PGPASSFILE, never a URL
    assert "secret=/run/secrets/postgres_password" in script
    assert "pgpass=/run/pgpass/pgpass" in script
    assert script.index("umask 077") < script.index('> "$pgpass"')  # the file is mode 0600
    assert 'export PGPASSFILE="$pgpass"' in script
    assert "--single-transaction -v ON_ERROR_STOP=1" in script
    assert "-h db -U postgres -d ownvitals -f /queue/20-queue.sql" in script


def test_the_database_creates_the_roles_from_the_password_files(compose: str) -> None:
    db = block(compose, "db")
    assert "./docker/postgres/init:/docker-entrypoint-initdb.d:ro" in db
    assert secrets_of(compose, "db") == {
        "postgres_password",
        "database_owner_password",
        "database_password",
    }
    assert (ROOT / "docker" / "postgres" / "init" / "10-roles.sql").is_file()
