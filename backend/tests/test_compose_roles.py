"""The local stack connects with the roles of ADR 0002 section 5: migrate as the owner, web and the
worker as the app role, web alone as the admin role, and no service but db and db-functions holds
the superuser's password. Only db runs the roles script; db-functions runs the job queue's script,
30-maint.sql, then 40-withings.sql (ADR 0005 section 3, #126, #21) in one transaction, a one-shot
whose image, command and (absent) mounts are pinned here. No service reads SQL from a host path
but db's first-start
init directory (test_compose_host_sql.py), and db runs postgres by the digest CI runs.

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
    web = secrets_of(compose, "web")
    assert {"database_password", "database_maint_password"} <= web
    assert web.isdisjoint({"postgres_password", "database_owner_password"})
    worker = secrets_of(compose, "worker")
    assert "database_password" in worker
    assert worker.isdisjoint(
        {"postgres_password", "database_owner_password", "database_maint_password"}
    )
    # The queue's one-shot: the superuser's password and nothing else.
    assert secrets_of(compose, "db-functions") == {"postgres_password"}
    holders = {
        name for name in services(compose) if "postgres_password" in secrets_of(compose, name)
    }
    assert holders == {"db", "db-functions"}
    maint_holders = {
        name for name in services(compose) if "database_maint_password" in secrets_of(compose, name)
    }
    assert maint_holders == {"db", "web"}


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
        ("worker", "database_maint_password"),
        ("migrate", "database_maint_password"),
        ("db-functions", "database_password"),
        ("db-functions", "database_owner_password"),
        ("db-functions", "database_maint_password"),
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
        "docker/postgres/queue/db-functions.sh /app/db-functions/",
        "COPY --chmod=0555 docker/postgres/maint/30-maint.sql /app/db-functions/",
        "COPY --chmod=0555 docker/postgres/withings/40-withings.sql /app/db-functions/",
        "COPY --chmod=0555 docker/postgres/withings/rate.sql /app/db-functions/",
        "COPY --chmod=0555 docker/postgres/notify/50-outbox.sql /app/db-functions/",
    ]
    user = dockerfile.index("\nUSER ownvitals\n")
    assert all(dockerfile.index(copy) < user for copy in copies)


def test_the_database_creates_the_roles_from_the_password_files(compose: str) -> None:
    db = block(compose, "db")
    assert "./docker/postgres/init:/docker-entrypoint-initdb.d:ro" in db
    assert secrets_of(compose, "db") == {
        "postgres_password",
        "database_owner_password",
        "database_password",
        "database_maint_password",
    }
    script = (ROOT / "docker" / "postgres" / "init" / "10-roles.sql").read_text(encoding="utf-8")
    assert script
    assert "DATABASE_MAINT_PASSWORD_FILE" in script
    assert "/run/secrets/database_maint_password" in script
    assert "CREATE ROLE ownvitals_maint LOGIN" in script
    assert "ALTER ROLE ownvitals_maint WITH LOGIN PASSWORD :'maint_password';" in script


MAINT_URL = "OWNVITALS_MAINT_DATABASE_URL: postgresql+psycopg://ownvitals_maint@db:5432/ownvitals"


def check_maint_url(compose: str) -> None:
    """The admin URL is on web only, never on the shared environment the worker inherits."""
    assert compose.count(MAINT_URL) == 1
    assert MAINT_URL in block(compose, "web")
    shared = compose[compose.index("x-app: &app") : compose.index("\nservices:")]
    assert "OWNVITALS_MAINT_DATABASE_URL" not in shared
    for name in ("worker", "migrate", "db-functions", "db", "mailpit"):
        assert "OWNVITALS_MAINT_DATABASE_URL" not in block(compose, name), name


def test_only_web_gets_the_maint_url(compose: str) -> None:
    check_maint_url(compose)


def test_the_maint_url_check_fails_when_the_worker_would_inherit_it(compose: str) -> None:
    planted = compose.replace(
        "    OWNVITALS_DATABASE_URL:",
        f"    {MAINT_URL}\n    OWNVITALS_DATABASE_URL:",
        1,
    )
    assert planted != compose
    with pytest.raises(AssertionError):
        check_maint_url(planted)


def test_db_functions_installs_maint_after_the_queue() -> None:
    """The five files go to install_functions, queue first, in the one transaction it runs."""
    script = DB_FUNCTIONS_SCRIPT.read_text(encoding="utf-8")
    assert script.rstrip("\n").endswith(
        "exec python -m ownvitals.db.install_functions \\\n"
        '  "host=db port=5432 dbname=ownvitals user=postgres" \\\n'
        "  /app/db-functions/20-queue.sql /app/db-functions/30-maint.sql \\\n"
        "  /app/db-functions/40-withings.sql /app/db-functions/rate.sql \\\n"
        "  /app/db-functions/50-outbox.sql"
    )
    assert "psql" not in script
    from ownvitals.db.install_functions import read_scripts

    sql = read_scripts(
        [
            str(ROOT / "docker" / "postgres" / "queue" / "20-queue.sql"),
            str(ROOT / "docker" / "postgres" / "maint" / "30-maint.sql"),
            str(ROOT / "docker" / "postgres" / "withings" / "40-withings.sql"),
            str(ROOT / "docker" / "postgres" / "withings" / "rate.sql"),
            str(ROOT / "docker" / "postgres" / "notify" / "50-outbox.sql"),
        ]
    )
    assert sql.index("ownvitals_queue") < sql.index("ownvitals_maint")
    assert sql.index("ownvitals_maint") < sql.index(
        "CREATE OR REPLACE FUNCTION public.ensure_month_partitions"
    )
    # The outbox (#194) last: its role and functions after the queue's admission list.
    assert sql.index("CREATE OR REPLACE FUNCTION public.withings_rate_claim") < sql.index(
        "CREATE OR REPLACE FUNCTION public.mail_outbox_claim"
    )


def test_ci_creates_the_maint_role_from_its_password_file() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = ci.split("- name: Create the application database and its roles", 1)[1].split(
        "\n      - uses:", 1
    )[0]
    assert 'openssl rand -hex 16 > "$secrets/database_maint_password"' in step
    assert 'export DATABASE_MAINT_PASSWORD_FILE="$secrets/database_maint_password"' in step
    assert step.index('"$secrets/database_maint_password"') < step.index("10-roles.sql")
    assert "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'ownvitals_maint'" in step


async def test_with_the_maint_url_and_password_admin_routes_are_not_unavailable() -> None:
    """503 ``admin_unavailable`` is ``AdminDb`` being ``None``. Both settings build an engine,
    which is what web's lifespan stores before the admin routes run."""
    from ownvitals.admin.db import AdminDb
    from ownvitals.config import Settings

    url = "postgresql+psycopg://ownvitals_maint@db:5432/ownvitals"
    assert (
        AdminDb.from_settings(
            Settings(maint_database_url=url)  # pyright: ignore[reportArgumentType]
        )
        is None
    )
    password = "synthetic-maint-password"
    assert (
        AdminDb.from_settings(
            Settings(database_maint_password=password)  # pyright: ignore[reportArgumentType]
        )
        is None
    )
    admin = AdminDb.from_settings(
        Settings(
            maint_database_url=url,  # pyright: ignore[reportArgumentType]
            database_maint_password=password,  # pyright: ignore[reportArgumentType]
        )
    )
    assert admin is not None
    await admin.dispose()
