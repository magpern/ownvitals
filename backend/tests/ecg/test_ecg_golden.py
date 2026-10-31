r"""Golden output, the version ledger and the file guards (tests file, "Seeds and other tests").

SYNTHETIC: the golden strips are generated; the golden file holds the ``estimated`` records of
a few of them. Three rules keep an algorithm change visible:

* the golden output must equal what the pipeline produces now;
* the ledger maps every pipeline version to the digest of the golden values recorded under it,
  so a golden change without a new pipeline version is refused;
* a module version change changes the pipeline version.

Re-record after a deliberate change (and a version bump) with::

    uv run python -c "import sys; sys.path[:0] = ['tests', 'tests/ecg']; \
    import test_ecg_golden as g; g.record()"

which writes ``golden_estimated.json`` and appends the ledger entry of the current version.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import scenarios

from ecgsynth import make_strip
from ownvitals.ecg import pipeline
from ownvitals.ecg.pipeline import analyse, canonical_json, to_estimated

SYNTHETIC = True

HERE = Path(__file__).resolve().parent
GOLDEN = HERE / "golden_estimated.json"
FINGERPRINT = "sha256:" + "0" * 64  # a placeholder: the caller of the pipeline knows the row

# name -> (seed, hr_bpm, sampling rate, kind or noise condition)
STRIPS: dict[str, tuple[int, float, int, str]] = {
    "sinus_60_300": (101, 60.0, 300, "sinus"),
    "sinus_140_500": (102, 140.0, 500, "sinus"),
    "af_120_300": (103, 120.0, 300, "af"),
    "ectopic_75_500": (104, 75.0, 500, "ectopic"),
    "respiratory_45_300": (105, 45.0, 300, "respiratory"),
}
NOISY = ("white6", "motion_half", "dropout_middle")  # rejected or borderline: reasons recorded


def records() -> dict[str, Any]:
    """The ``estimated`` records of the golden strips, by name."""
    out: dict[str, Any] = {}
    for name, (seed, hr, fs, kind) in STRIPS.items():
        strip = make_strip(seed, hr, fs, kind=kind)  # type: ignore[arg-type]
        result = analyse([strip.signal_uv], fs)
        out[name] = to_estimated(result, input_fingerprint=FINGERPRINT, input_revision=1)
    for condition in NOISY:
        case = scenarios.case(condition, 300, 3)
        out[f"{condition}_300"] = to_estimated(
            case.result, input_fingerprint=FINGERPRINT, input_revision=1
        )
    return json.loads(json.dumps(out))  # as stored: plain JSON types


def values_digest(recorded: dict[str, Any]) -> str:
    """The digest of the golden values: everything except the version fields."""
    stripped = {
        name: {k: v for k, v in record.items() if k not in ("algo_version", "modules")}
        for name, record in recorded.items()
    }
    return hashlib.sha256(json.dumps(stripped, sort_keys=True).encode()).hexdigest()


def load() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return loaded


def record() -> None:
    current = records()
    ledger = load()["ledger"] if GOLDEN.exists() else {}
    ledger[pipeline.PIPELINE_VERSION] = {
        "modules": dict(pipeline.MODULES),
        "digest": values_digest(current),
    }
    GOLDEN.write_text(
        json.dumps(
            {"synthetic": True, "ledger": ledger, "strips": current}, sort_keys=True, indent=1
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def test_the_golden_output_is_what_the_pipeline_produces() -> None:
    assert records() == load()["strips"], (
        "the output of the pipeline changed: bump the module versions and the pipeline version, "
        "then re-record (see the module docstring)"
    )


def test_a_golden_change_needs_a_new_pipeline_version() -> None:
    entry = load()["ledger"].get(pipeline.PIPELINE_VERSION)
    assert entry is not None, "the current pipeline version has no ledger entry: re-record"
    assert entry["digest"] == values_digest(records()), (
        "the golden values differ from the ones recorded under this pipeline version: "
        "bump the version (module and pipeline) and re-record"
    )
    assert entry["modules"] == pipeline.MODULES


def test_the_golden_strips_carry_the_current_versions() -> None:
    for name, recorded in load()["strips"].items():
        assert recorded["algo_version"] == pipeline.PIPELINE_VERSION, name
        assert recorded["modules"] == pipeline.MODULES, name


def test_a_module_version_change_needs_a_new_pipeline_version() -> None:
    """Each ledger entry pins the module versions of its pipeline version: bumping a module
    without the pipeline version fails ``test_a_golden_change_needs_a_new_pipeline_version``
    (the entry's modules differ), and two pipeline versions cannot share a module set."""
    modules = [json.dumps(e["modules"], sort_keys=True) for e in load()["ledger"].values()]
    assert len(modules) == len(set(modules)), "two pipeline versions share the same modules"


def test_the_golden_estimated_records_are_canonical_json() -> None:
    for name, recorded in load()["strips"].items():
        text = canonical_json(recorded)
        assert json.loads(text) == recorded, name
        assert text == canonical_json(json.loads(text)), name


# --- guards over backend/tests/ecg -------------------------------------------------------------

RECORDING_SUFFIXES = {
    ".hea", ".dat", ".atr", ".qrs", ".edf", ".bdf", ".scp", ".dcm", ".dicom", ".mat", ".ecg",
    ".aecg", ".hl7", ".csv", ".txt", ".xml", ".wav", ".npy", ".npz", ".h5", ".hdf5",
}  # fmt: skip
MAX_FILE_BYTES = 256 * 1024


def guarded_files() -> list[Path]:
    return [p for p in HERE.rglob("*") if p.is_file() and "__pycache__" not in p.parts]


def test_no_file_under_the_ecg_tests_is_over_256_kb() -> None:
    too_big = [p.name for p in guarded_files() if p.stat().st_size > MAX_FILE_BYTES]
    assert too_big == []


def test_no_recording_format_lives_under_the_ecg_tests() -> None:
    """WFDB, EDF, SCP-ECG, HL7 aECG and DICOM waveform files are refused by name and by header:
    no real or public recording belongs in the repository (AGENTS.md)."""
    offenders = []
    for path in guarded_files():
        if path.suffix.lower() in RECORDING_SUFFIXES:
            offenders.append((path.name, "suffix"))
            continue
        if path.suffix.lower() in {".py", ".json"}:
            continue
        head = path.read_bytes()[:512]
        if head[128:132] == b"DICM":
            offenders.append((path.name, "DICOM"))
        if head[:8] == b"0       ":
            offenders.append((path.name, "EDF"))
        if re.search(rb"AnnotatedECG|urn:hl7-org:v3|SCPECG", head):
            offenders.append((path.name, "HL7 aECG / SCP-ECG"))
    assert offenders == []


def test_the_golden_file_is_small_and_labelled_synthetic() -> None:
    assert GOLDEN.stat().st_size < MAX_FILE_BYTES // 2
    assert load()["synthetic"] is True


def test_every_python_module_of_the_ecg_tests_is_labelled_synthetic() -> None:
    for path in HERE.glob("*.py"):
        assert "synthetic" in path.read_text(encoding="utf-8").lower(), path.name
