"""Reconciliation drift (PLAN section 4.4): the rows a pass changed or deleted go to the
``/metrics`` counter and to one log line per stream pass, counts only. Moved out of
``reconcile.py``, which re-exports ``drift_totals``.
"""

import logging
from dataclasses import dataclass

from ownvitals.ingest.calls import Account
from ownvitals.withings.metrics import reconcile_rows, record_reconcile

# The logger keeps the name it had before this module was split out of ``reconcile.py``.
logger = logging.getLogger("ownvitals.withings.reconcile")


@dataclass(frozen=True, slots=True)
class DriftTotals:
    changed: int
    deleted: int


def drift_totals() -> DriftTotals:
    """Since the process started: the reconciliation drift counter of ``/metrics``."""
    total = reconcile_rows.get
    return DriftTotals(int(total(kind="changed")), int(total(kind="deleted")))


def record_drift(account: Account, stream: str, changed: int, deleted: int) -> None:
    record_reconcile(changed=changed, deleted=deleted)
    logger.info(
        "withings reconciliation drift",
        extra={
            "connection_id": str(account.connection_id), "stream": stream,
            "changed": changed, "deleted": deleted,
        },
    )  # fmt: skip
