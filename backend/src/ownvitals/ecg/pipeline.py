"""The ECG pipeline and the shape of ``ecg_recordings.estimated`` (ADR 0006 sections 4 and 9).

::

    signal (int32 uV) -> input -> filters -> rpeaks -> sqi (gate) -> hrv

``analyse`` returns one of three typed results: ``NotAnalysed`` (no signal, or a sampling rate
the pipeline does not analyse), ``QualityRejected`` (the gate failed: the reasons and the
per-window indices, nothing derived from the beats) or ``Analysed`` (the beats of the longest
span, HR and, when its minimums hold, HRV). ``to_estimated`` turns a result into the canonical
JSON-ready dictionary; the input fingerprint and revision come from the caller, who knows the
row. Storing and reading it is the job of later issues.

Pure: no I/O, no database, no clock, no randomness. The pipeline version changes with any
module version (ADR 0006 section 15).
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ownvitals.ecg import filters, hrv, rpeaks, sqi
from ownvitals.ecg import input as ecg_input
from ownvitals.ecg.input import NotAnalysed
from ownvitals.ecg.sqi import QualityRejected, WindowIndices

PIPELINE_VERSION = "ecg-4.0.0"

MODULES = {
    "input": ecg_input.ALGO_VERSION,
    "filters": filters.ALGO_VERSION,
    "rpeaks": rpeaks.ALGO_VERSION,
    "sqi": sqi.ALGO_VERSION,
    "hrv": hrv.ALGO_VERSION,
}
GATE_MODULES = ("input", "filters", "rpeaks", "sqi")  # a change here can alter a gate result


@dataclass(frozen=True)
class Analysed:
    """The strip passed the gate."""

    lead: int
    sampling_hz: int
    windows: tuple[WindowIndices, ...]
    span_s: float
    rpeaks: tuple[float, ...]  # sample positions of the confirmed beats of the span
    rr: hrv.RrAnalysis


Result = NotAnalysed | QualityRejected | Analysed


def analyse(leads: Sequence[Any] | None, sampling_hz: int | None) -> Result:
    """Run the pipeline on the first lead of a recording."""
    prepared = ecg_input.prepare(leads, sampling_hz)
    if isinstance(prepared, NotAnalysed):
        return prepared
    if not ecg_input.window_bounds(prepared.n_samples, prepared.sampling_hz):
        return QualityRejected(reasons=("too_short",), windows=())
    conditioned = filters.condition(prepared.x_mv, prepared.sampling_hz)
    detection = rpeaks.detect(conditioned, prepared.sampling_hz)
    gate = sqi.evaluate(prepared, conditioned, detection)
    if isinstance(gate, QualityRejected):
        return gate
    return Analysed(
        lead=prepared.lead,
        sampling_hz=prepared.sampling_hz,
        windows=gate.windows,
        span_s=gate.span_s,
        rpeaks=tuple(float(p) for p in gate.rpeaks),
        rr=hrv.analyse(gate.rpeaks, prepared.sampling_hz, gate.unconfirmed, gate.distorted),
    )


def _window_record(window: WindowIndices) -> dict[str, Any]:
    def rounded(value: float | None, digits: int) -> float | None:
        return None if value is None else round(value, digits)

    return {
        "start_s": round(window.start / window.sampling_hz, 3),
        "stop_s": round(window.stop / window.sampling_hz, 3),
        "passed": window.passed,
        "reasons": list(window.reasons),
        "bsqi": round(window.bsqi, 4),
        "rate_bpm": rounded(window.rate_bpm, 1),
        "gap_s": round(window.gap_s, 3),
        "tsqi": rounded(window.tsqi, 4),
        "amplitude_mv": round(window.amplitude_mv, 4),
        "clipped": window.clipped,
        "ksqi": rounded(window.ksqi, 3),
        "psqi": rounded(window.psqi, 4),
        "bassqi": rounded(window.bassqi, 4),
    }


def to_estimated(result: Result, *, input_fingerprint: str, input_revision: int) -> dict[str, Any]:
    """The ``estimated`` record (ADR 0006 section 9) for a result.

    ``sqi.result`` is ``pass``, ``fail`` or, for a recording that was not analysed, ``not_run``
    (no gate result). A pass carries ``rpeaks`` (2 decimals), ``excluded``, ``hr_bpm``,
    ``n_rr`` and ``hrv`` (or null with ``hrv_reasons``). A failure carries no beat-derived
    field.
    """
    record: dict[str, Any] = {
        "algo_version": PIPELINE_VERSION,
        "modules": dict(MODULES),
        "input_fingerprint": input_fingerprint,
        "input_revision": input_revision,
    }
    if isinstance(result, NotAnalysed):
        record["lead"] = None
        record["sqi"] = {"result": "not_run", "reasons": [result.reason], "span_s": None}
        record["sqi"]["windows"] = []
        return record
    if isinstance(result, QualityRejected):
        record["lead"] = 0
        record["sqi"] = {
            "result": "fail",
            "reasons": list(result.reasons),
            "span_s": None,
            "windows": [_window_record(w) for w in result.windows],
        }
        return record
    record["lead"] = result.lead
    record["sqi"] = {
        "result": "pass",
        "reasons": [],
        "span_s": round(result.span_s, 2),
        "windows": [_window_record(w) for w in result.windows],
    }
    record["rpeaks"] = [round(p, 2) for p in result.rpeaks]
    record["excluded"] = [{"i": e.i, "reason": e.reason} for e in result.rr.excluded]
    record["hr_bpm"] = result.rr.hr_bpm
    record["n_rr"] = result.rr.n_rr
    h = result.rr.hrv
    record["hrv"] = (
        None
        if h is None
        else {
            "rmssd_ms": h.rmssd_ms,
            "sdnn_ms": h.sdnn_ms,
            "pnn50_pct": h.pnn50_pct,
            "n_nn": h.n_nn,
            "n_pairs": h.n_pairs,
            "excluded_fraction": h.excluded_fraction,
        }
    )
    record["hrv_reasons"] = list(result.rr.hrv_reasons)
    return record


def canonical_json(record: dict[str, Any]) -> str:
    """Sorted keys, no spaces, no NaN: the same record is always the same bytes."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)


__all__ = [
    "GATE_MODULES",
    "MODULES",
    "PIPELINE_VERSION",
    "Analysed",
    "NotAnalysed",
    "QualityRejected",
    "analyse",
    "canonical_json",
    "to_estimated",
]
