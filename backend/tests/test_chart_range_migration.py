"""Migration 0028's downgrade (issue #402, review note of #404): a ``7d`` profile row becomes
``30d``, the CHECK narrows again, the default returns to ``90d`` and FORCE RLS is back. The
downgrade needs NO FORCE for its one UPDATE; without it the CHECK change fails.

Synthetic users only; the migration runs as the owner role, the data is planted as superuser.
"""

import uuid

import pytest
from alembic import command
from sqlalchemy import text

from authtools import (  # noqa: F401 - fixtures
    auth_schema_fixture,
    clean_auth_fixture,
)
from dbtools import alembic_config
from jobtools import engine_for, ensure_user

SYNTHETIC = True


@pytest.mark.usefixtures("schema")
def test_the_downgrade_maps_7d_rows_and_restores_the_old_check_default_and_rls(
    database_url: str, owner_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OWNVITALS_MIGRATIONS_DATABASE_URL", owner_database_url)
    owners = {uuid.uuid4(): "7d", uuid.uuid4(): "90d", uuid.uuid4(): "1y"}
    with engine_for(database_url) as engine, engine.begin() as connection:
        for owner, chart_range in owners.items():
            ensure_user(connection, owner)
            connection.execute(
                text("INSERT INTO user_profiles (owner_id, chart_range) VALUES (:o, :r)"),
                {"o": owner, "r": chart_range},
            )
    config = alembic_config()
    try:
        command.downgrade(config, "0027_ai_conversation_retention")
        with engine_for(database_url) as engine, engine.connect() as connection:
            saved = dict(
                connection.execute(text("SELECT owner_id, chart_range FROM user_profiles")).all()
            )
            assert [saved[o] for o in owners] == ["30d", "90d", "1y"]  # only 7d moved
            assert (
                connection.execute(
                    text(
                        "SELECT pg_get_expr(adbin, adrelid) FROM pg_attrdef WHERE adrelid = "
                        "'user_profiles'::regclass AND adnum = (SELECT attnum FROM pg_attribute "
                        "WHERE attrelid = 'user_profiles'::regclass AND attname = 'chart_range')"
                    )
                )
                .scalar_one()
                .startswith("'90d'")
            )
            forced = connection.execute(
                text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'user_profiles'")
            ).scalar_one()
            assert forced is True
            with pytest.raises(Exception, match="user_profiles_chart_range"):
                connection.execute(
                    text("UPDATE user_profiles SET chart_range = '7d' WHERE owner_id = :o"),
                    {"o": next(iter(owners))},
                )
    finally:
        command.upgrade(config, "head")
