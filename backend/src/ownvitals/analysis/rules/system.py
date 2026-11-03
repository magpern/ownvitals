"""System findings Q1 (ADR 0010 §7; roadmap Amendment 3; PLAN-operations §9.4). Pure.

Tier ``system``; confidence ``high`` (they state what Withings reported or what state the
connection is in). One rule per part: ``Q1:no_weigh_in`` (user), ``Q1:low_battery`` (device),
``Q1:reconnect`` (connection, urgent) and ``Q1:import_incomplete`` (stream). The inputs are plain
dataclasses of the states the caller read, so the rules stay pure; ``today`` is an argument.

Every part ends on its first non-firing evaluation (``advance_simple``). A part that cannot apply
(no weigh-in ever, a capability that is not ``supported``) is a ``NoFinding``: missing data is
never read as a problem, and the text of a finding states a data state, never a nudge.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from ownvitals.analysis.quality import UNCONFIRMED_ATTRIB, Reading
from ownvitals.analysis.rules.core import (
    SPECS,
    Confidence,
    Finding,
    NoFinding,
    RuleResult,
)

NO_WEIGH_IN_DAYS = 14
SUPPORTED = "supported"  # the capability states are PLAN §4.2's
LOW_BATTERY = "low"  # `user getdevice` battery enum: high, medium, low (§3)
NEEDS_RECONNECT = "needs_reconnect"  # connections.status (PLAN §4.3)


@dataclass(frozen=True, slots=True)
class DeviceState:
    """One device row of the latest ``getdevice``. ``battery`` is Withings' enum or ``None``."""

    hash_deviceid: str
    battery: str | None


@dataclass(frozen=True, slots=True)
class ConnectionState:
    """The connection's status (PLAN §4.3): ``connected``, ``needs_reconnect``, ``disconnected``."""

    status: str


@dataclass(frozen=True, slots=True)
class StreamState:
    """A stream's capability (PLAN §4.2: ``supported``, ``unsupported``, ``not_authorized``,
    ``not_available``, ``unknown``; ``None`` when none is recorded) and its per-stream completeness
    (§4.4): ``complete`` says the backfill range is covered, ``importing`` that it is running."""

    stream: str
    capability: str | None
    complete: bool
    importing: bool


def _finding(
    part: str, subject: str, today: date, *, start: date, reference: date, state: dict[str, Any]
) -> Finding:
    rule_id = f"Q1:{part}"
    spec = SPECS[rule_id]
    return Finding(
        rule_id=rule_id,
        rule_version=spec.version,
        tier=spec.tier,
        subject=subject,
        day=today,
        window_start=start,
        window_end=today,
        reference_day=reference,
        confidence=Confidence.HIGH,
        severity=spec.severity,
        evidence={"state": state, "trigger_groups": []},
        data_quality={"state": state},
    )


def q1_no_weigh_in(
    weight: Iterable[Reading], weight_capability: str | None, today: date
) -> RuleResult:
    """No usable weigh-in for 14 days: the user has had one, the ``measures`` capability is
    ``supported`` and ``(today - last usable weight day).days >= 14``. ``weight`` are the usable
    readings (an unconfirmed ``attrib`` 1 reading never counts, even if passed). The reference day
    is the last weigh-in, so the episode is stable while it lasts."""
    if weight_capability != SUPPORTED:
        return NoFinding("Q1:no_weigh_in", "user", today, "measures capability not supported")
    days = [r.local_date for r in weight if r.attrib != UNCONFIRMED_ATTRIB]
    if not days:
        return NoFinding("Q1:no_weigh_in", "user", today, "no usable weigh-in yet")
    last = max(days)
    elapsed = (today - last).days
    if elapsed < NO_WEIGH_IN_DAYS:
        return NoFinding("Q1:no_weigh_in", "user", today, "weighed in within 14 days")
    return _finding(
        "no_weigh_in",
        "user",
        today,
        start=last,
        reference=last,
        state={
            "capability": weight_capability,
            "last_weigh_in": last.isoformat(),
            "days_since": elapsed,
            "threshold_days": NO_WEIGH_IN_DAYS,
        },
    )


def q1_low_battery(device: DeviceState, today: date) -> RuleResult:
    """Low battery, per device: ``battery == "low"`` exactly; ``medium``, ``high``, a missing field
    and an undocumented value do not fire. Subject: the device's ``hash_deviceid``."""
    if device.battery != LOW_BATTERY:
        return NoFinding("Q1:low_battery", device.hash_deviceid, today, "battery not low")
    return _finding(
        "low_battery",
        device.hash_deviceid,
        today,
        start=today,
        reference=today,
        state={"hash_deviceid": device.hash_deviceid, "battery": device.battery},
    )


def q1_reconnect(connection: ConnectionState, today: date) -> RuleResult:
    """The connection is ``needs_reconnect`` (urgent). Subject: ``connection``."""
    if connection.status != NEEDS_RECONNECT:
        return NoFinding("Q1:reconnect", "connection", today, "connection does not need renewing")
    return _finding(
        "reconnect",
        "connection",
        today,
        start=today,
        reference=today,
        state={"status": connection.status},
    )


def q1_import_incomplete(stream: StreamState, today: date) -> RuleResult:
    """Import incomplete, per stream: the capability is ``supported``, the import is not complete
    and not running. A missing capability or a stream that is not supported never fires, and an
    import in progress is not "incomplete". Subject: the stream."""
    if stream.capability != SUPPORTED:
        return NoFinding("Q1:import_incomplete", stream.stream, today, "stream not supported")
    if stream.complete or stream.importing:
        return NoFinding("Q1:import_incomplete", stream.stream, today, "complete or importing")
    return _finding(
        "import_incomplete",
        stream.stream,
        today,
        start=today,
        reference=today,
        state={
            "stream": stream.stream,
            "capability": stream.capability,
            "complete": stream.complete,
            "importing": stream.importing,
        },
    )
