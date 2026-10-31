"""Operator subcommands of ``python -m ownvitals``.

With no subcommand ``python -m ownvitals`` starts the server (``--host``, ``--port``). The
subcommands arrive with the issues that own them (the job queue, accounts, key rotation): each
adds its parser in ``add_subcommands`` and its coroutine to ``COMMANDS``. A command is
``async def (args, settings, out, err) -> int`` that returns 0, or 1 for a refusal, and prints
results to ``out`` and diagnostics to ``err``; it never prints a secret.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Coroutine
from typing import Any, TextIO

from ownvitals.config import Settings
from ownvitals.db import event_loop_factory

Command = Callable[[argparse.Namespace, Settings, TextIO, TextIO], Coroutine[Any, Any, int]]

# Subcommand name -> its coroutine. Empty until a subcommand is added.
COMMANDS: dict[str, Command] = {}


def add_subcommands(parser: argparse.ArgumentParser) -> None:
    """Optional subparsers: without one the server starts."""
    parser.add_subparsers(dest="command", metavar="COMMAND")


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
    return asyncio.run(
        command(args, settings, out or sys.stdout, err or sys.stderr),
        loop_factory=event_loop_factory(),
    )
