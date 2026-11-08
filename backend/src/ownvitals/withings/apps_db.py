"""The owner-role writes of the ``withings-apps`` operator commands (``apps.py``; ADR 0002
section 3, ADR 0004 section 4): create an app, replace its client secret, request a webhook
secret rotation and rewrap the client secrets, each with its audit row. The engine's writes check
that the connection is ``ownvitals_owner``."""

import hashlib
import json
from collections import Counter
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from ownvitals.connections.providers.app_secret import rewrap_client_secrets, seal_client_secret
from ownvitals.crypto.keks import KeyRing

OWNER_ROLE = "ownvitals_owner"
_EMAIL = "withings-apps@localhost"
_IP = hashlib.sha256(b"ownvitals-withings-apps").digest()
_REASONS = frozenset({"routine", "compromise"})

ROTATE = text(
    """
    UPDATE withings_apps
       SET rotation_requested_at = now(),
           rotation_reason = CASE
               WHEN rotation_requested_at IS NOT NULL AND rotation_reason = 'compromise'
               THEN 'compromise'
               ELSE :reason
           END
     WHERE id = :id
    RETURNING rotation_reason
    """
)
_INSERT = text(
    """
    INSERT INTO withings_apps (
        id, client_id, callback_base, client_secret, client_secret_nonce,
        client_wrapped_dek, client_dek_nonce, client_key_id
    ) VALUES (
        :id, :client_id, :callback_base, :secret, :nonce, :wrapped, :dek_nonce, :key_id
    )
    """
)
_SECRET = text(
    """
    UPDATE withings_apps
       SET client_secret = :secret, client_secret_nonce = :nonce,
           client_wrapped_dek = :wrapped, client_dek_nonce = :dek_nonce, client_key_id = :key_id
     WHERE id = :id
    """
)
_NEXT = text("SELECT nextval('public.withings_apps_id_seq')")
_AUDIT = text(
    """
    INSERT INTO admin_audit (
        actor_email, action, target_kind, target_id, params, outcome, request_id, ip_hash
    ) VALUES (
        :email, :action, 'withings_app', :target_id, CAST(:params AS jsonb), 'ok',
        :request_id, :ip_hash
    )
    """
)

Factory = async_sessionmaker[AsyncSession]


class AppCommandError(Exception):
    """A refusal the operator can act on. The message names no secret."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def callback_base(value: str) -> str:
    """An https origin, stored without a path, query or userinfo."""
    parts = urlsplit(value.strip())
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise AppCommandError("callback-base must be an https origin")
    origin = "https://" + parts.hostname
    if parts.port is not None:
        origin += f":{parts.port}"
    if not 1 <= len(origin) <= 200:
        raise AppCommandError("callback-base must be an https origin")
    return origin


def _client_id(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise AppCommandError("client-id must be 1 to 80 characters")
    if any(c < "!" or c > "~" for c in value):
        raise AppCommandError("client-id must be 1 to 80 characters")
    return value


def _audit(action: str, app_id: int, params: dict[str, object]) -> dict[str, object]:
    return {
        "email": _EMAIL,
        "action": action,
        "target_id": str(app_id),
        "params": json.dumps(params, separators=(",", ":"), sort_keys=True),
        "request_id": uuid4().hex,
        "ip_hash": _IP,
    }


async def _role(conn: AsyncConnection) -> str:
    return str((await conn.execute(text("SELECT current_user"))).scalar_one())


async def create_app(
    engine: AsyncEngine,
    factory: Factory,
    ring: KeyRing,
    client_id: str,
    base: str,
    secret: str,
) -> int:
    """Insert the app and its audit row in one transaction."""
    client_id = _client_id(client_id)
    base = callback_base(base)
    async with engine.connect() as conn:
        if await _role(conn) != OWNER_ROLE:
            raise AppCommandError("withings-apps connects as the owner role")
        app_id = int((await conn.execute(_NEXT)).scalar_one())
        await conn.commit()
    sealed = await seal_client_secret(factory, ring, app_id, secret)
    async with engine.begin() as conn:
        if await _role(conn) != OWNER_ROLE:
            raise AppCommandError("withings-apps connects as the owner role")
        await conn.execute(
            _INSERT,
            {
                "id": app_id,
                "client_id": client_id,
                "callback_base": base,
                "secret": sealed.ciphertext,
                "nonce": sealed.nonce,
                "wrapped": sealed.wrapped_dek,
                "dek_nonce": sealed.dek_nonce,
                "key_id": sealed.key_id,
            },
        )
        await conn.execute(_AUDIT, _audit("withings_app.create", app_id, {"app_id": app_id}))
    return app_id


async def set_secret(
    engine: AsyncEngine, factory: Factory, ring: KeyRing, app_id: int, secret: str
) -> None:
    """Replace the client secret. Webhook columns are not touched."""
    if type(app_id) is not int or app_id < 1:
        raise AppCommandError("the app id is not usable")
    sealed = await seal_client_secret(factory, ring, app_id, secret)
    async with engine.begin() as conn:
        if await _role(conn) != OWNER_ROLE:
            raise AppCommandError("withings-apps connects as the owner role")
        result = await conn.execute(
            _SECRET,
            {
                "id": app_id,
                "secret": sealed.ciphertext,
                "nonce": sealed.nonce,
                "wrapped": sealed.wrapped_dek,
                "dek_nonce": sealed.dek_nonce,
                "key_id": sealed.key_id,
            },
        )
        if result.rowcount != 1:
            raise AppCommandError("no such withings app")
        await conn.execute(_AUDIT, _audit("withings_app.secret", app_id, {"app_id": app_id}))


async def request_rotation(engine: AsyncEngine, app_id: int, reason: str) -> str:
    """One conditional UPDATE plus its audit row. Returns the reason now stored."""
    if reason not in _REASONS or type(app_id) is not int or app_id < 1:
        raise AppCommandError("the rotation request is not usable")
    async with engine.begin() as conn:
        if await _role(conn) != OWNER_ROLE:
            raise AppCommandError("withings-apps connects as the owner role")
        stored = (await conn.execute(ROTATE, {"id": app_id, "reason": reason})).scalar_one_or_none()
        if stored not in _REASONS:
            raise AppCommandError("no such withings app")
        await conn.execute(
            _AUDIT,
            _audit("withings_app.rotate", app_id, {"app_id": app_id, "reason": stored}),
        )
    return str(stored)


async def rewrap_apps(engine: AsyncEngine, factory: Factory, ring: KeyRing) -> Counter[str]:
    """Rewrap every client secret that is not on the current KEK, then one audit row."""
    async with engine.connect() as conn:
        if await _role(conn) != OWNER_ROLE:
            raise AppCommandError("withings-apps connects as the owner role")
    counts = await rewrap_client_secrets(factory, ring)
    changed = sum(count for key, count in counts.items() if key.endswith(" rewrapped"))
    async with engine.begin() as conn:
        await conn.execute(_AUDIT, _audit("withings_app.rewrap", changed, {"rewrapped": changed}))
    return counts
