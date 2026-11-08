"""``probe --connection <id>``: the owner's shape-only check of a real Withings account
(PLAN-operations section 10.4, issue #29). Security-critical: it reads health data.

The owner runs it on the home server (``docs/runbooks/m2-probe.md``); agents never run it
against a real account. It calls the public API with the connection's stored grant, the way the
worker does, and prints **shapes only**:

- the outcome of every call (``ok``, a capability, or an error category with its API status);
- the capability state of every stream (``capabilities.py``), learned from the scopes and from
  these answers;
- measures: meastypes present and their counts, ``attrib``, ``category`` and ``position`` values
  with counts, and the names of the fields the groups and measures carry;
- ECG: how many recordings are listed, and for the newest few the number of arrays and their
  lengths, the sampling rate and the wear position, with counts, and the field names;
- notifications: the subscriptions' ``appli`` values and field names.

**Never** a measurement value, a sample, a timestamp, a date, a time zone, a device or user id, a
callback URL, a comment, a token or the provider's text: the report types hold only counts,
codes from fixed sets or below fixed bounds (anything else is ``other``), field names from the
models' fixed lists (an unknown key is only ``other``: a real account's key could be an id or a
timestamp) and categories from fixed sets, so nothing else can reach the output
(``tests/test_withings_probe*.py``).

Process role: this module is the one besides the worker's entry point that may install the
worker allowlist (``tests/egress_scan.py``, ``WORKER_ROLE_MODULES``); it never enables the model
host. Every call takes a token from the app's rate limiter on the user lane (``rate.py``) and
frees the slot after. A refused access token is refreshed once through the crash-safe path
(``tokens.py``), the only write the probe makes. The owner of the connection is found by visiting
each account's own session (``owner_context``, the way ``keys rewrap`` does), so the probe opens
no session itself. A failure prints the exception type only, never its message.

The parts live beside it: the report and its output (``probe_report.py``), the calling and
classifying (``probe_call.py``), the collection (``probe_collect.py``) and the rate-limiter slots
and stored tokens (``probe_access.py``). This module keeps the database, the grant and the CLI.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final, TextIO
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.config import Settings
from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.providers.app_secret import open_client_secret
from ownvitals.connections.providers.withings import open_withings
from ownvitals.connections.repository import connection_refs
from ownvitals.context import UserContext
from ownvitals.crypto.keks import KeyRingError, load_keyring
from ownvitals.db import (
    DatabaseRoleError,
    create_engine,
    create_session_factory,
    verify_database_role,
)
from ownvitals.egress import install_worker_allowlist
from ownvitals.jobs.issuer import owner_context
from ownvitals.log import configure_logging
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.probe_access import _Bucket, _StoredTokens
from ownvitals.withings.probe_call import SKIPPED_SCOPE as SKIPPED_SCOPE
from ownvitals.withings.probe_collect import DEFAULT_ECG_SIGNALS, collect
from ownvitals.withings.probe_collect import EP_GOALS as EP_GOALS
from ownvitals.withings.probe_collect import EP_HEART_GET as EP_HEART_GET
from ownvitals.withings.probe_collect import EP_HEART_LIST as EP_HEART_LIST
from ownvitals.withings.probe_collect import EP_MEASURES as EP_MEASURES
from ownvitals.withings.probe_collect import MAX_ECG_PAGES as MAX_ECG_PAGES
from ownvitals.withings.probe_collect import MAX_MEASURE_PAGES as MAX_MEASURE_PAGES
from ownvitals.withings.probe_report import HEADER as HEADER
from ownvitals.withings.probe_report import Report as Report
from ownvitals.withings.probe_report import as_dict as as_dict
from ownvitals.withings.probe_report import render_json, render_text
from ownvitals.withings.tokens import recover_refresh, refresh_tokens

if TYPE_CHECKING:
    import httpx

Factory = async_sessionmaker[AsyncSession]

MAX_ECG_SIGNALS: Final = 10


# ----------------------------------------------------------------------- database and CLI


def _scopes(public: dict[str, Any]) -> tuple[str, ...]:
    raw = public.get("scopes") or ()
    if isinstance(raw, str):
        raw = [part for part in raw.split(",") if part]
    return tuple(str(item) for item in raw) if isinstance(raw, list | tuple) else ()


async def _owner(factory: Factory, connection_id: UUID) -> UserContext | None:
    """The owner's bare context, found in each account's own session; None when no account has
    a live Withings connection with this id."""
    # The account list is the operator CLI's (``keys rewrap`` visits accounts the same way).
    # Imported here: ``ownvitals.auth.operator`` brings the web-only mail module, which no
    # module of this worker package may import at its top (``tests/egress_worker.py``).
    from ownvitals.auth.operator import user_ids

    for user_id in await user_ids(factory):
        ctx = owner_context(user_id)
        if ("withings", connection_id) in await connection_refs(factory, ctx):
            return ctx
    return None


async def probe(
    settings: Settings,
    connection_id: UUID,
    out: TextIO,
    err: TextIO,
    *,
    as_json: bool = False,
    ecg_signals: int = DEFAULT_ECG_SIGNALS,
    inner: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Open the grant, call Withings, print the shapes. 0 when complete, else 1. ``inner``
    replaces the network in tests (every egress check still runs)."""
    engine = create_engine(settings)
    try:
        await verify_database_role(engine)
        factory = create_session_factory(engine)
        ring = await load_keyring(factory, settings, role="worker")
        ctx = await _owner(factory, connection_id)
        if ctx is None:
            print("error: no live Withings connection has this id", file=err)
            return 1
        view = await open_withings(factory, ring, ctx, connection_id)
        app_id = int(view.public["app_id"])
        secret = await open_client_secret(factory, ring, ctx, app_id)
        app = WithingsApp(secret.client_id, secret.secret)
        async with WithingsClient.open(app, inner=inner) as client:

            async def renew() -> tuple[str, str]:
                # The worker may have rotated the grant since it was opened: use its new token
                # rather than rotate it again.
                now_open = await open_withings(factory, ring, ctx, connection_id)
                if now_open.generation != view.generation:
                    return "current", now_open.access_token
                done = await refresh_tokens(factory, ring, ctx, connection_id, client)
                if done.outcome == "recover":
                    done = await recover_refresh(factory, ring, ctx, connection_id, client)
                return done.outcome, done.access_token

            report = await collect(
                client,
                _StoredTokens(view.access_token, renew),
                _Bucket(engine, app_id, connection_id, sleep=sleep),
                _scopes(view.public),
                connection_id,
                ecg_signals=ecg_signals,
            )
        print(render_json(report) if as_json else render_text(report), file=out)
        return 0 if report.stopped is None else 1
    except ConnectionFailure as failure:  # a fixed category
        print(f"error: {failure.category}", file=err)
        return 1
    except (DatabaseRoleError, KeyRingError) as refused:  # names roles and keys, never a secret
        print(f"error: {type(refused).__name__}: {refused}", file=err)
        return 1
    except Exception as exc:
        print(f"error: {type(exc).__name__}", file=err)
        return 1
    finally:
        await engine.dispose()


def _signals(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("a whole number") from None
    if not 0 <= number <= MAX_ECG_SIGNALS:
        raise argparse.ArgumentTypeError(f"between 0 and {MAX_ECG_SIGNALS}")
    return number


def add_probe_subcommand(sub: Any) -> None:
    """``probe --connection <uuid> [--json] [--ecg-signals N]``."""
    parser = sub.add_parser(
        "probe", help="print the shapes of a real Withings account's data (no values)"
    )
    parser.add_argument("--connection", required=True, metavar="UUID")
    parser.add_argument("--json", action="store_true", help="the same content as JSON")
    parser.add_argument(
        "--ecg-signals",
        type=_signals,
        default=DEFAULT_ECG_SIGNALS,
        metavar="N",
        help=f"how many of the newest ECG recordings to fetch (0 to {MAX_ECG_SIGNALS})",
    )


async def run(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """The CLI command. Makes this process the worker first: the probe calls Withings."""
    try:
        connection_id = UUID(str(args.connection))
    except ValueError:
        print("error: the connection id is not a uuid", file=err)
        return 1
    install_worker_allowlist(ai_enabled=False)  # never the model host
    # Log lines (the client's call warnings) go to stderr: stdout holds the report only, so
    # ``--json`` output is pure JSON.
    configure_logging(settings.log_level, stream=err)
    return await probe(
        settings,
        connection_id,
        out,
        err,
        as_json=bool(args.json),
        ecg_signals=int(args.ecg_signals),
    )
