"""Disconnect and account revocation for Withings (PLAN section 4.3, ADR 0004 section 7).

The worker calls this after a step-up. Every subscription the provider lists under the
stored callback origin is revoked, then the OAuth grant, then local credentials. A listed
callback URL is compared and passed straight to the provider; it is not stored and not
logged. Credentials are removed even when a provider call fails. That failure cannot be
retried once the credential is gone, so the result says so and carries a short instruction
for the person to remove access in their Withings account.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.providers.app_secret import open_client_secret
from ownvitals.connections.providers.withings import open_withings
from ownvitals.connections.repository import disconnect, list_connections
from ownvitals.context import UserContext
from ownvitals.crypto.keks import KeyRing
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.errors import RetryBudget, Transient, WithingsError

logger = logging.getLogger(__name__)

_PROVIDER = "withings"
_REMOVAL = (
    "The upstream revoke did not finish. Remove this application's access in the Withings account."
)
_REASONS = frozenset({"step_up_required", "not_connected"})
# Jitter for the OAuth revoke retries. Not a secret.
_JITTER = random.Random()  # noqa: S311
Sleep = Callable[[float], Awaitable[None]]
Factory = async_sessionmaker[AsyncSession]


class DisconnectRefused(Exception):
    """The disconnect did not run. ``reason`` is a fixed code."""

    def __init__(self, reason: str) -> None:
        if reason not in _REASONS:
            raise ValueError("unknown disconnect refusal")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DisconnectResult:
    """What the disconnect did. No credential, no callback URL, no provider user id."""

    cleared: bool
    subscriptions_revoked: int
    token_revoked: bool
    revoke_failed: bool
    removal: str | None


async def _pause(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _under_base(url: str, base: str) -> bool:
    """True when ``url`` is on the stored callback origin. Same rule as connect."""
    root = base.rstrip("/")
    parsed = urlsplit(url)
    base_parts = urlsplit(root)
    return (
        parsed.scheme == base_parts.scheme
        and parsed.netloc == base_parts.netloc
        and (
            parsed.path == base_parts.path
            or parsed.path.startswith(base_parts.path.rstrip("/") + "/")
        )
    )


async def _listed(
    client: WithingsClient, token: str, base: str, budget: RetryBudget
) -> tuple[list[tuple[int, str]], bool]:
    """Subscriptions on our origin. The URL list is for the revoke call only."""
    try:
        listed = await client.notify_list(token, budget=budget)
    except WithingsError:
        return [], True
    found: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    failed = False
    for profile in listed.profiles:
        appli = profile.appli
        if isinstance(appli, bool) or not isinstance(appli, int):
            failed = True
            continue
        url = profile.callbackurl.get_secret_value()
        if not _under_base(url, base):
            continue
        item = (appli, url)
        if item not in seen:
            seen.add(item)
            found.append(item)
    return found, failed


async def _revoke_grant(
    client: WithingsClient, userid: int, budget: RetryBudget, pause: Sleep
) -> bool:
    """OAuth revoke. Each attempt takes a fresh nonce; the budget bounds the repeats."""
    while True:
        try:
            await client.revoke(userid)
            return True
        except Transient:
            delay = budget.next_delay(_JITTER, time.monotonic())
            if delay is None:
                return False
            await pause(delay)
        except WithingsError:
            return False


async def _revoke_upstream(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    connection_id: UUID,
    budget: RetryBudget,
    pause: Sleep,
    inner: object,
) -> tuple[int, bool, bool]:
    """Notify revokes, then the OAuth revoke. Raises only when the row cannot be opened."""
    opened = await open_withings(factory, ring, ctx, connection_id)
    app_id = opened.public.get("app_id")
    userid = opened.public.get("withings_userid")
    if type(app_id) is not int or type(userid) is not int or app_id < 1 or userid < 1:
        return 0, False, True
    app = await open_client_secret(factory, ring, ctx, app_id)
    token = opened.access_token
    async with WithingsClient.open(
        WithingsApp(app.client_id, app.secret),
        inner=inner,  # type: ignore[arg-type]
        sleep=pause,
    ) as client:
        targets, failed = await _listed(client, token, app.callback_base, budget)
        revoked = 0
        for appli, url in targets:
            try:
                await client.notify_revoke(token, callbackurl=url, appli=appli, budget=budget)
                revoked += 1
            except WithingsError:
                failed = True
        token_revoked = await _revoke_grant(client, userid, budget, pause)
    return revoked, token_revoked, failed or not token_revoked


async def disconnect_withings(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    connection_id: UUID,
    *,
    stepped_up: bool,
    inner: object = None,
    budget: RetryBudget | None = None,
    sleep: Sleep | None = None,
) -> DisconnectResult:
    """Revoke upstream, then drop credentials on this user's live Withings row.

    ``stepped_up`` must be true. A row that is not this user's live Withings row is
    refused and nothing is changed. Provider failures are retried inside ``budget``
    (two transient retries when none is passed).
    """
    if stepped_up is not True:
        raise DisconnectRefused("step_up_required")
    rows = await list_connections(factory, ctx)
    live = next(
        (
            row
            for row in rows
            if row.id == connection_id
            and row.provider == _PROVIDER
            and row.status != "disconnected"
        ),
        None,
    )
    if live is None:
        raise DisconnectRefused("not_connected")
    limit = budget if budget is not None else RetryBudget(retries=2)
    pause = sleep or _pause
    revoked = 0
    token_revoked = False
    failed = False
    unexpected: Exception | None = None
    try:
        revoked, token_revoked, failed = await _revoke_upstream(
            factory, ring, ctx, connection_id, limit, pause, inner
        )
    except ConnectionFailure:
        failed = True
    except Exception as exc:
        # The credential is removed even when the provider call fails closed.
        failed = True
        unexpected = exc
    cleared = await disconnect(factory, ctx, _PROVIDER)
    logger.info(
        "withings disconnect finished",
        extra={
            "connection_id": str(connection_id),
            "subscriptions_revoked": revoked,
            "token_revoked": token_revoked,
            "revoke_failed": failed,
        },
    )
    if unexpected is not None:
        raise unexpected
    return DisconnectResult(
        cleared=cleared,
        subscriptions_revoked=revoked,
        token_revoked=token_revoked,
        revoke_failed=failed,
        removal=_REMOVAL if failed else None,
    )
