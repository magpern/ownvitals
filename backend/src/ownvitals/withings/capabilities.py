"""Capability of one stream on one connection (PLAN section 4.2).

The state is learned from the grant's scopes and from the response, never from an empty table:

- no required scope, or API status 214: ``not_authorized``
- API status 2554: ``unsupported``
- a successful answer: ``supported``, including an empty one (the person has no rows yet)
- a successful ECG answer when the account has no devices and no recordings: ``not_available``
- not yet called, and the scope is present: ``unknown``

``shown_count`` is ``None`` unless the stream is ``supported``. An unsupported stream is shown
as that state, never as zero. Missing rows are not a measurement of zero.

``blocked_by_provider_plan``: how a post-13-Oct-2026 account without Withings+ is signalled is
unknown (PLAN section 4.2). No status is treated as that block. ``provider_plan_blocked`` is the
only detection hook and it returns false until a probe names a signal. Applying the connection
status then belongs to the connection writer. ``docs/status.md`` is held by another issue, so
this docstring is the record of the assumption; the Architect copies it into the status index.
"""

from typing import Final

from ownvitals.withings.errors import Permanent
from ownvitals.withings.streams import Stream

SUPPORTED: Final = "supported"
UNSUPPORTED: Final = "unsupported"
NOT_AUTHORIZED: Final = "not_authorized"
NOT_AVAILABLE: Final = "not_available"
UNKNOWN: Final = "unknown"
CAPABILITIES: Final = frozenset({SUPPORTED, UNSUPPORTED, NOT_AUTHORIZED, NOT_AVAILABLE, UNKNOWN})
SKIPPED_OUT_OF_RANGE: Final = "skipped (out of range)"


def from_scopes(stream: Stream, scopes: tuple[str, ...] | list[str]) -> str:
    """``not_authorized`` when the grant has none of the stream's scopes, else ``unknown``."""
    granted = set(scopes)
    if any(scope in granted for scope in stream.scopes):
        return UNKNOWN
    return NOT_AUTHORIZED


def from_permanent(error: Permanent) -> str | None:
    """The capability a permanent answer is, or ``None`` when it is some other failure."""
    if error.api_status == 214 or error.category == NOT_AUTHORIZED:
        return NOT_AUTHORIZED
    if error.api_status == 2554 or error.category == "not_implemented":
        return UNSUPPORTED
    return None


def after_success(*, stream: str, empty: bool, devices_known_empty: bool) -> str:
    """A status-0 answer. Empty is ``supported`` unless ECG has neither a device nor a recording."""
    if stream == "ecg" and empty and devices_known_empty:
        return NOT_AVAILABLE
    return SUPPORTED


def provider_plan_blocked(api_status: int | None) -> bool:
    """The plan-block hook. No status is a known signal, so this is false for every answer."""
    del api_status
    return False


def shown_count(capability: str, stored: int | None) -> int | None:
    """A number only for a supported stream. Every other state is shown as itself, not as 0."""
    if capability != SUPPORTED:
        return None
    return stored


def completeness(capability: str, *, backfill_complete: bool, skipped_out_of_range: int) -> str:
    """What the UI may say. An out-of-range row is skipped, not missing, and never a value."""
    parts = [capability]
    if not backfill_complete:
        parts.append("incomplete")
    if skipped_out_of_range > 0:
        parts.append(SKIPPED_OUT_OF_RANGE)
    return "; ".join(parts)
