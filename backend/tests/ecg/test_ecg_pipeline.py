"""The pipeline: input handling, typed results, no values from a failed gate, determinism and the
shape of ``estimated`` (ADR 0006 sections 4, 7, 9 and 15). Synthetic signals only."""

import dataclasses
import json

import ecgnoise
import numpy as np
import pytest
from scenarios import RATES

from ecgsynth import make_strip
from ownvitals.ecg import input as ecg_input
from ownvitals.ecg import pipeline
from ownvitals.ecg.input import NotAnalysed
from ownvitals.ecg.pipeline import Analysed, QualityRejected, analyse, canonical_json, to_estimated


def passing(fs: int = 300) -> Analysed:
    result = analyse([make_strip(4, 75.0, fs).signal_uv], fs)
    assert isinstance(result, Analysed)
    return result


# --- input ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("leads", [None, [], [[]], [np.zeros(0, dtype=np.int32)]])
def test_a_missing_signal_gives_no_result_at_all(leads: object) -> None:
    assert analyse(leads, 300) == NotAnalysed("signal_missing")  # type: ignore[arg-type]


@pytest.mark.parametrize("rate", [0, 125, 250, 299, 301, 1000, None])
def test_an_unsupported_sampling_rate_gives_no_result(rate: int | None) -> None:
    signal = make_strip(1, 60.0, 300).signal_uv
    assert analyse([signal], rate) == NotAnalysed("unsupported_sampling_rate")


def test_a_missing_signal_wins_over_an_unsupported_rate() -> None:
    assert analyse(None, 250) == NotAnalysed("signal_missing")


def test_only_the_first_lead_is_analysed() -> None:
    strip = make_strip(2, 80.0, 300)
    other = make_strip(3, 120.0, 300)
    both = analyse([strip.signal_uv, other.signal_uv], 300)
    assert both == analyse([strip.signal_uv], 300)
    assert isinstance(both, Analysed)
    assert both.lead == 0


@pytest.mark.parametrize(
    "lead",
    [np.array([1.5, 2.5]), np.array([[1, 2], [3, 4]]), np.array([2**31], dtype=np.int64), ["a"]],
)
def test_a_lead_that_is_not_int32_integers_is_refused_not_coerced(lead: object) -> None:
    with pytest.raises(ValueError, match="lead"):
        analyse([lead], 300)


def test_the_conversion_is_micro_to_millivolts() -> None:
    prepared = ecg_input.prepare([[1000, -250, 0]], 300)
    assert isinstance(prepared, ecg_input.Prepared)
    assert prepared.x_mv.tolist() == [1.0, -0.25, 0.0]
    assert prepared.raw_uv.dtype == np.int64


def test_the_supported_rates_are_300_and_500() -> None:
    assert ecg_input.SUPPORTED_RATES == (300, 500)


def test_a_plain_python_list_of_ints_works_like_an_array() -> None:
    strip = make_strip(5, 70.0, 300)
    assert analyse([strip.signal_uv.tolist()], 300) == analyse([strip.signal_uv], 300)


# --- typed results and the gate -------------------------------------------------------------------


def test_a_passing_strip_carries_the_beats_of_its_span_and_the_rest() -> None:
    result = passing()
    assert result.span_s >= 20.0
    assert len(result.rpeaks) == result.rr.n_rr + 1
    assert result.rr.hr_bpm == pytest.approx(75.0, abs=1.0)
    assert result.rr.hrv is not None


def test_a_rejection_carries_only_reasons_and_window_indices() -> None:
    assert {f.name for f in dataclasses.fields(QualityRejected)} == {"reasons", "windows"}
    result = analyse([ecgnoise.flat_line(make_strip(1, 60.0, 300)).signal_uv], 300)
    assert isinstance(result, QualityRejected)
    assert not hasattr(result, "rpeaks")
    assert not hasattr(result, "rr")


def test_a_failed_gate_yields_no_downstream_value_for_any_kind_of_signal() -> None:
    """Property: whatever the signal, a rejection has no R-peaks, HR or HRV anywhere, in the
    object or in the record that would be stored; a pass has a span of at least 20 s."""
    rng = np.random.default_rng(2024)
    signals: list[tuple[np.ndarray, int]] = []
    for fs in RATES:
        base = make_strip(9, 90.0, fs)
        signals += [
            (rng.integers(-500, 500, size=30 * fs).astype(np.int32), fs),
            (np.zeros(30 * fs, dtype=np.int32), fs),
            (base.signal_uv[: 15 * fs], fs),
            (ecgnoise.with_dropout(base, 5.0, 14.0).signal_uv, fs),
            (np.rint(1000 * np.sin(np.arange(30 * fs) * 0.05)).astype(np.int32), fs),
            ((base.signal_uv * 40).astype(np.int32), fs),
            (rng.integers(-3, 3, size=30 * fs).astype(np.int32), fs),
            (base.signal_uv[: 9 * fs], fs),
        ]
    outcomes = set()
    for signal, fs in signals:
        result = analyse([signal], fs)
        outcomes.add(type(result))
        record = to_estimated(result, input_fingerprint="f", input_revision=1)
        if isinstance(result, Analysed):
            assert result.span_s >= 20.0
            assert record["sqi"]["result"] == "pass"
        else:
            assert isinstance(result, QualityRejected)
            assert record["sqi"]["result"] == "fail"
            assert not {"rpeaks", "excluded", "hr_bpm", "n_rr", "hrv", "hrv_reasons"} & set(record)
            assert "too_short" in result.reasons
    assert QualityRejected in outcomes


def test_noise_alone_is_rejected_with_the_reasons_of_its_windows() -> None:
    rng = np.random.default_rng(3)
    noise = rng.integers(-300, 300, size=30 * 300).astype(np.int32)
    result = analyse([noise], 300)
    assert isinstance(result, QualityRejected)
    assert result.reasons[0] == "too_short"
    assert set(result.reasons[1:]) <= set(pipeline.sqi.REASONS)
    assert [r for r in pipeline.sqi.REASONS if r in result.reasons] == list(result.reasons)


# --- determinism, versions, estimated -----------------------------------------------------------


@pytest.mark.parametrize("fs", RATES)
def test_the_same_signal_gives_the_same_result_and_the_same_bytes(fs: int) -> None:
    signal = make_strip(6, 65.0, fs).signal_uv
    first, second = analyse([signal], fs), analyse([signal.copy()], fs)
    assert first == second
    a = to_estimated(first, input_fingerprint="x", input_revision=2)
    b = to_estimated(second, input_fingerprint="x", input_revision=2)
    assert canonical_json(a) == canonical_json(b)


def test_the_input_signal_is_not_modified() -> None:
    signal = make_strip(6, 65.0, 300).signal_uv
    before = signal.copy()
    analyse([signal], 300)
    assert np.array_equal(signal, before)


def test_every_module_has_a_semver_and_the_pipeline_lists_them() -> None:
    assert set(pipeline.MODULES) == {"input", "filters", "rpeaks", "sqi", "hrv"}
    for version in pipeline.MODULES.values():
        major, minor, patch_level = version.split(".")
        assert all(part.isdigit() for part in (major, minor, patch_level))
    assert pipeline.GATE_MODULES == ("input", "filters", "rpeaks", "sqi")
    assert pipeline.PIPELINE_VERSION.startswith("ecg-")


def test_the_estimated_record_of_a_pass_has_the_fields_of_section_9() -> None:
    result = passing()
    record = to_estimated(result, input_fingerprint="abc", input_revision=7)
    assert set(record) == {
        "algo_version",
        "modules",
        "input_fingerprint",
        "input_revision",
        "lead",
        "sqi",
        "rpeaks",
        "excluded",
        "hr_bpm",
        "n_rr",
        "hrv",
        "hrv_reasons",
    }
    assert record["algo_version"] == pipeline.PIPELINE_VERSION
    assert record["modules"] == pipeline.MODULES
    assert (record["input_fingerprint"], record["input_revision"], record["lead"]) == ("abc", 7, 0)
    assert set(record["sqi"]) == {"result", "reasons", "span_s", "windows"}
    assert record["sqi"]["result"] == "pass"
    assert len(record["sqi"]["windows"]) == 5
    assert set(record["hrv"]) == {
        "rmssd_ms",
        "sdnn_ms",
        "pnn50_pct",
        "n_nn",
        "n_pairs",
        "excluded_fraction",
    }
    assert all(round(p, 2) == p for p in record["rpeaks"])
    assert record["hrv_reasons"] == []


def test_the_estimated_record_is_canonical_json_with_the_rounding_of_section_8() -> None:
    record = to_estimated(passing(), input_fingerprint="abc", input_revision=1)
    text = canonical_json(record)
    assert json.loads(text) == record
    assert text == json.dumps(record, sort_keys=True, separators=(",", ":"))
    for key in ("rmssd_ms", "sdnn_ms", "pnn50_pct"):
        assert round(record["hrv"][key], 1) == record["hrv"][key]
    assert round(record["hr_bpm"], 1) == record["hr_bpm"]
    assert "NaN" not in text
    assert "Infinity" not in text


def test_a_null_hrv_carries_its_reasons() -> None:
    fs = 300
    result = analyse([make_strip(1, 120.0, fs, kind="af").signal_uv], fs)
    if isinstance(result, Analysed):  # the AF-like seed may also be rejected by the gate
        record = to_estimated(result, input_fingerprint="a", input_revision=1)
        assert record["hrv"] is None
        assert "too_many_excluded" in record["hrv_reasons"]


def test_a_recording_that_was_not_analysed_has_no_gate_result() -> None:
    for result in (NotAnalysed("signal_missing"), NotAnalysed("unsupported_sampling_rate")):
        record = to_estimated(result, input_fingerprint="a", input_revision=3)
        assert record["sqi"] == {
            "result": "not_run",
            "reasons": [result.reason],
            "span_s": None,
            "windows": [],
        }
        assert record["lead"] is None
        assert not {"rpeaks", "hr_bpm", "hrv"} & set(record)


def test_a_too_short_strip_has_a_gate_failure_and_no_windows() -> None:
    result = analyse([make_strip(1, 60.0, 300).signal_uv[:1000]], 300)
    record = to_estimated(result, input_fingerprint="a", input_revision=1)
    assert record["sqi"]["result"] == "fail"
    assert record["sqi"]["reasons"] == ["too_short"]
    assert record["sqi"]["windows"] == []
