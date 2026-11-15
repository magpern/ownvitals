"""One model call's attempts (ADR 0014 sections 5 and 7; #207): moved out of ``ai.client``
unchanged. ``ModelCall`` holds a call's attempts, status, answer and retry hint; ``attempt`` sends
the request once inside ``job.provider_call`` and ``recheck`` re-checks the flag, the live
consent and the job's cancel before a later attempt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Final

from ownvitals import egress
from ownvitals.ai import consent
from ownvitals.ai.audit import AuditKind
from ownvitals.ai.gates import ModelFailed, category_of, retry_after_of
from ownvitals.ai.requests import MESSAGES_PATH, Kind
from ownvitals.ai.responses import Reply, parse_reply
from ownvitals.config import Settings
from ownvitals.jobs import JobRun

if TYPE_CHECKING:
    import httpx

MAX_ATTEMPTS: Final = 3
BACKOFF_SECONDS: Final = (2.0, 8.0)
RETRYABLE_STATUSES: Final = frozenset({429, 529})
_URL: Final = f"https://{egress.MODEL_HOST}{MESSAGES_PATH}"

Sleep = Callable[[float], Awaitable[None]]


class ModelCall:
    """One model call's state: the attempts, the answer, the settlement."""

    def __init__(
        self, job: JobRun, settings: Settings, kind: Kind, model: str, audit_kind: AuditKind
    ) -> None:
        self.job, self.settings, self.model = job, settings, model
        self.kind: Kind = kind
        self.audit_kind: AuditKind = audit_kind
        self.attempts = 0
        self.status: int | None = None
        self.answer: bytes | None = None
        self.retry_after: float | None = None

    async def recheck(self) -> None:
        """Before a later attempt: the flag, the live consent and the job's cancel."""
        try:
            async with self.job.fenced() as fs:
                owner = fs.lease.ctx.user_id
                await consent.require_enabled(fs.db, owner, self.settings, nowait=True)
                if fs.cancel_requested:
                    raise ModelFailed("cancelled")
        except consent.AiNotEnabled:
            raise ModelFailed("consent") from None

    async def attempt(
        self, client: httpx.AsyncClient, raw: bytes, headers: Mapping[str, str]
    ) -> Reply | None:
        """One attempt; ``None`` when it may be retried (provably not processed)."""
        self.attempts += 1
        self.status, self.answer, self.retry_after = None, None, None
        try:
            async with self.job.provider_call(egress.MODEL_TOTAL_SECONDS):
                response = await client.post(_URL, content=raw, headers=dict(headers))
        except egress.OutboundBlocked:
            raise ModelFailed("not_sent") from None
        except egress.CONNECT_ERRORS:
            return None  # never sent
        except egress.TIMEOUT_ERRORS:
            raise ModelFailed("timeout") from None
        except egress.TRANSPORT_ERRORS:
            raise ModelFailed("transport") from None
        self.status, self.answer = response.status_code, response.content
        if response.status_code == 200:
            return parse_reply(response.content)
        self.retry_after = retry_after_of(response)
        if response.status_code in RETRYABLE_STATUSES:
            return None
        raise ModelFailed(category_of(response.status_code), self.retry_after)
