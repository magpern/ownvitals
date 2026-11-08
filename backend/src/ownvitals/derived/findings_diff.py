"""The pure diff of the rule evaluation (#187, ADR 0010 §4): turn the stored rows into the
computed ones as statement parameters.

Split from ``findings_store`` (no behaviour change; its docstring describes the diff rules).
No database, no clock: the caller passes the stored rows, the evaluation and today.
"""

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from uuid import UUID

from ownvitals.analysis.rules.core import Finding
from ownvitals.analysis.rules.episodes import origin
from ownvitals.derived.evaluate import (
    Q1_RULES,
    EpisodeRow,
    Evaluation,
    canonical,
    json_safe,
    rule_version,
)


def day_start(day: date) -> datetime:
    """The timestamp the pass stores for an evaluation day: 00:00 UTC of it."""
    return datetime.combine(day, time(), UTC)


# --- read --------------------------------------------------------------------------------------


def _document(value: Mapping[str, Any]) -> str:
    return json.dumps(json_safe(value), sort_keys=True)


def _params(row: EpisodeRow, owner: UUID, revision: int, digest: bytes) -> dict[str, Any]:
    return {
        "o": owner,
        "rule_id": row.rule_id,
        "rule_version": row.rule_version,
        "tier": row.tier,
        "severity": row.severity,
        "confidence": row.confidence,
        "data_quality": _document(row.data_quality),
        "metric_keys": list(row.metric_keys),
        "subject": row.subject,
        "window_start": row.window_start,
        "window_end": row.window_end,
        "reference_day": row.reference_day,
        "evidence": _document(row.evidence),
        "r": revision,
        "f": digest,
        "status": row.status,
        "first_seen": day_start(row.first_day),
        "last_seen": day_start(row.last_day),
        "ended_at": None if row.ended_day is None else day_start(row.ended_day),
        "origin": row.origin,
        "notify_state": "pending" if row.origin == "live" else "none",
    }


def content(row: Any) -> tuple[Any, ...]:
    """What a pass compares between a stored row and a computed one (``_params`` or a stored
    row): not the id, ``first_seen``, ``origin``, the notification state or provenance."""
    fields: Mapping[str, Any] = row if isinstance(row, dict) else row._mapping

    def document(value: Any) -> str:
        return canonical(json.loads(value) if isinstance(value, str) else value)

    return (
        fields["rule_version"],
        fields["tier"],
        fields["severity"],
        fields["confidence"],
        tuple(fields["metric_keys"]),
        fields["window_start"],
        fields["window_end"],
        fields["status"],
        fields["last_seen"],
        fields["ended_at"],
        document(fields["data_quality"]),
        document(fields["evidence"]),
    )


@dataclass(slots=True)
class _Diff:
    inserts: list[dict[str, Any]]
    ends: list[dict[str, Any]]  # updates of re-derived episodes that are now ended
    updates: list[dict[str, Any]]
    deletes: list[dict[str, Any]]
    closes: list[dict[str, Any]]  # Q1 episodes ended today (only status and ended_at change)


def _keep(stored: Any, computed: dict[str, Any]) -> dict[str, Any]:
    """The update of a matched episode: its id; ``last_seen`` never before its kept
    ``first_seen``."""
    params = dict(computed, id=stored.id)
    params["last_seen"] = max(params["last_seen"], stored.first_seen)
    return params


def _replayed(
    stored: list[Any], computed: Iterable[EpisodeRow], owner: UUID, r: int, f: bytes, diff: _Diff
) -> None:
    olds: dict[tuple[str, str, date], list[Any]] = defaultdict(list)
    for row in sorted(stored, key=lambda s: (s.first_seen, str(s.id))):
        olds[(row.rule_id, row.subject, row.reference_day)].append(row)
    news: dict[tuple[str, str, date], list[EpisodeRow]] = defaultdict(list)
    for row in computed:
        news[row.match_key].append(row)
    for key in sorted(set(olds) | set(news)):
        old, new = olds.get(key, []), sorted(news.get(key, []), key=lambda e: e.first_day)
        for stored_row, computed_row in zip(old, new, strict=False):
            params = _keep(stored_row, _params(computed_row, owner, r, f))
            if content(stored_row) != content(params):
                (diff.ends if params["status"] == "ended" else diff.updates).append(params)
        diff.deletes.extend({"o": owner, "id": s.id} for s in old[len(new) :])
        diff.inserts.extend(_params(e, owner, r, f) for e in new[len(old) :])


def _q1_row(finding: Finding, keys: tuple[str, ...], today: date) -> EpisodeRow:
    return EpisodeRow(
        rule_id=finding.rule_id,
        rule_version=rule_version(finding.rule_id),
        tier=finding.tier.value,
        severity=finding.severity.value,
        confidence=finding.confidence.value,
        subject=finding.subject,
        metric_keys=keys,
        reference_day=finding.reference_day,
        window_start=finding.window_start,
        window_end=finding.window_end,
        first_day=finding.day,
        last_day=finding.day,
        ended_day=None,
        origin=origin(finding.window_end, today).value,
        evidence=json_safe(finding.evidence),
        data_quality=json_safe(finding.data_quality),
    )


def _q1(
    stored: list[Any], evaluation: Evaluation, today: date, owner: UUID, r: int, f: bytes,
    diff: _Diff,
) -> None:  # fmt: skip
    active = {(s.rule_id, s.subject): s for s in stored if s.status == "active"}
    for result in evaluation.q1:
        old = active.pop((result.rule_id, result.subject), None)
        if result.finding is None:
            if old is not None:
                diff.closes.append(_ended(old, owner, today))
            continue
        row = _q1_row(result.finding, result.metric_keys, today)
        if old is None:
            diff.inserts.append(_params(row, owner, r, f))
            continue
        params = _keep(old, _params(row, owner, r, f))
        # advance_simple: the window grows, the reference day and origin stay.
        params["window_start"] = old.window_start
        params["window_end"] = max(old.window_end, row.window_end)
        if content(old) != content(params):
            diff.updates.append(params)
    for old in active.values():  # a subject no longer reported: the episode ends
        diff.closes.append(_ended(old, owner, today))


def _ended(old: Any, owner: UUID, today: date) -> dict[str, Any]:
    """An active Q1 episode ends today; the rest of the row stays as stored."""
    return {"o": owner, "id": old.id, "ended_at": max(day_start(today), old.last_seen)}


def diff_rows(
    stored: list[Any], evaluation: Evaluation, today: date, owner: UUID, r: int, f: bytes
) -> _Diff:
    """The statements that turn ``stored`` (this writer's rows) into ``evaluation``."""
    diff = _Diff([], [], [], [], [])
    q1 = set(Q1_RULES)
    _replayed(
        [s for s in stored if s.rule_id not in q1],
        evaluation.episodes,
        owner,
        r,
        f,
        diff,
    )
    _q1([s for s in stored if s.rule_id in q1], evaluation, today, owner, r, f, diff)
    return diff
