"""Shared logic of the v1 finding rules (ADR 0010 §2 to §6; PLAN §7.2). Pure.

* the **two bands** (§2): ``display_band`` (k 1.5) and ``finding_band`` (k 2.5, the 90 days
  before the reference day), both ``median ± max(w, k · 1.4826 · MAD)`` with the metric's minimum
  half-width *w*, gated by the metric's BASELINE policy; ``k`` is always passed explicitly;
* **confidence** (§5) and **severity** (§6);
* the result types: a rule returns a ``Finding`` proposal, a ``NoFinding`` or ``Insufficient``
  (insufficient data, never a finding);
* the rule registry (``SPECS``, ``SEVERITY``); the shared ``trend_rule`` is in ``trendrule``.

Daily series are built with ``Series.from_daily``, never ``from_instants`` (which counts UTC
days). Nothing here knows who may see a rule: an experimental rule only carries its tier.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ownvitals.analysis.dataquality import DataQuality
from ownvitals.analysis.policy import (
    InsufficientData,
)
from ownvitals.analysis.rules.band import (
    BAND_DAYS as BAND_DAYS,
)
from ownvitals.analysis.rules.band import (
    DISPLAY_K as DISPLAY_K,
)
from ownvitals.analysis.rules.band import (
    FINDING_K as FINDING_K,
)
from ownvitals.analysis.rules.band import (
    MIN_HALF_WIDTH as MIN_HALF_WIDTH,
)
from ownvitals.analysis.rules.band import (
    Band as Band,
)
from ownvitals.analysis.rules.band import (
    band_at as band_at,
)
from ownvitals.analysis.rules.band import (
    display_band as display_band,
)
from ownvitals.analysis.rules.band import (
    finding_band as finding_band,
)
from ownvitals.analysis.rules.band import (
    min_half_width as min_half_width,
)
from ownvitals.analysis.rules.confidence import (
    LOW_TREND_PHI as LOW_TREND_PHI,
)
from ownvitals.analysis.rules.confidence import (
    MORE_THAN_LOW_TREND as MORE_THAN_LOW_TREND,
)
from ownvitals.analysis.rules.confidence import (
    Confidence,
)
from ownvitals.analysis.rules.confidence import (
    confidence_data as confidence_data,
)
from ownvitals.analysis.rules.confidence import (
    confidence_jump as confidence_jump,
)
from ownvitals.analysis.rules.confidence import (
    confidence_run as confidence_run,
)
from ownvitals.analysis.rules.confidence import (
    confidence_trend as confidence_trend,
)
from ownvitals.analysis.rules.confidence import (
    lowest as lowest,
)
from ownvitals.analysis.rules.dailyvalues import (
    Provenance as Provenance,
)
from ownvitals.analysis.rules.dailyvalues import (
    ProvenanceMap as ProvenanceMap,
)
from ownvitals.analysis.rules.dailyvalues import (
    day_number as day_number,
)
from ownvitals.analysis.rules.dailyvalues import (
    groups_on as groups_on,
)
from ownvitals.analysis.rules.dailyvalues import (
    provenance_of as provenance_of,
)
from ownvitals.analysis.rules.dailyvalues import (
    require_value as require_value,
)
from ownvitals.analysis.rules.dailyvalues import (
    value_on as value_on,
)
from ownvitals.analysis.rules.dailyvalues import (
    window_of as window_of,
)

ALGO_VERSION = "rules/1"

TREND_ALPHA = 0.05


class Tier(StrEnum):
    DEVICE = "device"
    TREND = "trend"
    EXPERIMENTAL = "experimental"
    SYSTEM = "system"


class Severity(StrEnum):
    URGENT = "urgent"
    ATTENTION = "attention"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class RuleSpec:
    rule_id: str
    version: int
    tier: Tier
    severity: Severity
    persistent: bool = False  # the condition includes a ``trend`` test (§4 persistence)


def _spec(
    rule_id: str, tier: Tier, *, persistent: bool = False, severity: Severity = Severity.ATTENTION
) -> tuple[str, RuleSpec]:
    return rule_id, RuleSpec(rule_id, 1, tier, severity, persistent)


# Rules of this module's three files. A change to a rule bumps its ``version``.
SPECS: Mapping[str, RuleSpec] = MappingProxyType(
    dict(
        [
            _spec("B1", Tier.EXPERIMENTAL),
            _spec("B2", Tier.TREND, persistent=True),
            _spec("B3", Tier.EXPERIMENTAL),
            _spec("B4", Tier.TREND, persistent=True),
            _spec("B5", Tier.TREND, persistent=True),
            _spec("B6", Tier.TREND),
            _spec("C1", Tier.TREND),
            _spec("C1x", Tier.EXPERIMENTAL),
            _spec("C2", Tier.TREND, persistent=True),
            # Nerve health (nerve.py). N2's two parts are separate rules so that a guided and a
            # routine finding of one nerve key are separate episodes (one active per rule_id and
            # subject, §4). N1 is a gate, not a rule; N4 is not in v1.
            _spec("N2:guided", Tier.TREND),
            _spec("N2:routine", Tier.TREND),
            _spec("N3", Tier.TREND, persistent=True),
            _spec("N5", Tier.TREND),
            # System (system.py), one rule per Q1 part.
            _spec("Q1:no_weigh_in", Tier.SYSTEM, severity=Severity.INFO),
            _spec("Q1:low_battery", Tier.SYSTEM, severity=Severity.INFO),
            _spec("Q1:reconnect", Tier.SYSTEM, severity=Severity.URGENT),
            _spec("Q1:import_incomplete", Tier.SYSTEM, severity=Severity.INFO),
        ]
    )
)

# Severity of every v1 rule (ADR 0010 §6): never a diagnosis, never a clinical band. Q1 has one
# entry per part (``Q1:<part>``).
SEVERITY: Mapping[str, Severity] = MappingProxyType(
    {
        "E1": Severity.URGENT,
        "Q1:reconnect": Severity.URGENT,
        "Q1:no_weigh_in": Severity.INFO,
        "Q1:low_battery": Severity.INFO,
        "Q1:import_incomplete": Severity.INFO,
        **{
            rule: Severity.ATTENTION
            for rule in (
                *("E2", "E3", "E4", "E6", "B1", "B2", "B3", "B4", "B5", "B6"),
                *("N2:guided", "N2:routine", "N3", "N5", "C1", "C1x", "C2"),
            )
        },
    }
)


# --- results ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """A finding proposal: the rule fired on ``day``. ``window_*`` are the days its condition
    used, ending at ``day``; ``reference_day`` is the day its band was taken at (§2). For a
    trend-test rule (``SPECS[rule_id].persistent``) it is the evaluation's condition, and an
    episode opens only through ``advance_persistent``."""

    rule_id: str
    rule_version: int
    tier: Tier
    subject: str
    day: date
    window_start: date
    window_end: date
    reference_day: date
    confidence: Confidence
    severity: Severity
    evidence: dict[str, Any]
    data_quality: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NoFinding:
    rule_id: str
    subject: str
    day: date
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Insufficient:
    """Insufficient data (or a rule not available to the user): never a finding."""

    rule_id: str
    subject: str
    day: date
    reason: str
    shortfalls: tuple[str, ...] = ()
    data_quality: dict[str, Any] | None = None


type RuleResult = Finding | NoFinding | Insufficient


def insufficient_from(
    rule_id: str, subject: str, day: date, verdict: InsufficientData, what: str
) -> Insufficient:
    return Insufficient(
        rule_id,
        subject,
        day,
        reason=what,
        shortfalls=tuple(s.value for s in verdict.shortfalls),
        data_quality=verdict.data_quality.as_dict(),
    )


def make_finding(
    rule_id: str,
    subject: str,
    day: date,
    *,
    window_start: date,
    window_end: date,
    reference_day: date,
    confidence: Confidence,
    evidence: dict[str, Any],
    data_quality: DataQuality | None,
    trigger_groups: Iterable[int] = (),
) -> Finding:
    spec = SPECS[rule_id]
    return Finding(
        rule_id=rule_id,
        rule_version=spec.version,
        tier=spec.tier,
        subject=subject,
        day=day,
        window_start=window_start,
        window_end=window_end,
        reference_day=reference_day,
        confidence=confidence,
        severity=spec.severity,
        evidence={**evidence, "trigger_groups": sorted(set(trigger_groups))},
        data_quality={} if data_quality is None else data_quality.as_dict(),
    )
