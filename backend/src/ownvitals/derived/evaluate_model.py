"""The data model of the rule evaluation (#187; ADR 0010): inputs, episode rows and the constants
the evaluation and its writer share. Pure.

Split from ``derived.evaluate`` (no behaviour change; its docstring describes the evaluation).
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from ownvitals.analysis.daily import DailyValue
from ownvitals.analysis.quality import Reading
from ownvitals.analysis.rules import body, ecg, nerve
from ownvitals.analysis.rules.core import ALGO_VERSION, SPECS, Finding, Provenance, Tier
from ownvitals.analysis.rules.system import ConnectionState, DeviceState, StreamState

RULES_VERSION: Final = ALGO_VERSION  # "rules/1": the shared bands, confidence and episodes
EXPERIMENTAL_RULES: Final = frozenset(
    rule for rule, spec in SPECS.items() if spec.tier is Tier.EXPERIMENTAL
)
Q1_RULES: Final = tuple(sorted(rule for rule in SPECS if rule.startswith("Q1:")))
REPLAYED_RULES: Final = tuple(sorted(rule for rule in SPECS if rule not in Q1_RULES))
# Every rule this writer stores (E1 to E4 and E6 included, #191); any other rule id (E5 is
# deferred, ADR 0006 section 12) is never touched.
OWNED_RULES: Final = tuple(sorted(SPECS))

NERVE_KEYS: Final = nerve.NERVE_KEYS
# N5's per-foot pairs (ADR 0010 §3): the normaliser keys 158, 159, 197 and 198 as meastype_<n>.
FOOT_PAIRS: Final[Mapping[nerve.FootPair, tuple[str, str]]] = {
    "158_159": ("meastype_158", "meastype_159"),
    "197_198": ("meastype_197", "meastype_198"),
}
SEGMENT_TYPE: Final = 175  # segmental muscle mass: B6 reads it by position
SEGMENT_KEY: Final = "segmental_muscle_mass"
# The reading-level inputs besides the daily values: B3's water, N2's nerve readings (guided is
# attrib 15), N5's foot types, Q1's weigh-ins, E4's reported intervals.
READING_KEYS: Final = tuple(
    sorted(
        {
            "weight",
            "extracellular_water",
            "intracellular_water",
            *NERVE_KEYS,
            *(key for pair in FOOT_PAIRS.values() for key in pair),
            *ecg.E4_KEYS,
        }
    )
)
_SUBJECT_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:/-")


def rule_version(rule_id: str) -> str:
    """``findings.rule_version``: the shared rules version and the rule's own, ``rules/1.1``."""
    return f"{RULES_VERSION}.{SPECS[rule_id].version}"


@dataclass(frozen=True, slots=True)
class Q1State:
    """What Q1 reads now: the ``measures`` capability, the connection, the devices of the latest
    ``getdevice`` and each stream's completeness (ADR 0010 §7)."""

    weight_capability: str | None = None
    connection: ConnectionState | None = None
    devices: tuple[DeviceState, ...] = ()
    streams: tuple[StreamState, ...] = ()


@dataclass(frozen=True, slots=True)
class Inputs:
    """One owner's evaluation inputs. ``daily`` and ``provenance`` are per metric key (the stored
    ``metric_daily`` rows and their source groups and ``jump`` flags); ``readings`` are the usable
    readings of ``READING_KEYS`` (one per group, the zone already the day zone); ``segments`` are
    the usable segmental muscle readings with their positions; ``recordings`` the Withings fields
    of the ECG recordings E1 to E3 read, and ``sex`` the profile's (E4's QTc band)."""

    today: date
    experimental: bool
    daily: Mapping[str, tuple[DailyValue, ...]] = field(default_factory=dict)
    provenance: Mapping[str, Mapping[date, Provenance]] = field(default_factory=dict)
    readings: Mapping[str, tuple[Reading, ...]] = field(default_factory=dict)
    segments: tuple[body.Segment, ...] = ()
    q1: Q1State = field(default_factory=Q1State)
    recordings: tuple[ecg.Recording, ...] = ()
    sex: ecg.Sex | None = None


@dataclass(frozen=True, slots=True)
class EpisodeRow:
    """One episode as the writer stores it. ``first_day`` and ``last_day`` are the evaluation days
    of its first and latest firing, ``ended_day`` the evaluation that ended it (``None``: still
    active). ``origin`` is ``origin(window_end, today)``: the writer uses it only for an episode
    it writes for the first time (§4: computed once, never changed)."""

    rule_id: str
    rule_version: str
    tier: str
    severity: str
    confidence: str
    subject: str
    metric_keys: tuple[str, ...]
    reference_day: date
    window_start: date
    window_end: date
    first_day: date
    last_day: date
    ended_day: date | None
    origin: str
    evidence: dict[str, Any]
    data_quality: dict[str, Any]

    @property
    def status(self) -> str:
        return "active" if self.ended_day is None else "ended"

    @property
    def match_key(self) -> tuple[str, str, date]:
        return (self.rule_id, self.subject, self.reference_day)


@dataclass(frozen=True, slots=True)
class Q1Result:
    """Today's evaluation of one Q1 part and subject: ``finding`` when it fires."""

    rule_id: str
    subject: str
    finding: Finding | None
    metric_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Evaluation:
    episodes: tuple[EpisodeRow, ...]
    q1: tuple[Q1Result, ...]
    evaluated: Mapping[str, int]  # evaluations per rule id (counts only)


def json_safe(value: Any) -> Any:
    """``value`` as JSON the database accepts: a non-finite float becomes ``None``."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    return value


def canonical(value: Any) -> str:
    """One text per JSON value, to compare a computed document with a stored one."""
    return json.dumps(json_safe(value), sort_keys=True, separators=(",", ":"))


def subject_ok(subject: str) -> bool:
    """``findings.subject``'s check: 1 to 160 of letters, digits and ``_.:/-``."""
    return 0 < len(subject) <= 160 and set(subject) <= _SUBJECT_OK
