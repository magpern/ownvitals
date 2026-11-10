"""Withings rows on ``connections`` (ADR 0004 sections 6 and 7).

Token writes compare-and-swap ``token_generation``. The refresh lock is session-scoped on its
own pooled connection, so the intent commits before the Withings call and the lock still
covers that call. The app client secret lives in ``app_secret``.

This module opens a row and stores a grant. The refresh's writes (the lock, the intent, the
commit and the give-up) live in ``withings_refresh.py`` and the row's sealed payload and token
checks in ``withings_payload.py``; both are re-exported here.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from ownvitals.connections.errors import ConnectionFailure
from ownvitals.connections.providers.withings_payload import (
    _C,
    _REFRESH_TTL,
    Factory,
    _as_public,
    _payload,
    _public,
    _token,
)
from ownvitals.connections.providers.withings_payload import PROVIDER as PROVIDER
from ownvitals.connections.providers.withings_refresh import clear_intent as clear_intent
from ownvitals.connections.providers.withings_refresh import commit_refresh as commit_refresh
from ownvitals.connections.providers.withings_refresh import give_up as give_up
from ownvitals.connections.providers.withings_refresh import hold_refresh_lock as hold_refresh_lock
from ownvitals.connections.providers.withings_refresh import mark_intent as mark_intent
from ownvitals.connections.repository import write_tokens
from ownvitals.connections.tables import DISCONNECTED, connections
from ownvitals.context import UserContext
from ownvitals.crypto.envelope import (
    DecryptFailed,
    connection_aad,
    connection_wrap_aad,
    open_sealed,
    seal,
)
from ownvitals.crypto.keks import KeyRing, KeyUnavailable
from ownvitals.db import user_session

_REASONS = frozenset({"linked_elsewhere", "other_withings_account", "conflict"})


class GrantRefused(Exception):
    """The Withings account was not stored. ``reason`` is a fixed code, with no account id."""

    def __init__(self, reason: str) -> None:
        if reason not in _REASONS:
            raise ValueError("unknown grant refusal")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class StoredGrant:
    """The row that now holds the grant. No token."""

    id: UUID
    generation: int
    status: str


@dataclass(frozen=True, slots=True, repr=False)
class OpenWithings:
    """A decrypted Withings connection. ``repr`` has no token and no Withings user id."""

    id: UUID
    generation: int
    status: str
    access_token: str
    refresh_token: str
    public: dict[str, Any]
    intent: int | None
    stale_intent: bool

    def __repr__(self) -> str:
        return f"OpenWithings(id={self.id}, generation={self.generation}, status={self.status!r})"


def _scope_list(scope: str) -> list[str]:
    items = [part for part in scope.split(",") if part]
    if len(items) > 20 or any(
        len(part) > 40 or not part.replace(".", "").isalpha() for part in items
    ):
        raise ConnectionFailure("unknown")
    return items


def _matches(public: dict[str, Any], app_id: int, userid: int) -> bool:
    app = public.get("app_id")
    user = public.get("withings_userid")
    if app is None or user is None:
        return False
    try:
        return int(app) == app_id and int(user) == userid
    except (TypeError, ValueError):
        return False


async def open_withings(
    factory: Factory, ring: KeyRing, ctx: UserContext, connection_id: UUID
) -> OpenWithings:
    """Decrypt one Withings row. A GCM failure sets ``needs_reconnect`` on a connected row."""
    failure: str | None = None
    opened: OpenWithings | None = None
    async with user_session(factory, ctx) as db:
        row = (
            await db.execute(
                select(
                    _C.id,
                    _C.status,
                    _C.token_generation,
                    _C.refresh_in_flight,
                    _C.public,
                    _C.secret,
                    _C.secret_nonce,
                    _C.wrapped_dek,
                    _C.dek_nonce,
                    _C.key_id,
                    literal_column(
                        "(refresh_intent_at IS NOT NULL AND refresh_intent_at < "
                        "now() - interval '8 hours')"
                    ).label("stale"),
                ).where(_C.id == connection_id, _C.owner_id == ctx.user_id)
            )
        ).first()
        if row is None or row.status != "connected" or row.secret is None or row.key_id is None:
            failure = "not_connected"
        else:
            try:
                kek = await ring.kek_for(db, row.key_id)
            except KeyUnavailable:
                failure = "key_unavailable"
                kek = None
            payload: bytes | None = None
            if kek is not None:
                try:
                    payload = open_sealed(
                        row.secret,
                        row.secret_nonce,
                        row.wrapped_dek,
                        row.dek_nonce,
                        kek,
                        connection_aad(ctx.user_id, PROVIDER, row.id),
                        connection_wrap_aad(kek.key_id, ctx.user_id, PROVIDER, row.id),
                    )
                except DecryptFailed:
                    failure = "decrypt_failed"
            parsed: dict[str, Any] | None = None
            if payload is not None:
                try:
                    loaded = json.loads(payload)
                except ValueError:
                    loaded = None
                if (
                    isinstance(loaded, dict)
                    and isinstance(loaded.get("access_token"), str)
                    and isinstance(loaded.get("refresh_token"), str)
                ):
                    parsed = loaded
                else:
                    failure = "decrypt_failed"
            if parsed is not None:
                opened = OpenWithings(
                    id=row.id,
                    generation=int(row.token_generation),
                    status=row.status,
                    access_token=parsed["access_token"],
                    refresh_token=parsed["refresh_token"],
                    public=_as_public(row.public),
                    intent=None if row.refresh_in_flight is None else int(row.refresh_in_flight),
                    stale_intent=bool(row.stale),
                )
            if failure == "decrypt_failed" and row is not None:
                await db.execute(
                    update(connections)
                    .where(
                        _C.id == row.id,
                        _C.owner_id == ctx.user_id,
                        _C.token_generation == row.token_generation,
                        _C.status == "connected",  # a disconnect keeps the generation
                    )
                    .values(
                        status="needs_reconnect", last_error="decrypt_failed", updated_at=func.now()
                    )
                )
    if opened is None:
        raise ConnectionFailure(
            failure if failure in ("key_unavailable", "not_connected") else "decrypt_failed"
        )
    return opened


async def store_grant(
    factory: Factory,
    ring: KeyRing,
    ctx: UserContext,
    app_id: int,
    userid: int,
    scope: str,
    access_token: str,
    refresh_token: str,
    *,
    expires_in: int,
) -> StoredGrant:
    """Upsert by ``(app_id, withings_userid)``. Another account is refused."""
    if type(app_id) is not int or app_id < 1 or type(userid) is not int or userid < 1:
        raise GrantRefused("conflict")
    if type(expires_in) is not int or not 1 <= expires_in <= 86_400:
        raise GrantRefused("conflict")
    access, refresh = _token(access_token), _token(refresh_token)
    scopes = _scope_list(scope)
    public = _public(app_id, userid, scopes)
    access_expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    plaintext = _payload(access, refresh, access_expires_at)
    refused: str | None = None
    stored: StoredGrant | None = None
    async with user_session(factory, ctx) as db:
        kek = await ring.current(db)
        locked = (
            await db.execute(
                select(_C.id, _C.status, _C.token_generation, _C.public)
                .where(_C.owner_id == ctx.user_id, _C.provider == PROVIDER)
                .with_for_update()
            )
        ).all()
        same = [row for row in locked if _matches(_as_public(row.public), app_id, userid)]
        live = [row for row in locked if row.status != DISCONNECTED]
        if live and all(row.id != live[0].id for row in same):
            refused = "other_withings_account"
        elif same:
            target = next((row for row in same if row.status != DISCONNECTED), same[0])
            sealed = seal(
                plaintext,
                connection_aad(ctx.user_id, PROVIDER, target.id),
                kek,
                connection_wrap_aad(kek.key_id, ctx.user_id, PROVIDER, target.id),
            )
            if await write_tokens(
                db,
                ctx,
                target.id,
                int(target.token_generation),
                sealed,
                public=public,
                expires_at=datetime.now(UTC) + _REFRESH_TTL,
                reconnect=True,
            ):
                stored = StoredGrant(target.id, int(target.token_generation) + 1, "connected")
            else:
                refused = "conflict"
        else:
            row_id = uuid4()
            sealed = seal(
                plaintext,
                connection_aad(ctx.user_id, PROVIDER, row_id),
                kek,
                connection_wrap_aad(kek.key_id, ctx.user_id, PROVIDER, row_id),
            )
            try:
                async with db.begin_nested():
                    await db.execute(
                        pg_insert(connections).values(
                            id=row_id,
                            owner_id=ctx.user_id,
                            provider=PROVIDER,
                            status="connected",
                            public=public,
                            secret=sealed.ciphertext,
                            secret_nonce=sealed.nonce,
                            wrapped_dek=sealed.wrapped_dek,
                            dek_nonce=sealed.dek_nonce,
                            key_id=sealed.key_id,
                            token_generation=1,
                            expires_at=datetime.now(UTC) + _REFRESH_TTL,
                        )
                    )
            except IntegrityError as exc:
                diag = getattr(exc.orig, "diag", None)
                name = getattr(diag, "constraint_name", "") or ""
                refused = (
                    "other_withings_account"
                    if name == "connections_one_live"
                    else "linked_elsewhere"
                )
            else:
                stored = StoredGrant(row_id, 1, "connected")
    if refused is not None or stored is None:
        raise GrantRefused(refused or "conflict")
    return stored
