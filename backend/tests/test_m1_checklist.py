"""The M1 definition of done (PLAN section 4.4, issue #30): every checklist item names tests.

Each item maps to ``file::test`` ids. This test fails when a named test no longer exists (renamed,
deleted or turned into a helper), so the mapping cannot rot silently; CI runs the tests themselves.
No database, no network. Synthetic.
"""

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).parent

CHECKLIST: dict[str, tuple[str, ...]] = {
    "flow: connect, backfill with progress, webhook sync, poll, reconcile, disconnect, delete": (
        "test_m1_flow.py::test_connect_backfill_webhook_poll_reconcile_disconnect_and_delete",
        "test_worker_withings_e2e.py::test_a_webhook_leads_to_a_sync_that_ingests_the_measures",
    ),
    "failure: token-rotation crashes (4.3)": (
        "test_withings_tokens.py::test_a_crash_after_the_response_recovers_once",
        "test_withings_tokens.py::test_a_crash_after_commit_keeps_the_new_generation",
        "test_withings_tokens.py::test_two_workers_perform_one_refresh",
        "test_withings_tokens.py::test_an_older_generation_cannot_overwrite",
    ),
    "failure: 601 storms": (
        "test_withings_rate.py::test_a_601_storm_pauses_and_then_resumes",
        "test_withings_rate.py::test_a_provider_wait_cannot_be_infinite_or_negative",
    ),
    "failure: duplicate webhooks": (
        "test_withings_webhook.py::test_a_crash_after_commit_leaves_one_event_and_redelivery_collapses",
        "test_jobs_withings.py::test_duplicate_singleton_enqueues_collapse_and_debounce",
    ),
    "failure: out-of-order webhooks": (
        "test_m1_failures.py::test_out_of_order_webhooks_are_each_durable_and_collapse_into_one_sync",
    ),
    "failure: forged, oversized and malformed webhooks": (
        "test_withings_webhook.py::test_bad_notifications_create_no_job_and_do_not_log_the_secret",
    ),
    "failure: worker crash mid-backfill (lease expiry, resume)": (
        "test_withings_sync.py::test_backfill_resumes_after_a_lost_lease_and_a_rerun_ingests_nothing",
    ),
    "failure: crashed calls keep tokens consumed and free slots": (
        "test_withings_rate.py::test_a_crashed_call_keeps_its_token_and_frees_its_slot_at_expiry",
        "test_withings_rate.py::test_the_app_role_cannot_refund_a_token",
    ),
    "capability states drive behaviour; a missing stream is never zero": (
        "test_withings_sync.py::test_an_unsupported_stream_is_not_stored_as_zero",
        "test_withings_sync.py::test_a_missing_scope_is_not_authorized_and_makes_no_call",
    ),
    "RLS on every new table in web, worker, scheduler, webhook and admin contexts": (
        "test_isolation_contexts_withings.py::test_every_m1_owned_table_has_a_synthetic_row_for_the_context_probes",
        "test_isolation_contexts_guard.py::test_every_owned_table_has_a_synthetic_row",
        "test_isolation_contexts_guard.py::test_every_context_has_a_module_that_probes_the_catalog",
        "test_isolation_contexts_web.py::test_a_web_request_reaches_only_its_users_rows",
        "test_isolation_contexts_worker.py::test_a_worker_job_reaches_only_its_owners_rows",
        "test_isolation_contexts_scheduler.py::test_the_schedulers_session_reaches_no_owned_row",
        "test_isolation_contexts_webhook.py::test_the_receivers_session_reaches_no_owned_row",
        "test_isolation_contexts_admin.py::test_an_admin_route_reads_no_owner_or_health_column_and_writes_no_owned_row",
    ),
    "metrics exported for every listed item": (
        "test_metrics.py::test_webhook_age_moves",
        "test_metrics.py::test_last_sync_and_completeness_move",
        "test_metrics.py::test_subscriptions_reconcile_queue_refresh_api_move",
    ),
}


def _tests_in(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    }


def _ids() -> list[tuple[str, str]]:
    return [(item, ref) for item, refs in CHECKLIST.items() for ref in refs]


@pytest.mark.parametrize(("item", "ref"), _ids(), ids=[ref for _, ref in _ids()])
def test_a_named_test_exists(item: str, ref: str) -> None:
    file, _, name = ref.partition("::")
    path = TESTS / file
    assert path.is_file(), f"{item}: {file} is missing"
    assert name in _tests_in(path), f"{item}: {file} has no test {name}"


def test_every_checklist_item_names_at_least_one_test() -> None:
    assert all(refs for refs in CHECKLIST.values())
