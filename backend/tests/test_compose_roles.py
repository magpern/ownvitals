"""The local stack connects with the roles of ADR 0002 section 5: migrate as the owner, web and the
worker as the app role, and no service but db and db-functions holds the superuser's password.
Only db runs the roles script; db-functions runs the job queue's script (ADR 0005 section 3), a
one-shot whose image, command and (absent) mounts are pinned here. No service reads SQL from a
host path but db's first-start init directory, and db runs postgres by the digest CI runs.

Reads compose.yaml (and the Dockerfile and ci.yml) at the repository root as text, like
``test_image.py``."""

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


def app_image(compose: str) -> str:
    shared = compose[compose.index("x-app: &app") : compose.index("\nservices:")]
    found = re.search(r"^  image: (\S+)$", shared, re.MULTILINE)
    assert found is not None
    return found.group(1)


def check_db_functions(compose: str) -> None:
    """The one-shot runs the application image (on the server, the deployed release's digest),
    with its entrypoint from the image and nothing mounted: no volume of any kind, no environment,
    no port, no other user than the image's."""
    one_shot = block(compose, "db-functions")
    keys = re.findall(r"^    (\w[\w-]*):", one_shot, re.MULTILINE)
    assert keys == ["build", "image", "restart", "entrypoint", "secrets", "tmpfs", "depends_on"]
    assert "    build: .\n" in one_shot
    assert f"    image: {app_image(compose)}\n" in one_shot
    assert '    restart: "no"\n' in one_shot
    assert '    entrypoint: ["sh", "/app/db-functions/db-functions.sh"]\n' in one_shot
    # The tmpfs is the only mount besides the secret, private to the image's user (uid 10001).
    tmpfs = one_shot.split("    tmpfs:\n")[1].split("    depends_on:\n")[0]
    assert tmpfs == "      - /run/pgpass:mode=0700,uid=10001,gid=10001\n"
    depends = one_shot.split("    depends_on:\n")[1]
    assert re.findall(r"^      (\S+):$", depends, re.MULTILINE) == ["migrate"]
    assert "        condition: service_completed_successfully\n" in depends


def test_db_functions_is_a_pinned_one_shot_after_the_migrations(compose: str) -> None:
    """A change to the one-shot's image, command or mounts would run as the superuser: all are
    pinned. It runs once per start, after the migrations only; web and the worker wait for it,
    since their queue check refuses to start without the queue's functions."""
    check_db_functions(compose)
    for waiting in ("web", "worker"):
        assert re.search(
            r"depends_on:\n      db-functions:\n        condition: service_completed_successfully",
            block(compose, waiting),
        ), waiting


def _insert_after_tmpfs(compose: str, lines: str) -> str:
    body = block(compose, "db-functions")
    anchor = "      - /run/pgpass:mode=0700,uid=10001,gid=10001\n"
    changed = body.replace(anchor, anchor + lines, 1)
    assert changed != body
    return compose.replace(body, changed, 1)


@pytest.mark.parametrize(
    "lines",
    [
        "    volumes:\n      - ./secrets:/run/more:ro\n",
        "    volumes:\n      - ./docker/postgres/queue:/app/db-functions:ro\n",
        "    volumes:\n      - /var/run/postgresql:/var/run/postgresql\n",
        "    volumes:\n      - db-data:/data:ro\n",
        "    environment:\n      PGPASSFILE: /run/pgpass/pgpass\n",
        "    user: root\n",
        '    command: ["x"]\n',
        "      - /tmp\n",
    ],
)
def test_the_one_shot_check_fails_on_any_added_mount_or_setting(compose: str, lines: str) -> None:
    with pytest.raises(AssertionError):
        check_db_functions(_insert_after_tmpfs(compose, lines))


# The one host path holding SQL a service may read: the postgres image's init directory, run only
# on the first start of an empty volume. It creates the roles, not the definer functions a deploy
# updates (docs/dev/stack.md, "Where the database's SQL comes from").
ALLOWED_SQL_MOUNTS = {("db", "./docker/postgres/init:/docker-entrypoint-initdb.d:ro")}
HOST_PATH = re.compile(r"^(?:\.{1,2}(?:/|$)|/|~|\$|[A-Za-z]:[\\/])")


def _holds_sql(source: str) -> bool:
    """True when the host path is an SQL file or a directory with one anywhere below it. A path
    that can't be inspected (absolute, a variable, the home directory, outside the repository)
    counts as holding SQL: the check fails closed."""
    if not source.startswith("."):
        return True
    path = (ROOT / source).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        return True
    if path.is_dir():
        return any(p.is_file() for p in path.rglob("*.sql"))
    return path.suffix.lower() == ".sql"


def sql_mounts(compose: str) -> set[tuple[str, str]]:
    """Every (service, mount) whose host path holds SQL. The long volume syntax, a mount in the
    shared x-app block, `configs:` and a named volume backed by a host path are refused outright,
    since the short syntax below is all this reads."""
    assert not re.search(r"^\s+- type:", compose, re.MULTILINE), "long volume syntax"
    assert not re.search(r"^\s*configs:", compose, re.MULTILINE), "configs"
    shared = compose[compose.index("x-app: &app") : compose.index("\nservices:")]
    assert "volumes:" not in shared
    top = compose[compose.index("\nvolumes:\n") :]
    assert top == "\nvolumes:\n  db-data:\n", "named volumes carry no driver options"
    found = set()
    for service in services(compose):
        section = re.search(r"^    volumes:\n((?:      .*\n)+)", block(compose, service), re.M)
        if section is None:
            continue
        for mount in re.findall(r"^      - (\S+)$", section.group(1), re.MULTILINE):
            source = mount.split(":", 1)[0]
            if HOST_PATH.match(source) and _holds_sql(source):
                found.add((service, mount))
    for source in re.findall(r"^    file: (\S+)$", compose[compose.index("\nsecrets:\n") :], re.M):
        if _holds_sql(source):
            found.add(("secrets", source))
    return found


def test_no_service_reads_sql_from_a_host_path(compose: str) -> None:
    """The database's functions come from the release image, never from the host: a deploy then
    installs the release's functions and a rollback the previous release's (docs/dev/deploy.md)."""
    assert sql_mounts(compose) == ALLOWED_SQL_MOUNTS


@pytest.mark.parametrize(
    ("service", "mount"),
    [
        ("db-functions", "./docker/postgres/queue:/app/db-functions:ro"),
        ("db-functions", "./docker/postgres/queue/20-queue.sql:/app/db-functions/20-queue.sql:ro"),
        ("db-functions", "./docker:/x:ro"),
        ("worker", "/srv/sql:/sql:ro"),
        ("db", "./docker/postgres/queue:/docker-entrypoint-initdb.d/q:ro"),
    ],
)
def test_the_host_sql_check_fails_on_a_planted_mount(
    compose: str, service: str, mount: str
) -> None:
    body = block(compose, service)
    if "    volumes:\n" in body:
        changed = body.replace("    volumes:\n", f"    volumes:\n      - {mount}\n", 1)
    else:
        changed = body.rstrip("\n") + f"\n    volumes:\n      - {mount}\n\n"
    planted = compose.replace(body, changed, 1)
    assert planted != compose
    assert sql_mounts(planted) != ALLOWED_SQL_MOUNTS


POSTGRES_PIN = re.compile(r"postgres:17\.11@(sha256:[0-9a-f]{64})")


def test_the_database_image_is_pinned_by_the_same_digest_as_ci(compose: str) -> None:
    """`db` runs postgres:17.11 by its multi-arch index digest, the one CI's backend service
    runs (the queue digests depend on how that image renders the functions; docs/dev/jobs.md)."""
    db = re.search(r"^    image: (\S+)$", block(compose, "db"), re.MULTILINE)
    assert db is not None
    pinned = POSTGRES_PIN.fullmatch(db.group(1))
    assert pinned is not None, db.group(1)
    images = re.findall(r"^\s+image: (\S*postgres\S*)$", compose, re.MULTILINE)
    assert images == [db.group(1)]  # no other service runs a postgres image
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.findall(r"^\s+image: (postgres\S*)$", ci, re.MULTILINE) == [db.group(1)]


def test_the_superuser_password_never_reaches_the_environment_or_a_command_line(
    compose: str,
) -> None:
    assert "PGPASSWORD" not in compose
    command_lines = re.findall(r"^\s+(?:environment|command|entrypoint):.*$", compose, re.M)
    assert command_lines
    assert all("postgres_password" not in line for line in command_lines)
    script = DB_FUNCTIONS_SCRIPT.read_text(encoding="utf-8")
    assert "PGPASSWORD" not in script
    assert "postgres@" not in script  # by host, user and PGPASSFILE, never a URL
    assert "secret=/run/secrets/postgres_password" in script
    assert "pgpass=/run/pgpass/pgpass" in script
    assert script.index("umask 077") < script.index('> "$pgpass"')  # the file is mode 0600
    assert 'export PGPASSFILE="$pgpass"' in script


def test_the_image_carries_the_functions_and_their_entrypoint() -> None:
    """The release image holds the SQL and the entrypoint db-functions runs, owned by root."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copies = re.findall(r"^COPY .*db-functions.*$", dockerfile, re.MULTILINE)
    assert copies == [
        "COPY --chmod=0555 docker/postgres/queue/20-queue.sql "
        "docker/postgres/queue/db-functions.sh /app/db-functions/"
    ]
    assert dockerfile.index(copies[0]) < dockerfile.index("\nUSER ownvitals\n")


def test_the_database_creates_the_roles_from_the_password_files(compose: str) -> None:
    db = block(compose, "db")
    assert "./docker/postgres/init:/docker-entrypoint-initdb.d:ro" in db
    assert secrets_of(compose, "db") == {
        "postgres_password",
        "database_owner_password",
        "database_password",
    }
    assert (ROOT / "docker" / "postgres" / "init" / "10-roles.sql").is_file()
