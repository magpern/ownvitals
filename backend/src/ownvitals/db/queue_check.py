"""The queue check of ADR 0005 section 3 (guard 9): the worker refuses to run on a drifted queue.

The ``SECURITY DEFINER`` functions of ``docker/postgres/queue/20-queue.sql`` and their owner
``ownvitals_queue`` are the only cross-user paths into the owned job tables. The check runs at
startup (``verify_queue``) and on every new pooled connection (``install_queue_check``), like the
role check, and refuses on any difference with ``QueueDriftError`` (a ``DatabaseRoleError``; no
override): each function exists once, owned by ``ownvitals_queue``, ``SECURITY DEFINER`` with
exactly the fixed ``search_path``, the version comment, the shipped ``md5(pg_get_functiondef)``
(``ownvitals.db.queue_digests``) and the ACL ``{ownvitals_queue=X/ownvitals_queue,
ownvitals_app=X/ownvitals_queue}``; the role has exactly its attributes, no memberships either way,
no settings, no default privileges and exactly its privileges; no other user-defined function,
procedure, aggregate or operator exists in any schema, except the Withings partition, webhook-secret
and webhook-event functions and the metrics aggregates (#167) admitted by name in ``20-queue.sql``,
and the rate functions
``withings_rate_claim``,
``withings_rate_release`` and ``withings_rate_pause`` admitted the same way, and the mail outbox's
``mail_outbox_claim``, ``mail_outbox_mark`` and ``owner_notice_queue`` (#194, ADR 0007 section 3;
their digests in ``ownvitals.db.outbox_digests``), each pinned like a queue function when it is
installed, and the AI cost cap's ``ai_reserve``, ``ai_settle``, ``ai_release`` and
``ai_settle_lost`` (``docker/postgres/ai/50-ai.sql``, ADR 0014 section 7; #205), owned by
``ownvitals_ai``: absent until that script has run, then all four, each pinned;
no table has a trigger (other than the internal ones of foreign keys) or a rule; and no default,
CHECK, index or partition expression of any table or domain calls one of ``SQL_TEXT_BUILTINS``.
So nothing can shadow a built-in a function body calls, and nothing a function fires (a trigger,
a rule, a default, a CHECK, an index expression) can reach code other than the built-ins while
running as ``ownvitals_queue``, and none of those runs SQL text. Objects are named alike here,
in guard 9 (``tests/queue_guard.py``) and in ``20-queue.sql`` (``function schema.name(types)``,
``trigger name on schema.table``, ...).

Every grant or ownership naming a role is recorded in ``pg_shdepend``, so comparing that list with
the expected one catches a privilege of any kind, in any database of the cluster.

Where it runs: the worker (``ownvitals.worker``, #9), ``python -m ownvitals jobs stats`` and
``jobs replay``, and web (#77): its lifespan installs the per-connection check before the first
connection and calls ``verify_queue`` before it serves, so web refuses a drifted queue too.
"""

import logging
import weakref
from collections.abc import Sequence
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from ownvitals.db.outbox_digests import OUTBOX_DIGESTS
from ownvitals.db.queue_digests import QUEUE_DIGESTS
from ownvitals.db.queue_expected import (  # noqa: F401 (re-exported)
    COLUMN_COUNT,
    ENQUEUE_INGEST_ACL,
    EXPECTED_ACL,
    EXPECTED_ATTRIBUTES,
    EXPECTED_DEPENDENCIES,
    EXPECTED_PRIVILEGES,
    QUEUE_FUNCTIONS,
    QUEUE_QUERY,
    QUEUE_ROLE,
    QUEUE_VERSION,
    SEARCH_PATH,
    SQL_TEXT_BUILTINS,
    WITHINGS_FUNCTION_NAMES,
    WITHINGS_PINS,
    WITHINGS_VERSION,
)
from ownvitals.db.queue_expected_extra import (  # noqa: F401 (re-exported)
    AI_FUNCTION_NAMES,
    AI_PINS,
    AI_ROLE,
    AI_VERSION,
    OUTBOX_FUNCTION_NAMES,
    OUTBOX_PINS,
    OUTBOX_ROLE,
    OUTBOX_VERSION,
    RATE_FUNCTION_NAMES,
    RATE_PINS,
    RATE_VERSION,
)
from ownvitals.db.role_check import DatabaseRoleError, is_authentication_failure

logger = logging.getLogger(__name__)


class QueueDriftError(DatabaseRoleError):
    """The job queue does not match ADR 0005 section 3. Names objects, never data or credentials."""


def queue_problems(row: Sequence[Any] | None) -> list[str]:
    """Problems for one ``QUEUE_QUERY`` row; empty when the queue matches ADR 0005 section 3."""
    if row is None or len(row) != COLUMN_COUNT:
        raise ValueError(f"the queue query must return {COLUMN_COUNT} columns")
    attributes, memberships, settings, dependencies, privileges, functions, foreign = row
    if attributes is None:
        return [f"the role {QUEUE_ROLE} does not exist (run docker/postgres/queue/20-queue.sql)"]
    found: list[str] = []
    if list(attributes) != EXPECTED_ATTRIBUTES:
        found.append(
            f"{QUEUE_ROLE} must be NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
            "BYPASSRLS NOINHERIT"
        )
    if memberships:
        found.append(f"{QUEUE_ROLE} must be a member of no role and have no members")
    if settings:
        found.append(f"{QUEUE_ROLE} must have no role settings and no default privileges")
    if set(dependencies) != EXPECTED_DEPENDENCIES or len(dependencies) != len(set(dependencies)):
        found.append(f"{QUEUE_ROLE} owns or holds privileges other than the expected ones")
    if set(privileges) != EXPECTED_PRIVILEGES or len(privileges) != len(set(privileges)):
        found.append(f"{QUEUE_ROLE} must have exactly its table privileges and USAGE on public")
    if foreign:
        found.append(
            "user-defined functions, operators, triggers or rules, or expressions calling a "
            "built-in that runs SQL text, exist besides the queue functions (ADR 0005 section 3): "
            + ", ".join(foreign)
        )
    by_name: dict[str, list[list[str]]] = {}
    for item in functions:
        parts = item.split("|")
        by_name.setdefault(parts[0], []).append(parts)
    for name, args in QUEUE_FUNCTIONS.items():
        rows = by_name.get(name, [])
        if len(rows) != 1:
            found.append(f"public.{name} must exist exactly once, found {len(rows)}")
            continue
        acl = EXPECTED_ACL
        # Once 40-withings.sql has installed the ingest, it must reach queue_enqueue: without
        # the grant every notification fails after its secret matched (#280).
        if name == "queue_enqueue" and by_name.get("webhook_event_insert"):
            acl = ENQUEUE_INGEST_ACL
        expected = [
            name,
            args,
            "true",
            QUEUE_ROLE,
            SEARCH_PATH,
            QUEUE_VERSION,
            QUEUE_DIGESTS.get(name, ""),
            acl,
        ]
        if rows[0] != expected:
            found.append(
                f"public.{name} differs from the shipped definition (owner, SECURITY DEFINER, "
                "search_path, version, md5 or ACL)"
            )
    for name in WITHINGS_FUNCTION_NAMES:
        rows = by_name.get(name, [])
        if not rows:
            continue
        args, owner, config, acl = WITHINGS_PINS[name]
        expected = [name, args, "true", owner, config, WITHINGS_VERSION, QUEUE_DIGESTS[name], acl]
        if len(rows) != 1 or rows[0] != expected:
            found.append(
                f"public.{name} differs from the shipped definition (owner, SECURITY DEFINER, "
                "settings, version, md5 or ACL)"
            )
    for name in RATE_FUNCTION_NAMES:
        rows = by_name.get(name, [])
        if not rows:
            continue
        args, owner, config, acl = RATE_PINS[name]
        expected = [name, args, "true", owner, config, RATE_VERSION, QUEUE_DIGESTS[name], acl]
        if len(rows) != 1 or rows[0] != expected:
            found.append(
                f"public.{name} differs from the shipped definition (owner, SECURITY DEFINER, "
                "settings, version, md5 or ACL)"
            )
    for name in OUTBOX_FUNCTION_NAMES:
        rows = by_name.get(name, [])
        if not rows:
            continue
        args, owner, config, acl = OUTBOX_PINS[name]
        expected = [name, args, "true", owner, config, OUTBOX_VERSION, OUTBOX_DIGESTS[name], acl]
        if len(rows) != 1 or rows[0] != expected:
            found.append(
                f"public.{name} differs from the shipped definition (owner, SECURITY DEFINER, "
                "settings, version, md5 or ACL)"
            )
    # The AI family is all or nothing: one dropped function of an installed family is drift.
    ai_installed = any(by_name.get(name) for name in AI_FUNCTION_NAMES)
    for name in AI_FUNCTION_NAMES if ai_installed else ():
        rows = by_name.get(name, [])
        args, owner, config, acl = AI_PINS[name]
        expected = [name, args, "true", owner, config, AI_VERSION, QUEUE_DIGESTS[name], acl]
        if len(rows) != 1 or rows[0] != expected:
            found.append(
                f"public.{name} differs from the shipped definition (owner, SECURITY DEFINER, "
                "settings, version, md5 or ACL)"
            )
    return found


def raise_if_drifted(row: Sequence[Any] | None) -> None:
    found = queue_problems(row)
    if found:
        raise QueueDriftError(
            "the job queue does not match ADR 0005 section 3: " + "; ".join(found)
        )


_verified: "weakref.WeakKeyDictionary[Engine, bool]" = weakref.WeakKeyDictionary()


def install_queue_check(engine: AsyncEngine) -> None:
    """Run the queue check on every new connection of ``engine`` (after the role check that
    ``create_engine`` installed); a drifted queue raises ``QueueDriftError`` there, so the pool
    never hands such a connection out. ``queue_verified(engine)`` turns true once one passed."""
    sync_engine = engine.sync_engine
    if sync_engine in _verified:
        return
    _verified[sync_engine] = False

    @event.listens_for(sync_engine, "connect")
    def check_queue(dbapi_connection: Any, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(QUEUE_QUERY)
            row = cursor.fetchone()
        finally:
            cursor.close()
        dbapi_connection.rollback()
        raise_if_drifted(row)
        _verified[sync_engine] = True


def queue_verified(engine: AsyncEngine) -> bool:
    """True once a connection of ``engine`` passed the queue check. The worker claims nothing
    before that (ADR 0005 section 3): a database that was down at startup was not checked yet."""
    return _verified.get(engine.sync_engine, False)


async def verify_queue(engine: AsyncEngine) -> None:
    """The startup check (ADR 0005 section 3): refuse to start on a drifted queue.

    ``engine`` must have the per-connection check installed (``install_queue_check``); this also
    checks a connection that was pooled before. An unreachable database is not an error here: the
    per-connection check applies to every connection made later, and ``queue_verified`` stays
    false until one passed."""
    if engine.sync_engine not in _verified:
        raise RuntimeError("install_queue_check(engine) first")
    try:
        async with engine.connect() as connection:
            row = (await connection.execute(text(QUEUE_QUERY))).one()
    except OperationalError as exc:
        if is_authentication_failure(exc):
            raise DatabaseRoleError(
                "the database refused the configured credentials; check database_url and the "
                "database_password secret file"
            ) from None
        logger.debug("database unreachable at startup; queue checked on first connection")
        return
    raise_if_drifted(row)
    _verified[engine.sync_engine] = True
