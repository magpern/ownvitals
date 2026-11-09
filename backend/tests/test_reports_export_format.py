"""The export's row formats and archive stream (ADR 0013 sections 1 and 4; #211). Pure: no
database. Synthetic values only."""

import csv
import hashlib
import io
import json
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from ownvitals.reports.export_archive import Archive, TooLarge
from ownvitals.reports.export_files import estimate_rows, overlay_state
from ownvitals.reports.export_format import csv_cell, csv_lines, json_row, plain, real_value
from ownvitals.reports.tables import CHUNK_MAX_BYTES

SYNTHETIC = True


@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
def test_a_text_cell_with_a_formula_lead_is_prefixed(lead: str) -> None:
    assert csv_cell(f"{lead}cmd|' /C calc'!A0") == f"'{lead}cmd|' /C calc'!A0"


@pytest.mark.parametrize("value", [-5, -0.25, 0, 3, Decimal("-1.50"), 1e-9])
def test_numbers_are_never_prefixed(value: object) -> None:
    cell = csv_cell(value)
    assert isinstance(cell, int | float)
    assert not str(cell).startswith("'")


def test_nested_values_and_other_types() -> None:
    assert csv_cell(None) == ""
    assert csv_cell(True) == "true"
    assert csv_cell({"b": 1, "a": "=x"}) == '{"a": "=x", "b": 1}'
    assert csv_cell(["-1"]) == '["-1"]'
    assert csv_cell(UUID(int=7)) == "00000000-0000-0000-0000-000000000007"
    assert csv_cell(date(2026, 1, 2)) == "2026-01-02"
    zone = timezone(timedelta(hours=2))
    assert plain(datetime(2026, 1, 2, 12, tzinfo=zone)) == "2026-01-02T10:00:00+00:00"
    with pytest.raises(TypeError):
        plain(b"\x5a")


def test_csv_round_trips() -> None:
    rows = [("=a", -1, None, "plain, with comma", '"quoted"', "line\nbreak")]
    parsed = list(csv.reader(io.StringIO(csv_lines(rows))))
    assert parsed == [["'=a", "-1", "", "plain, with comma", '"quoted"', "line\nbreak"]]


def test_json_rows_are_canonical() -> None:
    row = {"b": Decimal("1.5"), "a": UUID(int=1), "c": [date(2026, 1, 1)], "é": "ü"}
    assert json_row(row) == (
        '{"a":"00000000-0000-0000-0000-000000000001","b":1.5,"c":["2026-01-01"],"é":"ü"}'
    )


def test_the_real_value() -> None:
    assert real_value(7215, -2) == 72.15
    assert real_value(5, 3) == 5000.0
    assert real_value(None, -2) is None
    assert real_value(True, 0) is None


def test_the_overlay_states() -> None:
    assert overlay_state(False, False) == "shown"
    assert overlay_state(True, False) == "hidden_gate_failed"
    assert overlay_state(False, True) == "not_evaluated"


def test_an_empty_estimate_is_left_out() -> None:
    from ownvitals.ecg.overlay import EMPTY_ESTIMATE

    assert estimate_rows({UUID(int=1): EMPTY_ESTIMATE}) == []


def test_the_archive_streams_in_chunks_and_unzips() -> None:
    archive = Archive(max_bytes=1 << 30)
    chunks: list[bytes] = []
    archive.begin("data/big.json", "big")
    # Incompressible-ish synthetic text so the stream passes several chunks.
    for n in range(40):
        archive.write(json.dumps([f"{n}-{i}-{i * 7919 % 104729}" for i in range(20000)]), rows=1)
        chunks.extend(archive.take())
    archive.end()
    archive.close()
    chunks.extend(archive.take(final=True))
    assert len(chunks) >= 2
    assert all(len(c) == CHUNK_MAX_BYTES for c in chunks[:-1])
    assert 0 < len(chunks[-1]) <= CHUNK_MAX_BYTES
    body = b"".join(chunks)
    assert len(body) == archive.size
    assert hashlib.sha256(body).digest() == archive.sha256
    with zipfile.ZipFile(io.BytesIO(body)) as opened:
        assert opened.testzip() is None
        assert opened.namelist() == ["data/big.json"]
    entry = archive.files[0]
    assert entry.rows == 40
    assert entry.sha256 == hashlib.sha256(opened_bytes(body, "data/big.json")).hexdigest()


def opened_bytes(body: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(body)) as opened:
        return opened.read(name)


def test_over_the_limit_names_the_largest_part() -> None:
    archive = Archive(max_bytes=4096)
    archive.begin("data/small.json", "small")
    archive.write("[]")
    archive.end()
    archive.begin("data/ecg_signal.json", "ecg_signal")
    noise = "".join(hashlib.sha256(str(n).encode()).hexdigest() for n in range(1000))
    with pytest.raises(TooLarge) as raised:
        archive.write(noise)
    assert raised.value.largest == "ecg_signal"
    raised.value.archive.discard()
    assert archive.take(final=True) == []


def test_entries_are_one_at_a_time() -> None:
    archive = Archive(max_bytes=1 << 20)
    archive.begin("a.json", "a")
    with pytest.raises(RuntimeError):
        archive.begin("b.json", "b")
    with pytest.raises(ValueError, match="a file stem"):
        Archive(max_bytes=1 << 20).begin("x.json", "Not-A-Stem")
