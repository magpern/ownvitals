"""The upgrade DO block of ``40-withings.sql`` for ``metrics_webhook_ages`` (#303, #366).

An older database holds the one-argument version of #167, or a two-argument one with another
result row; ``CREATE OR REPLACE`` cannot change either, so the script drops them first. Synthetic
objects only: the legacy functions return nothing and hold no data.
"""

from collections.abc import Iterator

import pytest
from sqlalchemy import text

from jobtools import engine_for
from withingsschema import install, seed_keys, uninstall

SIGNATURES = """
SELECT pg_get_function_identity_arguments(p.oid), pg_get_function_result(p.oid)
  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'public' AND p.proname = 'metrics_webhook_ages' ORDER BY 1
"""
CURRENT = (
    "p_after timestamp with time zone, p_after_id uuid",
    "TABLE(received timestamp with time zone, event_id uuid, age_seconds double precision)",
)
LEGACY_ONE_ARGUMENT = (
    "CREATE FUNCTION public.metrics_webhook_ages(p_after timestamptz) "
    "RETURNS TABLE (received timestamptz, age_seconds double precision) "
    "LANGUAGE sql AS $$ SELECT NULL::timestamptz, NULL::double precision WHERE false $$"
)
LEGACY_RESULT = (
    "CREATE FUNCTION public.metrics_webhook_ages(p_after timestamptz, p_after_id uuid) "
    "RETURNS TABLE (received timestamptz, age_seconds double precision) "
    "LANGUAGE sql AS $$ SELECT NULL::timestamptz, NULL::double precision WHERE false $$"
)


@pytest.fixture
def functions(schema: None, database_url: str) -> Iterator[None]:
    install(database_url)
    seed_keys(database_url)
    try:
        yield
    finally:
        uninstall(database_url)


def _signatures(url: str) -> list[tuple[str, str]]:
    with engine_for(url) as engine, engine.connect() as conn:
        return [(a, r) for a, r in conn.execute(text(SIGNATURES))]


@pytest.mark.parametrize("legacy", [LEGACY_ONE_ARGUMENT, LEGACY_RESULT])
def test_the_script_replaces_an_older_signature_or_result(
    functions: None, database_url: str, legacy: str
) -> None:
    with engine_for(database_url) as engine, engine.begin() as conn:
        if legacy is LEGACY_RESULT:
            conn.execute(text("DROP FUNCTION public.metrics_webhook_ages(timestamptz, uuid)"))
        conn.execute(text(legacy))
    assert _signatures(database_url) != [CURRENT]
    install(database_url)
    assert _signatures(database_url) == [CURRENT]
    install(database_url)  # and a second run changes nothing
    assert _signatures(database_url) == [CURRENT]
