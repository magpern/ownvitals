"""No compose service reads SQL from a host path (docs/dev/stack.md, "Where the database's SQL
comes from"): the functions come from the release image, so a deploy installs the release's and a
rollback the previous release's. The one exception is db's first-start init directory.

The check parses every compose file at the repository root as YAML, in every volume spelling,
and fails closed on what Compose could use to bring in another file (include, extends, develop,
configs, volumes_from, override files, COMPOSE_FILE in .env.example, a tracked .env)."""

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from test_compose_roles import ROOT, block
from test_compose_roles import compose as compose

# The one host path holding SQL a service may read: the postgres image's init directory, run only
# on the first start of an empty volume. It creates the roles, not the definer functions a deploy
# updates (docs/dev/stack.md, "Where the database's SQL comes from").
ALLOWED_INIT = "./docker/postgres/init:/docker-entrypoint-initdb.d:ro"
ALLOWED_SQL_MOUNTS = {("db", ALLOWED_INIT)}
QUEUE_DIR = "./docker/postgres/queue"
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
# Keys that bring in host files or other compose files besides volumes: refused outright. The
# repository's compose uses none of Compose's merging features, so the check fails closed on them.
REFUSED_SERVICE_KEYS = ("volumes_from", "configs", "extends", "develop")
REFUSED_TOP_LEVEL_KEYS = ("configs", "include")
# The compose files of the repository (the check owns this glob), and the ones Compose merges by
# default beside compose.yaml, which must not exist at all.
COMPOSE_GLOBS = ("compose*.y*ml", "docker-compose*.y*ml")
DEFAULT_OVERRIDE = re.compile(r"(docker-)?compose\.override\.ya?ml")


def _holds_sql(source: str) -> bool:
    """True when the host path is an SQL file or a directory with one anywhere below it. A path
    that can't be inspected (absolute, a variable, the home directory, outside the repository)
    counts as holding SQL: the check fails closed."""
    if not source.startswith(".") or "$" in source:
        return True
    path = (ROOT / source).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        return True
    if path.is_dir():
        return any(p.is_file() for p in path.rglob("*.sql"))
    return path.suffix.lower() == ".sql"


def _is_host_path(source: str) -> bool:
    """Compose reads a short-syntax source as a host path when it starts with ``.``, ``/`` or
    ``~`` (or a drive letter); anything else is a named volume. A variable can be either: host."""
    return source.startswith((".", "/", "~", "$")) or bool(WINDOWS_DRIVE.match(source))


def _short_source(entry: str) -> str | None:
    """The host path of a short-syntax entry, or None for a named or anonymous volume."""
    if WINDOWS_DRIVE.match(entry):
        return entry  # C:\\x:/y: the whole entry is a host path; it can't be inspected
    if ":" not in entry:
        # An anonymous volume at a container path (``/data``); a relative path alone is refused.
        return None if entry.startswith("/") else entry
    source = entry.split(":", 1)[0]
    return source if _is_host_path(source) else None


def _long_source(entry: dict[str, Any], named: set[str]) -> str | None:
    kind = entry.get("type")
    if kind == "bind":
        return str(entry.get("source", ""))
    if kind == "volume":
        assert entry.get("source") in named, f"unknown named volume {entry.get('source')!r}"
        return None
    assert kind == "tmpfs", f"volume type {kind!r} is not allowed"
    return None


def load_compose(text: str) -> dict[str, Any]:
    loaded = yaml.safe_load(text)  # merge keys (<<: *app) are resolved
    assert isinstance(loaded, dict)
    return loaded


def sql_mounts(compose: str) -> set[tuple[str, str]]:
    """Every (service, mount) whose host path holds SQL, read from the parsed YAML: short
    entries (plain, quoted, in a block or a flow list) and long ones (``type: bind``). Whatever
    this does not understand is refused: another volume type, ``volumes_from``, ``configs``,
    ``extends``, ``develop``, a top-level ``include``, a named volume with driver options, a
    volumes value that is not a list."""
    loaded = load_compose(compose)
    for key in REFUSED_TOP_LEVEL_KEYS:
        assert key not in loaded, key
    named_volumes = loaded.get("volumes") or {}
    assert isinstance(named_volumes, dict)
    for name, spec in named_volumes.items():
        assert not spec, f"named volume {name} carries options"
    named = set(named_volumes)
    found: set[tuple[str, str]] = set()
    for service, spec in (loaded.get("services") or {}).items():
        assert isinstance(spec, dict), service
        for key in REFUSED_SERVICE_KEYS:
            assert key not in spec, f"{service}: {key}"
        volumes = spec.get("volumes", [])
        assert isinstance(volumes, list), f"{service}: volumes is not a list"
        for entry in volumes:
            if isinstance(entry, str):
                source, shown = _short_source(entry), entry
            else:
                assert isinstance(entry, dict), f"{service}: {entry!r}"
                source, shown = _long_source(entry, named), repr(entry)
            if source is not None and _holds_sql(source):
                found.add((service, shown))
    for name, secret in (loaded.get("secrets") or {}).items():
        assert isinstance(secret, dict), name
        if "file" in secret and _holds_sql(str(secret["file"])):
            found.add(("secrets", str(secret["file"])))
    return found


def passes(compose: str) -> bool:
    try:
        return sql_mounts(compose) == ALLOWED_SQL_MOUNTS
    except AssertionError:
        return False


def check_compose_files(root: Path) -> None:
    """Every compose file at ``root``: none that Compose merges by default (an override file), and
    in each other one no host SQL beyond the allowed init mount and no merging feature."""
    files = sorted({p for pattern in COMPOSE_GLOBS for p in root.glob(pattern)})
    assert files, "no compose file"
    for path in files:
        assert not DEFAULT_OVERRIDE.fullmatch(path.name), f"{path.name}: Compose merges it"
        found = sql_mounts(path.read_text(encoding="utf-8"))
        assert found == ALLOWED_SQL_MOUNTS if path.name == "compose.yaml" else not found, path
    example = root / ".env.example"
    if example.is_file():  # Compose reads these from .env in several spellings: any mention fails
        text = example.read_text("utf-8")
        for name in ("COMPOSE_FILE", "COMPOSE_PATH_SEPARATOR"):
            assert name not in text, f".env.example names {name}"


def test_no_service_reads_sql_from_a_host_path(compose: str) -> None:
    """The database's functions come from the release image, never from the host: a deploy then
    installs the release's functions and a rollback the previous release's (docs/dev/deploy.md).
    Every compose file at the repository root is read."""
    assert sql_mounts(compose) == ALLOWED_SQL_MOUNTS
    check_compose_files(ROOT)


def _files_pass(root: Path, files: dict[str, str]) -> bool:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    try:
        check_compose_files(root)
    except AssertionError:
        return False
    return True


@pytest.mark.parametrize(
    "name",
    [
        "compose.override.yaml",
        "compose.override.yml",
        "docker-compose.override.yml",
        "docker-compose.override.yaml",
    ],
)
def test_an_override_file_compose_merges_is_refused(
    compose: str, tmp_path: Path, name: str
) -> None:
    assert not _files_pass(tmp_path, {"compose.yaml": compose, name: "services: {}\n"})


@pytest.mark.parametrize(
    ("files", "ok"),
    [
        ({"compose.e2e.yaml": "services:\n  x:\n    image: y\n"}, True),  # not merged by default
        ({"docs/compose.override.yaml": "services: {}\n"}, True),  # not at the root
        ({"docker-compose.yml": f"services:\n  x:\n    volumes: ['{QUEUE_DIR}:/q']\n"}, False),
        ({"compose.e2e.yaml": "include:\n  - ./docker/evil.yaml\n"}, False),
        ({".env.example": "COMPOSE_FILE=compose.yaml:docker/evil.yaml\n"}, False),
        ({".env.example": "export COMPOSE_FILE=compose.yaml:docker/evil.yaml\n"}, False),
        ({".env.example": "COMPOSE_FILE: compose.yaml:docker/evil.yaml\n"}, False),
        ({".env.example": "COMPOSE_PATH_SEPARATOR=;\n"}, False),
        ({".env.example": "# the project's name\nCOMPOSE_PROJECT_NAME=ownvitals\n"}, True),
        # Even an allowed mount: another compose file carries no host SQL at all.
        ({"compose.e2e.yaml": f"services:\n  db:\n    volumes: ['{ALLOWED_INIT}']\n"}, False),
    ],
)
def test_every_compose_file_at_the_root_is_read(
    compose: str, tmp_path: Path, files: dict[str, str], ok: bool
) -> None:
    assert _files_pass(tmp_path, {"compose.yaml": compose, **files}) is ok


def tracked_env(root: Path) -> list[str]:
    out = subprocess.run(  # noqa: S603 - git, read-only
        ["git", "-C", str(root), "ls-files", "--", ".env"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


def test_no_root_env_file_is_tracked(tmp_path: Path) -> None:
    """A tracked .env could set COMPOSE_FILE directly (AGENTS.md: never commit .env)."""
    assert tracked_env(ROOT) == []
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
    (tmp_path / ".env").write_text("COMPOSE_FILE=x\n")
    assert tracked_env(tmp_path) == []  # near miss: present, not tracked
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", ".env"], check=True)  # noqa: S603, S607
    assert tracked_env(tmp_path) == [".env"]


def test_a_top_level_include_is_refused(compose: str) -> None:
    assert not passes("include:\n  - ./docker/evil.yaml\n" + compose)
    assert not passes("include: [./frontend/compose.yaml]\n" + compose)  # even without SQL
    assert passes("x-include: [./docker/evil.yaml]\n" + compose)  # near miss: an x- key


def test_a_secret_file_that_holds_sql_is_refused(compose: str) -> None:
    secret = "file: ./secrets/postgres_password"
    assert secret in compose
    assert not passes(compose.replace(secret, f"file: {QUEUE_DIR}/20-queue.sql"))
    assert not passes(compose.replace(secret, f"file: {QUEUE_DIR}"))
    assert passes(compose.replace(secret, "file: ./secrets/another_password"))  # near miss


def plant(compose: str, service: str, lines: str) -> str:
    """``lines`` (service keys, indented four spaces) appended to ``service``."""
    body = block(compose, service)
    planted = compose.replace(body, body.rstrip("\n") + "\n" + lines + "\n", 1)
    assert planted != compose
    load_compose(planted)  # still valid YAML
    return planted


QUEUE = QUEUE_DIR
LONG = "    volumes:\n      - type: {kind}\n        source: {source}\n        target: /q\n"
LONG_BIND = LONG.replace("{kind}", "bind")

REFUSED_PLANTS = {
    "bare": f"    volumes:\n      - {QUEUE}:/q:ro\n",
    "double-quoted": f'    volumes:\n      - "{QUEUE}:/q:ro"\n',
    "single-quoted": f"    volumes:\n      - '{QUEUE}:/q:ro'\n",
    "flow list": f'    volumes: ["{QUEUE}:/q:ro"]\n',
    "flow list, bare": f"    volumes: [{QUEUE}:/q]\n",
    "long bind": LONG_BIND.format(source=QUEUE),
    "long bind, quoted": LONG_BIND.format(source=f'"{QUEUE}"'),
    "long bind, one file": LONG_BIND.format(source=f"{QUEUE}/20-queue.sql"),
    "long bind, flow mapping": f"    volumes: [{{type: bind, source: {QUEUE}, target: /q}}]\n",
    "a parent directory": "    volumes:\n      - ./docker:/x:ro\n",
    "an absolute path": "    volumes:\n      - /srv/sql:/sql:ro\n",
    "a variable": "    volumes:\n      - ${SQL_DIR}:/sql:ro\n",
    "a home path": "    volumes:\n      - ~/sql:/sql:ro\n",
    "a relative path alone": "    volumes:\n      - ./docker/postgres/queue\n",
    "an unknown volume type": LONG.format(kind="image", source="x"),
    "an unknown named volume": LONG.format(kind="volume", source="other"),
    "volumes as a string": f"    volumes: {QUEUE}:/q\n",
    "volumes_from": "    volumes_from:\n      - db\n",
    "configs": "    configs:\n      - x\n",
    "extends another file": "    extends:\n      file: ./docker/evil.yaml\n      service: s\n",
    "extends a service": "    extends:\n      service: db\n",
    "extends, short form": "    extends: db\n",
    "develop watch": "    develop:\n      watch:\n        - action: sync\n"
    f"          path: {QUEUE}\n          target: /q\n",
}
# The same spellings without SQL behind them: the check reads them and lets them through.
NEAR_MISSES = {
    "quoted, no SQL": '    volumes:\n      - "./frontend:/f:ro"\n',
    "flow list, no SQL": '    volumes: ["./frontend:/f:ro"]\n',
    "long bind, no SQL": LONG_BIND.format(source="./frontend"),
    "named volume, short": "    volumes:\n      - db-data:/d:ro\n",
    "named volume, long": LONG.format(kind="volume", source="db-data"),
    "anonymous volume": "    volumes:\n      - /data\n",
    "tmpfs, long": "    volumes:\n      - type: tmpfs\n        target: /t\n",
    "labels with the words": "    labels:\n      extends: x\n      develop: y\n      include: z\n",
}


@pytest.mark.parametrize("lines", REFUSED_PLANTS.values(), ids=REFUSED_PLANTS.keys())
@pytest.mark.parametrize("service", ["worker", "db-functions", "db"])
def test_the_host_sql_check_refuses_every_spelling(compose: str, service: str, lines: str) -> None:
    if service == "db" and lines.startswith("    volumes:"):
        # db has a volumes list already: add the entries to it instead of a second key.
        lines = lines.removeprefix("    volumes:\n")
        if not lines.startswith("      -"):
            pytest.skip("only list entries can join db's existing list")
        body = block(compose, "db")
        planted = compose.replace(body, body.replace("    volumes:\n", "    volumes:\n" + lines, 1))
    else:
        planted = plant(compose, service, lines)
    assert not passes(planted)


@pytest.mark.parametrize("lines", NEAR_MISSES.values(), ids=NEAR_MISSES.keys())
def test_the_host_sql_check_reads_the_same_spellings_without_sql(compose: str, lines: str) -> None:
    assert passes(plant(compose, "worker", lines))


def test_the_host_sql_check_refuses_a_named_volume_with_options(compose: str) -> None:
    planted = compose.replace(
        "\nvolumes:\n  db-data:\n",
        "\nvolumes:\n  db-data:\n    driver_opts:\n      type: none\n      o: bind\n"
        f"      device: {QUEUE}\n",
    )
    assert planted != compose
    assert not passes(planted)


def test_the_allowed_init_mount_is_allowed_only_as_written(compose: str) -> None:
    """The exception is the exact short entry on db; the same mount on another service, or a
    second SQL directory on db, is refused."""
    entry = "./docker/postgres/init:/docker-entrypoint-initdb.d:ro"
    assert not passes(plant(compose, "worker", f"    volumes:\n      - {entry}\n"))
    assert not passes(compose.replace(entry, "./docker/postgres/init:/docker-entrypoint-initdb.d"))
