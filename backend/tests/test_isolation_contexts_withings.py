"""M1 tables in every RLS execution context (PLAN section 4.4; issue #30, extends #15).

The enumeration guard of ``test_isolation_contexts_guard.py`` probes every owned table the
catalog lists in each context module (web, worker, scheduler, webhook, admin and maintenance).
This file pins that the M1 tables are among them by name, so that a Withings table left out of
``contexttools.ROWS`` (and so out of every probe) fails here with a message that says so.
Synthetic; needs no database.
"""

from contexttools import CONTEXTS, ROWS
from ownvitals.withings.tables import WITHINGS_OWNED

# Owned by the sync engine (migrations 0008 to 0010) beside the raw tables of ``tables.py``.
SYNC_OWNED = frozenset({"connections", "sync_cursors", "data_revisions", "jobs", "job_events"})
EXPECTED_CONTEXTS = {"web", "worker", "scheduler", "webhook", "admin"}


def test_every_m1_owned_table_has_a_synthetic_row_for_the_context_probes() -> None:
    names = {str(name) for name in WITHINGS_OWNED}
    assert names, "the Withings owned-table list is empty"
    missing = sorted((names | SYNC_OWNED) - set(ROWS))
    assert not missing, f"M1 owned tables the context probes never reach: {missing}"


def test_the_contexts_the_issue_names_are_all_probed() -> None:
    assert set(CONTEXTS) >= EXPECTED_CONTEXTS
