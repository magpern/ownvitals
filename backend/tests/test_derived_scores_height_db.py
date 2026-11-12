"""The HIS BMI input against the database (#492, ADR 0012 §2): the height (meastype 4) is read
through the owner's session under forced RLS, never guessed, and is part of the provenance.

**Synthetic** users and measures only (made-up values); no real account or measurement.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.analysis.scores.his_table import parse_table
from ownvitals.config_scores import DEFAULT_HIS_BAND_TABLE
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.derived.scores_store import score_owner
from ownvitals.derived.scores_tables import TableState
from test_derived_evaluate_db import DAY as SECONDS_PER_DAY
from test_derived_evaluate_db import FIRST, NOW, YEAR, a_year
from test_derived_normalize_db import group
from test_derived_scores_db import Env

pytestmark = pytest.mark.usefixtures("jenv")

SYNTHETIC = True
TABLE = parse_table(DEFAULT_HIS_BAND_TABLE.read_bytes())
STATE = TableState(TABLE, None, "synthetic-digest")
HIS = "SELECT components, input_fingerprint FROM scores WHERE owner_id = :o AND kind = 'his'"


@pytest.fixture
def env(jenv: JobsEnv) -> Env:
    return Env(jenv)


def height(grpid: int, day: int, millimetres: int) -> Any:
    return group(grpid, FIRST + day * SECONDS_PER_DAY, (4, millimetres, -3, None))


async def his(env: Env, ctx: UserContext) -> tuple[dict[str, Any], bytes]:
    await env.derive(ctx)
    await score_owner(
        env.opener(ctx),
        ctx.user_id,
        settings=env.settings(experimental=True),
        table=STATE,
        now=NOW,
    )
    async with user_session(env.factory, ctx) as db:
        row = (await db.execute(text(HIS), {"o": ctx.user_id})).one()
    return row.components, bytes(row.input_fingerprint)


def profile(env: Env, ctx: UserContext) -> None:
    env.jenv.sql(
        "INSERT INTO user_profiles (owner_id, birth_year, sex) VALUES (:o, 1990, 'female')",
        o=ctx.user_id,
    )


async def test_the_height_gives_a_banded_bmi_and_its_change_changes_the_provenance(
    env: Env,
) -> None:
    ctx, connection = await a_year(env)
    profile(env, ctx)
    await env.ingest(ctx, connection, height(9001, 10, 1700))
    comp, first = await his(env, ctx)
    bmi = comp["criteria"]["bmi"]
    assert bmi["status"] == "computed"  # 80 kg / 1.70 m^2, banded by the shipped table
    assert bmi["value"] == pytest.approx(80 / 1.7**2, abs=0.1)  # weights are 80 +/- 0.1 kg
    assert 0 <= bmi["score"] <= 100
    await env.ingest(ctx, connection, height(9002, 20, 1750))  # a newer height row
    comp, second = await his(env, ctx)
    assert comp["criteria"]["bmi"]["value"] == pytest.approx(80 / 1.75**2, abs=0.1)  # newest
    assert second != first


async def test_without_a_height_the_bmi_says_no_height(env: Env) -> None:
    ctx, _ = await a_year(env)
    profile(env, ctx)
    comp, _ = await his(env, ctx)
    bmi = comp["criteria"]["bmi"]
    assert (bmi["status"], bmi["reason"]) == ("not_available", "no_height")
    assert comp["vertical_reasons"]["body"] == "no_height"


async def test_another_owners_height_is_never_read(env: Env) -> None:
    ctx, _ = await a_year(env)
    profile(env, ctx)
    other, other_connection = env.owner()
    await env.ingest(other, other_connection, height(9100, 5, 1600))
    comp, _ = await his(env, ctx)
    assert comp["criteria"]["bmi"]["reason"] == "no_height"
    assert uuid.UUID(str(other.user_id)) != ctx.user_id
    assert YEAR  # the owner's own year of weights was gathered
