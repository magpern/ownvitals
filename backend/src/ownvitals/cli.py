"""Operator subcommands of ``python -m ownvitals``.

With no subcommand ``python -m ownvitals`` starts the server (``--host``, ``--port``). The
subcommands arrive with the issues that own them (the job queue, accounts, key rotation): each
adds its parser in ``add_subcommands`` and its coroutine to ``COMMANDS``. A command is
``async def (args, settings, out, err) -> int`` that returns 0, or 1 for a refusal, and prints
results to ``out`` and diagnostics to ``err``; it never prints a secret.

Every subcommand runs with the egress hook installed (``install_egress_hook("cli")``, ADR 0009)
and on the standard library's selector loop (``require_standard_event_loop``).

``jobs stats`` (#9) prints the queue's counts per job state (``queue_stats()``, ADR 0005 section
3: counts only, never ids), one ``<metric> <count>`` line each. Like the worker it refuses a
privileged role and a drifted queue (exit 1).

``invite create``, ``admin grant``, ``admin revoke`` and ``admin mfa-reset`` (#10, ADR 0003
section 1 "Admins") run as the app role through ``ownvitals.auth.operator``; they never touch the
tables themselves. ``invite create`` prints the code once on stdout (it is not stored); every
other line goes to stderr and names no secret. In the stack:
``docker compose exec web python -m ownvitals admin grant owner@example.com``.
"""

import argparse
import asyncio
import re
import sys
from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any, TextIO

from ownvitals.auth.operator import (
    AccountError,
    create_invite,
    grant_admin,
    mfa_reset,
    revoke_admin,
)
from ownvitals.config import Settings
from ownvitals.db import (
    DatabaseRoleError,
    create_engine,
    create_session_factory,
    event_loop_factory,
    verify_database_role,
)
from ownvitals.db.queue_check import install_queue_check, verify_queue
from ownvitals.egress import install_egress_hook, require_standard_event_loop
from ownvitals.jobs import queue
from ownvitals.mail import MailError

_DURATION = re.compile(r"(\d+)([dhm])")
_UNITS = {"d": "days", "h": "hours", "m": "minutes"}
MAX_INVITE_DAYS = 365

Command = Callable[[argparse.Namespace, Settings, TextIO, TextIO], Coroutine[Any, Any, int]]


def parse_duration(value: str) -> timedelta:
    """``7d``, ``12h`` or ``30m``; at least one minute, at most a year."""
    match = _DURATION.fullmatch(value.strip())
    if match is None:
        raise argparse.ArgumentTypeError("use a number and d, h or m, for example 7d")
    duration = timedelta(**{_UNITS[match.group(2)]: int(match.group(1))})
    if not timedelta(minutes=1) <= duration <= timedelta(days=MAX_INVITE_DAYS):
        raise argparse.ArgumentTypeError("the duration must be between 1 minute and 365 days")
    return duration


def add_subcommands(parser: argparse.ArgumentParser) -> None:
    """Optional subparsers: without one the server starts."""
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    invite = sub.add_parser("invite", help="manage invite codes").add_subparsers(
        dest="action", required=True
    )
    create = invite.add_parser("create", help="print one new invite code (shown once)")
    create.add_argument("--expires", type=parse_duration, default=timedelta(days=7), metavar="7d")
    admin = sub.add_parser("admin", help="manage admins").add_subparsers(
        dest="action", required=True
    )
    for name, text in (
        ("grant", "make an existing user an admin"),
        ("revoke", "make an admin a plain user"),
        ("mfa-reset", "remove the user's TOTP and recovery codes and mail a reset link"),
    ):
        admin.add_parser(name, help=text).add_argument("email")
    jobs = sub.add_parser("jobs", help="the job queue").add_subparsers(dest="action", required=True)
    jobs.add_parser("stats", help="count jobs per state (no ids)")


async def run_jobs(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """``jobs stats``: the queue's counts, after the role and queue checks."""
    engine = create_engine(settings)
    install_queue_check(engine)
    try:
        await verify_database_role(engine)
        await verify_queue(engine)
        for metric, total in await queue.stats(create_session_factory(engine)):
            print(f"{metric} {total}", file=out)
        return 0
    except DatabaseRoleError as refused:  # names roles and objects only, never a credential
        print(f"error: {refused}", file=err)
        return 1
    finally:
        await engine.dispose()


async def _accounts(args: argparse.Namespace, settings: Settings, factory: Any, err: TextIO) -> int:
    if args.action == "grant":
        changed = await grant_admin(factory, args.email)
        print("admin granted; all sessions revoked" if changed else "already an admin", file=err)
        return 0
    if args.action == "revoke":
        changed = await revoke_admin(factory, settings, args.email)
        print("admin revoked; all sessions revoked" if changed else "not an admin", file=err)
        return 0
    try:
        sent = await mfa_reset(factory, settings, args.email)
    except MailError as exc:  # names the exception class only
        print(
            f"TOTP and recovery codes removed; sessions revoked; reset link NOT mailed: {exc}",
            file=err,
        )
        return 1
    print(
        "TOTP and recovery codes removed; all sessions revoked; "
        + ("reset link mailed" if sent else "reset link NOT mailed: no sender is registered"),
        file=err,
    )
    return 0


async def run_auth(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """``invite create`` and ``admin grant|revoke|mfa-reset``, after the role check."""
    engine = create_engine(settings)
    try:
        await verify_database_role(engine)
        factory = create_session_factory(engine)
        if args.command == "invite":
            print(await create_invite(factory, args.expires), file=out)  # shown once
            print("invite code created; it is not stored and cannot be shown again", file=err)
            return 0
        return await _accounts(args, settings, factory, err)
    except (DatabaseRoleError, AccountError) as refused:  # never a credential
        print(f"error: {refused}", file=err)
        return 1
    finally:
        await engine.dispose()


# Subcommand name -> its coroutine.
COMMANDS: dict[str, Command] = {"jobs": run_jobs, "invite": run_auth, "admin": run_auth}


def run_command(
    args: argparse.Namespace,
    settings: Settings,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run an operator subcommand; the exit code is 0, or 1 for a refusal."""
    command = COMMANDS.get(args.command)
    if command is None:
        print(f"error: unknown command {args.command!r}", file=err or sys.stderr)
        return 2
    install_egress_hook("cli", settings)  # before anything can connect (ADR 0009)

    async def checked() -> int:
        require_standard_event_loop()  # the egress hook sees no other loop's sockets
        return await command(args, settings, out or sys.stdout, err or sys.stderr)

    return asyncio.run(checked(), loop_factory=event_loop_factory())
