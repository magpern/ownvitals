"""``ecg_gate_overlay`` against every gate state, the A7 link and section 9's up-to-date rule
(ADR 0006 sections 9 and 11; the test specification's "The overlay, the read function and the
notification hold"). Synthetic recordings and estimates only: no real data.

The many-recording counts, the trigger-only rule and the provenance cases are in
``test_overlay_counts.py``; the read function and the API in ``tests/test_views_findings*.py``.
"""

import itertools
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from ownvitals.ecg import overlay
from ownvitals.ecg.input import NotAnalysed
from ownvitals.ecg.overlay import (
    GroupKey,
    RecordingKey,
    a7_links,
    ecg_gate_overlay,
    estimate_view,
    gate_result,
    resolve_links,
)
from ownvitals.ecg.pipeline import GATE_MODULES, MODULES, PIPELINE_VERSION, to_estimated
from ownvitals.ecg.validate_cli import ECG_TYPES, MeasureLink, Recording
from ownvitals.ecg.validate_cli import input_fingerprint as validate_fingerprint
from ownvitals.ecg.validate_rules import _matches  # pyright: ignore[reportPrivateUsage]
from ownvitals.ecg_validate import _device_id, _device_ids  # pyright: ignore[reportPrivateUsage]
from ownvitals.ingest.codec import input_fingerprint as codec_fingerprint

SYNTHETIC = True  # every recording, group and estimate here is made up
CONNECTION = UUID("00000000-0000-4000-8000-0000000000c1")
DEVICE = "synthetic_device_a"
BASE = datetime(2026, 9, 27, 7, 0, tzinfo=UTC)
FP = "f" * 64  # a row's own input fingerprint, synthetic


def estimated(
    result: str = "pass",
    *,
    fingerprint: str = FP,
    modules: Mapping[str, str] | None = None,
    algo_version: str = PIPELINE_VERSION,
    reasons: Sequence[str] = (),
) -> dict[str, Any]:
    """A synthetic ``estimated`` record of the shape ``pipeline.to_estimated`` writes."""
    record: dict[str, Any] = {
        "algo_version": algo_version,
        "modules": dict(MODULES if modules is None else modules),
        "input_fingerprint": fingerprint,
        "input_revision": 1,
        "lead": 0,
        "sqi": {
            "result": result,
            "reasons": list(reasons),
            "span_s": 25.0 if result == "pass" else None,
            "windows": [{"start_s": 0.0, "stop_s": 10.0, "passed": result == "pass"}],
        },
    }
    if result == "pass":
        record |= {
            "rpeaks": [150.0, 450.5, 750.25],
            "excluded": [],
            "hr_bpm": 60.0,
            "n_rr": 2,
            "hrv": {"rmssd_ms": 30.0, "sdnn_ms": 40.0},
            "hrv_reasons": [],
        }
    return record


def not_run(reason: str) -> dict[str, Any]:
    """``unsupported_sampling_rate`` or ``signal_missing``, as the pipeline writes them."""
    return to_estimated(NotAnalysed(reason=reason), input_fingerprint=FP, input_revision=1)  # pyright: ignore[reportArgumentType]


def recording(
    est: Mapping[str, Any] | None,
    *,
    minutes: int = 0,
    device: str | None = DEVICE,
    model: int | None = 91,
) -> RecordingKey:
    return RecordingKey(
        id=uuid4(),
        connection=CONNECTION,
        recorded_at=BASE + timedelta(minutes=minutes),
        device_id=device,
        model_id=model,
        fingerprint=FP,
        estimated=est,
    )


def group(
    grpid: int,
    *,
    minutes: int = 0,
    devices: frozenset[str] = frozenset({DEVICE}),
    types: frozenset[int] = frozenset({135, 136, 138}),
    model: int | None = 91,
) -> GroupKey:
    return GroupKey(
        grpid=grpid,
        connection=CONNECTION,
        measured_at=BASE + timedelta(minutes=minutes),
        device_ids=devices,
        model_id=model,
        types=types,
    )


def linked(*recordings: RecordingKey | None) -> dict[int, tuple[RecordingKey, ...]]:
    """One trigger group per argument; ``None`` is an unlinked trigger group."""
    return {i: (() if r is None else (r,)) for i, r in enumerate(recordings, start=1)}


def other_modules(name: str) -> dict[str, str]:
    changed = dict(MODULES)
    changed[name] = "999.0.0"
    return changed


PASS, FAIL = estimated("pass"), estimated("fail", reasons=["low_bsqi"])
# Every state with no gate result, by name.
NO_RESULT: dict[str, dict[str, Any] | None] = {
    "no_estimated": None,
    "other_fingerprint": estimated("fail", fingerprint="0" * 64),
    **{f"module_{m}": estimated("fail", modules=other_modules(m)) for m in GATE_MODULES},
    "unsupported_sampling_rate": not_run("unsupported_sampling_rate"),
    "signal_missing": not_run("signal_missing"),
}


# --- gate states -------------------------------------------------------------------------------


@pytest.mark.parametrize("rule", ["E4", "E6"])
def test_gated_rules_hide_on_a_failure_and_show_on_a_pass(rule: str) -> None:
    failed = ecg_gate_overlay(rule, linked(recording(FAIL)))
    assert (failed.hidden, failed.outcome, failed.counted, failed.failed) == (True, "failed", 1, 1)
    passed = ecg_gate_overlay(rule, linked(recording(PASS)))
    assert (passed.hidden, passed.outcome, passed.counted) == (False, "passed", 1)


@pytest.mark.parametrize("rule", ["E1", "E2", "E3", "E4", "E6"])
@pytest.mark.parametrize("state", sorted(NO_RESULT))
def test_every_no_result_state_is_not_evaluated(rule: str, state: str) -> None:
    result = ecg_gate_overlay(rule, linked(recording(NO_RESULT[state])))
    assert (result.hidden, result.outcome, result.counted) == (False, "not_evaluated", 0)


@pytest.mark.parametrize("rule", ["E1", "E2", "E3", "E4", "E6"])
def test_no_linked_recording_is_not_evaluated(rule: str) -> None:
    for links in ({1: ()}, {}):
        result = ecg_gate_overlay(rule, links)
        assert (result.hidden, result.outcome) == (False, "not_evaluated")


@pytest.mark.parametrize("rule", ["E1", "E2", "E3"])
def test_device_rules_are_never_hidden(rule: str) -> None:
    for links in (linked(recording(FAIL)), linked(recording(FAIL), recording(FAIL))):
        result = ecg_gate_overlay(rule, links)
        assert result.hidden is False
        assert result.outcome == "failed"  # reported, shown only behind the switch


@pytest.mark.parametrize("rule", ["C1", "C1x", "B2", "N3", "Q1", "E5", ""])
def test_the_overlay_does_not_apply_to_other_rules(rule: str) -> None:
    result = ecg_gate_overlay(rule, linked(recording(FAIL)))
    assert result == overlay.NOT_APPLICABLE


def test_an_hrv_only_difference_keeps_the_gate_result() -> None:
    hrv_bump = estimated("fail", modules=other_modules("hrv"), algo_version="ecg-99.0.0")
    assert gate_result(hrv_bump, FP) == "fail"
    assert ecg_gate_overlay("E4", linked(recording(hrv_bump))).hidden is True
    for name in GATE_MODULES:
        assert gate_result(estimated("fail", modules=other_modules(name)), FP) is None


def test_a_malformed_estimated_has_no_gate_result() -> None:
    for broken in ({}, {"modules": "x"}, {"input_fingerprint": FP}, {**PASS, "sqi": "pass"}):
        assert gate_result(broken, FP) is None


# --- the A7 link -------------------------------------------------------------------------------


def test_a7_link_rules() -> None:
    strip = recording(PASS)
    assert a7_links(strip, group(1))
    assert not a7_links(strip, group(1, minutes=1))  # another timestamp
    assert not a7_links(strip, group(1, types=frozenset({11})))  # no ECG type
    assert not a7_links(strip, group(1, devices=frozenset({"synthetic_device_b"})))
    assert not a7_links(recording(PASS, device=None), group(1))  # no device: no match
    assert a7_links(strip, group(1, devices=frozenset()))  # same model, no device ids
    assert not a7_links(strip, group(1, devices=frozenset(), model=92))
    other = RecordingKey(uuid4(), uuid4(), BASE, DEVICE, 91, FP, PASS)
    assert not a7_links(other, group(1))  # another connection


def test_a7_matches_the_validators_rule() -> None:
    """One link rule: the overlay's and ``ecg validate``'s OC5 agree on every combination."""
    assert overlay.ECG_TYPES == ECG_TYPES
    for device, model, devices, gmodel, types, minutes in itertools.product(
        (DEVICE, None, "synthetic_device_b"),
        (91, None),
        (frozenset({DEVICE}), frozenset[str](), frozenset({"synthetic_device_b"})),
        (91, 92, None),
        (frozenset({135}), frozenset({130}), frozenset({11})),
        (0, 1),
    ):
        mine = a7_links(
            recording(PASS, device=device, model=model),
            group(1, minutes=minutes, devices=devices, types=types, model=gmodel),
        )
        theirs = _matches(
            Recording(
                leads=None,
                sampling_hz=300,
                heart_rate=None,
                afib_details=None,
                wearposition=None,
                estimated=None,
                payload_hash=None,
                signalid=1,
                recorded_at=BASE,
                connection=CONNECTION,
                model_id=model,
                device_id=device,
            ),
            MeasureLink(
                measured_at=BASE + timedelta(minutes=minutes),
                connection=CONNECTION,
                device_ids=devices,
                model_id=gmodel,
                types=types,
            ),
        )
        assert mine == theirs


def test_device_ids_are_read_as_the_validator_reads_them() -> None:
    reported: list[object] = [
        None,
        {},
        {"deviceid": DEVICE},
        {"deviceid": ""},
        {"raw": {"deviceid": DEVICE}},
        {"deviceid": 5, "raw": {"deviceid": "x"}},
        "text",
    ]
    for value in reported:
        assert overlay.recording_device_id(value) == _device_id(value)
    raws: list[object] = [None, {}, {"deviceid": "a"}, {"hash_deviceid": "b", "deviceid": ""}]
    for hash_id, raw in itertools.product((None, "", "h"), raws):
        assert overlay.group_device_ids(hash_id, raw) == _device_ids(hash_id, raw)


def test_fingerprints_agree() -> None:
    for digest, rate in ((None, None), (b"\x01" * 32, 300), (b"\x02" * 32, None)):
        assert validate_fingerprint(7, digest, rate) == codec_fingerprint(7, digest, rate)


def test_resolve_links_counts_each_recording_once_and_keeps_unlinked_groups() -> None:
    strip = recording(PASS)
    links = resolve_links([1, 2, 1], [group(1), group(1), group(3)], [strip])
    assert links == {1: (strip,), 2: ()}


def test_a_recording_linked_to_two_trigger_groups_counts_once() -> None:
    strip = recording(FAIL)
    result = ecg_gate_overlay("E6", {1: (strip,), 2: (strip,), 3: (recording(PASS),)})
    assert (result.counted, result.failed, result.hidden) == (2, 1, True)


def test_reprs_name_no_value() -> None:
    assert repr(recording(PASS)) == "RecordingKey redacted"
    assert repr(group(5)) == "GroupKey redacted"
    assert "60" not in repr(estimate_view(PASS, FP))


# --- section 9: the up-to-date rule ------------------------------------------------------------


def test_a_current_pass_shows_everything() -> None:
    view = estimate_view(PASS, FP)
    assert view.gate == "pass"
    assert view.hr_bpm == 60.0
    assert view.hrv == {"rmssd_ms": 30.0, "sdnn_ms": 40.0}
    assert view.rpeaks == (150.0, 450.5, 750.25)
    assert len(view.windows) == 1


def test_an_hrv_only_difference_drops_hr_and_hrv_but_keeps_markers_and_sqi() -> None:
    stale = estimated("pass", modules=other_modules("hrv"), algo_version="ecg-99.0.0")
    view = estimate_view(stale, FP)
    assert (view.hr_bpm, view.hrv) == (None, None)
    assert view.gate == "pass"
    assert view.rpeaks == (150.0, 450.5, 750.25)
    assert len(view.windows) == 1


def test_another_fingerprint_or_gate_module_leaves_everything_out() -> None:
    absent = [estimated("pass", fingerprint="0" * 64)]
    absent += [estimated("pass", modules=other_modules(m)) for m in GATE_MODULES]
    for est in [*absent, None]:
        assert estimate_view(est, FP) == overlay.EMPTY_ESTIMATE


def test_a_failure_keeps_its_reasons_and_has_no_beats() -> None:
    view = estimate_view(FAIL, FP)
    assert view.gate == "fail"
    assert view.reasons == ("low_bsqi",)
    assert (view.rpeaks, view.hr_bpm, view.hrv) == (None, None, None)


def test_a_not_run_estimate_has_no_gate_result_and_no_values() -> None:
    view = estimate_view(not_run("signal_missing"), FP)
    assert view.gate is None
    assert view.reasons == ("signal_missing",)
    assert (view.rpeaks, view.hr_bpm, view.hrv) == (None, None, None)


def test_a_boolean_heart_rate_is_not_a_number() -> None:
    assert estimate_view({**PASS, "hr_bpm": True}, FP).hr_bpm is None
