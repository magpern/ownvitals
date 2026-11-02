"""Rows that were not written. Counts and reason codes only: values are never logged.

``out_of_range`` is a timestamp outside [2009-01-01, now + 3 months). Completeness calls that
``skipped (out of range)``, not missing. ``invalid_record`` is a row the tables cannot store
(a zone name, an identifier or a number that does not fit).
"""

import logging
from collections import Counter

logger = logging.getLogger(__name__)

OUT_OF_RANGE = "out_of_range"
INVALID = "invalid_record"
SKIPPED: Counter[str] = Counter()


def note(stream: str, reason: str) -> None:
    """Count one skipped row. The log line carries the stream and the reason code only."""
    SKIPPED[reason] += 1
    logger.info("sync row skipped", extra={"stream": stream, "reason": reason})
