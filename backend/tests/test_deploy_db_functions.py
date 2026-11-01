"""The deploy's db-functions check (``check_db_functions_mounts`` in ``deploy/ownvitals-deploy``):
in the resolved ``docker compose config``, db-functions has no ``volumes``, ``volumes_from``,
``configs`` or ``develop``, and no secret but ``postgres_password`` at its default
target. Each of those could put a file from the server's disk where the image's SQL is, and a
deploy or rollback would then not change the functions (docs/dev/deploy.md, check 11).

The fake docker prints the configuration as Compose v5 normalises it (``render_config``); the
check was also run against real Compose v5.5.1 output (PR #118). Synthetic data, fakes only."""

from pathlib import Path

import pytest

from test_deploy_harness import Host
from test_deploy_script import deploy, refused, started

SECRET = [
    "    secrets:",
    "      - source: postgres_password",
    "        target: /run/secrets/postgres_password",
]
TMPFS = ["    tmpfs:", "      - /run/pgpass:mode=0700,uid=10001,gid=10001"]
BIND = [
    "      - bind:",
    "          create_host_path: true",
    "        read_only: true",
    "        source: /opt/ownvitals/docker/postgres/queue",
    "        target: /app/db-functions",
    "        type: bind",
]
SQL_TARGET = "        target: /app/db-functions/20-queue.sql"


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return Host(tmp_path)


REFUSED = {
    "a bind mount": ([*SECRET, *TMPFS, "    volumes:", *BIND], "has volumes"),
    "a named volume": (
        [
            *SECRET,
            *TMPFS,
            "    volumes:",
            "      - source: db-data",
            SQL_TARGET,
            "        type: volume",
        ],
        "has volumes",
    ),
    "a tmpfs volume": (
        [*SECRET, "    volumes:", "      - target: /run/pgpass", "        type: tmpfs"],
        "has volumes",
    ),
    "volumes_from": (
        [*SECRET, *TMPFS, "    volumes_from:", "      - service:db"],
        "has volumes_from",
    ),
    "a config with a target": (
        ["    configs:", "      - source: q", SQL_TARGET, *SECRET, *TMPFS],
        "has configs",
    ),
    "a config at its default target": (
        ["    configs:", "      - source: q", *SECRET, *TMPFS],
        "has configs",
    ),
    "develop watch": (
        ["    develop:", "      watch:", "        - action: sync", *SECRET, *TMPFS],
        "has develop",
    ),
    "another secret": (
        [*SECRET, "      - source: s", SQL_TARGET.replace("20-queue", "s"), *TMPFS],
        "has secrets other than postgres_password",
    ),
    "postgres_password at an SQL path": (
        ["    secrets:", "      - source: postgres_password", SQL_TARGET, *TMPFS],
        "has secrets other than postgres_password",
    ),
    "postgres_password with a mode": (
        [*SECRET, "        mode: 420", *TMPFS],
        "has secrets other than postgres_password",
    ),
    "no secret at all": (TMPFS, "has secrets other than postgres_password"),
}


@pytest.mark.parametrize(("lines", "check"), REFUSED.values(), ids=REFUSED.keys())
def test_a_db_functions_service_that_can_read_the_host_is_refused(
    host: Host, lines: list[str], check: str
) -> None:
    host.world["mounts"]["db-functions"] = lines
    refused(deploy(host, "v1.2.0"), f"compose: db-functions {check}")
    assert [a[0] for a in started(host)] == ["pull"]
    assert host.state() is None


def test_the_check_reads_only_db_functions(host: Host) -> None:
    """Near misses: volumes, configs and other secrets on another service; the words deeper in
    db-functions (a label); the default shape passes."""
    host.world["mounts"]["worker"] = ["    configs:", "      - source: q", "    volumes:", *BIND]
    host.world["mounts"]["db-functions"] = [
        "    labels:",
        "      volumes: x",
        "      configs: y",
        *SECRET,
        *TMPFS,
    ]
    assert deploy(host, "v1.2.0").code == 0


def scenario_refuses_db_functions_with_a_volume(host: Host) -> None:
    """A bind mount over /app/db-functions from the server's disk is refused before anything
    runs (also run by test_deploy_mutations against each loosened copy of the check)."""
    host.world["mounts"]["db-functions"] = [*SECRET, *TMPFS, "    volumes:", *BIND]
    refused(deploy(host, "v1.2.0"), "compose: db-functions has volumes")
    assert [a[0] for a in started(host)] == ["pull"]


def scenario_refuses_db_functions_with_a_config(host: Host) -> None:
    host.world["mounts"]["db-functions"] = [
        "    configs:",
        "      - source: q",
        SQL_TARGET,
        *SECRET,
    ]
    refused(deploy(host, "v1.2.0"), "compose: db-functions has configs")


def scenario_refuses_db_functions_with_another_secret(host: Host) -> None:
    host.world["mounts"]["db-functions"] = [
        "    secrets:",
        "      - source: postgres_password",
        SQL_TARGET,
        *TMPFS,
    ]
    refused(deploy(host, "v1.2.0"), "compose: db-functions has secrets other than")


@pytest.mark.parametrize(
    "scenario",
    [
        scenario_refuses_db_functions_with_a_volume,
        scenario_refuses_db_functions_with_a_config,
        scenario_refuses_db_functions_with_another_secret,
    ],
)
def test_scenario(tmp_path: Path, scenario: object) -> None:
    assert callable(scenario)
    scenario(Host(tmp_path))
