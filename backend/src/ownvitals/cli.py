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
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any, TextIO

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

Command = Callable[[argparse.Namespace, Settings, TextIO, TextIO], Coroutine[Any, Any, int]]


def add_subcommands(parser: argparse.ArgumentParser) -> None:
    """Optional subparsers: without one the server starts."""
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
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


# Subcommand name -> its coroutine.
COMMANDS: dict[str, Command] = {"jobs": run_jobs}


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
