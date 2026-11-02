"""``python -m ownvitals keys rewrap`` and ``keys wipe`` (ADR 0004 section 3).

Prints counts only. Rewrap runs as the app role: one context per account, one row per
transaction, then the authenticator wraps. Wipe runs as the owner role, with no KEK, after
web and worker are stopped::

    docker compose --profile maint run --rm maint python -m ownvitals keys wipe --confirm

It refuses while the app role still has a session, or while the configured KEK id already
has a check row. Per account, credentials are removed and the row kept, the authenticator
is reset, and a mail is sent after the commit. Then every check row is deleted and the
affected tables are rewritten. ``CHECKPOINT`` needs a superuser; when it is refused, the
command prints the statement to run.
"""

import argparse
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, TextIO

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from ownvitals.auth.operator import (
    app_sessions,
    mark_wipe_mailed,
    pending_wipe_mails,
    user_ids,
    vacuum_wiped_auth,
)
from ownvitals.auth.totp_keys import rewrap_totp_secrets, totp_rows_by_key
from ownvitals.config import Settings
from ownvitals.connections.rewrap import rewrap_owner, rows_by_key
from ownvitals.connections.wipe import vacuum_connections, wipe_owner
from ownvitals.crypto import keks
from ownvitals.db import (
    DatabaseRoleError,
    create_engine,
    create_migrations_engine,
    create_session_factory,
    verify_database_role,
)
from ownvitals.jobs.issuer import owner_context
from ownvitals.mail import Mailer, MailError, MailMessage

CHECKPOINT_STEP = "docker compose exec db psql -U postgres -d ownvitals -c CHECKPOINT"
_OK_OUTCOMES = (" rewrapped", " skipped")
Factory = Any


def add_keys_subcommands(sub: Any) -> None:
    """``keys rewrap`` and ``keys wipe --confirm | --resend``."""
    keys = sub.add_parser("keys", help="rewrap or wipe connection keys").add_subparsers(
        dest="action", required=True
    )
    keys.add_parser("rewrap", help="move every connection and authenticator onto the current KEK")
    wipe = keys.add_parser(
        "wipe",
        help="after a suspected KEK compromise: remove credentials and reset every authenticator",
        description=(
            "Run in the maint service with web and worker stopped: docker compose --profile "
            "maint run --rm maint python -m ownvitals keys wipe --confirm. If CHECKPOINT is "
            f"refused, run: {CHECKPOINT_STEP}"
        ),
    )
    mode = wipe.add_mutually_exclusive_group(required=True)
    mode.add_argument("--confirm", action="store_true", help="really wipe")
    mode.add_argument("--resend", action="store_true", help="re-send the unsent wipe mails")


def _print_counts(title: str, counts: Counter[str], out: TextIO) -> None:
    print(title, file=out)
    if not counts:
        print("  (none)", file=out)
        return
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}", file=out)


def _failures(counts: Counter[str]) -> int:
    return sum(count for key, count in counts.items() if not key.endswith(_OK_OUTCOMES))


def _mail(settings: Settings, totp_reset: bool, email: str) -> MailMessage:
    """What was removed and what to do. No token, no address, no provider account."""
    lines = ["Saved provider connections were removed from this ownvitals account."]
    if totp_reset:
        lines.append("The authenticator and its recovery codes were removed too.")
    lines.append("Sign in, set up an authenticator again, and reconnect your provider.")
    lines.append(f"Sign in: {settings.public_base_url}/signin")
    lines.append("If you did not expect this message, contact whoever runs the site.")
    return MailMessage(email, "Reconnect your ownvitals account", "\n".join(lines) + "\n")


async def _send(settings: Settings, factory: Factory, event_id: Any, message: MailMessage) -> bool:
    try:
        await Mailer(settings).send(message)
    except MailError:
        return False
    await mark_wipe_mailed(factory, event_id)
    return True


async def rewrap(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Move every stored wrap onto the current KEK. Counts per key id only."""
    engine = create_engine(settings)
    try:
        await verify_database_role(engine)
        factory = create_session_factory(engine)
        ring = await keks.load_keyring(factory, settings, role="web")
        moved: Counter[str] = Counter()
        remaining: Counter[str] = Counter()
        for user_id in await user_ids(factory):
            ctx = owner_context(user_id)
            moved += await rewrap_owner(factory, ring, ctx)
            remaining += await rows_by_key(factory, ctx)
        totp_moved = await rewrap_totp_secrets(factory, ring)
        _print_counts("connections rewrapped (old key id, outcome):", moved, out)
        _print_counts("authenticators rewrapped (old key id, outcome):", totp_moved, out)
        _print_counts("connections per key id now:", remaining, out)
        _print_counts("authenticators per key id now:", await totp_rows_by_key(factory), out)
        failed = _failures(moved) + _failures(totp_moved)
        if failed:
            print(f"{failed} rows could not be rewrapped", file=err)
            return 1
        return 0
    except (DatabaseRoleError, keks.KeyRingError) as refused:
        print(f"error: {refused}", file=err)
        return 1
    finally:
        await engine.dispose()


async def _refusal(settings: Settings, factory: Factory) -> str | None:
    role = settings.sqlalchemy_url().username or "ownvitals_app"
    if await app_sessions(factory, role):
        return "the app role is still connected (stop web and worker first)"
    if await keks.has_check_row(factory, settings.connection_kek_id):
        return (
            "the configured KEK id already has a check row: install a new KEK under a new id first"
        )
    return None


async def resend(settings: Settings, factory: Factory, out: TextIO) -> int:
    pending = await pending_wipe_mails(factory)
    sent = 0
    for event in pending:
        try:
            ok = await _send(
                settings, factory, event.event_id, _mail(settings, event.totp_reset, event.email)
            )
        except Exception:
            ok = False
        sent += int(ok)
    unsent = len(pending) - sent
    print(f"wipe mails: {sent} sent, {unsent} unsent", file=out)
    return 1 if unsent else 0


async def _each(factory: Factory, settings: Settings, out: TextIO, err: TextIO) -> int | None:
    """Wipe every account. ``None`` when every account was processed (mails may be unsent)."""
    counts: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    for user_id in await user_ids(factory):
        try:
            event = await wipe_owner(factory, owner_context(user_id))
        except Exception as error:
            counts["failed"] += 1
            kinds[type(error).__name__] += 1
            continue
        if event is None:
            counts["nothing to wipe"] += 1
            continue
        counts["wiped"] += 1
        counts["connections"] += len(event.providers)
        counts["totp reset"] += int(event.totp_reset)
        try:
            sent = await _send(
                settings, factory, event.event_id, _mail(settings, event.totp_reset, event.email)
            )
        except Exception:
            sent = False
        counts["mails sent" if sent else "mails unsent"] += 1
    _print_counts("wipe:", counts, out)
    if counts["failed"]:
        causes = ", ".join(f"{kind}: {count}" for kind, count in sorted(kinds.items()))
        print(f"{counts['failed']} accounts were not processed ({causes}); run it again", file=err)
        return 1
    if counts["mails unsent"]:
        print("unsent mails: retry with keys wipe --resend", file=out)
    return None


async def _finish(engine: AsyncEngine, out: TextIO) -> None:
    factory = create_session_factory(engine)
    forgotten = await keks.delete_check_rows(factory)
    print(f"KEK check rows deleted: {forgotten}", file=out)
    async with engine.connect() as connection:
        autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
        await vacuum_connections(autocommit)
        await vacuum_wiped_auth(autocommit)
        await keks.vacuum_check_table(autocommit)
        try:
            await autocommit.execute(text("CHECKPOINT"))
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) != "42501":
                raise
            print(f"CHECKPOINT refused for the owner role; run it now: {CHECKPOINT_STEP}", file=out)
        else:
            print("CHECKPOINT done", file=out)


async def wipe(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """No app-role check on the engine: wipe runs as the owner role, on purpose."""
    engine = create_migrations_engine(settings)
    try:
        factory = create_session_factory(engine)
        if args.resend:
            return await resend(settings, factory, out)
        refusal = await _refusal(settings, factory)
        if refusal is not None:
            print(f"refused: {refusal}", file=err)
            return 1
        failed = await _each(factory, settings, out, err)
        if failed is not None:
            return failed
        await _finish(engine, out)
        return 0
    finally:
        await engine.dispose()


async def run(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    command: Callable[..., Awaitable[int]]
    if args.action == "rewrap":
        command = rewrap
    elif args.action == "wipe":
        command = wipe
    else:
        print(f"error: unknown keys action {args.action!r}", file=err)
        return 2
    return await command(args, settings, out, err)
