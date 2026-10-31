"""What the Withings fake's data endpoints do with a ``SyntheticDataset``: filters, pagination,
``lastupdate`` and ``data_fields`` semantics, and the mutations tests apply between reads.

Synthetic data only, standard library only. Each ``query`` function takes the request parameters
(strings, as sent) and returns the response ``body``; invalid parameters raise ``InvalidParams``
(the fake answers status 503). Documented behaviour from the public OpenAPI file: ``more`` is
an integer for ``getmeas`` and a boolean elsewhere, ``offset`` continues a page, ``lastupdate``
replaces the date range, ``getintradayactivity`` returns at most 24 h from ``startdate``, and
``sleep get`` returns only the series asked for in ``data_fields``. Pagination is index based
over the current result, so a deletion or a backdated edit between two pages skips or repeats
rows, as it would against the real API.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from withingsdata import SyntheticDataset

DAY = 86_400
ACTIVITY_BASE = (
    "date", "timezone", "deviceid", "hash_deviceid", "brand", "is_tracker", "modified", "model",
    "modelid",
)  # fmt: skip
INTRADAY_BASE = ("deviceid", "model", "model_id")
SLEEP_SERIES_FIELDS = (
    "hr", "rr", "snoring", "sdnn_1", "rmssd", "hrv_quality", "mvt_score",
    "chest_movement_rate", "withings_index", "breathing_sounds",
)  # fmt: skip


class InvalidParams(Exception):
    """A request parameter is missing or malformed (the fake answers status 503)."""


def _int(p: dict[str, str], name: str, *, required: bool = False) -> int | None:
    raw = p.get(name)
    if raw in (None, ""):
        if required:
            raise InvalidParams(f"{name} is required")
        return None
    try:
        return int(str(raw))
    except ValueError:
        raise InvalidParams(f"{name} must be an integer") from None


def _ints(raw: str | None) -> set[int]:
    try:
        return {int(x) for x in (raw or "").split(",") if x.strip()}
    except ValueError:
        raise InvalidParams("a list of integers is malformed") from None


def _ymd(p: dict[str, str], name: str) -> str | None:
    raw = p.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        raise InvalidParams(f"{name} must be YYYY-MM-DD") from None


def _fields(p: dict[str, str]) -> set[str] | None:
    raw = p.get("data_fields")
    return {f.strip() for f in raw.split(",") if f.strip()} if raw else None


def _modified(record: dict[str, Any]) -> int:
    return record.get("modified", record.get("created", record.get("date", 0)))


def _page(rows: list[Any], p: dict[str, str], page_size: int) -> tuple[list[Any], bool, int]:
    offset = _int(p, "offset") or 0
    if offset < 0:
        raise InvalidParams("offset must not be negative")
    chunk = rows[offset : offset + page_size]
    more = offset + page_size < len(rows)
    return chunk, more, offset + page_size if more else 0


def _by_day_or_update(rows: list[dict[str, Any]], p: dict[str, str]) -> list[dict[str, Any]]:
    """``startdateymd``/``enddateymd`` on ``date``, or ``lastupdate`` on ``modified``."""
    last = _int(p, "lastupdate")
    if last is not None:
        return [r for r in rows if _modified(r) > last]
    start, end = _ymd(p, "startdateymd"), _ymd(p, "enddateymd")
    if start is None and end is None:
        raise InvalidParams("startdateymd and enddateymd, or lastupdate, are required")
    if (start is None) != (end is None):
        raise InvalidParams("startdateymd and enddateymd go together")
    return [r for r in rows if start <= r.get("date", "") <= end]  # type: ignore[operator]


# ------------------------------------------------------------------------------------ queries


def getmeas(
    data: SyntheticDataset, p: dict[str, str], *, page_size: int, now: int
) -> dict[str, Any]:
    types = _ints(p.get("meastypes")) | _ints(p.get("meastype"))
    category = _int(p, "category")
    last, start, end = _int(p, "lastupdate"), _int(p, "startdate"), _int(p, "enddate")
    rows = []
    for group in data.measuregrps:
        if category is not None and group.get("category") != category:
            continue
        if last is not None:
            if _modified(group) <= last:
                continue
        elif (start is not None and group["date"] < start) or (
            end is not None and group["date"] > end
        ):
            continue
        measures = [m for m in group["measures"] if not types or m["type"] in types]
        if measures or not types:
            rows.append({**group, "measures": measures})
    rows.sort(key=lambda g: (-g["date"], -g["grpid"]))
    chunk, more, offset = _page(rows, p, page_size)
    return {
        "updatetime": now,
        "timezone": data.timezone,
        "measuregrps": chunk,
        "more": int(more),
        "offset": offset,
    }


def heart_list(
    data: SyntheticDataset, p: dict[str, str], *, page_size: int, now: int
) -> dict[str, Any]:
    start, end = _int(p, "startdate"), _int(p, "enddate")
    rows = [
        r["list"]
        for r in data.ecg
        if (start is None or r["list"]["timestamp"] >= start)
        and (end is None or r["list"]["timestamp"] <= end)
    ]
    rows.sort(key=lambda e: -e["timestamp"])
    chunk, more, offset = _page(rows, p, page_size)
    return {"series": chunk, "more": more, "offset": offset}


def heart_get(
    data: SyntheticDataset, p: dict[str, str], *, page_size: int, now: int
) -> dict[str, Any]:
    signal_id = _int(p, "signalid", required=True)
    for record in data.ecg:
        if record["list"].get("ecg", {}).get("signalid") == signal_id:
            return dict(record["signal"])
    raise InvalidParams("no such signalid")


def getactivity(
    data: SyntheticDataset, p: dict[str, str], *, page_size: int, now: int
) -> dict[str, Any]:
    rows = sorted(_by_day_or_update(data.activities, p), key=lambda r: r["date"])
    fields = _fields(p)
    chunk, more, offset = _page(rows, p, page_size)
    if fields is not None:
        keep = set(ACTIVITY_BASE) | fields
        chunk = [{k: v for k, v in r.items() if k in keep} for r in chunk]
    return {"activities": chunk, "more": more, "offset": offset}


def getworkouts(
    data: SyntheticDataset, p: dict[str, str], *, page_size: int, now: int
) -> dict[str, Any]:
    rows = sorted(_by_day_or_update(data.workouts, p), key=lambda r: r["startdate"])
    fields = _fields(p)
    chunk, more, offset = _page(rows, p, page_size)
    if fields is not None:
        chunk = [
            {**r, "data": {k: v for k, v in r.get("data", {}).items() if k in fields}}
            for r in chunk
        ]
    return {"series": chunk, "more": more, "offset": offset}


def getintradayactivity(
    data: SyntheticDataset, p: dict[str, str], *, page_size: int, now: int
) -> dict[str, Any]:
    start, end = _int(p, "startdate"), _int(p, "enddate")
    if start is None and end is None and data.intraday:
        end = max(data.intraday) + 1
    if start is None:
        start = (end or 0) - DAY
    end = start + DAY if end is None else min(end, start + DAY)  # at most 24 h after startdate
    fields = _fields(p)
    series = {}
    for ts in sorted(data.intraday):
        if start <= ts <= end:
            sample = data.intraday[ts]
            if fields is not None:
                sample = {k: v for k, v in sample.items() if k in fields or k in INTRADAY_BASE}
            series[str(ts)] = sample
    return {"series": series}


def sleep_get(
    data: SyntheticDataset, p: dict[str, str], *, page_size: int, now: int
) -> dict[str, Any]:
    start = _int(p, "startdate", required=True) or 0
    end = _int(p, "enddate", required=True) or 0
    end = min(end, start + DAY)  # a longer window returns the first 24 h only
    fields = _fields(p) or set()
    segments: list[dict[str, Any]] = []
    model = 0
    for body in data.sleep_series:
        model = body.get("model", model)
        for segment in body["series"]:
            if segment["startdate"] < end and segment["enddate"] > start:
                out = {k: v for k, v in segment.items() if k not in SLEEP_SERIES_FIELDS}
                for name in SLEEP_SERIES_FIELDS:
                    if name in fields and name in segment:
                        out[name] = {
                            ts: v for ts, v in segment[name].items() if start <= int(ts) < end
                        }
                segments.append(out)
    return {"model": model, "series": segments}


def sleep_getsummary(
    data: SyntheticDataset, p: dict[str, str], *, page_size: int, now: int
) -> dict[str, Any]:
    rows = sorted(_by_day_or_update(data.sleep_summaries, p), key=lambda r: r["startdate"])
    fields = _fields(p)
    chunk, more, offset = _page(rows, p, page_size)
    if fields is not None:
        chunk = [{**r, "data": {k: v for k, v in r["data"].items() if k in fields}} for r in chunk]
    return {"series": chunk, "more": more, "offset": offset}


def getdevice(
    data: SyntheticDataset, p: dict[str, str], *, page_size: int, now: int
) -> dict[str, Any]:
    return {"devices": list(data.devices)}


def getgoals(
    data: SyntheticDataset, p: dict[str, str], *, page_size: int, now: int
) -> dict[str, Any]:
    return {"goals": dict(data.goals)}


# ----------------------------------------------------------------------- mutations for tests


def find_group(data: SyntheticDataset, grpid: int) -> dict[str, Any]:
    for group in data.measuregrps:
        if group["grpid"] == grpid:
            return group
    raise KeyError(grpid)


def add_group(data: SyntheticDataset, group: dict[str, Any], *, now: int) -> dict[str, Any]:
    """Add a measure group as the API would after a sync: ``created`` and ``modified`` are
    ``now`` whatever ``date`` is, so a backdated ``date`` is found by ``lastupdate`` only."""
    group = {**group, "created": now, "modified": now}
    group.setdefault("category", 1)
    group.setdefault("attrib", 0)
    if not group.get("grpid"):
        group["grpid"] = max((g["grpid"] for g in data.measuregrps), default=100_000) + 1
    data.measuregrps.append(group)
    return group


def edit_group(
    data: SyntheticDataset,
    grpid: int,
    *,
    now: int,
    measures: list[dict[str, Any]] | None = None,
    attrib: int | None = None,
    date: int | None = None,
) -> dict[str, Any]:
    """Edit a group in place (a user correction): ``modified`` becomes ``now``."""
    group = find_group(data, grpid)
    if measures is not None:
        group["measures"] = measures
    if attrib is not None:
        group["attrib"] = attrib
    if date is not None:
        group["date"] = date
    group["modified"] = now
    return group


def delete_group(data: SyntheticDataset, grpid: int) -> None:
    """Delete a group. The API reports no deletions (findings section 5, question 3): the group
    simply stops being returned, which only a reconciliation notices."""
    data.measuregrps.remove(find_group(data, grpid))
    data.deleted_grpids.append(grpid)


# endpoint -> (store function, any one of these scopes). The public docs map scopes to
# notification applis, not to endpoints: this table is assumed from those and the scope texts.
DATA_ROUTES: dict[tuple[str, str], tuple[Callable[..., dict[str, Any]], tuple[str, ...]]] = {
    ("/measure", "getmeas"): (getmeas, ("user.metrics",)),
    ("/v2/heart", "list"): (heart_list, ("user.metrics",)),
    ("/v2/heart", "get"): (heart_get, ("user.metrics",)),
    ("/v2/measure", "getactivity"): (getactivity, ("user.activity",)),
    ("/v2/measure", "getintradayactivity"): (getintradayactivity, ("user.activity",)),
    ("/v2/measure", "getworkouts"): (getworkouts, ("user.activity",)),
    ("/v2/sleep", "get"): (sleep_get, ("user.activity",)),
    ("/v2/sleep", "getsummary"): (sleep_getsummary, ("user.activity",)),
    ("/v2/user", "getdevice"): (getdevice, ("user.info",)),
    ("/v2/user", "getgoals"): (getgoals, ("user.info", "user.metrics")),
}
