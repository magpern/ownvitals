"""Row-to-view mapping for the one findings read function (``ownvitals.views.findings_read``).

Pure: a database row (a mapping) in, a typed view out. No session, no query and no table name:
the read function selects the rows and applies the experimental switch and the overlay; this
module only shapes what it selected (split from ``findings_read``, no behaviour change).
"""

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from ownvitals.ecg.overlay import GATED_RULES, GroupKey, Overlay, group_device_ids
from ownvitals.views.findings_models import (
    EXPERIMENTAL,
    EXPERIMENTAL_RULES,
    FindingView,
    GateView,
    Reading,
    area_of,
)


def is_experimental(tier: str, rule_id: str) -> bool:
    return tier == EXPERIMENTAL or rule_id in EXPERIMENTAL_RULES


def mapping_of(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}  # pyright: ignore[reportUnknownVariableType]


def int_of(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def group_key(row: Mapping[Any, Any]) -> GroupKey:
    types = row["types"] or ()
    return GroupKey(
        grpid=int(row["grpid"]),
        connection=row["connection_id"],
        measured_at=row["measured_at"],
        device_ids=group_device_ids(row["hash_deviceid"], row["raw"]),
        model_id=int_of(row["model_id"]),
        types=frozenset(int(t) for t in types),
    )


def finding_view(row: Mapping[Any, Any], overlay: Overlay, switch: bool) -> FindingView:
    rule_id = str(row["rule_id"])
    gated = overlay.applies and rule_id in GATED_RULES
    return FindingView(
        id=row["id"],
        rule_id=rule_id,
        rule_version=str(row["rule_version"]),
        tier=str(row["tier"]),
        area=area_of(rule_id),
        severity=str(row["severity"]),
        confidence=str(row["confidence"]),
        data_quality=mapping_of(row["data_quality"]),
        origin=str(row["origin"]),
        metric_keys=tuple(row["metric_keys"] or ()),
        subject=str(row["subject"]),
        window_start=row["window_start"],
        window_end=row["window_end"],
        reference_day=row["reference_day"],
        evidence=mapping_of(row["evidence"]),
        status=str(row["status"]),
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        ended_at=row["ended_at"],
        notify_state=str(row["notify_state"]),
        notified_at=row["notified_at"],
        seen_at=row["seen_at"],
        hidden=overlay.hidden,
        not_evaluated=gated and overlay.outcome == "not_evaluated",
        gate=(
            GateView(overlay.outcome, overlay.counted, overlay.failed)
            if switch and overlay.applies
            else None
        ),
    )


def readings_of(rows: Iterable[Mapping[Any, Any]]) -> tuple[Reading, ...]:
    return tuple(
        Reading(
            metric_key=str(row["metric_key"]),
            ts=row["ts"],
            value=Decimal(row["value"]),
            grpid=int_of(row["source_grpid"]),
            quality_flags=tuple(row["quality_flags"] or ()),
        )
        for row in rows
    )
