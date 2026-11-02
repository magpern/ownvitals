"""Synthetic checks for the owner ECG report (ADR 0006 §14). No real recording is used."""

import argparse
from datetime import UTC, datetime
from uuid import UUID

import numpy as np

from ecgsynth import make_strip
from ownvitals.cli import COMMANDS, add_subcommands
from ownvitals.ecg.pipeline import analyse, to_estimated
from ownvitals.ecg.validate_cli import (
    MeasureLink,
    OwnerAnswers,
    Recording,
    answers_from,
    evaluate,
    input_fingerprint,
)
from ownvitals.ingest.codec import input_fingerprint as codec_fingerprint

WHEN = datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
CONN = UUID("12345678-90ab-4cde-8fab-1234567890ab")


def _rec(**kwargs: object) -> Recording:
    values: dict[str, object] = {
        "leads": None,
        "sampling_hz": 300,
        "heart_rate": None,
        "afib_details": 0,
        "wearposition": None,
        "estimated": None,
        "payload_hash": None,
        "signalid": 1,
        "recorded_at": WHEN,
        "connection": CONN,
        "model_id": None,
        "device_id": None,
    }
    values.update(kwargs)
    return Recording(**values)  # type: ignore[arg-type]


def _gate(
    result: str, hr: float | None = None, reasons: list[str] | None = None
) -> dict[str, object]:
    estimated: dict[str, object] = {"sqi": {"result": result, "reasons": reasons or []}}
    if hr is not None:
        estimated["hr_bpm"] = hr
    if reasons and result == "pass":
        estimated["hrv_reasons"] = reasons
    return estimated


def _passed(hr: float, heart: int, reasons: list[str] | None = None) -> Recording:
    return _rec(heart_rate=heart, estimated=_gate("pass", hr, reasons))


def _sine(*parts: tuple[float, int], fs: int = 300, seconds: int = 30) -> tuple[int, ...]:
    """SYNTHETIC. ``parts`` are (frequency Hz, amplitude µV)."""
    count = fs * seconds
    ticks = np.arange(count) / fs
    wave = np.zeros(count, dtype=np.float64)
    for frequency, amplitude in parts:
        wave += amplitude * np.sin(2.0 * np.pi * frequency * ticks)
    return tuple(int(sample) for sample in np.rint(wave))


def test_the_fingerprint_matches_ingest() -> None:
    assert input_fingerprint(7, None, None) == codec_fingerprint(7, None, None)
    assert input_fingerprint(7, b"ab", 300) == codec_fingerprint(7, b"ab", 300)


def test_an_empty_account_does_not_fail() -> None:
    report = evaluate((), (), OwnerAnswers())
    assert report.failed is False
    assert "recordings 0" in report.lines
    assert "OC5 not-applicable" in report.lines
    assert "OC5 late-link not-applicable" in report.lines
    assert "V1 not-applicable" in report.lines
    assert "V5 not-applicable" in report.lines


def test_oc1_passes_only_when_the_scale_and_the_owner_agree() -> None:
    plausible = ((1000, -800, 200),)
    tiny = ((1, -2, 3),)
    assert "OC1 pass" in evaluate((_rec(leads=plausible),), (), OwnerAnswers(oc1="yes")).lines
    refused = evaluate((_rec(leads=plausible),), (), OwnerAnswers(oc1="no"))
    assert "OC1 scale pass" in refused.lines
    assert "OC1 fail" in refused.lines
    scaled = evaluate((_rec(leads=tiny),), (), OwnerAnswers(oc1="yes"))
    assert "OC1 scale fail" in scaled.lines
    assert "OC1 fail" in scaled.lines


def test_oc2_is_the_owners_morphology_answer() -> None:
    lead = _rec(wearposition=4)
    blank = _rec()
    assert "OC2 pass" in evaluate((lead,), (), OwnerAnswers(oc2="yes")).lines
    assert "OC2 positions 1" in evaluate((lead, lead), (), OwnerAnswers()).lines
    assert "OC2 fail" in evaluate((lead,), (), OwnerAnswers(oc2="no")).lines
    assert "OC2 fail" in evaluate((blank,), (), OwnerAnswers(oc2="yes")).lines


def test_oc3_lowpass_and_mains_are_yes_no() -> None:
    quiet = _rec(leads=(_sine((10.0, 1000)),))
    bright = _rec(leads=(_sine((10.0, 1000), (80.0, 1000)),))
    mains = _rec(leads=(_sine((10.0, 1000), (50.0, 800)),))
    assert "OC3 lowpass pass" in evaluate((quiet,), (), OwnerAnswers()).lines
    assert "OC3 mains no" in evaluate((quiet,), (), OwnerAnswers()).lines
    assert "OC3 lowpass fail" in evaluate((bright,), (), OwnerAnswers()).lines
    assert "OC3 mains yes" in evaluate((mains,), (), OwnerAnswers()).lines
    assert (
        "OC3 lowpass not-applicable"
        in evaluate((_rec(leads=((1, 2, 3),)),), (), OwnerAnswers()).lines
    )


def test_oc4_requires_simultaneous_leads_when_there_are_two() -> None:
    strip = make_strip(1, 60.0, 300)
    lead = tuple(int(sample) for sample in strip.signal_uv)
    same = evaluate((_rec(leads=(lead, lead)),), (), OwnerAnswers())
    assert "OC4 pass" in same.lines
    assert "OC4 coincident 100%" in same.lines
    shifted = lead[80:] + lead[:80]
    assert "OC4 fail" in evaluate((_rec(leads=(lead, shifted)),), (), OwnerAnswers()).lines
    short = lead[:-10]
    assert "OC4 fail" in evaluate((_rec(leads=(lead, short)),), (), OwnerAnswers()).lines
    assert "OC4 not-applicable" in evaluate((_rec(leads=(lead,)),), (), OwnerAnswers()).lines


def test_oc5_counts_recordings_without_a_matching_group() -> None:
    link = MeasureLink(WHEN, CONN, frozenset({"device-a"}), 10, frozenset({135, 130}))
    matched = _rec(device_id="device-a", model_id=10)
    assert "OC5 pass" in evaluate((matched,), (link,), OwnerAnswers()).lines
    assert "OC5 without 0" in evaluate((matched,), (link,), OwnerAnswers()).lines
    other_device = _rec(device_id="device-b", model_id=10)
    missing = evaluate((other_device,), (link,), OwnerAnswers())
    assert "OC5 fail" in missing.lines
    assert "OC5 without 1" in missing.lines
    weight_only = MeasureLink(WHEN, CONN, frozenset({"device-a"}), 10, frozenset({1}))
    assert "OC5 fail" in evaluate((matched,), (weight_only,), OwnerAnswers()).lines
    by_model = MeasureLink(WHEN, CONN, frozenset(), 10, frozenset({136}))
    assert (
        "OC5 pass"
        in evaluate((_rec(device_id="device-a", model_id=10),), (by_model,), OwnerAnswers()).lines
    )
    wrong_time = MeasureLink(
        datetime(2024, 1, 3, tzinfo=UTC), CONN, frozenset({"device-a"}), 10, frozenset({137})
    )
    assert "OC5 fail" in evaluate((matched,), (wrong_time,), OwnerAnswers()).lines
    assert "device-a" not in "\n".join(missing.lines)


def test_oc5_a_missing_device_id_is_not_a_match() -> None:
    unnamed = _rec(device_id=None, model_id=10)
    other_device = MeasureLink(WHEN, CONN, frozenset({"device-a"}), 10, frozenset({135}))
    missing = evaluate((unnamed,), (other_device,), OwnerAnswers())
    assert "OC5 fail" in missing.lines
    assert "OC5 without 1" in missing.lines
    other_model = MeasureLink(WHEN, CONN, frozenset(), 11, frozenset({130}))
    assert "OC5 fail" in evaluate((unnamed,), (other_model,), OwnerAnswers()).lines
    same_model = MeasureLink(WHEN, CONN, frozenset(), 10, frozenset({136}))
    assert "OC5 fail" in evaluate((unnamed,), (same_model,), OwnerAnswers()).lines
    named = _rec(device_id="device-b", model_id=10)
    assert "OC5 fail" in evaluate((named,), (other_device,), OwnerAnswers()).lines


def test_v1_uses_the_eighty_percent_floor_and_ignores_noisy_codes() -> None:
    clean_pass = tuple(_rec(signalid=index, estimated=_gate("pass")) for index in range(4))
    clean_fail = (_rec(signalid=4, estimated=_gate("fail")),)
    noisy = (_rec(signalid=5, afib_details=-1, estimated=_gate("fail")),)
    passed = evaluate((*clean_pass, *clean_fail, *noisy), (), OwnerAnswers())
    assert "V1 pass" in passed.lines
    assert "V1 eligible 5" in passed.lines
    assert "V1 rate 80%" in passed.lines
    failed = evaluate(
        tuple(
            _rec(signalid=index, estimated=_gate("pass" if index < 3 else "fail"))
            for index in range(5)
        ),
        (),
        OwnerAnswers(),
    )
    assert "V1 fail" in failed.lines
    assert "V1 rate 60%" in failed.lines


def test_v2_reports_agreement_reasons_and_bands_without_raw_codes() -> None:
    clean = _rec(estimated=_gate("pass", 60.0, ["too_many_excluded"]))
    noisy = _rec(
        afib_details=2,
        heart_rate=40,
        estimated=_gate("fail", reasons=["clipping", "canary-reason-zz"]),
    )
    slow = _rec(heart_rate=42, estimated=_gate("fail", reasons=["flat_line"]))
    report = evaluate((clean, noisy, slow), (), OwnerAnswers())
    text = "\n".join(report.lines)
    assert "V2 withings-clean our-pass 1" in report.lines
    assert "V2 withings-noise our-fail 1" in report.lines
    assert "V2 withings-clean our-fail 1" in report.lines
    assert "V2 hrv-reason too_many_excluded 1 100%" in report.lines
    assert "V2 fail-band under-50 clipping 1" in report.lines
    assert "V2 fail-band under-50 flat_line 1" in report.lines
    assert "V2 fail-band under-50 other 1" in report.lines
    assert "canary-reason-zz" not in text
    assert "40" not in text
    assert "42" not in text


def test_v3_compares_heart_rates_and_skips_an_irregular_rhythm() -> None:
    close = tuple(_passed(61.0, 60) for _ in range(19))
    wide = _passed(80.0, 60)
    irregular = _rec(
        heart_rate=200,
        estimated={
            "sqi": {"result": "pass", "reasons": []},
            "hr_bpm": 40.0,
            "hrv_reasons": ["irregular_rhythm"],
        },
    )
    report = evaluate((*close, wide, irregular), (), OwnerAnswers())
    assert "V3 pass" in report.lines
    assert "V3 pairs 20" in report.lines
    assert "V3 within-5 95%" in report.lines
    assert "V3 bias-sign positive" in report.lines
    missed = evaluate(
        tuple(_passed(70.0, 60) for _ in range(2)) + tuple(_passed(60.0, 60) for _ in range(18)),
        (),
        OwnerAnswers(),
    )
    assert "V3 fail" in missed.lines
    assert "V3 within-5 90%" in missed.lines
    assert "200" not in "\n".join(report.lines)
    assert "80" not in "\n".join(report.lines)


def test_v4_needs_enough_strips_and_the_owners_counts() -> None:
    passed = tuple(_rec(signalid=index, estimated=_gate("pass")) for index in range(20))
    rejected = tuple(_rec(signalid=100 + index, estimated=_gate("fail")) for index in range(10))
    rows = (*passed, *rejected)
    pending = evaluate(rows, (), OwnerAnswers())
    assert "V4 pending" in pending.lines
    assert "V4 passed-strips 20" in pending.lines
    assert "V4 rejected-strips 10" in pending.lines
    good = OwnerAnswers(v4_wrong_beats=0, v4_inspected_beats=100, v4_justified=8)
    assert "V4 pass" in evaluate(rows, (), good).lines
    beats = OwnerAnswers(v4_wrong_beats=2, v4_inspected_beats=100, v4_justified=8)
    assert "V4 fail" in evaluate(rows, (), beats).lines
    justified = OwnerAnswers(v4_wrong_beats=0, v4_inspected_beats=100, v4_justified=7)
    assert "V4 fail" in evaluate(rows, (), justified).lines
    short = OwnerAnswers(v4_wrong_beats=0, v4_inspected_beats=100, v4_justified=8)
    assert "V4 fail" in evaluate(passed[:19] + rejected, (), short).lines


def test_v5_is_a_byte_for_byte_recompute() -> None:
    samples = tuple(range(-30, 30))
    digest = b"\x11" * 32
    signalid = 8675309
    fingerprint = input_fingerprint(signalid, digest, 300)
    result = analyse((samples,), 300)
    stored = to_estimated(result, input_fingerprint=fingerprint, input_revision=4)
    recording = _rec(leads=(samples,), payload_hash=digest, signalid=signalid, estimated=stored)
    assert "V5 pass" in evaluate((recording,), (), OwnerAnswers()).lines
    changed = dict(stored)
    changed["sqi"] = {**stored["sqi"], "result": "pass"}
    forged = _rec(leads=(samples,), payload_hash=digest, signalid=signalid, estimated=changed)
    failed = evaluate((forged,), (), OwnerAnswers())
    assert "V5 fail" in failed.lines
    assert "V5 mismatch 1" in failed.lines
    assert "8675309" not in "\n".join(failed.lines)


def test_stdout_drops_samples_rates_reasons_and_ids() -> None:
    recording = _rec(
        leads=((918273645, -1, 2),),
        sampling_hz=3210,
        heart_rate=135791357,
        afib_details=44,
        wearposition=8642,
        signalid=867530986,
        device_id="canary-device-token",
        recorded_at=datetime(2024, 7, 4, 4, 5, 6, tzinfo=UTC),
        connection=CONN,
        estimated={
            "hr_bpm": 654.3,
            "rpeaks": [11111.11],
            "hrv_reasons": ["canary-reason-zz"],
            "sqi": {"result": "pass", "reasons": [], "windows": [{"amplitude_mv": 9.8765}]},
        },
    )
    text = "\n".join(evaluate((recording,), (), OwnerAnswers()).lines)
    link = MeasureLink(
        datetime(2024, 7, 4, 4, 5, 6, tzinfo=UTC),
        CONN,
        frozenset({"canary-device-token"}),
        3210,
        frozenset({135}),
    )
    assert repr(recording) == "Recording redacted"
    assert repr(link) == "MeasureLink redacted"
    for needle in (
        "918273645",
        "135791357",
        "867530986",
        "8642",
        "3210",
        "654.3",
        "11111.11",
        "9.8765",
        "canary-device-token",
        "canary-reason-zz",
        "2024-07-04",
        "04:05:06",
        str(CONN),
        "44",
    ):
        assert needle not in text, needle
        assert needle not in repr(recording), needle
        assert needle not in repr(link), needle


def test_partial_v4_answers_are_refused_without_the_numbers() -> None:
    refused = answers_from(
        argparse.Namespace(
            oc1=None, oc2=None, v4_wrong_beats=3, v4_inspected_beats=None, v4_justified=1
        )
    )
    assert refused == "error: v4 counts are incomplete"
    assert "3" not in str(refused)


def test_the_command_is_installed() -> None:
    parser = argparse.ArgumentParser()
    add_subcommands(parser)
    args = parser.parse_args(
        ["ecg", "validate", "--user", "00000000-0000-4000-8000-000000000001", "--oc1", "yes"]
    )
    assert args.command == "ecg"
    assert args.action == "validate"
    assert COMMANDS["ecg"] is not None
