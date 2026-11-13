"""Every ``server_default`` in ``derived/tables.py`` equals the migrated column default (#501
item 2). Mutation caught: changing a default in the model or in a migration but not the other
(for example ``chart_range`` back to ``'90d'`` in one place). Synthetic; schema only."""

import re

import pytest
from sqlalchemy import create_engine, text

from ownvitals.derived.tables import metadata

pytestmark = pytest.mark.usefixtures("schema")

_CAST = re.compile(r"::[a-z_ ]+(\[\])?$")


def _norm(expr: str) -> str:
    return _CAST.sub("", expr.strip()).replace(" ", "").lower()


def _declared() -> dict[tuple[str, str], str]:
    out: dict[tuple[str, str], str] = {}
    for table in metadata.tables.values():
        for column in table.columns:
            default = column.server_default
            if default is None:
                continue
            arg = getattr(default, "arg", None)
            out[(table.name, column.name)] = str(arg.compile() if hasattr(arg, "compile") else arg)
    return out


def test_every_model_server_default_is_the_migrated_default(database_url: str) -> None:
    declared = _declared()
    assert ("user_profiles", "chart_range") in declared  # the generic walk covers it
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT table_name, column_name, column_default "
                    "FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND column_default IS NOT NULL"
                )
            ).all()
    finally:
        engine.dispose()
    actual = {(t, c): d for t, c, d in rows}
    wrong = {
        key: (want, actual.get(key))
        for key, want in declared.items()
        if _norm(want) != _norm(actual.get(key) or "")
    }
    assert not wrong, f"model default differs from the migrated default: {wrong}"
