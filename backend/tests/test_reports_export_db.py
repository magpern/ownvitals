"""Migration 0022's export tables (ADR 0013 section 4; #211): owned, forced RLS, the vocabularies
of ``ownvitals.reports.tables`` equal to the check constraints, one build per owner, a chunk only
of the owner's own export, and the cascades. Synthetic rows only."""

import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from authtools import auth_schema_fixture, clean_auth_fixture  # noqa: F401 - fixtures
from jobtools import admin_url, engine_for, ensure_user
from ownvitals.reports.tables import (
    CHUNK_MAX_BYTES,
    EXPORT_KINDS,
    EXPORT_OWNED,
    EXPORT_STATES,
    FAILURES,
)

SYNTHETIC = True
pytestmark = pytest.mark.usefixtures("clean_auth")


def _constraint(name: str) -> str:
    with engine_for(admin_url()) as engine, engine.connect() as conn:
        return conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar_one()


def _words(definition: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", definition))


def test_the_vocabularies_are_the_check_constraints() -> None:
    assert _words(_constraint("exports_kind")) == set(EXPORT_KINDS)
    assert _words(_constraint("exports_state")) == set(EXPORT_STATES)
    assert _words(_constraint("exports_failure")) == set(FAILURES)
    assert str(CHUNK_MAX_BYTES) in _constraint("export_chunks_bytes")


def test_both_tables_are_owned_with_forced_rls() -> None:
    with engine_for(admin_url()) as engine, engine.connect() as conn:
        forced = conn.execute(
            text("SELECT relname FROM pg_class WHERE relname = ANY(:t) AND relforcerowsecurity"),
            {"t": list(EXPORT_OWNED)},
        ).scalars()
        assert set(forced) == set(EXPORT_OWNED)


def test_one_build_per_owner_and_chunks_only_of_own_exports() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    with engine_for(admin_url()) as engine:
        with engine.begin() as conn:
            ensure_user(conn, a)
            ensure_user(conn, b)
            export = conn.execute(
                text("INSERT INTO exports (owner_id, kind) VALUES (:o, 'data') RETURNING id"),
                {"o": a},
            ).scalar_one()
        with pytest.raises(IntegrityError, match="exports_one_building"), engine.begin() as conn:
            conn.execute(text("INSERT INTO exports (owner_id, kind) VALUES (:o, 'data')"), {"o": a})
        with pytest.raises(IntegrityError, match="export_chunks_export"), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO export_chunks (owner_id, export_id, seq, bytes) "
                    "VALUES (:o, :e, 0, decode('5a', 'hex'))"
                ),
                {"o": b, "e": export},
            )
        with pytest.raises(IntegrityError, match="exports_ready"), engine.begin() as conn:
            conn.execute(text("UPDATE exports SET state = 'ready' WHERE id = :e"), {"e": export})
        with pytest.raises(IntegrityError, match="exports_failed"), engine.begin() as conn:
            conn.execute(text("UPDATE exports SET state = 'failed' WHERE id = :e"), {"e": export})
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO export_chunks (owner_id, export_id, seq, bytes) "
                    "VALUES (:o, :e, 0, decode('5a', 'hex'))"
                ),
                {"o": a, "e": export},
            )
            conn.execute(text("DELETE FROM users WHERE id = :o"), {"o": a})
            left = conn.execute(
                text("SELECT count(*) FROM export_chunks WHERE owner_id = :o"), {"o": a}
            ).scalar_one()
            assert left == 0
