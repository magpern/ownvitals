"""Migration 0016 (#187, Architect-approved schema fix): ``findings.rule_id`` holds exactly the
ids of the v1 rules (``SPECS``) and the read function's experimental ids, the system tier is
``Q1`` and its parts only, and the read function still drops a ``C1x`` row by its id without the
switch. Synthetic rows only; no real account or measurement.
"""

import importlib.util
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from authtools import (  # noqa: F401 - fixtures
    auth_schema_fixture,
    clean_auth_fixture,
)
from dbtools import alembic_config
from jobtools import engine_for, ensure_user
from ownvitals.analysis.rules.core import SPECS, Tier
from ownvitals.derived.evaluate import rule_version
from ownvitals.views.findings_read import EXPERIMENTAL_RULES, read_finding
from test_views_findings import (  # noqa: F401 - fixtures
    ids,
    owner_env_fixture,
    plain_fixture,
    plant_finding,
    user,
)

MIGRATION = (
    Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0016_findings_rule_ids.py"
)
HASH = b"\x44" * 32  # a labelled synthetic fingerprint, not a digest of anything


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("m0016", MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The rules outside SPECS that the test names: Q1 as Amendment 3 spells it, ADR 0006's E rules.
_OTHER_TIERS = {"Q1": "system", "E1": "device", "E4": "experimental", "E5": "experimental"}


def _tier(rule: str) -> str:
    spec = SPECS.get(rule)
    return spec.tier.value if spec is not None else _OTHER_TIERS.get(rule, "trend")


def test_every_rule_id_and_version_fits_the_new_checks() -> None:
    migration = _migration()
    for rule in (*SPECS, *EXPERIMENTAL_RULES, "Q1", "E1", "E6"):
        assert re.fullmatch(migration.RULE_ID, rule), rule
        system = bool(re.match(migration.SYSTEM_RULE, rule))
        assert system == (_tier(rule) == Tier.SYSTEM.value), rule
    for rule in SPECS:
        assert re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,39}", rule_version(rule)), rule
    for bad in ("c1", "C1X", "C1y", "C", "C123", "N2:", "N2:Guided", "Q1:no-weigh", "Q1::a"):
        assert not re.fullmatch(migration.RULE_ID, bad), bad


@pytest.fixture
def conn(database_url: str) -> Iterator[Connection]:
    """A superuser transaction with one synthetic user; rolled back afterwards."""
    with engine_for(database_url) as engine, engine.connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def _finding(conn: Connection, owner: uuid.UUID, rule: str, **overrides: Any) -> None:
    tier = overrides.pop("tier", _tier(rule))
    row: dict[str, Any] = {
        "rule_id": rule,
        "tier": tier,
        "confidence": "high" if tier in ("device", "system") else "medium",
        "subject": f"synthetic_{uuid.uuid4().hex[:8]}",
        **overrides,
    }
    conn.execute(
        text(
            "INSERT INTO findings (owner_id, rule_id, rule_version, tier, severity, confidence, "
            "origin, subject, window_start, window_end, input_revision, input_fingerprint) "
            "VALUES (:owner, :rule_id, 'rules/1.1', :tier, 'attention', :confidence, 'live', "
            ":subject, '2026-10-01', '2026-10-02', 0, :hash)"
        ),
        {"owner": owner, "hash": HASH, **row},
    )


def _refused(conn: Connection, owner: uuid.UUID, rule: str, **overrides: Any) -> None:
    with pytest.raises(IntegrityError), conn.begin_nested():
        _finding(conn, owner, rule, **overrides)


@pytest.mark.usefixtures("schema")
def test_the_database_accepts_every_rule_id_and_refuses_the_rest(conn: Connection) -> None:
    owner = uuid.uuid4()
    ensure_user(conn, owner)
    for rule in sorted({*SPECS, *EXPERIMENTAL_RULES, "Q1"}):
        _finding(conn, owner, rule)
    for bad in ("c1", "C1X", "C1y", "N2:", "N2:Guided", "Q1:no-weigh", "B12345"):
        _refused(conn, owner, bad)
    # The system tier is Q1's and its parts', both ways (Amendment 3).
    _refused(conn, owner, "C1", tier="system", confidence="high")
    _refused(conn, owner, "N2:guided", tier="system", confidence="high")
    _refused(conn, owner, "Q1:reconnect", tier="trend")
    _refused(conn, owner, "Q1x", tier="system", confidence="high")


@pytest.mark.usefixtures("schema")
def test_the_downgrade_removes_only_the_rows_the_old_checks_cannot_hold(
    database_url: str, owner_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", owner_database_url)
    owner = uuid.uuid4()
    with engine_for(database_url) as engine, engine.begin() as connection:
        ensure_user(connection, owner)
        for rule in ("C1", "C1x", "Q1", "Q1:reconnect", "N2:guided"):
            _finding(connection, owner, rule)
    config = alembic_config()
    try:
        command.downgrade(config, "0015_qr_login")
        with engine_for(database_url) as engine, engine.connect() as connection:
            kept = connection.execute(
                text("SELECT rule_id FROM findings WHERE owner_id = :o ORDER BY rule_id"),
                {"o": owner},
            ).scalars()
            assert list(kept) == ["C1", "Q1"]
            forced = connection.execute(
                text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'findings'")
            ).scalar_one()
            assert forced is True
    finally:
        command.upgrade(config, "head")


# --- the read function keys the experimental drop on the rule id (findings_read, D9) ----------


@pytest.mark.parametrize("consumer", ["dashboard", "notification", "export"])
async def test_a_c1x_row_is_dropped_by_its_id_without_the_switch(plain: Any, consumer: str) -> None:
    """A ``C1x`` row whose tier says ``trend`` (a disguised experimental row) still never reaches a
    caller without the switch: the drop keys on the rule id, which the new check now admits."""
    assert "C1x" in EXPERIMENTAL_RULES
    ctx, _ = user()
    disguised = plant_finding(ctx.user_id, "C1x", tier="trend", metric_keys=("heart_pulse",))
    c1 = plant_finding(ctx.user_id, "C1", metric_keys=("heart_pulse",))
    assert await ids(plain, ctx, consumer) == {c1}
    assert await read_finding(plain.factory, ctx, plain.settings, disguised) is None


async def test_with_the_switch_a_c1x_row_is_shown(owner_env: Any) -> None:
    ctx, _ = user()
    c1x = plant_finding(ctx.user_id, "C1x", tier="experimental", metric_keys=("heart_pulse",))
    assert await ids(owner_env, ctx) == {c1x}
