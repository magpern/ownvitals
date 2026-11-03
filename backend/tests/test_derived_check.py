"""``ownvitals analysis check`` (#187): the owner's reproducibility check. It matches after the
worker's passes, finds a tampered daily value or finding, writes nothing, and prints counts,
versions and rule ids only: the probe-style shape test (PLAN-operations §10.4) finds no value, no
date, no timestamp and no id in its output. **Synthetic** data only (the year of
``test_derived_evaluate_db``).
"""

import argparse
import io
import re
import uuid
from datetime import timedelta
from typing import Any

import pytest

from jobcore import JobsEnv, jenv_fixture  # noqa: F401 - fixtures
from jobtools import admin_fixture, queue_schema_fixture  # noqa: F401 - fixtures
from ownvitals.cli import COMMANDS, add_subcommands
from ownvitals.context import UserContext
from ownvitals.db import user_session
from ownvitals.derived.check_cli import Report, check_owner, run
from test_derived_evaluate_db import NOW, YEAR, Env, a_year, day_of, year_groups

pytestmark = pytest.mark.usefixtures("jenv")

# A line is a name and then names, versions, states or small counts.
_LINE = re.compile(
    r"[a-z_]+( ([a-z_]+|[a-z0-9_-]+/[0-9]+|[A-Z][0-9]{1,2}(x|:[a-z_]+)?|[0-9]{1,6}))*"
)


@pytest.fixture
def env(jenv: JobsEnv) -> Env:
    return Env(jenv)


async def report_for(env: Env, ctx: UserContext, **kw: Any) -> Report:
    async with user_session(env.factory, ctx) as db:
        return await check_owner(db, ctx.user_id, env.settings(**kw), now=NOW)


async def cli(env: Env, ctx: UserContext) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    args = argparse.Namespace(command="analysis", action="check", user=str(ctx.user_id))
    code = await run(args, env.jenv.settings, out, err)
    return code, out.getvalue(), err.getvalue()


def assert_shapes_only(output: str, *needles: str) -> None:
    for line in output.splitlines():
        assert _LINE.fullmatch(line), line
    leaked = [n for n in needles if n in output]
    assert leaked == [], leaked
    assert not re.search(r"\d{4}-\d{2}|\d\.\d|\d{7,}", output)  # no date, value or id


async def test_after_the_passes_everything_matches(env: Env) -> None:
    ctx, connection = await a_year(env)
    await env.all_of(ctx)
    report = await report_for(env, ctx)
    assert not report.failed
    assert report.lines[0] == "daily_version daily/1"
    assert "daily_rows stored 730 computed 730 matching 730" in report.lines
    assert "rules_version rules/1" in report.lines
    assert "evaluated C1 365" in report.lines
    assert "rule_rows stored 2 computed 2 active 1 ended 1" in report.lines
    assert "insert 0 update 0 end 0 delete 0" in report.lines
    assert report.lines[-1] == "result match"
    code, out, err = await cli(env, ctx)
    assert (code, err) == (0, "")
    assert out.splitlines() == list(report.lines)
    assert_shapes_only(
        out,
        str(ctx.user_id),
        str(connection),
        "heart_pulse",
        "weight",
        day_of(150).isoformat(),
        "80.",
        "71",
        "72",
    )


async def test_a_tampered_daily_value_or_finding_is_a_mismatch_and_nothing_is_written(
    env: Env,
) -> None:
    ctx, _ = await a_year(env)
    await env.all_of(ctx)
    env.jenv.sql(
        "UPDATE metric_daily SET value = value + 1, morning = morning + 1, min = min + 1, "
        "max = max + 1, mean = mean + 1 WHERE owner_id = :o AND metric_key = 'weight' "
        "AND day = :d",
        o=ctx.user_id,
        d=day_of(5),
    )
    daily = await report_for(env, ctx)
    assert daily.failed
    assert "daily_rows stored 730 computed 730 matching 729" in daily.lines
    env.jenv.sql(
        "UPDATE metric_daily SET value = value - 1, morning = morning - 1, min = min - 1, "
        "max = max - 1, mean = mean - 1 WHERE owner_id = :o AND metric_key = 'weight' "
        "AND day = :d",
        o=ctx.user_id,
        d=day_of(5),
    )
    assert not (await report_for(env, ctx)).failed
    before = await env.found(ctx)
    env.jenv.sql(
        "UPDATE findings SET confidence = 'high' WHERE owner_id = :o AND status = 'active'",
        o=ctx.user_id,
    )
    rules = await report_for(env, ctx)
    assert rules.failed
    assert "insert 0 update 1 end 0 delete 0" in rules.lines
    code, out, _ = await cli(env, ctx)
    assert code == 1
    assert out.splitlines()[-1] == "result mismatch"
    assert_shapes_only(out, str(ctx.user_id))
    after = await env.found(ctx)
    assert [r.confidence for r in after] != [r.confidence for r in before]  # still tampered
    assert [r.id for r in after] == [r.id for r in before]


async def test_a_missing_episode_and_the_switch_show_in_the_report(env: Env) -> None:
    ctx, _ = await a_year(env)
    await env.all_of(ctx)
    with_switch = await report_for(env, ctx, experimental=True)
    assert with_switch.failed  # the C1x episode is not stored: the worker would insert it
    assert "experimental on" in with_switch.lines
    assert "insert 1 update 0 end 0 delete 0" in with_switch.lines


async def test_behind_and_nothing_imported(env: Env) -> None:
    empty, _ = env.owner()
    report = await report_for(env, empty)
    assert not report.failed
    assert "rule_inputs nothing" in report.lines
    assert "daily_rows stored 0 computed 0 matching 0" in report.lines
    ctx, connection = await a_year(env)
    await env.all_of(ctx)
    await env.ingest(ctx, connection, *year_groups(range(YEAR, YEAR + 1)))  # not normalised yet
    behind = await report_for(env, ctx)
    assert behind.failed
    assert "rule_inputs behind" in behind.lines


async def test_time_moves_q1_and_the_report_says_so(env: Env) -> None:
    ctx, connection = await a_year(env)
    env.jenv.sql(
        "INSERT INTO sync_cursors (owner_id, connection_id, stream, capability, backfill_state) "
        "VALUES (:o, :c, 'measures', 'supported', '{\"status\": \"complete\"}')",
        o=ctx.user_id,
        c=connection,
    )
    await env.all_of(ctx)
    async with user_session(env.factory, ctx) as db:
        later = await check_owner(db, ctx.user_id, env.settings(), now=NOW + timedelta(days=20))
    assert later.failed  # no weigh-in for 21 days: the next pass would open Q1
    assert "insert 1 update 0 end 0 delete 0" in later.lines


async def test_a_refused_user_id_names_no_value(env: Env) -> None:
    out, err = io.StringIO(), io.StringIO()
    args = argparse.Namespace(command="analysis", action="check", user="not-a-uuid")
    assert await run(args, env.jenv.settings, out, err) == 1
    assert (out.getvalue(), err.getvalue()) == ("", "error: the user id is not a uuid\n")
    unknown = argparse.Namespace(command="analysis", action="other", user=str(uuid.uuid4()))
    assert await run(unknown, env.jenv.settings, out, err) == 2


def test_the_operator_cli_has_the_command() -> None:
    parser = argparse.ArgumentParser()
    add_subcommands(parser)
    user = str(uuid.uuid4())
    args = parser.parse_args(["analysis", "check", "--user", user])
    assert (args.command, args.action, args.user) == ("analysis", "check", user)
    assert COMMANDS["analysis"] is run
