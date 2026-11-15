"""The read-time ECG gate overlay (ADR 0006 sections 9 and 11). Pure.

No finding stores our gate outcome. Whenever a finding is read, ``ownvitals.views.findings_read``
loads the recordings linked to its trigger groups and calls ``ecg_gate_overlay``, so a link that
appears later, a deleted recording, a recomputed ``estimated`` and a new gate version are current
by construction.

- **Trigger**: the measure groups in the finding's ``evidence["trigger_groups"]`` (the rule that
  produced the value records them; nothing here re-derives the set). A group that is also in the
  baseline counts as a trigger; recordings only in the baseline are never linked.
- **Linked** (A7): a recording with the same connection and timestamp as a live trigger group that
  holds an ECG measure type, from the same device (``a7_links``). Resolved at read time, never
  stored. A deleted recording or a soft-deleted group is no link.
- **Current**: an ``estimated`` counts as absent when its ``input_fingerprint`` differs from the
  row's or a gate module (``input``, ``filters``, ``rpeaks``, ``sqi``) differs from the running
  one. A recording has a **gate result** (``pass`` or ``fail``) only from a current
  ``estimated``; ``not_run`` (``unsupported_sampling_rate``, ``signal_missing``) is none.
- **E4, E6**: of the trigger recordings with a gate result, hidden when at least half failed;
  shown with "not evaluated by ownvitals" when none has one. An unlinked trigger group and a
  linked recording without a result stay out of the count; a failure never counts as a pass.
- **E1 to E3**: never hidden. Their gate outcome is reported here; the read function shows it
  only behind the owner-only flag.
- Every other rule: the overlay does not apply.

``estimate_view`` is section 9's up-to-date rule for one recording: HR and HRV only from an
``estimated`` that is not stale (``algo_version`` and ``input_fingerprint`` both current);
R-peak markers, SQI values and gate reasons only from one the rule above does not make absent
(an ``hrv``-only difference keeps them). The rest is left out, never shown with a mark.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from ownvitals.ecg.pipeline import GATE_MODULES, MODULES, PIPELINE_VERSION

# ADR 0006 A7: the AFib result (130) and the intervals (135 to 138) belong to the ECG.
ECG_TYPES = frozenset({130, 135, 136, 137, 138})
# Section 11's table: the rules the overlay hides, and the rules it never hides.
GATED_RULES = frozenset({"E4", "E6"})
NEVER_HIDDEN_RULES = frozenset({"E1", "E2", "E3"})
OVERLAY_RULES = GATED_RULES | NEVER_HIDDEN_RULES

Gate = Literal["pass", "fail"]
Outcome = Literal["passed", "failed", "not_evaluated", "not_applicable"]


@dataclass(frozen=True)
class GroupKey:
    """One live measure group a finding names as a trigger. ``repr`` is fixed: no values."""

    grpid: int
    connection: UUID
    measured_at: datetime
    device_ids: frozenset[str]
    model_id: int | None
    types: frozenset[int]

    def __repr__(self) -> str:
        return "GroupKey redacted"


@dataclass(frozen=True)
class RecordingKey:
    """One stored recording, as much as the link and the gate need. ``repr`` is fixed."""

    id: UUID
    connection: UUID
    recorded_at: datetime
    device_id: str | None
    model_id: int | None
    fingerprint: str  # the row's own input fingerprint (signalid, payload hash, sampling rate)
    estimated: Mapping[str, Any] | None

    def __repr__(self) -> str:
        return "RecordingKey redacted"


@dataclass(frozen=True)
class Overlay:
    """The overlay of one finding. ``counted`` recordings have a gate result, ``failed`` of
    them failed. ``hidden`` only ever for E4 and E6."""

    applies: bool
    hidden: bool
    outcome: Outcome
    counted: int
    failed: int


NOT_APPLICABLE = Overlay(applies=False, hidden=False, outcome="not_applicable", counted=0, failed=0)


def recording_device_id(reported: object) -> str | None:
    """The device id Withings reported with a recording (``reported.deviceid`` or its raw)."""
    if not isinstance(reported, Mapping):
        return None
    device = reported.get("deviceid")
    if isinstance(device, str) and device:
        return device
    raw = reported.get("raw")
    if isinstance(raw, Mapping):
        nested = raw.get("deviceid")
        if isinstance(nested, str) and nested:
            return nested
    return None


def group_device_ids(hash_deviceid: object, raw: object) -> frozenset[str]:
    """The device ids a measure group names (its column and its raw payload)."""
    found: set[str] = set()
    if isinstance(hash_deviceid, str) and hash_deviceid:
        found.add(hash_deviceid)
    if isinstance(raw, Mapping):
        for key in ("deviceid", "hash_deviceid"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                found.add(value)
    return frozenset(found)


def a7_links(recording: RecordingKey, group: GroupKey) -> bool:
    """ADR 0006 A7: the same connection and timestamp, an ECG type in the group, the same
    device. A missing device id is not a match; a group without one falls back to the model."""
    if group.connection != recording.connection or group.measured_at != recording.recorded_at:
        return False
    if not (group.types & ECG_TYPES):
        return False
    if recording.device_id is None:
        return False
    if recording.device_id in group.device_ids:
        return True
    return (
        not group.device_ids
        and recording.model_id is not None
        and recording.model_id == group.model_id
    )


def resolve_links(
    trigger_groups: Iterable[int],
    groups: Iterable[GroupKey],
    recordings: Iterable[RecordingKey],
) -> dict[int, tuple[RecordingKey, ...]]:
    """``grpid -> linked recordings`` for each trigger group (empty: unlinked)."""
    wanted = list(dict.fromkeys(trigger_groups))
    pool = tuple(recordings)
    by_grpid: dict[int, list[GroupKey]] = {}
    for group in groups:
        by_grpid.setdefault(group.grpid, []).append(group)
    linked: dict[int, tuple[RecordingKey, ...]] = {}
    for grpid in wanted:
        found: dict[UUID, RecordingKey] = {}
        for group in by_grpid.get(grpid, ()):
            for recording in pool:
                if a7_links(recording, group):
                    found.setdefault(recording.id, recording)
        linked[grpid] = tuple(found.values())
    return linked


def _gate_current(estimated: Mapping[str, Any] | None, fingerprint: str) -> bool:
    if not isinstance(estimated, Mapping):
        return False
    if estimated.get("input_fingerprint") != fingerprint:
        return False
    modules = estimated.get("modules")
    if not isinstance(modules, Mapping):
        return False
    return all(modules.get(name) == MODULES[name] for name in GATE_MODULES)


def estimated_current(estimated: Mapping[str, Any] | None, fingerprint: str) -> bool:
    """Whether a recording has a current ``estimated`` (its fingerprint and gate modules), with or
    without a gate result: the notification hold waits only for a recording without one."""
    return _gate_current(estimated, fingerprint)


def _sqi(estimated: Mapping[str, Any]) -> Mapping[str, Any]:
    sqi = estimated.get("sqi")
    return sqi if isinstance(sqi, Mapping) else {}


def gate_result(estimated: Mapping[str, Any] | None, fingerprint: str) -> Gate | None:
    """``pass`` or ``fail`` from a current ``estimated``; ``None`` for no gate result."""
    if not _gate_current(estimated, fingerprint) or estimated is None:
        return None
    result = _sqi(estimated).get("result")
    if result == "pass":
        return "pass"
    if result == "fail":
        return "fail"
    return None  # not_run: unsupported_sampling_rate, signal_missing


def ecg_gate_overlay(rule_id: str, linked: Mapping[int, Sequence[RecordingKey]]) -> Overlay:
    """Section 11's overlay for a finding of ``rule_id`` whose trigger groups link to ``linked``
    (every trigger group a key; an unlinked one maps to nothing)."""
    if rule_id not in OVERLAY_RULES:
        return NOT_APPLICABLE
    seen: dict[UUID, Gate | None] = {}
    for recordings in linked.values():
        for recording in recordings:
            seen.setdefault(recording.id, gate_result(recording.estimated, recording.fingerprint))
    results = [g for g in seen.values() if g is not None]
    counted = len(results)
    failed = sum(1 for g in results if g == "fail")
    if counted == 0:
        outcome: Outcome = "not_evaluated"
    elif 2 * failed >= counted:
        outcome = "failed"
    else:
        outcome = "passed"
    hidden = rule_id in GATED_RULES and outcome == "failed"
    return Overlay(applies=True, hidden=hidden, outcome=outcome, counted=counted, failed=failed)


@dataclass(frozen=True)
class EstimateView:
    """Section 9's up-to-date view of one ``estimated``. ``None`` fields are left out, never
    shown with a mark. Shown only behind the owner-only flag (the read function decides)."""

    gate: Gate | None
    reasons: tuple[str, ...]
    windows: tuple[Mapping[str, Any], ...]
    rpeaks: tuple[float, ...] | None
    hr_bpm: float | None
    hrv: Mapping[str, Any] | None

    def __repr__(self) -> str:
        return "EstimateView redacted"


EMPTY_ESTIMATE = EstimateView(None, (), (), None, None, None)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(v for v in value if isinstance(v, str))  # pyright: ignore[reportUnknownVariableType]


def estimate_view(estimated: Mapping[str, Any] | None, fingerprint: str) -> EstimateView:
    """What every output may show of one recording's ``estimated`` (section 9)."""
    if estimated is None or not _gate_current(estimated, fingerprint):
        return EMPTY_ESTIMATE
    sqi = _sqi(estimated)
    windows_raw = sqi.get("windows")
    windows = (
        tuple(w for w in windows_raw if isinstance(w, Mapping))  # pyright: ignore[reportUnknownVariableType]
        if isinstance(windows_raw, list)
        else ()
    )
    gate = gate_result(estimated, fingerprint)
    peaks_raw = estimated.get("rpeaks")
    rpeaks = (
        tuple(float(p) for p in peaks_raw if isinstance(p, int | float) and not isinstance(p, bool))  # pyright: ignore[reportUnknownVariableType]
        if gate == "pass" and isinstance(peaks_raw, list)
        else None
    )
    fresh = estimated.get("algo_version") == PIPELINE_VERSION  # not stale (section 15)
    hr = estimated.get("hr_bpm")
    number = isinstance(hr, int | float) and not isinstance(hr, bool)
    hrv = estimated.get("hrv")
    return EstimateView(
        gate=gate,
        reasons=_strings(sqi.get("reasons")),
        windows=windows,  # pyright: ignore[reportArgumentType]
        rpeaks=rpeaks,
        hr_bpm=float(hr) if fresh and gate == "pass" and number else None,  # pyright: ignore[reportArgumentType]
        hrv=hrv if fresh and gate == "pass" and isinstance(hrv, Mapping) else None,  # pyright: ignore[reportUnknownArgumentType]
    )


__all__ = [
    "ECG_TYPES",
    "GATED_RULES",
    "NEVER_HIDDEN_RULES",
    "NOT_APPLICABLE",
    "OVERLAY_RULES",
    "EstimateView",
    "GroupKey",
    "Overlay",
    "RecordingKey",
    "a7_links",
    "ecg_gate_overlay",
    "estimate_view",
    "estimated_current",
    "gate_result",
    "group_device_ids",
    "recording_device_id",
    "resolve_links",
]
