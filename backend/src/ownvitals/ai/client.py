"""The model client: one request to the model for one job, behind every lock (ADR 0014 sections
1, 5, 6 and 7; PLAN sections 2 and 7.4; #207).

``call_model(job, settings, kind=..., messages=...)`` is the only code that sends a model request.
It runs in the worker only, inside a job run, and in this order:

1. **The process**: the client is built first by ``egress.model_async_client``, which refuses
   outside a worker whose list holds the model host (web, or a worker with AI off) before any
   database or network socket is opened (``outbound_blocked``).
2. **The owner's locks, from the settings** (no I/O): ``ai_insights`` on, a pinned ``ai_model``,
   a recorded ``ai_provider_review_ref`` (lock 2), ``ai_eval_ref`` naming exactly this model,
   prompt version and tool-set version (lock 3), the model priced; the API key file
   ``anthropic_api_key`` (worker secrets only) and ``app_secret_key`` for the audit key.
3. **The request** (``ai.requests``): fixed prompt, tool definitions, closed messages; the bytes
   are scanned (no UUID, email, key or app secret) and bounded (``pricing.request_bound``).
4. **Consent, flag and budget, together**: one fenced transaction re-checks the stored flag and
   the live consent (``consent.require_enabled``, the consent row ``FOR SHARE``), the job's
   cancel, and reserves the bound (``budget.reserve``). It commits before the call.
5. **The call**, inside ``job.provider_call`` (the lease outlasts the total deadline): up to
   ``MAX_ATTEMPTS`` attempts under the **same reservation**, but only after an attempt that
   provably was not processed (not sent, 429, 529), and each later attempt re-checks consent,
   flag and cancel first. Every other failure ends the call.
6. **Settle or release** (``budget.may_release``) and write the ``ai_audit`` row in one new
   fenced transaction, with one log line (``ai.audit``); with the lease lost, the log line only.

The API key goes only into the ``x-api-key`` header; the body never holds it (checked). The
answer is a proposal (``ai.responses``): nothing here acts on it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from ownvitals import egress
from ownvitals.ai import audit, budget, consent
from ownvitals.ai.audit import AuditEntry, Settlement
from ownvitals.ai.gates import (
    ModelFailed,
    ModelRefused,
    category_of,
    check_locks,
    read_api_key,
    retry_after_of,
)
from ownvitals.ai.pricing import PRICES, Price, request_bound, usage_cost
from ownvitals.ai.requests import (
    API_VERSION,
    MAX_TOKENS,
    MESSAGES_PATH,
    PROMPT_VERSION,
    Kind,
    Message,
    RequestRefused,
    build_request,
    check_body,
    serialize,
)
from ownvitals.ai.responses import BadResponse, Reply, parse_reply
from ownvitals.ai.tools_model import TOOLSET_VERSION
from ownvitals.config import Settings
from ownvitals.jobs import FencedSession, JobRun, LeaseLost, RunStopped

if TYPE_CHECKING:
    import httpx

MAX_ATTEMPTS: Final = 3
BACKOFF_SECONDS: Final = (2.0, 8.0)
RETRYABLE_STATUSES: Final = frozenset({429, 529})
_URL: Final = f"https://{egress.MODEL_HOST}{MESSAGES_PATH}"

Sleep = Callable[[float], Awaitable[None]]


class _Call:
    """One model call's state: the attempts, the answer, the settlement."""

    def __init__(self, job: JobRun, settings: Settings, kind: Kind, model: str) -> None:
        self.job, self.settings, self.model = job, settings, model
        self.kind: Kind = kind
        self.attempts = 0
        self.status: int | None = None
        self.answer: bytes | None = None
        self.retry_after: float | None = None

    async def recheck(self) -> None:
        """Before a later attempt: the flag, the live consent and the job's cancel."""
        try:
            async with self.job.fenced() as fs:
                await consent.require_enabled(fs.db, self.job.ctx.user_id, self.settings)
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


async def _reserve(job: JobRun, settings: Settings, amount: int) -> budget.Reservation:
    try:
        async with job.fenced() as fs:
            if fs.cancel_requested:
                raise ModelRefused("cancelled")
            await consent.require_enabled(fs.db, job.ctx.user_id, settings)
            held = await budget.reserve(fs, amount, settings)
    except consent.AiNotEnabled:
        raise ModelRefused("ai_not_enabled") from None
    except LeaseLost:
        raise ModelRefused("lease_lost") from None
    if held is None:
        raise ModelRefused("ai_budget_exhausted")
    return held


Mode = Literal["settle", "charge", "release"]


async def _apply(
    fs: FencedSession, held: budget.Reservation, mode: Mode, actual: int
) -> tuple[Settlement, int]:
    """Settle at ``actual``, charge the reserved amount, or release; the settlement and the
    amount this call was charged."""
    if mode == "release":
        return ("released" if await budget.release(fs, held) else "unchanged"), 0
    charged = held.amount if mode == "charge" else actual
    outcome = await budget.settle(fs, held, charged)
    if outcome is budget.Settled.UNCHANGED:
        return "unchanged", 0
    if outcome is budget.Settled.OVERRUN:
        return "overrun", charged
    return ("charged" if mode == "charge" else "settled"), charged


async def call_model(
    job: JobRun,
    settings: Settings,
    *,
    kind: Kind,
    messages: Sequence[Message],
    prices: Mapping[str, Price] = PRICES,
    inner: httpx.AsyncBaseTransport | None = None,
    secrets_directory: Path | None = None,
    sleep: Sleep = asyncio.sleep,
) -> Reply:
    """One model request for ``job``'s user; the parsed answer. ``ModelRefused`` when no
    request was sent (nothing stays reserved); ``ModelFailed`` when one failed."""
    try:
        client = egress.model_async_client(inner=inner)
    except egress.OutboundBlocked:
        raise ModelRefused("outbound_blocked") from None
    async with client:
        model = check_locks(settings, prices)
        api_key = read_api_key(secrets_directory)
        try:
            key = audit.audit_key(settings)
            body = build_request(model, kind, messages)
            raw = serialize(body)
            app_secret = settings.app_secret_key
            check_body(raw, (api_key, "" if app_secret is None else app_secret.get_secret_value()))
        except audit.AuditNotConfigured:
            raise ModelRefused("ai_not_configured") from None
        except RequestRefused as refused:
            raise ModelRefused(refused.code) from None
        held = await _reserve(
            job, settings, request_bound(model, len(raw), MAX_TOKENS[kind], prices)
        )
        headers = {
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
        }
        call = _Call(job, settings, kind, model)
        started = time.monotonic()
        reply: Reply | None = None
        failure: ModelFailed | None = None
        stopped: RunStopped | None = None
        try:
            while reply is None:
                if call.attempts:
                    await sleep(call.retry_after or BACKOFF_SECONDS[call.attempts - 1])
                    await call.recheck()
                reply = await call.attempt(client, raw, headers)
                if reply is None and call.attempts >= MAX_ATTEMPTS:
                    raise ModelFailed(category_of(call.status) if call.status else "not_sent")
        except ModelFailed as exc:
            failure = exc
        except BadResponse:
            failure = ModelFailed("bad_response")
        except RunStopped as exc:  # cancelled, shut down or lost before a send
            stopped, failure = exc, ModelFailed("cancelled")
        except LeaseLost:  # lost during a re-check, after an earlier attempt was sent
            stopped, failure = RunStopped("lost"), ModelFailed("cancelled")
        latency = int((time.monotonic() - started) * 1000)
        return await _settle_and_audit(
            call, held, key, raw, reply, failure, stopped, latency, prices
        )


async def _settle_and_audit(
    call: _Call,
    held: budget.Reservation,
    key: bytes,
    raw: bytes,
    reply: Reply | None,
    failure: ModelFailed | None,
    stopped: RunStopped | None,
    latency: int,
    prices: Mapping[str, Price],
) -> Reply:
    usage = reply.usage if reply is not None else None
    mode: Mode
    if reply is not None and usage is not None:
        mode, actual = (
            "settle",
            usage_cost(
                call.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                prices=prices,
            ),
        )
    else:
        assert failure is not None  # noqa: S101 - a call ends with an answer or a failure
        unprocessed = failure.code in {"not_sent", "consent", "cancelled"} or (
            call.status is not None and budget.may_release(sent=True, status=call.status)
        )
        mode, actual = ("release" if unprocessed else "charge"), held.amount

    def entry(settlement: Settlement, cost: int) -> AuditEntry:
        return AuditEntry(
            job_id=call.job.lease.job_id,
            kind=call.kind,
            model=call.model,
            prompt_version=PROMPT_VERSION,
            toolset_version=TOOLSET_VERSION,
            reservation_id=held.id,
            request_hmac=audit.fingerprint(key, raw),
            response_hmac=None if call.answer is None else audit.fingerprint(key, call.answer),
            status="completed" if failure is None else "failed",
            error=None if failure is None else failure.code,
            http_status=call.status,
            settlement=settlement,
            latency_ms=latency,
            attempts=max(call.attempts, 1),
            cost_microusd=cost,
            tools=audit.tool_names(reply.tool_requests) if reply is not None else [],
            input_tokens=0 if usage is None else usage.input_tokens,
            output_tokens=0 if usage is None else usage.output_tokens,
            cache_write_tokens=0 if usage is None else usage.cache_write_tokens,
            cache_read_tokens=0 if usage is None else usage.cache_read_tokens,
        )

    # With the lease lost: no row (the attempt is no longer ours), the log line only, and the
    # reaper charges the reservation at its full amount.
    if stopped is not None and stopped.reason == "lost":
        audit.log(entry("lost", held.amount))
    else:
        try:
            # The settlement and its audit row commit together, in the job's fenced transaction.
            async with call.job.fenced() as fs:
                settlement, cost = await _apply(fs, held, mode, actual)
                await audit.record(fs, entry(settlement, cost))
        except LeaseLost:
            audit.log(entry("lost", held.amount))
    if stopped is not None:
        raise stopped
    if failure is not None:
        raise failure
    assert reply is not None  # noqa: S101
    return reply
