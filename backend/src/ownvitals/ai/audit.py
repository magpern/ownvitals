"""The AI audit log with redacted content (ADR 0014 section 6; #207).

One ``ai_audit`` row per model call (migration 0022), written in the job owner's
``user_session`` (forced RLS; the app role may only ``SELECT`` and ``INSERT``), and one log line
with the same fields but the cost and the fingerprints. A row holds the job, kind, model, prompt
and tool-set versions, the names of the tools the answer asked for with their closed arguments,
the token counts, the cost, the reservation, the latency, attempts, status, a closed error
category, the HTTP status, the settlement and two fingerprints: HMAC-SHA-256 of the exact request
bytes and of the answer bytes under the key derived from ``app_secret_key`` with HKDF label
``ai-audit`` (ADR 0003 section 1). A row can be matched to a body we hold; a guessed message
cannot be confirmed from the row alone.

**Never** in a row or a log line: message text, tool results, the system prompt, the API key.
Nothing here takes free text: ``AuditEntry`` holds counts, closed codes and fingerprints, and
``tool_names`` keeps only a known tool's name and its validated closed arguments.
"""

import hashlib
import hmac
import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ownvitals.ai.requests import closed_arguments
from ownvitals.ai.responses import ToolRequest
from ownvitals.ai.tools_model import TOOLS
from ownvitals.auth.keys import derive_key
from ownvitals.config import MIN_APP_SECRET_BYTES, Settings
from ownvitals.context import UserContext, require_context
from ownvitals.db import user_session

logger = logging.getLogger(__name__)

Factory = async_sessionmaker[AsyncSession]
AI_AUDIT_OWNED: Final = ("ai_audit",)
AUDIT_LABEL: Final = "ai-audit"
AUDIT_TOOLS_MAX: Final = 16

# ``ai_audit.kind`` also admits the owner's evaluation run (ADR 0014 section 9), which that
# issue adds here with its own guard entry.
AuditKind = Literal["chat", "digest"]
Status = Literal["completed", "failed"]
ErrorCategory = Literal[
    "refused",
    "rate_limited",
    "overloaded",
    "server_error",
    "http_other",
    "timeout",
    "not_sent",
    "transport",
    "bad_response",
    "consent",
    "cancelled",
]
Settlement = Literal["settled", "overrun", "released", "charged", "unchanged", "lost"]

_INSERT = text(
    "INSERT INTO ai_audit (owner_id, job_id, kind, model, prompt_version, toolset_version, tools, "
    "input_tokens, output_tokens, cache_write_tokens, cache_read_tokens, cost_microusd, "
    "reservation_id, latency_ms, attempts, status, error, http_status, settlement, request_hmac, "
    "response_hmac) VALUES (:owner, :job, :kind, :model, :prompt, :toolset, CAST(:tools AS jsonb), "
    ":input, :output, :cache_write, :cache_read, :cost, :reservation, :latency, :attempts, "
    ":status, :error, :http_status, :settlement, :request_hmac, :response_hmac)"
)


class AuditNotConfigured(RuntimeError):
    """No ``app_secret_key``: no fingerprint key, so no call is made (``ai_not_configured``)."""


def audit_key(settings: Settings) -> bytes:
    """The fingerprint key: HKDF-SHA256 of ``app_secret_key`` with label ``ai-audit``."""
    secret = settings.app_secret_key
    if secret is None or len(secret.get_secret_value().encode()) < MIN_APP_SECRET_BYTES:
        raise AuditNotConfigured("ai_not_configured")
    return derive_key(secret.get_secret_value().encode(), AUDIT_LABEL)


def fingerprint(key: bytes, raw: bytes) -> bytes:
    """HMAC-SHA-256 of ``raw`` under ``key``."""
    return hmac.new(key, raw, hashlib.sha256).digest()


def tool_names(requests: Iterable[ToolRequest]) -> list[dict[str, Any]]:
    """The tools an answer asked for: a known tool's name and its *validated* arguments; an
    unknown name is recorded as ``unknown`` and invalid arguments as ``null``, never as sent."""
    out: list[dict[str, Any]] = []
    for request in list(requests)[:AUDIT_TOOLS_MAX]:
        arguments = closed_arguments(request.name, request.arguments)
        name = request.name if request.name in TOOLS else "unknown"
        out.append({"name": name, "arguments": arguments})
    return out


@dataclass(frozen=True, slots=True)
class AuditEntry:
    job_id: UUID | None
    kind: AuditKind
    model: str
    prompt_version: str
    toolset_version: str
    reservation_id: UUID
    request_hmac: bytes
    status: Status
    settlement: Settlement
    latency_ms: int
    attempts: int
    cost_microusd: int
    tools: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    error: ErrorCategory | None = None
    http_status: int | None = None
    response_hmac: bytes | None = None

    def log_fields(self) -> Mapping[str, Any]:
        """What the log line carries: the row's fields without the cost and the fingerprints."""
        return {
            "event": "ai_call",
            "job_id": None if self.job_id is None else str(self.job_id),
            "ai_kind": self.kind,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "toolset_version": self.toolset_version,
            "tools": [t["name"] for t in self.tools],
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "status": self.status,
            "error": self.error,
            "http_status": self.http_status,
            "settlement": self.settlement,
        }


async def record(factory: Factory, ctx: UserContext, entry: AuditEntry) -> None:
    """Insert the row as ``ctx``'s user and log the call."""
    require_context(ctx)
    tools = json.dumps(entry.tools[:AUDIT_TOOLS_MAX], separators=(",", ":"))
    async with user_session(factory, ctx) as db:
        await db.execute(
            _INSERT,
            {
                "owner": ctx.user_id,
                "job": entry.job_id,
                "kind": entry.kind,
                "model": entry.model,
                "prompt": entry.prompt_version,
                "toolset": entry.toolset_version,
                "tools": tools,
                "input": entry.input_tokens,
                "output": entry.output_tokens,
                "cache_write": entry.cache_write_tokens,
                "cache_read": entry.cache_read_tokens,
                "cost": entry.cost_microusd,
                "reservation": entry.reservation_id,
                "latency": entry.latency_ms,
                "attempts": entry.attempts,
                "status": entry.status,
                "error": entry.error,
                "http_status": entry.http_status,
                "settlement": entry.settlement,
                "request_hmac": entry.request_hmac,
                "response_hmac": entry.response_hmac,
            },
        )
    logger.info("ai call", extra=dict(entry.log_fields()))
