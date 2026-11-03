"""Withings job kinds, lanes and the webhook-secret target check (ADR 0005 section 8, #18).

The names, lanes and input models live here. Handlers are not registered on ``KINDS`` (the
API's registry, which stays empty): the worker process builds its own registry in
``ownvitals.worker.kinds``. Jobs are created by ``queue_enqueue`` (webhook), ``queue_schedule``
(the scheduler's poll and reconciliation) or by replay.

Lanes, highest first (lower number claimed first): user work, webhook, poll, reconcile, backfill.
A singleton key is one queued job per owner, connection and stream. A job that names a webhook
``secret_id`` stores it on ``target_secret_id``; the runner asks ``SecretTargets`` before the
handler and stops when the entry is not confirmed active.
"""

import re
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# PLAN section 4.4, plus ``notify_update`` (ADR 0008: re-point a subscription at a secret).
WITHINGS_KINDS = (
    "withings_connect",
    "withings_backfill",
    "withings_sync",
    "ecg_fetch",
    "withings_reconcile",
    "withings_poll",
    "token_refresh_recover",
    "notify_update",
)
SINGLETON_KIND = "withings_sync"
DEBOUNCE_SECONDS = 30

LANE_SYNC_NOW = 1
LANE_WEBHOOK = 2
LANE_POLL = 3
LANE_RECONCILE = 4
LANE_BACKFILL = 5

_STREAM = re.compile(r"^[a-z_]{1,40}$")


def lane_for(kind: str, origin: str) -> int:
    """The claim lane for a kind and origin. The same order as ``queue_enqueue``."""
    if kind == "withings_backfill":
        return LANE_BACKFILL
    if kind == "withings_reconcile":
        return LANE_RECONCILE
    if kind == "withings_poll":
        return LANE_POLL
    if origin == "webhook":
        return LANE_WEBHOOK
    if origin == "user":
        return LANE_SYNC_NOW
    return LANE_POLL


def singleton_for(connection_id: UUID, stream: str) -> str:
    """The singleton key of a sync for one connection and stream (ids only, no secret)."""
    if _STREAM.fullmatch(stream) is None:
        raise ValueError("a stream name is 1 to 40 of a-z and _")
    return f"{connection_id.hex}:{stream}"


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConnectInput(_Input):
    connection_id: UUID


class StreamInput(_Input):
    connection_id: UUID
    stream: str = Field(pattern=r"^[a-z_]{1,40}$")


class NotifyUpdateInput(_Input):
    connection_id: UUID
    secret_id: UUID


INPUT_MODELS: dict[str, type[_Input]] = {
    "withings_connect": ConnectInput,
    "withings_backfill": StreamInput,
    "withings_sync": StreamInput,
    "ecg_fetch": StreamInput,
    "withings_reconcile": ConnectInput,
    "withings_poll": ConnectInput,
    "token_refresh_recover": ConnectInput,
    "notify_update": NotifyUpdateInput,
}


def target_secret(parsed: BaseModel) -> UUID | None:
    """The webhook secret a job re-checks, when its input names one."""
    secret = getattr(parsed, "secret_id", None)
    return secret if isinstance(secret, UUID) else None


class SecretTargets(Protocol):
    """Whether a webhook secret entry is still the one a job was queued for.

    ``False`` when it is retired, missing or not confirmed: the run stops. The worker process
    passes ``ownvitals.withings.webhooks.WebhookTargets`` (``ownvitals.worker.kinds``); this
    module's default confirms nothing.
    """

    async def entry_active(
        self, factory: async_sessionmaker[AsyncSession], secret_id: UUID
    ) -> bool:
        """``True`` only when ``secret_id`` is still an active target."""
        ...


class NoTargets:
    """Fail closed: no entry is confirmed active, so a targeted job stops."""

    async def entry_active(self, factory: object, secret_id: UUID) -> bool:
        return False


NO_TARGETS: SecretTargets = NoTargets()


def parse_input(kind: str, data: dict[str, Any]) -> BaseModel:
    """Validate ``data`` for ``kind``. ``ValueError`` when the kind is not a Withings kind."""
    model = INPUT_MODELS.get(kind)
    if model is None:
        raise ValueError("unknown withings kind")
    return model.model_validate(data)
