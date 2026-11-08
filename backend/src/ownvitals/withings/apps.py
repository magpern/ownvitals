"""Operator commands for ``withings_apps`` (ADR 0002 section 3, ADR 0004 section 4).

The process connects as ``ownvitals_owner``. The app role has no INSERT, UPDATE or DELETE on
the table. ``rotate`` is one conditional UPDATE: a pending ``compromise`` is never replaced by
``routine``. The command sets the request and an audit row. It does not derive or read a webhook
secret. In M1 a ``needs_reconnect`` from the worker is stored on the connection; this module
sends no mail.

The writes live in ``apps_db.py`` and are re-exported here; this module keeps the command line.
"""

import argparse
from pathlib import Path
from typing import Any, TextIO

from ownvitals.config import Settings
from ownvitals.crypto import keks
from ownvitals.crypto.keks import KeyRing, KeyRingError
from ownvitals.db import create_migrations_engine, create_session_factory
from ownvitals.withings.apps_db import (
    _REASONS,
    AppCommandError,
    Factory,
    create_app,
    request_rotation,
    rewrap_apps,
    set_secret,
)
from ownvitals.withings.apps_db import OWNER_ROLE as OWNER_ROLE
from ownvitals.withings.apps_db import ROTATE as ROTATE
from ownvitals.withings.apps_db import callback_base as callback_base


def add_withings_subcommands(sub: Any) -> None:
    """``withings-apps create|secret|rotate|rewrap``."""
    apps = sub.add_parser(
        "withings-apps", help="Withings application configuration (owner role)"
    ).add_subparsers(dest="action", required=True)
    create = apps.add_parser("create", help="store an app and its client secret")
    create.add_argument("--client-id", required=True)
    create.add_argument("--callback-base", required=True)
    create.add_argument("--secret-file", required=True)
    secret = apps.add_parser("secret", help="replace the client secret from a file")
    secret.add_argument("--app-id", type=int, required=True)
    secret.add_argument("--secret-file", required=True)
    rotate = apps.add_parser("rotate", help="ask the worker to rotate the webhook secret")
    rotate.add_argument("--app-id", type=int, required=True)
    rotate.add_argument("--reason", choices=tuple(_REASONS), required=True)
    apps.add_parser("rewrap", help="rewrap client secrets onto the current KEK")


def read_secret_file(path: str) -> str:
    """The file's contents, one trailing newline removed. Printable ASCII, no secret in a URL."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        raise AppCommandError("the secret file cannot be read") from None
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        raise AppCommandError("the client secret is not usable") from None
    if not value or len(value) > 256 or any(c < "!" or c > "~" or c in ";&=#" for c in value):
        raise AppCommandError("the client secret is not usable")
    return value


async def run_withings_apps(
    args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO
) -> int:
    """The ``withings-apps`` command. Exit 1 on a refusal. Nothing secret is printed."""
    engine = create_migrations_engine(settings)
    factory = create_session_factory(engine)
    try:
        if args.action == "rotate":
            stored = await request_rotation(engine, args.app_id, args.reason)
            print(f"rotation requested {stored}", file=out)
            return 0
        try:
            ring = await _ring(factory, settings)
        except KeyRingError as exc:
            print(f"error: {exc}", file=err)
            return 1
        if args.action == "create":
            app_id = await create_app(
                engine,
                factory,
                ring,
                args.client_id,
                args.callback_base,
                read_secret_file(args.secret_file),
            )
            print(app_id, file=out)
            return 0
        if args.action == "secret":
            await set_secret(engine, factory, ring, args.app_id, read_secret_file(args.secret_file))
            print("client secret stored", file=out)
            return 0
        counts = await rewrap_apps(engine, factory, ring)
        print("withings app secrets", file=out)
        if not counts:
            print("  (none)", file=out)
        for key in sorted(counts):
            print(f"  {key}: {counts[key]}", file=out)
        failed = sum(n for key, n in counts.items() if not key.endswith((" rewrapped", " skipped")))
        return 1 if failed else 0
    except AppCommandError as refused:
        print(f"error: {refused.message}", file=err)
        return 1
    finally:
        await engine.dispose()


async def _ring(factory: Factory, settings: Settings) -> KeyRing:
    return await keks.load_keyring(factory, settings, role="web")
