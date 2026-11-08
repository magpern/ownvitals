"""ECG frame, NULL-signal fingerprint and how a stream is shown. Synthetic samples only."""

from ownvitals.ingest.codec import (
    decode_signal,
    estimate_current,
    frame_signal,
    hold_pending,
    input_fingerprint,
    missing_estimate,
)
from ownvitals.jobs import KINDS, KindRegistry
from ownvitals.withings.capabilities import (
    NOT_AUTHORIZED,
    UNSUPPORTED,
    completeness,
    from_scopes,
    provider_plan_blocked,
    shown_count,
)
from ownvitals.withings.streams import stream_named
from ownvitals.withings.sync_kinds import register_sync_kinds

SIGNAL = [12, -40, 0, 300, -1]


def test_a_synthetic_signal_round_trips_and_its_hash_is_stable() -> None:
    frame, digest = frame_signal([SIGNAL])
    again, digest_again = frame_signal([list(SIGNAL)])
    assert decode_signal(frame) == [SIGNAL]
    assert decode_signal(again) == [SIGNAL]
    assert digest == digest_again
    assert len(digest) == 32


def test_a_null_signal_estimate_is_current_and_its_hold_ends_at_once() -> None:
    fingerprint = input_fingerprint(7, None, 300)
    estimated = missing_estimate(7, 300, revision=1)
    assert estimated["sqi"]["reasons"] == ["signal_missing"]
    assert estimate_current(estimated, signalid=7, digest=None, sampling_hz=300)
    assert estimated["input_fingerprint"] == fingerprint
    assert not hold_pending(estimates_current=True, another_group_may_link=False, elapsed_seconds=0)
    assert hold_pending(estimates_current=False, another_group_may_link=False, elapsed_seconds=0)


def test_capability_is_not_shown_as_zero_and_the_plan_signal_is_unknown() -> None:
    measures = stream_named("measures")
    assert from_scopes(measures, ("user.info",)) == NOT_AUTHORIZED
    assert from_scopes(measures, ("user.metrics",)) == "unknown"
    assert shown_count(UNSUPPORTED, 0) is None
    assert shown_count(NOT_AUTHORIZED, 0) is None
    assert shown_count("supported", 3) == 3
    text = completeness(UNSUPPORTED, backfill_complete=True, skipped_out_of_range=2)
    assert "skipped (out of range)" in text
    assert "missing" not in text
    assert " 0" not in text
    assert provider_plan_blocked(None) is False
    assert provider_plan_blocked(214) is False
    assert provider_plan_blocked(2554) is False


def test_only_this_issues_kinds_register_and_the_shipped_registry_stays_empty() -> None:
    assert KINDS.names() == []
    local = KindRegistry()
    register_sync_kinds(local)
    assert local.names() == [
        "ecg_fetch",
        "withings_backfill",
        "withings_connect",
        "withings_poll",
        "withings_sync",
    ]
    assert KINDS.names() == []
