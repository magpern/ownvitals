"""The Withings app client secret (ADR 0004 sections 2 and 3).

Sealed with AAD ``ownvitals-app-v1|<app_id>``. The app role can read the ciphertext and cannot
write the row. Rewrap changes only the wrap. The operator CLI is the writer.
"""

from collections import Counter
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.connections.errors import ConnectionFailure
from ownvitals.context import UserContext
from ownvitals.crypto.envelope import (
    DecryptFailed,
    Sealed,
    app_aad,
    app_wrap_aad,
    open_sealed,
    rewrap,
    seal,
)
from ownvitals.crypto.keks import KeyRing, KeyUnavailable
from ownvitals.db import user_session
from ownvitals.jobs.issuer import owner_context
from ownvitals.withings.tables import withings_apps

_OPERATOR = UUID("00000000-0000-4000-8000-000000000022")
_A = withings_apps.c
Factory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True, repr=False)
class AppSecret:
    """One app's decrypted client secret. ``repr`` shows the id only."""

    app_id: int
    client_id: str
    secret: str
    callback_base: str

    def __repr__(self) -> str:
        return f"AppSecret(app_id={self.app_id})"


async def _kek(factory: Factory, ring: KeyRing) -> Any:
    async with user_session(factory, owner_context(_OPERATOR)) as db:
        kek: Any = await ring.current(db)
        return kek


async def seal_client_secret(factory: Factory, ring: KeyRing, app_id: int, secret: str) -> Sealed:
    """Envelope ``secret`` for ``withings_apps.id``. The plaintext is the secret, not JSON."""
    kek = await _kek(factory, ring)
    return seal(secret.encode("utf-8"), app_aad(app_id), kek, app_wrap_aad(kek.key_id, app_id))


async def open_client_secret(
    factory: Factory, ring: KeyRing, ctx: UserContext, app_id: int
) -> AppSecret:
    """The client id, secret and callback base, or ``key_unavailable`` / ``not_connected``."""
    failure: str | None = None
    opened: AppSecret | None = None
    async with user_session(factory, ctx) as db:
        row = (
            await db.execute(
                select(
                    _A.client_id,
                    _A.callback_base,
                    _A.client_secret,
                    _A.client_secret_nonce,
                    _A.client_wrapped_dek,
                    _A.client_dek_nonce,
                    _A.client_key_id,
                ).where(_A.id == app_id)
            )
        ).first()
        if row is None:
            failure = "not_connected"
        else:
            try:
                kek = await ring.kek_for(db, row.client_key_id)
            except KeyUnavailable:
                failure = "key_unavailable"
                kek = None
            raw: bytes | None = None
            if kek is not None:
                try:
                    raw = open_sealed(
                        row.client_secret,
                        row.client_secret_nonce,
                        row.client_wrapped_dek,
                        row.client_dek_nonce,
                        kek,
                        app_aad(app_id),
                        app_wrap_aad(kek.key_id, app_id),
                    )
                except DecryptFailed:
                    failure = "decrypt_failed"
            if raw is not None:
                opened = AppSecret(app_id, row.client_id, raw.decode("utf-8"), row.callback_base)
    if opened is None:
        category = failure if failure in ("key_unavailable", "not_connected") else "decrypt_failed"
        raise ConnectionFailure(category)
    return opened


async def rewrap_client_secrets(factory: Factory, ring: KeyRing) -> Counter[str]:
    """Move each wrap onto the current KEK. Counts are ``"<key id> <outcome>"``."""
    async with user_session(factory, owner_context(_OPERATOR)) as db:
        pending = (
            await db.execute(
                select(_A.id, _A.client_key_id).where(_A.client_key_id != ring.current_id)
            )
        ).all()
    counts: Counter[str] = Counter()
    for app_id, key_id in pending:
        counts[f"{key_id} {await _rewrap_one(factory, ring, int(app_id))}"] += 1
    return counts


async def _rewrap_one(factory: Factory, ring: KeyRing, app_id: int) -> str:
    outcome = "skipped"
    async with user_session(factory, owner_context(_OPERATOR)) as db:
        current = await ring.current(db)
        row = (
            await db.execute(
                select(_A.client_key_id, _A.client_wrapped_dek, _A.client_dek_nonce)
                .where(_A.id == app_id)
                .with_for_update()
            )
        ).first()
        if row is None or row.client_key_id == current.key_id:
            return "skipped"
        try:
            old = await ring.kek_for(db, row.client_key_id)
            wrapped, nonce = rewrap(
                row.client_wrapped_dek,
                row.client_dek_nonce,
                old,
                app_wrap_aad(old.key_id, app_id),
                current,
                app_wrap_aad(current.key_id, app_id),
            )
        except KeyUnavailable:
            outcome = "key_unavailable"
        except DecryptFailed:
            outcome = "decrypt_failed"
        else:
            result = await db.execute(
                update(withings_apps)
                .where(_A.id == app_id, _A.client_key_id == row.client_key_id)
                .values(
                    client_wrapped_dek=wrapped,
                    client_dek_nonce=nonce,
                    client_key_id=current.key_id,
                )
                .returning(_A.id)
            )
            outcome = "rewrapped" if result.first() is not None else "skipped"
    return outcome
