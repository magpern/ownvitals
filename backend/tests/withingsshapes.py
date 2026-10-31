"""The documented response shapes of the public Withings API, as a tiny validator.

Hand-written from the public OpenAPI file (``developer.withings.com/openapi.yaml``) and
``docs/withings-findings.md`` section 2; used by the contract tests to hold the fake (and the
synthetic fixtures) to what the API documents. ``validate(value, shape)`` returns the list of
violations. Unknown extra fields are tolerated, as the client must tolerate them; with
``strict=False`` unknown enum values and missing fields are tolerated too (the quirk datasets).

Where the public file contradicts itself or reality, the shape accepts both and says so:
``updatetime`` (string in the file, an integer on the wire), ``series`` of ``sleep get`` (an
object in the file, a list of segments on the wire) and ``night_events`` (array or dictionary).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

INT, STR, BOOL, NUM = "int", "str", "bool", "num"


@dataclass(frozen=True)
class Opt:
    shape: Any


@dataclass(frozen=True)
class Enum:
    values: frozenset[Any]

    def __init__(self, values: Any) -> None:
        object.__setattr__(self, "values", frozenset(values))


@dataclass(frozen=True)
class Map:
    shape: Any


@dataclass(frozen=True)
class Either:
    shapes: tuple[Any, ...]

    def __init__(self, *shapes: Any) -> None:
        object.__setattr__(self, "shapes", shapes)


_TYPES = {
    INT: lambda v: isinstance(v, int) and not isinstance(v, bool),
    NUM: lambda v: isinstance(v, int | float) and not isinstance(v, bool),
    STR: lambda v: isinstance(v, str),
    BOOL: lambda v: isinstance(v, bool),
}


def validate(value: Any, shape: Any, path: str = "$", *, strict: bool = True) -> list[str]:
    if isinstance(shape, Opt):
        return validate(value, shape.shape, path, strict=strict)
    if isinstance(shape, Either):
        attempts = [validate(value, s, path, strict=strict) for s in shape.shapes]
        return [] if any(not a for a in attempts) else attempts[0]
    if isinstance(shape, Enum):
        ok = value in shape.values or (not strict and isinstance(value, int | str))
        return [] if ok else [f"{path}: {value!r} is not one of {sorted(shape.values, key=str)}"]
    if isinstance(shape, str):
        return [] if _TYPES[shape](value) else [f"{path}: expected {shape}, got {value!r:.40}"]
    if isinstance(shape, Map):
        if not isinstance(value, dict):
            return [f"{path}: expected an object"]
        return [
            e
            for k, v in value.items()
            for e in validate(v, shape.shape, f"{path}.{k}", strict=strict)
        ]
    if isinstance(shape, list):
        if not isinstance(value, list):
            return [f"{path}: expected a list"]
        return [
            e
            for i, v in enumerate(value)
            for e in validate(v, shape[0], f"{path}[{i}]", strict=strict)
        ]
    if not isinstance(value, dict):
        return [f"{path}: expected an object"]
    errors: list[str] = []
    for name, sub in shape.items():
        if name not in value:
            if strict and not isinstance(sub, Opt):
                errors.append(f"{path}.{name}: missing")
            continue
        errors += validate(value[name], sub, f"{path}.{name}", strict=strict)
    return errors


def envelope(body: Any) -> dict[str, Any]:
    return {"status": INT, "body": body}


ATTRIB = (0, 1, 2, 4, 5, 7, 8, 15)
POSITIONS = range(32)  # measure position enum 0..31
MEASURE = {
    "value": INT, "type": INT, "unit": INT,
    "algo": Opt(INT), "fm": Opt(INT), "position": Opt(Enum(POSITIONS)),
}  # fmt: skip
MEASUREGRP = {
    "grpid": INT, "attrib": Enum(ATTRIB), "date": INT, "created": INT, "modified": INT,
    "category": Enum({1, 2}), "measures": [MEASURE], "deviceid": Opt(STR),
    "hash_deviceid": Opt(STR), "model": Opt(STR), "model_id": Opt(INT), "comment": Opt(STR),
}  # fmt: skip
AFIB_DETAILS = Enum({-1, 0, 1, 2, 5, 6, 7, 9, 10, 11, 12, 13})
ECG_ENTRY = {
    "deviceid": Opt(STR), "model": Enum({44, 91}),
    "ecg": {"signalid": INT, "afib": Enum({0, 1, 2}), "afib_details": Opt(AFIB_DETAILS)},
    "bloodpressure": Opt({"diastole": INT, "systole": INT}), "heart_rate": INT,
    "modified": INT, "timestamp": INT,
}  # fmt: skip
ACTIVITY = {
    "date": STR, "timezone": STR, "deviceid": STR, "hash_deviceid": STR, "brand": INT,
    "is_tracker": BOOL, "modified": INT, "model": Opt(STR), "modelid": Opt(INT), "steps": INT,
    "distance": NUM, "elevation": NUM, "soft": INT, "moderate": INT, "intense": INT, "active": INT,
    "calories": NUM, "totalcalories": NUM, "hr_average": INT, "hr_min": INT, "hr_max": INT,
    "hr_zone_0": INT, "hr_zone_1": INT, "hr_zone_2": INT, "hr_zone_3": INT,
}  # fmt: skip
SEGMENT = {
    "startdate": INT, "enddate": INT, "state": Enum({0, 1, 2, 3, 4, 5, 15}),
    "model": Opt(STR), "model_id": Opt(INT),
    **dict.fromkeys(("hr", "rr", "snoring", "sdnn_1", "rmssd"), Opt(Map(INT))),
}  # fmt: skip
SLEEP_DATA = Map(Either(NUM, Map(Either(NUM, [NUM])), [NUM]))  # incl. night_events (array or map)
DEVICE_TYPES = (
    "Scale", "Babyphone", "Blood Pressure Monitor", "Activity Tracker", "Sleep Monitor",
    "Smart Connected Thermometer", "Gateway", "Smart Meter", "HealthKit Apple",
    "HealthKit Google", "HealthKit Huawei", "Lingo", "Fluid Analyzer",
)  # fmt: skip
PROFILE = {"appli": INT, "callbackurl": STR, "comment": STR}

# endpoint key (``path:action``) -> shape of the response ``body``
BODIES: dict[str, Any] = {
    "v2/oauth2:requesttoken": {
        "userid": INT,
        "access_token": STR,
        "refresh_token": STR,
        "expires_in": INT,
        "scope": STR,
        "token_type": STR,
        "csrf_token": Opt(STR),
    },
    "v2/oauth2:getdemoaccess": {"access_token": STR, "refresh_token": STR, "expires_in": INT},
    "v2/oauth2:revoke": {},
    "v2/signature:getnonce": {"nonce": STR},
    "measure:getmeas": {
        "updatetime": Either(INT, STR),
        "timezone": STR,
        "measuregrps": [MEASUREGRP],
        "more": INT,
        "offset": INT,
    },
    "v2/heart:list": {"series": [ECG_ENTRY], "more": BOOL, "offset": INT},
    "v2/heart:get": {
        "signal": [INT],
        "sampling_frequency": INT,
        "wearposition": Enum(POSITIONS),
        "model": Enum({44, 91}),
        "heart_rate": Opt({"grpid": INT, "value": INT, "date": INT, "is_deleted": BOOL}),
    },
    "v2/measure:getactivity": {"activities": [ACTIVITY], "more": BOOL, "offset": INT},
    "v2/measure:getintradayactivity": {
        "series": Map({"deviceid": STR, "model": STR, "model_id": INT, "heart_rate": Opt(INT)})
    },
    "v2/measure:getworkouts": {
        "series": [
            {
                "id": INT,
                "category": INT,
                "timezone": STR,
                "model": INT,
                "attrib": INT,
                "startdate": INT,
                "enddate": INT,
                "date": STR,
                "modified": INT,
                "deviceid": STR,
                "data": Map(NUM),
            }
        ],
        "more": BOOL,
        "offset": INT,
    },
    "v2/sleep:get": {"model": INT, "series": Either([SEGMENT], SEGMENT)},
    "v2/sleep:getsummary": {
        "series": [
            {
                "id": INT,
                "timezone": STR,
                "model": Enum({16, 32}),
                "model_id": INT,
                "startdate": INT,
                "enddate": INT,
                "date": STR,
                "created": INT,
                "modified": INT,
                "hash_deviceid": STR,
                "completed": BOOL,
                "data": SLEEP_DATA,
            }
        ],
        "more": BOOL,
        "offset": INT,
    },
    "v2/user:getdevice": {
        "devices": [
            {
                "type": Enum(DEVICE_TYPES),
                "model": STR,
                "model_id": INT,
                "battery": Enum({"high", "medium", "low"}),
                "deviceid": STR,
                "hash_deviceid": STR,
                "timezone": STR,
                "first_session_date": INT,
                "last_session_date": INT,
            }
        ]
    },
    "v2/user:getgoals": {
        "goals": {"steps": INT, "sleep": INT, "weight": {"value": INT, "unit": INT}}
    },
    "notify:subscribe": {},
    "notify:update": {},
    "notify:revoke": {},
    "notify:list": {"profiles": [PROFILE]},
    "notify:get": PROFILE,
}


def validate_response(endpoint: str, payload: Any, *, strict: bool = True) -> list[str]:
    """Violations of the documented envelope and body shape of ``endpoint``'s answer."""
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), int):
        return ["$: not an envelope with an integer status"]
    if payload["status"] != 0:  # errors: an integer status, optionally error text and body
        return validate(
            payload, {"error": Opt(STR), "body": Opt(Map(Either(NUM, STR)))}, strict=False
        )
    return validate(payload, envelope(BODIES[endpoint]), strict=strict)
