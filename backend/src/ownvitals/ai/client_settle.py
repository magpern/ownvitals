"""The end of a model call: settle, charge or release the reservation and write the ``ai_audit``
row, in one fenced transaction (ADR 0014 sections 6 and 7; #207). Split from ``ai.client``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal

from ownvitals.ai import audit, budget
from ownvitals.ai.audit import AuditEntry, Settlement
from ownvitals.ai.gates import ModelFailed
from ownvitals.ai.pricing import Price, usage_cost
from ownvitals.ai.requests import PROMPT_VERSION
from ownvitals.ai.responses import Reply
from ownvitals.ai.tools_model import TOOLSET_VERSION
from ownvitals.jobs import FencedSession, LeaseLost, RunStopped

if TYPE_CHECKING:
    from ownvitals.ai.client import _Call  # pyright: ignore[reportPrivateUsage]

Mode = Literal["settle", "charge", "release"]


async def apply(
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


async def settle_and_audit(
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
                settlement, cost = await apply(fs, held, mode, actual)
                await audit.record(fs, entry(settlement, cost))
        except LeaseLost:
            audit.log(entry("lost", held.amount))
    if stopped is not None:
        raise stopped
    if failure is not None:
        raise failure
    assert reply is not None  # noqa: S101
    return reply
