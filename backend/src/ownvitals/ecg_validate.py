"""``ecg validate``: read one user's stored recordings and print the pure report.

This module is outside ``ownvitals.ecg`` because that package is a pure library (the purity
guard). It is the one operator module allowed to open a ``user_session`` for this command
(``tests/session_scan.py``). The context comes from the job issuer, the same way key rewrap
visits one owner at a time. Output is the report's lines only; a failure names the exception
type, never the message, so a database error cannot echo a row.
"""

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, TextIO
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from zstandard import ZstdError

from ownvitals.config import Settings
from ownvitals.db import (
    DatabaseRoleError,
    create_engine,
    create_session_factory,
    user_session,
    verify_database_role,
)
from ownvitals.ecg.validate_cli import (
    MeasureLink,
    OwnerAnswers,
    Recording,
    answers_from,
    evaluate,
)
from ownvitals.ingest.codec import SignalRejected, decode_signal
from ownvitals.jobs.issuer import owner_context

_ECG = text(
    "SELECT signal, payload_hash, sampling_hz, heart_rate, wearposition, afib_details, "
    "estimated, recorded_at, connection_id, model_id, reported, signalid "
    "FROM ecg_recordings WHERE owner_id = :owner "
    "ORDER BY recorded_at, signalid"
)
_LINKS = text(
    "SELECT g.measured_at, g.connection_id, g.hash_deviceid, g.model_id, g.raw, m.type "
    "FROM measure_groups AS g "
    "LEFT JOIN measures AS m ON m.owner_id = g.owner_id AND m.measure_group_id = g.id "
    "WHERE g.owner_id = :owner AND g.deleted_at IS NULL"
)


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _device_id(reported: object) -> str | None:
    if not isinstance(reported, dict):
        return None
    device = reported.get("deviceid")
    if isinstance(device, str) and device:
        return device
    raw = reported.get("raw")
    if isinstance(raw, dict):
        nested = raw.get("deviceid")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _details(value: object) -> int | None:
    if isinstance(value, dict):
        return _int(value.get("code"))
    return _int(value)


def _leads(signal: object) -> tuple[tuple[int, ...], ...] | None:
    if not isinstance(signal, bytes | bytearray | memoryview):
        return None
    try:
        decoded = decode_signal(bytes(signal))
    except (SignalRejected, ZstdError, TypeError, ValueError):
        return None
    return tuple(tuple(int(sample) for sample in lead) for lead in decoded)


def _estimated(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    return None


def _recording(row: Mapping[Any, Any]) -> Recording:
    digest = row["payload_hash"]
    recorded = row["recorded_at"]
    connection = row["connection_id"]
    if not isinstance(recorded, datetime) or not isinstance(connection, UUID):
        raise TypeError("stored recording shape")
    signalid = _int(row["signalid"])
    if signalid is None:
        raise TypeError("stored recording shape")
    return Recording(
        leads=_leads(row["signal"]),
        sampling_hz=_int(row["sampling_hz"]),
        heart_rate=_int(row["heart_rate"]),
        afib_details=_details(row["afib_details"]),
        wearposition=_int(row["wearposition"]),
        estimated=_estimated(row["estimated"]),
        payload_hash=None if digest is None else bytes(digest),
        signalid=signalid,
        recorded_at=recorded,
        connection=connection,
        model_id=_int(row["model_id"]),
        device_id=_device_id(row["reported"]),
    )


def _device_ids(hash_id: object, raw: object) -> frozenset[str]:
    found: set[str] = set()
    if isinstance(hash_id, str) and hash_id:
        found.add(hash_id)
    if isinstance(raw, dict):
        for key in ("deviceid", "hash_deviceid"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                found.add(value)
    return frozenset(found)


def _links(rows: Sequence[Mapping[Any, Any]]) -> tuple[MeasureLink, ...]:
    grouped: dict[tuple[object, ...], dict[str, Any]] = {}
    for row in rows:
        measured = row["measured_at"]
        connection = row["connection_id"]
        if not isinstance(measured, datetime) or not isinstance(connection, UUID):
            continue
        key = (measured, connection, row["hash_deviceid"], row["model_id"])
        bucket = grouped.get(key)
        if bucket is None:
            bucket = {
                "measured_at": measured,
                "connection": connection,
                "device_ids": set(),
                "model_id": _int(row["model_id"]),
                "types": set(),
            }
            grouped[key] = bucket
        bucket["device_ids"].update(_device_ids(row["hash_deviceid"], row["raw"]))
        kind = _int(row["type"])
        if kind is not None:
            bucket["types"].add(kind)
    return tuple(
        MeasureLink(
            measured_at=bucket["measured_at"],
            connection=bucket["connection"],
            device_ids=frozenset(bucket["device_ids"]),
            model_id=bucket["model_id"],
            types=frozenset(bucket["types"]),
        )
        for bucket in grouped.values()
    )


async def _load(
    db: AsyncSession, owner: UUID
) -> tuple[tuple[Recording, ...], tuple[MeasureLink, ...]]:
    recordings = (await db.execute(_ECG, {"owner": owner})).mappings().all()
    links = (await db.execute(_LINKS, {"owner": owner})).mappings().all()
    return tuple(_recording(row) for row in recordings), _links(links)


async def run(args: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO) -> int:
    """Read ``--user`` under forced RLS and print the categorical report. Exit 1 on a failure."""
    if args.action != "validate":
        print("error: unknown ecg action", file=err)
        return 2
    try:
        user_id = UUID(str(args.user))
    except ValueError:
        print("error: the user id is not a uuid", file=err)
        return 1
    answers = answers_from(args)
    if isinstance(answers, str):
        print(answers, file=err)
        return 1
    return await _report(settings, user_id, answers, out, err)


async def _report(
    settings: Settings, user_id: UUID, answers: OwnerAnswers, out: TextIO, err: TextIO
) -> int:
    engine = create_engine(settings)
    try:
        await verify_database_role(engine)
        factory = create_session_factory(engine)
        async with user_session(factory, owner_context(user_id)) as db:
            recordings, links = await _load(db, user_id)
        report = evaluate(recordings, links, answers)
        for line in report.lines:
            print(line, file=out)
        return 1 if report.failed else 0
    except DatabaseRoleError as refused:
        print(f"error: {refused}", file=err)
        return 1
    except Exception as exc:
        print(f"error: {type(exc).__name__}", file=err)
        return 1
    finally:
        await engine.dispose()
