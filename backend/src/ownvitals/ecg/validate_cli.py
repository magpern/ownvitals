"""Owner-run ECG checks (ADR 0006 sections 2 and 14).

Pure: recordings and measure links come in as values, and the result is a list of categorical
lines (pass/fail, yes/no, counts, whole percents). No database, no logging, and nothing here
prints a sample, a measurement, a timestamp or an id. The operator command that loads one
user's rows lives outside this package, because the package is a pure library.

OC1 and OC2 stay owner judgments (``--oc1`` / ``--oc2``). OC1 also refuses a first lead whose
peak absolute sample is outside 100-10,000 µV, so a scale that cannot be microvolts fails
before the owner confirms it. OC3's 30 dB figure is the ADR's; a mains line is "present" when
its +/-1 Hz power is at least 10 dB above the side bands 3-10 Hz away and at least 1% of the
5-15 Hz power (our choices for a yes/no the ADR leaves unnamed). OC5's late-link count stays
``not-applicable`` until the notification hold records end reasons (ADR amendment, #88).
"""

import argparse
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

NOISE_DETAILS = frozenset({-1, 2, 5, 13})
ECG_TYPES = frozenset({130, 135, 136, 137, 138})
SCALE_MIN_UV, SCALE_MAX_UV = 100, 10_000
LOWPASS_DB = 30.0
MAINS_DB = 10.0
MAINS_SHARE = 0.01
COINCIDE_MS = 20.0
COINCIDE_FRACTION = 0.95
V1_MIN_PERCENT, V3_MIN_PERCENT, V3_MAX_BPM = 80, 95, 5.0
V4_MIN_PASSED, V4_MIN_REJECTED, V4_MIN_JUSTIFIED = 20, 10, 8
V4_MAX_WRONG_PERCENT = 1
HRV_REASONS = (
    "unconfirmed_beat",
    "rr_out_of_range",
    "missed_beat",
    "distorted_beat",
    "rr_deviation",
    "too_many_excluded",
    "insufficient_nn",
    "irregular_rhythm",
)
SQI_REASONS = (
    "too_short",
    "flat_line",
    "amplitude_out_of_range",
    "clipping",
    "detector_disagreement",
    "implausible_rate",
    "beat_gap",
    "low_template_correlation",
)
_AGREEMENT = (
    ("withings-clean", "our-pass"),
    ("withings-clean", "our-fail"),
    ("withings-clean", "our-none"),
    ("withings-noise", "our-pass"),
    ("withings-noise", "our-fail"),
    ("withings-noise", "our-none"),
)
_BANDS = ("under-50", "from-50", "unknown")


@dataclass(frozen=True, slots=True)
class Report:
    """The command's stdout, one categorical line each, and whether any decided check failed."""

    lines: tuple[str, ...]
    failed: bool


@dataclass(frozen=True, slots=True)
class OwnerAnswers:
    """What the owner types after looking. Omitted answers stay pending."""

    oc1: str | None = None
    oc2: str | None = None
    v4_wrong_beats: int | None = None
    v4_inspected_beats: int | None = None
    v4_justified: int | None = None


@dataclass(frozen=True, slots=True, repr=False)
class Recording:
    """One stored recording. ``repr`` is fixed so a log of the object cannot carry values."""

    leads: tuple[tuple[int, ...], ...] | None
    sampling_hz: int | None
    heart_rate: int | None
    afib_details: int | None
    wearposition: int | None
    estimated: dict[str, Any] | None
    payload_hash: bytes | None
    signalid: int
    recorded_at: datetime
    connection: UUID
    model_id: int | None
    device_id: str | None

    def __repr__(self) -> str:
        return "Recording redacted"


@dataclass(frozen=True, slots=True, repr=False)
class MeasureLink:
    """One live measure group that might belong to an ECG. ``repr`` is fixed."""

    measured_at: datetime
    connection: UUID
    device_ids: frozenset[str]
    model_id: int | None
    types: frozenset[int]

    def __repr__(self) -> str:
        return "MeasureLink redacted"


def input_fingerprint(signalid: int, digest: bytes | None, sampling_hz: int | None) -> str:
    """The same fingerprint as ingest (ADR 0006 section 9). Kept here so this module stays pure."""
    hashed = "" if digest is None else digest.hex()
    rate = "" if sampling_hz is None else str(sampling_hz)
    text = f"v1\nsignalid:{signalid}\npayload_hash:{hashed}\nsampling_hz:{rate}\n"
    return hashlib.sha256(text.encode()).hexdigest()


def evaluate(
    recordings: Sequence[Recording],
    links: Sequence[MeasureLink],
    answers: OwnerAnswers,
) -> Report:
    """OC1-OC5 and V1-V5. The lines are the whole stdout of the command."""
    from ownvitals.ecg.validate_rules import (
        _agreement,
        _failure_counts,
        _hrv_counts,
        _oc1,
        _oc2,
        _oc3,
        _oc4,
        _oc5,
        _positions,
        _scale,
        _v1,
        _v3,
        _v4,
        _v5,
        _whole_percent,
    )

    scale = _scale(recordings)
    oc1 = _oc1(scale, answers.oc1)
    positions = _positions(recordings)
    oc2 = _oc2(positions, answers.oc2)
    lowpass, mains = _oc3(recordings)
    oc4, coincident = _oc4(recordings)
    oc5, missing = _oc5(recordings, links)
    v1, eligible, v1_rate = _v1(recordings)
    agreement = _agreement(recordings)
    passed_n, hrv_counts = _hrv_counts(recordings)
    failures = _failure_counts(recordings)
    v3, pairs, within, bias, limits = _v3(recordings)
    v4, passed_strips, rejected_strips, wrong, justified = _v4(recordings, answers)
    v5, compared, mismatches = _v5(recordings)
    owner_oc1 = answers.oc1 or "pending"
    owner_oc2 = answers.oc2 or "pending"
    lines = [
        f"recordings {len(recordings)}",
        f"OC1 scale {scale}",
        f"OC1 owner {owner_oc1}",
        f"OC1 {oc1}",
        f"OC2 positions {positions}",
        f"OC2 owner {owner_oc2}",
        f"OC2 {oc2}",
        f"OC3 lowpass {lowpass}",
        f"OC3 mains {mains}",
        f"OC4 {oc4}",
        f"OC4 coincident {coincident}",
        f"OC5 {oc5}",
        f"OC5 without {missing}",
        "OC5 late-link not-applicable",
        f"V1 {v1}",
        f"V1 eligible {eligible}",
        f"V1 rate {v1_rate}",
    ]
    lines.extend(f"V2 {side} {our} {agreement[(side, our)]}" for side, our in _AGREEMENT)
    if passed_n:
        for reason in (*HRV_REASONS, "other"):
            count = hrv_counts[reason]
            if count:
                lines.append(f"V2 hrv-reason {reason} {count} {_whole_percent(count, passed_n)}%")
    for band in _BANDS:
        for reason in (*SQI_REASONS, "other", "none"):
            count = failures[(band, reason)]
            if count:
                lines.append(f"V2 fail-band {band} {reason} {count}")
    lines.extend(
        (
            f"V3 {v3}",
            f"V3 pairs {pairs}",
            f"V3 within-5 {within}",
            f"V3 bias-sign {bias}",
            f"V3 limits-within-5 {limits}",
            f"V4 {v4}",
            f"V4 passed-strips {passed_strips}",
            f"V4 rejected-strips {rejected_strips}",
            f"V4 wrong-beats {wrong}",
            f"V4 justified {justified}",
            f"V5 {v5}",
            f"V5 compared {compared}",
            f"V5 mismatch {mismatches}",
        )
    )
    failed = any(status == "fail" for status in (oc1, oc2, lowpass, oc4, oc5, v1, v3, v4, v5))
    return Report(tuple(lines), failed)


def _count(value: str) -> int:
    if not value.isdigit():
        raise argparse.ArgumentTypeError("use a whole number of beats or strips")
    return int(value)


def add_ecg_subcommands(sub: Any) -> None:
    """``ecg validate``. Installed by the operator CLI."""
    ecg = sub.add_parser("ecg", help="ECG checks for one user").add_subparsers(
        dest="action", required=True
    )
    validate = ecg.add_parser(
        "validate",
        help="OC1-OC5 and V1-V5 as counts, yes/no and whole percents",
    )
    validate.add_argument("--user", required=True, help="the account to read, under forced RLS")
    validate.add_argument("--oc1", choices=("yes", "no"), help="owner: R amplitudes match")
    validate.add_argument("--oc2", choices=("yes", "no"), help="owner: morphology matches the lead")
    validate.add_argument("--v4-wrong-beats", type=_count)
    validate.add_argument("--v4-inspected-beats", type=_count)
    validate.add_argument("--v4-justified", type=_count)


def answers_from(args: argparse.Namespace) -> OwnerAnswers | str:
    """Map the owner flags. A partial V4 answer is refused without echoing the numbers."""
    flags = (args.v4_wrong_beats, args.v4_inspected_beats, args.v4_justified)
    if any(flag is not None for flag in flags) and any(flag is None for flag in flags):
        return "error: v4 counts are incomplete"
    wrong, inspected, justified = flags
    if inspected is not None and wrong is not None and justified is not None:
        if inspected < 1:
            return "error: inspected beats must be at least 1"
        if wrong > inspected:
            return "error: wrong beats cannot exceed inspected beats"
    return OwnerAnswers(
        oc1=args.oc1,
        oc2=args.oc2,
        v4_wrong_beats=wrong,
        v4_inspected_beats=inspected,
        v4_justified=justified,
    )
