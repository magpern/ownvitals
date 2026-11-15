"""The AI evaluation's synthetic accounts (ADR 0014 section 9; #531): ``users.synthetic``.

The model evals run on a database of their own, whose name ends in ``_ai_eval``, and seed their
own accounts there, each marked ``users.synthetic = true``. Migration 0031 lets only the table's
owner, ``ownvitals_owner`` (the migration role), write that column: the app role (web, worker,
the operator CLI) can neither create a marked account nor mark or unmark one, in any database.

- ``create_synthetic_account`` inserts one marked account on a connection of the owner role.
  It refuses (``SyntheticRefused``) unless the connected database's own name ends in
  ``_ai_eval`` (read from the server, never from a URL or setting) and the address is in the
  reserved ``.invalid`` domain of RFC 2606 (``EVAL_EMAIL_DOMAIN``), so it can never be a person's.
  It also refuses to add an account beside an unmarked one: a database that holds a real (or
  any unmarked) account is not an evaluation database.
- ``unmarked_accounts`` counts the accounts that are not marked; the evaluation calls it before
  every model call and stops on anything but 0. It reads, so the app role may call it.
- ``account_is_synthetic`` says whether one account is marked, for the data seeder's check of
  the context it was handed.

Only accounts are touched here: the evaluation's owned data goes in through a ``UserContext``
(``ownvitals.ai.eval_seed``). Synthetic data only.
"""

from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from ownvitals.auth.tables import users
from ownvitals.emailnorm import normalize_email

EVAL_DATABASE_SUFFIX: Final = "_ai_eval"
# RFC 2606 reserves ``.invalid``: no address in it can reach anyone.
EVAL_EMAIL_DOMAIN: Final = "@ai-evaluation.invalid"
_LOCAL_PART_MAX: Final = 48


class SyntheticRefused(Exception):
    """The database or the address is not one the evaluation may seed; nothing was written."""


def is_eval_database_name(name: str) -> bool:
    """A database the evaluation may use: a name ending in ``_ai_eval``, with more before it."""
    return len(name) > len(EVAL_DATABASE_SUFFIX) and name.endswith(EVAL_DATABASE_SUFFIX)


def _check_email(email: str) -> str:
    local, at, domain = email.partition("@")
    if (
        email != email.strip()
        or at != "@"
        or f"@{domain}" != EVAL_EMAIL_DOMAIN
        or not 1 <= len(local) <= _LOCAL_PART_MAX
        or not all(c.isascii() and (c.isalnum() or c in "-_.") for c in local)
    ):
        raise SyntheticRefused(f"a synthetic account's address must be <name>{EVAL_EMAIL_DOMAIN}")
    return email


async def eval_database(conn: AsyncConnection | AsyncSession) -> bool:
    """Whether the database this connection is on is an evaluation database, by the server's own
    ``current_database()``."""
    name = (await conn.execute(select(func.current_database()))).scalar_one()
    return is_eval_database_name(str(name))


async def unmarked_accounts(conn: AsyncConnection | AsyncSession) -> int:
    """How many accounts are not marked synthetic (0 in a sound evaluation database)."""
    return int(
        (
            await conn.execute(
                select(func.count()).select_from(users).where(users.c.synthetic.is_(False))
            )
        ).scalar_one()
    )


async def account_is_synthetic(conn: AsyncConnection | AsyncSession, user_id: UUID) -> bool:
    """Whether ``user_id`` is an account marked synthetic (False when there is none)."""
    marked = (
        await conn.execute(select(users.c.synthetic).where(users.c.id == user_id))
    ).scalar_one_or_none()
    return marked is True


async def create_synthetic_account(owner: AsyncConnection, email: str) -> UUID:
    """Insert one marked account and return its id; an account with that address that is
    already marked is returned as it is (a seed can run again). ``owner`` is a connection of
    the owner role inside a transaction the caller commits; the app role is refused by the
    database (no INSERT privilege on ``synthetic``)."""
    address = _check_email(email)
    if not await eval_database(owner):
        raise SyntheticRefused(f"the database's name must end in {EVAL_DATABASE_SUFFIX}")
    if await unmarked_accounts(owner):
        raise SyntheticRefused("the database holds an unmarked account")
    inserted = (
        await owner.execute(
            pg_insert(users)
            .values(
                email=address,
                email_normalized=normalize_email(address),
                email_verified_at=func.now(),
                synthetic=True,
            )
            .on_conflict_do_nothing(index_elements=[users.c.email_normalized])
            .returning(users.c.id)
        )
    ).scalar_one_or_none()
    if inserted is not None:
        return inserted
    existing = (
        await owner.execute(
            select(users.c.id, users.c.synthetic).where(
                users.c.email_normalized == normalize_email(address)
            )
        )
    ).one()
    if existing.synthetic is not True:  # unreachable after the count above; kept fail-closed
        raise SyntheticRefused("the database holds an unmarked account")
    return existing.id
