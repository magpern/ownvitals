"""The ``input_revision`` of a stored score (#302): an unambiguous digest of the stream counters.

Pure. The three counters (``measures``, ``sleep_summary``, ``activity_daily``, in that order) were
summed before, and different counter sets can sum equal (``(2, 0, 0)`` and ``(1, 1, 0)``). The
digest is the first 63 bits of a SHA-256 over the tagged, ordered counters, so it fits the
``bigint >= 0`` column and no migration is needed: a row stored under the old sum differs from
the digest, reads as stale and is rewritten by the next pass (the nightly reconciliation).
"""

import hashlib

_TAG = b"ownvitals.scores.input_revision/1:"
_MASK = (1 << 63) - 1


def revision_digest(revisions: tuple[int, int, int]) -> int:
    """63-bit digest of ``revisions`` (measures, sleep_summary, activity_daily)."""
    body = _TAG + ",".join(str(r) for r in revisions).encode()
    return int.from_bytes(hashlib.sha256(body).digest()[:8], "big") & _MASK


__all__ = ["revision_digest"]
