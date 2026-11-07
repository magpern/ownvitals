"""The probe's report (``probe.py``): the shapes it keeps and their text and JSON forms.

Only counts, codes from fixed sets or below fixed bounds (anything else is ``other``), field
names from the models' fixed lists and fixed categories: nothing else can reach the output.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel

HEADER: Final = "# ownvitals probe: shapes only (no values, timestamps or ids but the connection)"
FORMAT: Final = "ownvitals-probe-1"


ABSENT: Final = "absent"
_OTHER: Final = "other"


Code = int | str  # an integer code from the API, or ``absent``


# ------------------------------------------------------------------------------------ report


@dataclass
class MeasureShape:
    groups: int = 0
    pages: int = 0
    truncated: bool = False
    meastypes: Counter[Code] = field(default_factory=Counter)
    attrib: Counter[Code] = field(default_factory=Counter)
    category: Counter[Code] = field(default_factory=Counter)
    position: Counter[Code] = field(default_factory=Counter)
    group_fields: set[str] = field(default_factory=set)
    measure_fields: set[str] = field(default_factory=set)


@dataclass
class EcgShape:
    listed: int = 0
    pages: int = 0
    truncated: bool = False
    fetched: int = 0
    arrays: Counter[Code] = field(default_factory=Counter)  # arrays per recording
    lengths: Counter[Code] = field(default_factory=Counter)  # samples per array
    sampling_rate: Counter[Code] = field(default_factory=Counter)
    wearposition: Counter[Code] = field(default_factory=Counter)
    list_fields: set[str] = field(default_factory=set)
    signal_fields: set[str] = field(default_factory=set)


@dataclass
class NotifyShape:
    subscriptions: int = 0
    appli: Counter[Code] = field(default_factory=Counter)
    fields: set[str] = field(default_factory=set)


@dataclass
class Report:
    """Everything the probe prints. Only counts, codes, field names and fixed categories."""

    connection: UUID
    grant: str = "stored"  # how the access token was obtained (a refresh outcome)
    stopped: str | None = None
    calls: dict[str, str] = field(default_factory=dict)
    capabilities: dict[str, str] = field(default_factory=dict)
    measures: MeasureShape = field(default_factory=MeasureShape)
    ecg: EcgShape = field(default_factory=EcgShape)
    notify: NotifyShape = field(default_factory=NotifyShape)

    @property
    def outcome(self) -> str:
        return "complete" if self.stopped is None else f"stopped {self.stopped}"


def _code(
    value: object, *, known: frozenset[int] | None = None, at_most: int | None = None
) -> Code:
    """An integer code as sent when it is in ``known`` or in ``0..at_most``; ``absent`` when not
    sent; anything else (a non-integer, an undocumented or out-of-bound value) is ``other``."""
    if value is None:
        return ABSENT
    if isinstance(value, bool) or not isinstance(value, int):
        return _OTHER
    if known is not None and value in known:
        return value
    if at_most is not None and 0 <= value <= at_most:
        return value
    return _OTHER


def _names(model: BaseModel) -> set[str]:
    """The names of the fields ``model`` carries. Only the model's own field names (the public
    spec's) are printed; every unknown key is ``other``, never its text."""
    found = {
        name
        for name in type(model).model_fields
        if name != "raw" and getattr(model, name) is not None
    }
    if getattr(model, "raw", None):
        found.add(_OTHER)
    return found


def _int_arrays(raw: dict[str, Any]) -> list[int]:
    """Lengths of unknown fields that are arrays of integers (another lead, if one is sent)."""
    return [
        len(value)
        for value in raw.values()
        if isinstance(value, list)
        and value
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ]


# ---------------------------------------------------------------------------------- output


def _sorted_counts(counter: Counter[Code]) -> list[tuple[Code, int]]:
    ints = sorted((k, v) for k, v in counter.items() if isinstance(k, int))
    names = sorted((k, v) for k, v in counter.items() if isinstance(k, str))
    return [*ints, *names]


def as_dict(report: Report) -> dict[str, Any]:
    """The report as JSON-ready data (``--json``); the same content as the text."""

    def counts(counter: Counter[Code]) -> dict[str, int]:
        return {str(key): int(total) for key, total in _sorted_counts(counter)}

    ms, ecg, notify = report.measures, report.ecg, report.notify
    return {
        "format": FORMAT,
        "connection": str(report.connection),
        "grant": report.grant,
        "outcome": report.outcome,
        "calls": dict(sorted(report.calls.items())),
        "capabilities": dict(sorted(report.capabilities.items())),
        "measures": {
            "groups": ms.groups,
            "pages": ms.pages,
            "truncated": ms.truncated,
            "meastypes": counts(ms.meastypes),
            "attrib": counts(ms.attrib),
            "category": counts(ms.category),
            "position": counts(ms.position),
            "group_fields": sorted(ms.group_fields),
            "measure_fields": sorted(ms.measure_fields),
        },
        "ecg": {
            "listed": ecg.listed,
            "pages": ecg.pages,
            "truncated": ecg.truncated,
            "fetched": ecg.fetched,
            "arrays": counts(ecg.arrays),
            "lengths": counts(ecg.lengths),
            "sampling_rate": counts(ecg.sampling_rate),
            "wearposition": counts(ecg.wearposition),
            "list_fields": sorted(ecg.list_fields),
            "signal_fields": sorted(ecg.signal_fields),
        },
        "notify": {
            "subscriptions": notify.subscriptions,
            "appli": counts(notify.appli),
            "fields": sorted(notify.fields),
        },
    }


def render_json(report: Report) -> str:
    return json.dumps(as_dict(report), indent=2, sort_keys=False)


def render_text(report: Report) -> str:
    """Plain lines the owner pastes into the issue."""
    data = as_dict(report)
    lines = [
        HEADER,
        f"connection {data['connection']}",
        f"grant {data['grant']}",
        f"outcome {data['outcome']}",
    ]
    lines += [f"call {label} {result}" for label, result in data["calls"].items()]
    lines += [f"capability {stream} {state}" for stream, state in data["capabilities"].items()]
    for section in ("measures", "ecg", "notify"):
        for key, value in data[section].items():
            if isinstance(value, bool):
                lines.append(f"{section} {key} {'yes' if value else 'no'}")
            elif isinstance(value, int):
                lines.append(f"{section} {key} {value}")
            elif isinstance(value, list):
                lines.append(f"{section} {key} {','.join(value) if value else 'none'}")
            elif not value:
                lines.append(f"{section} {key} none")
            else:
                lines += [f"{section} {key} {code} count {n}" for code, n in value.items()]
    return "\n".join(lines)
