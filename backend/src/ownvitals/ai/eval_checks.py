"""The owner's model evaluation: the checks of its database (ADR 0014 section 9; #527).

Everything here reaches only the evaluation database, on the owner role (the one role that may read
``users.synthetic`` rows of every account without a user context and write the mark) or the app
role:

- ``schema_at_head``: the database's ``alembic_version`` is the single head of the migrations
  the image carries. The owner prepares the database with the existing ``migrate`` and
  ``db-functions`` steps (ADR 0014's note of #527); the evaluation process holds no superuser and
  creates nothing, so it refuses a database that is not at head.
- ``verify_database``: the app role is not privileged and the queue's and the AI functions'
  digests are those of this image (``verify_startup``): what ``db-functions`` installs.
- ``synthetic_only``: the connected database's own name ends in ``_ai_eval`` and **every**
  account in it is marked synthetic. The evaluation asks it before every model call.
- ``ledger_total``: what the month's global row already holds, so the run's cap is its own.
"""

import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ownvitals.ai.budget import month_start
from ownvitals.ai.eval_locks import EvalRefused
from ownvitals.auth.synthetic import eval_database, unmarked_accounts
from ownvitals.db import DatabaseRoleError
from ownvitals.worker.loop import verify_startup

_REVISION = re.compile(r'^revision\b[^=\n]*=\s*"([^"]+)"', re.M)
_DOWN_LINE = re.compile(r"^down_revision\b.*$", re.M)
_QUOTED = re.compile(r'"([^"]+)"')
_VERSION = text("SELECT version_num FROM alembic_version")
_LEDGER = text(
    "SELECT coalesce(sum(reserved + spent), 0) FROM public.ai_spend_global WHERE month = :month"
)


def migrations_directory() -> Path:
    """The ``migrations/versions`` directory of the image (``/app``, the working directory) or of
    the checkout."""
    for base in (Path.cwd(), Path.cwd() / "backend"):
        found = base / "migrations" / "versions"
        if found.is_dir():
            return found
    raise EvalRefused("migrations_missing")


def head_revision(directory: Path | None = None) -> str:
    """The one head of the migrations the image carries: the revision no other one follows."""
    revisions: set[str] = set()
    followed: set[str] = set()
    for script in (directory or migrations_directory()).glob("*.py"):
        source = script.read_text(encoding="utf-8")
        mine = _REVISION.search(source)
        if mine is None:
            continue
        revisions.add(mine.group(1))
        down = _DOWN_LINE.search(source)
        followed.update(_QUOTED.findall(down.group()) if down else ())
    heads = revisions - followed
    if len(heads) != 1:
        raise EvalRefused("migrations_missing")
    return heads.pop()


async def schema_at_head(owner: AsyncEngine, directory: Path | None = None) -> None:
    """``EvalRefused('schema_not_at_head')`` unless the database is at the migrations' one head."""
    head = head_revision(directory)
    async with owner.connect() as conn:
        rows = list((await conn.execute(_VERSION)).scalars().all())
    if rows != [head]:
        raise EvalRefused("schema_not_at_head")


async def verify_database(app: AsyncEngine) -> None:
    """The app role and the installed functions are as the image expects."""
    try:
        await verify_startup(app)
    except DatabaseRoleError:
        raise EvalRefused("database_not_prepared") from None


async def synthetic_only(owner: AsyncEngine) -> bool:
    """The database is an evaluation database and holds no unmarked account."""
    async with owner.connect() as conn:
        return await eval_database(conn) and await unmarked_accounts(conn) == 0


async def ledger_total(owner: AsyncEngine, now: datetime) -> int:
    """Micro-USD the evaluation database's ledger holds this month (reserved and spent)."""
    async with owner.connect() as conn:
        return int((await conn.execute(_LEDGER, {"month": month_start(now)})).scalar_one())
