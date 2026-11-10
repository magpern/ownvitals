"""Shared setup for the Withings schema tests. Synthetic rows only."""

from pathlib import Path

from sqlalchemy import text

from jobtools import engine_for, run_script

SCRIPT = (
    Path(__file__).resolve().parents[2] / "docker" / "postgres" / "withings" / "40-withings.sql"
)
HMAC = b"\x11" * 32
HASH = b"\x22" * 32
SECRET = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SEED = "seed-1"
SEED_2 = "seed-2"
PEPPER = "pep-1"
PEPPER_2 = "pep-2"

_DROP = """
DROP FUNCTION IF EXISTS public.ensure_month_partitions(text, date, date);
DROP FUNCTION IF EXISTS public.webhook_secrets_apply(
    integer, text, text, text, text, bytea, timestamptz, timestamptz, text);
DROP FUNCTION IF EXISTS public.webhook_event_insert(
    integer, bigint, integer, bigint, bigint, text, uuid);
DROP FUNCTION IF EXISTS public.webhook_events_purge_owner();
DROP FUNCTION IF EXISTS public.webhook_events_purge_account(uuid);
DROP FUNCTION IF EXISTS public.metrics_withings_aggregates();
DROP FUNCTION IF EXISTS public.metrics_webhook_ages(timestamptz);
DROP FUNCTION IF EXISTS public.metrics_webhook_ages(timestamptz, uuid);
"""


def install(url: str) -> None:
    run_script(url, SCRIPT.read_text(encoding="utf-8"))


def uninstall(url: str) -> None:
    with engine_for(url, isolation_level="AUTOCOMMIT") as engine, engine.connect() as conn:
        conn.execute(text(_DROP))
        for role in ("ownvitals_webhook_secrets", "ownvitals_webhook_events", "ownvitals_metrics"):
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
            ).first()
            if exists is not None:
                conn.execute(text(f"DROP OWNED BY {role} CASCADE"))
                conn.execute(text(f"DROP ROLE {role}"))


def seed_keys(url: str) -> None:
    """Synthetic check rows. Not key material: sixteen labelled bytes each."""
    with engine_for(url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO connection_keks (key_id, check_value) VALUES "
                "(:s, :c), (:s2, :c), (:p, :c), (:p2, :c) ON CONFLICT DO NOTHING"
            ),
            {"s": SEED, "s2": SEED_2, "p": PEPPER, "p2": PEPPER_2, "c": b"\xab" * 16},
        )


def new_app(url: str) -> int:
    """A synthetic Withings app row. The ciphertext is labelled filler, not a client secret."""
    with engine_for(url) as engine, engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO withings_apps (client_id, callback_base, client_secret, "
                    "client_secret_nonce, client_wrapped_dek, client_dek_nonce, client_key_id) "
                    "VALUES ('synthetic-client', 'https://example.test/cb', :secret, :nonce, "
                    ":wrapped, :nonce, 'kek-synthetic') RETURNING id"
                ),
                {"secret": b"\x01" * 16, "nonce": b"\x02" * 12, "wrapped": b"\x03" * 48},
            ).scalar_one()
        )
