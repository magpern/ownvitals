"""Two helpers the ingest row writers share.

Split from ``ingest.rows`` (no behaviour change).
"""

from datetime import datetime
from typing import Any

from ownvitals.ingest.when import from_epoch, in_range


def optional_instant(epoch: int | None, now: datetime) -> datetime | None:
    if epoch is None:
        return None
    instant = from_epoch(epoch)
    return instant if in_range(instant, now) else None


def changed(found: Any) -> bool:
    return found is not None


def written(found: Any) -> str:
    """``inserted`` for a fresh row, ``updated`` when the upsert changed an existing one.

    The upserts return ``(xmax = 0) AS inserted``: true only for a row the statement created.
    """
    return "inserted" if found.inserted else "updated"
