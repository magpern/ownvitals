"""How one exported row becomes JSON and CSV (ADR 0013 section 1; #211). Pure: no I/O.

- **JSON**: one object per row, UTF-8, canonical key order (sorted), no insignificant space.
  UUIDs, dates and times are strings (times in UTC, ISO 8601); numbers stay numbers; ``jsonb``
  and arrays stay nested.
- **CSV**: the same values flattened, RFC 4180 (``\\r\\n``), a nested value as its JSON text in one
  cell, ``None`` as an empty cell, booleans as ``true``/``false``.
- **CSV injection** (OWASP): a text cell that begins with ``=``, ``+``, ``-``, ``@``, a tab or a
  carriage return is prefixed with ``'``. Numeric values are written as numbers and never
  prefixed, so a negative number stays a number. Text from Withings (device names, time-zone
  strings) is user-adjacent input and gets the same rule.
"""

import csv
import io
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

INJECTION_LEADS: Final = frozenset("=+-@\t\r")


def real_value(value_raw: object, unit: object) -> float | None:
    """Withings' real value: ``value_raw * 10 ** unit`` (findings section 1)."""
    if not isinstance(value_raw, int) or isinstance(value_raw, bool):
        return None
    if not isinstance(unit, int) or isinstance(unit, bool):
        return None
    return float(Decimal(value_raw).scaleb(unit))


def plain(value: Any) -> Any:
    """A value as JSON holds it: strings for UUIDs, dates and times; floats for decimals."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list | tuple | frozenset | set):
        return [plain(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, bytes | bytearray | memoryview):
        raise TypeError("binary values are never exported")
    raise TypeError(f"no export form for {type(value).__name__}")


def json_row(row: Mapping[str, Any]) -> str:
    return json.dumps(plain(row), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def json_text(value: Any) -> str:
    """A whole JSON document (the manifest, the account object), canonical and indented."""
    return json.dumps(plain(value), sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def csv_cell(value: Any) -> str | int | float:
    """One CSV cell: numbers as numbers, everything else as text with the injection rule."""
    shown = plain(value)
    if shown is None:
        return ""
    if isinstance(shown, bool):
        return "true" if shown else "false"
    if isinstance(shown, int | float):
        return shown
    text = (
        shown if isinstance(shown, str) else json.dumps(shown, sort_keys=True, ensure_ascii=False)
    )
    if text and text[0] in INJECTION_LEADS:
        return "'" + text
    return text


def csv_lines(rows: Iterable[Sequence[Any]]) -> str:
    """CSV text of ``rows`` (each already a sequence of raw values in column order)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    for row in rows:
        writer.writerow([csv_cell(value) for value in row])
    return out.getvalue()


def csv_header(columns: Sequence[str]) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerow(list(columns))
    return out.getvalue()
