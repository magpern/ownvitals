"""The data-state rules of the read API (``ownvitals.views.states``) without a database: the
order of precedence (connection, Withings+ block, capability, import, freshness) and the progress
toward a minimum-data requirement. Synthetic facts only.
"""

from datetime import UTC, datetime, timedelta
from fractions import Fraction

import pytest

from ownvitals.analysis.policy import Requirement, Shortfall
from ownvitals.views import states
from ownvitals.views.states import ConnectionFacts, StreamFacts, stream_state

SYNTHETIC = True
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
LIVE = ConnectionFacts("connected")


def facts(**changes: object) -> StreamFacts:
    base: dict[str, object] = {
        "stream": "measures",
        "capability": "supported",
        "backfill_status": "complete",
        "windows_total": 18,
        "last_ok_at": NOW - timedelta(hours=1),
    }
    return StreamFacts(**(base | changes))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("connection", "stream", "state", "percent"),
    [
        (None, facts(), "not_connected", None),
        (ConnectionFacts("needs_reconnect"), facts(), "needs_reconnect", None),
        (
            ConnectionFacts("connected", "blocked_by_provider_plan"),
            facts(),
            "withings_plus_needed",
            None,
        ),
        (LIVE, facts(last_error_category="blocked_by_provider_plan"), "withings_plus_needed", None),
        (LIVE, None, "importing", 0),
        (LIVE, facts(capability="not_authorized"), "not_shared", None),
        (LIVE, facts(capability="unsupported"), "not_supported", None),
        (LIVE, facts(capability="not_available"), "not_available", None),
        (
            LIVE,
            facts(backfill_status="running", windows_done=9, import_job_active=True),
            "importing",
            50,
        ),
        (LIVE, facts(backfill_status="running", windows_done=9), "incomplete", 50),
        (LIVE, facts(backfill_status=None), "importing", 0),
        (
            LIVE,
            facts(backfill_status="running", windows_done=18, import_job_active=True),
            "importing",
            99,
        ),
        (LIVE, facts(last_ok_at=NOW - timedelta(hours=49)), "stale", None),
        (LIVE, facts(last_ok_at=NOW - timedelta(hours=48)), "up_to_date", None),
        (LIVE, facts(last_ok_at=None), "up_to_date", None),
        (LIVE, facts(), "up_to_date", None),
    ],
)
def test_stream_state(
    connection: ConnectionFacts | None, stream: StreamFacts | None, state: str, percent: int | None
) -> None:
    got = stream_state(connection, stream, NOW)
    assert (got.state, got.progress_percent) == (state, percent)
    assert got.state in states.STATES


def test_a_broken_connection_wins_over_everything_else() -> None:
    broken = ConnectionFacts("needs_reconnect", "blocked_by_provider_plan")
    got = stream_state(broken, facts(capability="unsupported", backfill_status="running"), NOW)
    assert got.state == "needs_reconnect"


def test_a_blocked_capability_wins_over_the_import_and_freshness() -> None:
    stale_and_running = facts(
        capability="unsupported", backfill_status="running", last_ok_at=NOW - timedelta(days=30)
    )
    assert stream_state(LIVE, stale_and_running, NOW).state == "not_supported"


def test_progress_toward_a_requirement() -> None:
    trend = Requirement(min_days_with_data=8, min_span_fraction=Fraction(3, 5))
    got = states.progress_toward(
        trend,
        (Shortfall.DAYS_WITH_DATA, Shortfall.SPAN_FRACTION),
        n_days=4,
        span_days=27,
        window_days=90,
    )
    assert (got.days_required, got.span_required) == (8, 54)
    assert got.fraction == 0.5
    assert got.shortfalls == ("days_with_data", "span_fraction")
    nerve = Requirement(min_days_with_data=4, min_span_days=28)
    met = states.progress_toward(nerve, (), n_days=9, span_days=40, window_days=90)
    assert met.fraction == 1.0
    assert met.span_required == 28
