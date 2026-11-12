"""The session anomaly policy (#461, ADR 0017): each signal on its own, with the clock and every
input passed in. Pure: no database, no request. Synthetic addresses (documentation ranges) and
agent strings only."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ownvitals.auth import session_anomaly_policy as policy
from ownvitals.auth.session_anomaly_policy import History, Request, assess

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
FIREFOX_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
FIREFOX_WINDOWS_NEWER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:141.0) Gecko/20100101 Firefox/141.0"
)
CURL = "curl/8.9.1"
CHROME_LINUX = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0"
HISTORY = History(
    ip="198.51.100.7",
    user_agent=FIREFOX_WINDOWS,
    country=None,
    window_at=None,
    window_count=0,
    flagged=False,
)
SAME = Request(ip="198.51.100.7", user_agent=FIREFOX_WINDOWS, country=None, now=NOW)


def test_an_ordinary_request_has_no_signal() -> None:
    verdict = assess(HISTORY, SAME)
    assert (verdict.signals, verdict.record) == ((), False)
    assert (verdict.window_at, verdict.window_count) == (NOW, 1)


# --- new_ip_and_agent ---------------------------------------------------------------------------


def test_a_new_network_and_another_agent_family_is_a_signal() -> None:
    seen = replace(SAME, ip="203.0.113.9", user_agent=CHROME_LINUX)
    verdict = assess(HISTORY, seen)
    assert verdict.signals == (policy.NEW_IP_AND_AGENT,)
    assert verdict.record


@pytest.mark.parametrize(
    "seen",
    [
        pytest.param(replace(SAME, ip="203.0.113.9"), id="new-network-same-agent"),
        pytest.param(replace(SAME, user_agent=CURL), id="same-address-new-agent"),
        pytest.param(
            replace(SAME, ip="203.0.113.9", user_agent=FIREFOX_WINDOWS_NEWER), id="browser-update"
        ),
        pytest.param(replace(SAME, ip=None, user_agent=CURL), id="unknown-address"),
        pytest.param(replace(SAME, ip="203.0.113.9", user_agent=None), id="unknown-agent"),
    ],
)
def test_one_change_alone_or_an_unknown_value_is_no_signal(seen: Request) -> None:
    assert assess(HISTORY, seen).signals == ()


def test_the_same_ipv6_64_is_the_same_network() -> None:
    history = replace(HISTORY, ip="2001:db8:1:2::10")
    near = replace(SAME, ip="2001:db8:1:2:ffff::1", user_agent=CURL)
    far = replace(SAME, ip="2001:db8:1:3::1", user_agent=CURL)
    assert assess(history, near).signals == ()
    assert assess(history, far).signals == (policy.NEW_IP_AND_AGENT,)


def test_a_session_without_a_stored_address_or_agent_never_signals_it() -> None:
    seen = replace(SAME, ip="203.0.113.9", user_agent=CURL)
    assert assess(replace(HISTORY, ip=None), seen).signals == ()
    assert assess(replace(HISTORY, user_agent=None), seen).signals == ()


def test_the_agent_is_compared_as_the_session_stores_it() -> None:
    """Only the first 200 characters are stored: a marker past them is not looked at either."""
    padded = "x" * policy.AGENT_LIMIT + " Firefox/140.0 (Windows)"
    assert policy.agent_family(padded) == ("other", "other")


# --- request_rate -------------------------------------------------------------------------------


def test_the_rate_window_counts_up_and_starts_again_after_a_minute() -> None:
    history = replace(HISTORY, window_at=NOW, window_count=7)
    later = NOW + timedelta(seconds=59)
    assert policy.next_window(history, later) == (NOW, 8)
    fresh = NOW + policy.RATE_WINDOW
    assert policy.next_window(history, fresh) == (fresh, 1)
    earlier = NOW - timedelta(seconds=1)  # the clock went back: a new window
    assert policy.next_window(history, earlier) == (earlier, 1)


def test_more_requests_than_the_limit_in_one_window_is_a_signal() -> None:
    at_limit = replace(HISTORY, window_at=NOW, window_count=policy.RATE_LIMIT - 1)
    assert assess(at_limit, replace(SAME, now=NOW + timedelta(seconds=30))).signals == ()
    over = replace(HISTORY, window_at=NOW, window_count=policy.RATE_LIMIT)
    verdict = assess(over, replace(SAME, now=NOW + timedelta(seconds=30)))
    assert verdict.signals == (policy.REQUEST_RATE,)
    assert verdict.window_count == policy.RATE_LIMIT + 1


def test_a_busy_minute_that_ended_is_no_signal() -> None:
    over = replace(HISTORY, window_at=NOW, window_count=policy.RATE_LIMIT * 5)
    verdict = assess(over, replace(SAME, now=NOW + policy.RATE_WINDOW))
    assert (verdict.signals, verdict.window_count) == ((), 1)


# --- country_change -----------------------------------------------------------------------------


def test_the_first_country_becomes_the_sessions_own() -> None:
    verdict = assess(HISTORY, replace(SAME, country="SE"))
    assert (verdict.signals, verdict.country) == ((), "SE")


def test_another_country_is_a_signal_and_the_first_one_stays() -> None:
    verdict = assess(replace(HISTORY, country="SE"), replace(SAME, country="BR"))
    assert verdict.signals == (policy.COUNTRY_CHANGE,)
    assert verdict.country == "SE"


@pytest.mark.parametrize(
    ("before", "now"), [("SE", None), ("SE", "XX"), (None, "BR"), ("XX", "BR")]
)
def test_an_unknown_country_is_never_a_change(before: str | None, now: str | None) -> None:
    assert assess(replace(HISTORY, country=before), replace(SAME, country=now)).signals == ()


# --- recording ----------------------------------------------------------------------------------


def test_a_flagged_session_is_not_recorded_again() -> None:
    seen = replace(SAME, ip="203.0.113.9", user_agent=CURL, country="BR")
    history = replace(HISTORY, country="SE", window_at=NOW, window_count=policy.RATE_LIMIT)
    first = assess(history, seen)
    assert first.signals == policy.SIGNALS  # every signal, in their fixed order
    assert first.record
    again = assess(replace(history, flagged=True), seen)
    assert again.signals == policy.SIGNALS
    assert not again.record
