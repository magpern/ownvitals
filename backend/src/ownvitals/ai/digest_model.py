"""The weekly digest's rules, without I/O (ADR 0014 sections 3, 5 and 7; ADR 0010 section 8; PLAN
section 7.4; issue 209).

- ``DIGEST_KIND`` and ``DigestInput``: the job (a week only: no text, no id but the job's own).
- ``digest_week(now)``: the week a digest written at ``now`` covers: the last full ISO week
  (Monday to Sunday, UTC) before ``now``'s day. ``week_ok`` admits that week and the three before
  it (a job queued late in one week and run early in the next), nothing in the future.
- ``request(week)``: the one fixed user message of a digest turn. It names the period and the
  tools to read, includes the nerve metrics' monthly summary (ADR 0010 section 8: "a view (the
  M6 nerve page, the M7 digest), not a rule") and asks for quiet copy without nudges
  (PLAN-operations section 9.4). It holds no user data; the data comes only from the tools.
- ``ending_for(code)``: what a refused or failed model call means for the digest. The cap
  (``ai_budget_exhausted``) stores the week as skipped (ADR 0014 section 7: "the digest for that
  week is skipped with the same in-app note"); a call that may have been sent stores the week as
  ``unavailable``, so a failing provider is tried once a week, never again every few hours; a
  missing consent, an owner lock, the egress block or a cancel stores nothing.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from ownvitals.ai.requests import Message, Text
from ownvitals.ai.tables import DIGEST_MAX_CHARS, DigestStatus

DIGEST_KIND: Final = "ai_digest"
SINGLETON_KEY: Final = "ai_digest"
# How many earlier weeks a queued digest may still be written for.
LATE_WEEKS: Final = 3
LIST_LIMIT: Final = 12  # digests shown, the newest
# ADR 0010 section 8: the nerve page's metrics (``views.catalog.NERVE_KEYS``; a test holds both
# equal).
NERVE_METRICS: Final = ("nerve_health_score", "nerve_response_score", "esc")
# The tool's name (``ai.tools_model.TOOLS``), named once here (``test_findings_read_guard``).
_FINDINGS_TOOL: Final = "findings"

# The owner's locks and configuration: no call could be made, and none was.
_NOT_CONFIGURED: Final = frozenset(
    {
        "ai_model_unset",
        "ai_provider_review_missing",
        "ai_eval_mismatch",
        "ai_model_unpriced",
        "ai_not_configured",
    }
)
_NOT_ENABLED: Final = frozenset({"ai_not_enabled", "consent"})
_STOPPED: Final = frozenset({"cancelled", "lease_lost"})

Ending = Literal["stop", "ai_not_enabled", "outbound_blocked", "ai_unavailable", "store"]


class DigestInput(BaseModel):
    """The ``ai_digest`` job's input: the Monday of the week to write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    week_start: date


@dataclass(frozen=True, slots=True)
class DigestEnd:
    """``stop``: a cancel stops the run; ``store``: the week is stored with ``status`` (no
    text); anything else: the job fails with that category and nothing is stored."""

    ending: Ending
    status: DigestStatus | None = None


def digest_week(now: datetime) -> date:
    """The Monday of the last full ISO week before ``now``'s day (UTC)."""
    today = now.date()
    return today - timedelta(days=today.weekday() + 7)


def week_ok(week: date, now: datetime) -> bool:
    """A Monday, not after ``digest_week(now)`` and at most ``LATE_WEEKS`` weeks before it."""
    latest = digest_week(now)
    earliest = latest - timedelta(weeks=LATE_WEEKS)
    return week.weekday() == 0 and earliest <= week <= latest


def request(week: date) -> list[Message]:
    """The digest turn's one user message (fixed text; dates only)."""
    end = week + timedelta(days=6)
    nerve = ", ".join(NERVE_METRICS)
    instruction = (
        f"Write this user's weekly summary for the week from {week.isoformat()} to "
        f"{end.isoformat()}, both days included. Read the data with the tools: list_metrics "
        "first, then metric_summary or daily_values for the metrics that have data, and "
        f"{_FINDINGS_TOOL} with status active. Describe briefly, in a few short paragraphs, what "
        "was measured that week and how it compares with the user's typical range and trend. "
        f"Then add a short monthly summary of the nerve metrics ({nerve}) from metric_summary "
        f"with period 30 and from {_FINDINGS_TOOL}, only when the user has those metrics; "
        "otherwise leave it out. Keep the tone quiet and neutral: do not tell the user what to "
        "do, do not urge more measurements and do not praise or warn. Write in English."
    )
    return [Message("user", (Text(instruction),))]


def content_fits(text: str) -> bool:
    """A digest the ``ai_digests.content`` check admits as it is (never cut short)."""
    return 0 < len(text) <= DIGEST_MAX_CHARS and text.strip() != ""


def ending_for(code: str) -> DigestEnd:
    """What a refused (``ModelRefused``) or failed (``ModelFailed``) call means; ``code`` is
    the client's closed code."""
    if code in _STOPPED:
        return DigestEnd("stop")
    if code in _NOT_ENABLED:
        return DigestEnd("ai_not_enabled")
    if code == "outbound_blocked":
        return DigestEnd("outbound_blocked")
    if code in _NOT_CONFIGURED:
        return DigestEnd("ai_unavailable")
    if code == "ai_budget_exhausted":
        return DigestEnd("store", "skipped_budget")
    return DigestEnd("store", "unavailable")
