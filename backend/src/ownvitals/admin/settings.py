"""The admin area's stored flags (classbeat ADR 0011 sections 6 and 7; ownvitals #12).

Every route has ``admin_gate`` (admin role, TOTP within 12 hours, step-up within 5 minutes for
every write, the admin rate limit, ``no-store``; CSRF by the auth middleware) and does its
database work in one ``admin_session`` on ``ownvitals_maint``: the audit row first, a read then
read only.

- ``GET /settings``: every flag key with its effective value and where it comes from
  (``environment`` or ``database``), the value stored and whether it validates, and the
  environment's own value.
- ``PUT /settings/{key}`` ``{value}``: stores a flag through ``ownvitals.flags.core``
  validation. The database can tighten the environment, never loosen it, so ``open`` sign-up is
  refused (422 ``environment_only``), as is any value that does not validate (422
  ``invalid_value``) and a key that is no flag (422 ``unknown_key``). A refusal changes nothing
  and writes one ``refused`` audit row.
- ``DELETE /settings/{key}``: clears the stored value (the JSON ``null``, which every reader
  takes as no row: maint has no ``DELETE`` on ``app_settings``), so the environment's value
  applies (404 ``not_found`` when nothing was stored).

The audit ``params`` of a write hold the key and what the value means (the mode or switch, a
banner's level and length), never free text.
"""

import json
from datetime import datetime
from typing import Annotated, Any

from fastapi import Path, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ownvitals.admin.audit import KEY_PATTERN, Target
from ownvitals.admin.db import AdminRefused, admin_session
from ownvitals.admin.routes import AdminActor, admin_router, require_db
from ownvitals.config import Settings
from ownvitals.flags import core
from ownvitals.flags.core import FlagRefused

router = admin_router()

SETTINGS = "/settings"
MAX_VALUE_BYTES = 2048  # the app_settings_value check

_ALL_SETTINGS = text("SELECT key, value, updated_at, updated_by FROM app_settings")
_UPSERT = text(
    "INSERT INTO app_settings (key, value, updated_by) VALUES (:k, CAST(:v AS jsonb), :by) "
    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
    "updated_by = EXCLUDED.updated_by"
)
_CLEAR = text(
    "UPDATE app_settings SET value = 'null'::jsonb, updated_at = now(), updated_by = :by "
    "WHERE key = :k AND value <> 'null'::jsonb RETURNING key"
)


class Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class FlagOut(Out):
    key: str
    source: str  # environment or database
    effective: Any
    environment: Any
    stored: Any  # the stored value, None when there is none
    stored_valid: bool
    updated_at: datetime | None
    updated_by: str | None  # the admin's id; the email is in the audit log


class SettingsOut(Out):
    flags: list[FlagOut]


class SettingIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Any


KeyPath = Annotated[str, Path(pattern=KEY_PATTERN)]


def _stored_valid(key: str, value: Any) -> bool:
    if value is None:
        return True
    try:
        core.validate_for_write(key, value)
    except FlagRefused:
        return False
    return True


def _flag(key: str, settings: Settings, row: Any) -> FlagOut:
    stored = None if row is None else row.value
    environment: Any
    effective: Any
    if key == "signup_mode":
        environment = settings.signup_mode
        effective = core.stricter_signup(settings.signup_mode, stored)
        source = "database" if effective != environment else "environment"
    elif key in core.SWITCH_KEYS:
        env_on: bool = getattr(settings, key)
        environment = "on" if env_on else "off"
        effective = "on" if core.stricter_switch(key, env_on, stored) else "off"
        source = "database" if effective != environment else "environment"
    else:  # banner
        environment = None
        shown = core.banner_setting(stored)
        effective = None if shown is None else {"text": shown.text, "level": shown.level}
        source = "database" if shown is not None else "environment"
    return FlagOut(
        key=key,
        source=source,
        effective=effective,
        environment=environment,
        stored=stored,
        stored_valid=_stored_valid(key, stored),
        updated_at=None if row is None else row.updated_at,
        updated_by=None if row is None or row.updated_by is None else str(row.updated_by),
    )


async def _view(session: AsyncSession, settings: Settings) -> SettingsOut:
    rows = {r.key: r for r in (await session.execute(_ALL_SETTINGS)).all()}
    return SettingsOut(flags=[_flag(k, settings, rows.get(k)) for k in core.KEYS])


async def _flag_view(session: AsyncSession, settings: Settings, key: str) -> FlagOut:
    (view,) = [f for f in (await _view(session, settings)).flags if f.key == key]
    return view


def _set_params(key: str, value: Any) -> dict[str, Any]:
    """The audit params of a validated write: what it means, never free text."""
    if key == "banner":
        return {"key": key, "level": value["level"], "text_length": len(value["text"])}
    return {"key": key, "value": value}


def _stored_bytes(value: Any) -> int:
    """The bytes ``value::text`` takes in ``app_settings``: UTF-8, as jsonb prints it."""
    return len(json.dumps(value, ensure_ascii=False, separators=(", ", ": ")).encode())


@router.get(SETTINGS, response_model=SettingsOut)
async def read_settings(request: Request, actor: AdminActor) -> SettingsOut:
    async with admin_session(require_db(request), actor, "setting.read") as session:
        return await _view(session, request.app.state.settings)


@router.put(f"{SETTINGS}/{{key}}", response_model=FlagOut)
async def set_flag(request: Request, actor: AdminActor, key: KeyPath, body: SettingIn) -> FlagOut:
    settings: Settings = request.app.state.settings
    canonical: Any = None
    refusal: str | None = None
    try:
        canonical = core.validate_for_write(key, body.value)
        if _stored_bytes(canonical) > MAX_VALUE_BYTES:
            refusal = "invalid_value"
    except FlagRefused as exc:
        refusal = exc.reason
    params = {"key": key} if refusal is not None else _set_params(key, canonical)
    async with admin_session(
        require_db(request),
        actor,
        "setting.set",
        write=True,
        target=Target(kind="setting", id=key),
        params=params,
    ) as session:
        if refusal is not None:
            raise AdminRefused(refusal, 422)
        await session.execute(_UPSERT, {"k": key, "v": json.dumps(canonical), "by": actor.user_id})
        return await _flag_view(session, settings, key)


@router.delete(f"{SETTINGS}/{{key}}", response_model=FlagOut)
async def clear_flag(request: Request, actor: AdminActor, key: KeyPath) -> FlagOut:
    settings: Settings = request.app.state.settings
    async with admin_session(
        require_db(request),
        actor,
        "setting.clear",
        write=True,
        target=Target(kind="setting", id=key),
    ) as session:
        if key not in core.KEYS:
            raise AdminRefused("unknown_key", 422)
        if (await session.execute(_CLEAR, {"k": key, "by": actor.user_id})).first() is None:
            raise AdminRefused("not_found", 404)
        return await _flag_view(session, settings, key)
