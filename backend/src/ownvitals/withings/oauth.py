"""Connect Withings (PLAN §4.3, ADR 0004 section 7).

``start`` keeps the OAuth state as a SHA-256, bound to the session, for 10 minutes and one use.
It refuses a session that has not stepped up. ``callback`` consumes the state before it exchanges
the code, in this request, through ``egress.exchange_withings_code``: the caller cannot pass a
body, a header or a URL. Refresh and revoke are not called here.

The state lives in the web process. ``oauth_flows`` accepts only the Google sign-in provider, and
this issue adds no table. One web process is what the home server runs; a second process would
not see the state.

On connect and reconnect the worker calls ``revoke_stale_subscriptions``. It lists subscriptions
and revokes every callback under our base that is not the current one. Those URLs are not stored
and not logged.

The state store lives in ``oauth_state.py`` and is re-exported here.
"""

import logging
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.providers.app_secret import open_client_secret
from ownvitals.connections.providers.withings import (
    GrantRefused,
    StoredGrant,
    open_withings,
    store_grant,
)
from ownvitals.context import UserContext
from ownvitals.crypto.keks import KeyRing
from ownvitals.egress import WITHINGS_ACCOUNT_HOST, exchange_withings_code
from ownvitals.withings.client import WithingsApp, WithingsClient
from ownvitals.withings.errors import WithingsError, interpret
from ownvitals.withings.models import TokenResponse
from ownvitals.withings.oauth_state import STATE_TTL_SECONDS as STATE_TTL_SECONDS
from ownvitals.withings.oauth_state import StateStore

logger = logging.getLogger(__name__)

_SCOPES = "user.info,user.metrics,user.activity,user.sleepevents"
_AUTHORIZE_PATH = "/oauth2_user/authorize2"
_AUTHORIZE_FIELDS = ("response_type", "client_id", "redirect_uri", "scope", "state")
_REASONS = frozenset(
    {
        "step_up_required",
        "rate_limited",
        "state_rejected",
        "linked_elsewhere",
        "other_withings_account",
        "exchange_failed",
        "not_connected",
        "conflict",
    }
)

Factory = async_sessionmaker[AsyncSession]


class ConnectRefused(Exception):
    """The connect did not store a grant. ``reason`` is a fixed code."""

    def __init__(self, reason: str) -> None:
        if reason not in _REASONS:
            raise ValueError("unknown connect refusal")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SessionProof:
    """The session the route already resolved. ``stepped_up`` is that session's recent step-up."""

    session_id: UUID
    stepped_up: bool


@dataclass(frozen=True, slots=True, repr=False)
class Started:
    """The browser redirect. ``repr`` hides the URL because it carries the state."""

    authorize_url: str
    state: str

    def __repr__(self) -> str:
        return "Started redacted"


def redirect_uri(callback_base: str) -> str:
    """The OAuth redirect under a stored callback origin."""
    return callback_base.rstrip("/") + "/api/oauth/withings/callback"


def authorize_url(*, client_id: str, redirect: str, state: str) -> str:
    """The browser redirect. The query is the five authorize fields and nothing else."""
    fields = (
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect),
        ("scope", _SCOPES),
        ("state", state),
    )
    if tuple(name for name, _ in fields) != _AUTHORIZE_FIELDS:
        raise RuntimeError("the authorize URL gained a field")
    return f"https://{WITHINGS_ACCOUNT_HOST}{_AUTHORIZE_PATH}?{urlencode(fields)}"


def _under_base(url: str, base: str) -> bool:
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


async def start(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    proof: SessionProof,
    app_id: int,
    *,
    store: StateStore,
) -> Started:
    """Begin a connect. Step-up is required; the state is bound to ``proof.session_id``."""
    if proof.stepped_up is not True:
        raise ConnectRefused("step_up_required")
    app = await open_client_secret(factory, ring, ctx, app_id)
    redirect = redirect_uri(app.callback_base)
    state = await store.issue(
        session_id=proof.session_id, user_id=ctx.user_id, app_id=app_id, redirect_uri=redirect
    )
    if state is None:
        raise ConnectRefused("rate_limited")
    return Started(authorize_url(client_id=app.client_id, redirect=redirect, state=state), state)


async def callback(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    proof: SessionProof,
    code: str,
    state: str,
    *,
    store: StateStore,
    inner: object = None,
) -> StoredGrant:
    """Exchange ``code`` in this call, then upsert. Replay and a foreign session are refused."""
    if proof.stepped_up is not True:
        raise ConnectRefused("step_up_required")
    pending = await store.take(state)
    if pending is None or pending.session_id != proof.session_id or pending.user_id != ctx.user_id:
        raise ConnectRefused("state_rejected")
    app = await open_client_secret(factory, ring, ctx, pending.app_id)
    try:
        response = await exchange_withings_code(
            client_id=app.client_id,
            client_secret=app.secret,
            code=code,
            redirect_uri=pending.redirect_uri,
            inner=inner,  # type: ignore[arg-type]
        )
        issued = interpret(
            "v2/oauth2:requesttoken", response.status_code, response.content, TokenResponse
        )
    except WithingsError:
        raise ConnectRefused("exchange_failed") from None
    try:
        stored = await store_grant(
            factory,
            ring,
            ctx,
            pending.app_id,
            int(issued.userid),
            issued.scope,
            issued.access_token.get_secret_value(),
            issued.refresh_token.get_secret_value(),
            expires_in=int(issued.expires_in),
        )
    except GrantRefused as refused:
        raise ConnectRefused(refused.reason) from None
    except ConnectionFailure:
        raise ConnectRefused("exchange_failed") from None
    logger.info(
        "withings connection stored",
        extra={"connection_id": str(stored.id), "generation": stored.generation},
    )
    return stored


async def revoke_stale_subscriptions(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    connection_id: UUID,
    *,
    current: str | None,
    inner: object = None,
) -> tuple[int, int]:
    """Revoke subscriptions under our callback base that are not ``current``.

    Returns ``(revoked, kept)``. URLs from ``notify list`` are compared and passed to revoke,
    then dropped. They are not logged.
    """
    try:
        opened = await open_withings(factory, ring, ctx, connection_id)
        app_id = int(opened.public["app_id"])
        app = await open_client_secret(factory, ring, ctx, app_id)
    except (ConnectionFailure, KeyError, TypeError, ValueError):
        raise ConnectRefused("not_connected") from None
    revoked = 0
    kept = 0
    async with WithingsClient.open(
        WithingsApp(app.client_id, app.secret),
        inner=inner,  # type: ignore[arg-type]
    ) as client:
        listed = await client.notify_list(opened.access_token)
        for profile in listed.profiles:
            url = profile.callbackurl.get_secret_value()
            if not _under_base(url, app.callback_base):
                continue
            if current is not None and url == current:
                kept += 1
                continue
            await client.notify_revoke(
                opened.access_token, callbackurl=url, appli=int(profile.appli)
            )
            revoked += 1
    logger.info(
        "withings subscriptions revoked",
        extra={"connection_id": str(connection_id), "revoked": revoked, "kept": kept},
    )
    return revoked, kept
