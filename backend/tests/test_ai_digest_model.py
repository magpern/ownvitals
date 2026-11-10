"""The weekly digest's pure rules (issue 209; ADR 0014 sections 5 and 7, ADR 0010 section 8): the
week a digest covers, the weeks a job may still write, the fixed request, the ending of a refused
or failed call, the content bound, and the vocabularies held equal to the migration's.

Synthetic dates only; no database, no model.
"""

import json
from datetime import UTC, date, datetime

import pytest

from ownvitals.ai import digest_model as m
from ownvitals.ai import requests as r
from ownvitals.ai.tables import DIGEST_STATUSES
from ownvitals.ai.tools_model import TOOLS
from ownvitals.views.catalog import NERVE_KEYS

SYNTHETIC = True


@pytest.mark.parametrize(
    ("now", "week"),
    [
        (datetime(2026, 11, 9, 0, 0, tzinfo=UTC), date(2026, 11, 2)),  # Monday, midnight
        (datetime(2026, 11, 10, 12, 0, tzinfo=UTC), date(2026, 11, 2)),  # Tuesday
        (datetime(2026, 11, 15, 23, 59, tzinfo=UTC), date(2026, 11, 2)),  # Sunday, late
        (datetime(2026, 11, 16, 0, 0, tzinfo=UTC), date(2026, 11, 9)),  # the next Monday
        (datetime(2027, 1, 1, 9, 0, tzinfo=UTC), date(2026, 12, 21)),  # across the year
    ],
)
def test_the_digest_covers_the_last_full_week(now: datetime, week: date) -> None:
    assert m.digest_week(now) == week
    assert week.weekday() == 0


def test_a_job_writes_only_that_week_or_the_three_before() -> None:
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert m.week_ok(date(2026, 11, 2), now)
    assert m.week_ok(date(2026, 10, 12), now)  # three weeks late
    assert not m.week_ok(date(2026, 10, 5), now)  # four
    assert not m.week_ok(date(2026, 11, 9), now)  # the current week is not over
    assert not m.week_ok(date(2026, 11, 3), now)  # not a Monday


def test_the_request_is_one_fixed_user_message_of_dates_and_tool_names() -> None:
    messages = m.request(date(2026, 11, 2))
    assert len(messages) == 1
    assert messages[0].role == "user"
    (block,) = messages[0].blocks
    assert isinstance(block, r.Text)
    assert "2026-11-02" in block.text
    assert "2026-11-08" in block.text
    # The nerve monthly summary (ADR 0010 section 8) and quiet copy (PLAN-operations 9.4).
    for metric in NERVE_KEYS:
        assert metric in block.text
    assert "do not urge more measurements" in block.text
    # Every tool it names exists; it holds no identifier, so the body scan admits it.
    for name in ("list_metrics", "metric_summary", "daily_values", "findings"):
        assert name in TOOLS
        assert name in block.text
    body = r.build_request("synthetic-model-1", "digest", messages)
    assert body["max_tokens"] == 2048
    assert body["system"] == r.SYSTEM_PROMPT
    raw = r.serialize(body)
    r.check_body(raw, ["synthetic-secret"])
    assert set(json.loads(raw)) == {"model", "max_tokens", "system", "tools", "messages"}


def test_the_nerve_metrics_are_the_nerve_pages() -> None:
    assert m.NERVE_METRICS == NERVE_KEYS


@pytest.mark.parametrize(
    ("code", "ending", "status"),
    [
        ("cancelled", "stop", None),
        ("lease_lost", "stop", None),
        ("ai_not_enabled", "ai_not_enabled", None),
        ("consent", "ai_not_enabled", None),
        ("outbound_blocked", "outbound_blocked", None),
        ("ai_model_unset", "ai_unavailable", None),
        ("ai_provider_review_missing", "ai_unavailable", None),
        ("ai_eval_mismatch", "ai_unavailable", None),
        ("ai_model_unpriced", "ai_unavailable", None),
        ("ai_not_configured", "ai_unavailable", None),
        ("ai_budget_exhausted", "store", "skipped_budget"),
        # A call that may have been sent (and billed): the week is stored, never tried again.
        ("server_error", "store", "unavailable"),
        ("timeout", "store", "unavailable"),
        ("rate_limited", "store", "unavailable"),
        ("bad_response", "store", "unavailable"),
        ("identifier_in_request", "store", "unavailable"),
        ("anything_else", "store", "unavailable"),
    ],
)
def test_the_ending_of_a_refused_or_failed_call(code: str, ending: str, status: str | None) -> None:
    end = m.ending_for(code)
    assert (end.ending, end.status) == (ending, status)


def test_the_content_bound_and_the_statuses() -> None:
    assert m.content_fits("Synthetic digest.")
    assert not m.content_fits("")
    assert not m.content_fits("   \n")
    assert m.content_fits("x" * 16_000)
    assert not m.content_fits("x" * 16_001)
    assert DIGEST_STATUSES == ("ready", "skipped_budget", "unavailable")


def test_the_input_is_the_week_only() -> None:
    parsed = m.DigestInput.model_validate({"week_start": "2026-11-02"})
    assert parsed.week_start == date(2026, 11, 2)
    with pytest.raises(ValueError, match="extra"):
        m.DigestInput.model_validate({"week_start": "2026-11-02", "owner_id": "x"})
