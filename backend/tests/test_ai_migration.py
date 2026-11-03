"""Migration 0013's own grants (ADR 0014 section 7, #205), without 50-ai.sql: the app role
reads ``ai_spend`` and ``ai_reservations`` and holds nothing else on the three AI tables. The
script revokes the same again, so every other test sees the two together; this one shows the
migration alone already leaves the app role read-only."""

from sqlalchemy import create_engine, text

from dbtools import migrated

TABLES = {"ai_spend": {"SELECT"}, "ai_reservations": {"SELECT"}, "ai_spend_global": set()}
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def test_the_migration_alone_leaves_the_app_role_read_only(
    database_url: str, owner_database_url: str
) -> None:
    # A revision name, not "head": the harness then runs no db-functions script.
    with migrated(owner_database_url, "0013_ai_budget"):
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT count(*) FROM pg_roles WHERE rolname = 'ownvitals_ai'")
                    ).scalar()
                    == 0
                )
                for table, wanted in TABLES.items():
                    held = {
                        privilege
                        for privilege in PRIVILEGES
                        if connection.execute(
                            text("SELECT has_table_privilege('ownvitals_app', :t, :p)"),
                            {"t": f"public.{table}", "p": privilege},
                        ).scalar()
                    }
                    assert held == wanted, table
        finally:
            engine.dispose()
