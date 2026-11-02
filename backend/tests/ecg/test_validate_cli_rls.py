"""The owner command reads one user under forced RLS. Synthetic rows only; no real account."""

import argparse
import io
import json
import logging
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from jobtools import engine_for, ensure_user
from ownvitals.config import Settings
from ownvitals.ecg_validate import run
from ownvitals.ingest.codec import frame_signal

WHEN = datetime(2024, 7, 4, 4, 5, 6, tzinfo=UTC)
OURS = {
    "signalid": 867530986,
    "heart": 135791357,
    "wear": 8642,
    "rate": 3210,
    "sample": 918273645,
    "device": "canary-device-token",
    "zone": "Canary/Zone",
    "reason": "canary-reason-zz",
}
THEIRS = {
    "signalid": 1123581321,
    "heart": 975318642,
    "wear": 7531,
    "rate": 4321,
    "sample": 564738291,
    "device": "other-canary-device",
    "zone": "Other/Canary",
    "reason": "other-canary-reason",
}


def _account(database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    owner, connection = uuid.uuid4(), uuid.uuid4()
    with engine_for(database_url) as engine, engine.begin() as conn:
        ensure_user(conn, owner)
        conn.execute(
            text(
                "INSERT INTO connections (id, owner_id, provider, status, public) "
                "VALUES (:id, :owner, 'withings', 'needs_reconnect', '{}')"
            ),
            {"id": connection, "owner": owner},
        )
    return owner, connection


def _insert(
    database_url: str, owner: uuid.UUID, connection: uuid.UUID, marker: dict[str, object]
) -> None:
    sample = marker["sample"]
    reason = marker["reason"]
    assert isinstance(sample, int)
    assert isinstance(reason, str)
    frame, digest = frame_signal([[sample, -2, 3]])
    estimated = {
        "hr_bpm": 654.3,
        "rpeaks": [11111.11],
        "hrv_reasons": [reason],
        "sqi": {"result": "pass", "reasons": [], "windows": [{"amplitude_mv": 9.8765}]},
    }
    with engine_for(database_url) as engine, engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ecg_recordings (owner_id, connection_id, signalid, recorded_at, "
                "source_tz, heart_rate, sampling_hz, wearposition, signal, payload_hash, "
                "reported, estimated, afib_details) VALUES ("
                ":owner, :connection, :signalid, :at, :zone, :heart, :rate, :wear, :signal, "
                ":hash, CAST(:reported AS jsonb), CAST(:estimated AS jsonb), "
                "CAST(:details AS jsonb))"
            ),
            {
                "owner": owner,
                "connection": connection,
                "signalid": marker["signalid"],
                "at": WHEN,
                "zone": marker["zone"],
                "heart": marker["heart"],
                "rate": marker["rate"],
                "wear": marker["wear"],
                "signal": frame,
                "hash": digest,
                "reported": json.dumps({"deviceid": marker["device"], "raw": {}}),
                "estimated": json.dumps(estimated),
                "details": json.dumps({"code": 44}),
            },
        )


async def test_another_user_is_invisible_and_nothing_identifying_is_printed(
    schema: None,
    database_url: str,
    app_database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owner, connection = _account(database_url)
    other, other_connection = _account(database_url)
    _insert(database_url, owner, connection, OURS)
    _insert(database_url, other, other_connection, THEIRS)
    parser_args = argparse.Namespace(
        action="validate",
        user=str(owner),
        oc1=None,
        oc2=None,
        v4_wrong_beats=None,
        v4_inspected_beats=None,
        v4_justified=None,
    )
    out, err = io.StringIO(), io.StringIO()
    caplog.set_level(logging.DEBUG)
    before = len(caplog.records)
    code = await run(
        parser_args,
        Settings(database_url=app_database_url),  # pyright: ignore[reportArgumentType]
        out,
        err,
    )
    logged = "".join(record.getMessage() for record in caplog.records[before:])
    printed = out.getvalue() + err.getvalue() + logged
    assert "recordings 1\n" in out.getvalue()
    assert code in (0, 1)
    assert "error:" not in err.getvalue()
    for marker in (OURS, THEIRS):
        for key in ("signalid", "heart", "wear", "rate", "sample", "device", "zone", "reason"):
            assert str(marker[key]) not in printed
    for needle in ("654.3", "11111.11", "9.8765", "2024-07-04", "04:05:06", str(owner), str(other)):
        assert needle not in printed
