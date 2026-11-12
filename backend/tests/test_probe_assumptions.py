"""The assumption checker over the probe's JSON (#450). Synthetic reports only: every count,
code and id below is made up (synthetic: true), and no test touches a network or a database."""

from __future__ import annotations

import io
import json
from collections import Counter
from typing import Any
from uuid import UUID

import pytest

from ownvitals.withings import probe_assumptions as pa
from ownvitals.withings.probe_assumptions_cli import main
from ownvitals.withings.probe_report import FORMAT, Report, as_dict

SYNTHETIC_CONNECTION = UUID("00000000-0000-4000-8000-00000000c0de")


def good_report() -> Report:
    """A synthetic account that agrees with every documented reading."""
    report = Report(connection=SYNTHETIC_CONNECTION)
    report.calls = {"measure getmeas": "ok", "v2/heart list": "ok"}
    m = report.measures
    m.groups, m.pages, m.truncated = 40, 1, False
    m.meastypes = Counter({1: 10, 158: 3, 159: 3, 135: 2, 136: 2, 137: 2, 138: 2, 130: 2})
    m.attrib = Counter({0: 30, 1: 5, 15: 5})
    e = report.ecg
    e.listed, e.fetched = 2, 2
    e.arrays = Counter({1: 2})
    e.sampling_rate = Counter({300: 2})
    e.lengths = Counter({9000: 2})  # 30 s at 300 Hz
    e.wearposition = Counter({"absent": 2})
    return report


def verdicts(report: Report | dict[str, Any]) -> dict[str, str]:
    data = as_dict(report) if isinstance(report, Report) else report
    return {c.id: c.verdict for c in pa.evaluate(data)}


def test_format_constant_matches_the_probe() -> None:
    assert pa.FORMAT == FORMAT


def test_a_consistent_synthetic_account_passes() -> None:
    got = verdicts(good_report())
    for check_id in (
        "hosts_token_endpoint",
        "attrib_documented",
        "n5_foot_pairs",
        "ecg_a3_rate",
        "ecg_a5_one_array",
        "ecg_a6_strip_30s",
        "ecg_a7_groups",
    ):
        assert got[check_id] == pa.PASS, check_id
    assert got["attrib_1_ambiguous"] == pa.MANUAL
    assert got["attrib_15_guided"] == pa.MANUAL
    assert got["ecg_a2_lead"] == pa.NO_DATA  # wearposition only ever absent
    assert got["source_tz_iana"] == pa.MANUAL
    assert set(got.values()) <= set(pa.VERDICTS)
    assert len(got) == len(pa.CHECKS)


@pytest.mark.parametrize(
    ("change", "check_id", "verdict"),
    [
        (lambda r: setattr(r.ecg, "sampling_rate", Counter({250: 2})), "ecg_a3_rate", pa.FAIL),
        (lambda r: setattr(r.ecg, "sampling_rate", Counter({"other": 2})), "ecg_a3_rate", pa.FAIL),
        (lambda r: setattr(r.ecg, "arrays", Counter({1: 1, 3: 1})), "ecg_a5_one_array", pa.FAIL),
        (
            lambda r: setattr(r.ecg, "lengths", Counter({9000: 1, 3000: 1})),
            "ecg_a6_strip_30s",
            pa.FAIL,
        ),
        (
            lambda r: setattr(r.ecg, "sampling_rate", Counter({300: 1, 500: 1})),
            "ecg_a6_strip_30s",
            pa.MANUAL,
        ),
        (lambda r: r.measures.meastypes.pop(138), "ecg_a7_groups", pa.FAIL),
        (lambda r: r.measures.meastypes.pop(130), "ecg_a7_groups", pa.FAIL),
        (lambda r: setattr(r.measures, "truncated", True), "ecg_a7_groups", pa.MANUAL),
        (lambda r: r.measures.meastypes.pop(159), "n5_foot_pairs", pa.FAIL),
        (lambda r: r.measures.meastypes.update({197: 1}), "n5_foot_pairs", pa.FAIL),
        (lambda r: r.measures.attrib.update({"other": 1}), "attrib_documented", pa.FAIL),
        (lambda r: r.measures.attrib.pop(15), "attrib_15_guided", pa.NO_DATA),
        (lambda r: setattr(r, "stopped", "rate_limited"), "hosts_token_endpoint", pa.FAIL),
        (
            lambda r: setattr(r, "calls", {"measure getmeas": "transient timeout"}),
            "hosts_token_endpoint",
            pa.FAIL,
        ),
        (lambda r: setattr(r, "calls", {}), "hosts_token_endpoint", pa.NO_DATA),
        (lambda r: setattr(r.ecg, "wearposition", Counter({3: 2})), "ecg_a2_lead", pa.MANUAL),
    ],
)
def test_each_contradiction_is_reported(change: Any, check_id: str, verdict: str) -> None:
    report = good_report()
    change(report)
    assert verdicts(report)[check_id] == verdict


def test_an_empty_account_has_no_data() -> None:
    got = verdicts(Report(connection=SYNTHETIC_CONNECTION))
    for check_id in (
        "attrib_documented",
        "n5_foot_pairs",
        "ecg_a3_rate",
        "ecg_a5_one_array",
        "ecg_a6_strip_30s",
        "ecg_a7_groups",
        "ecg_a2_lead",
        "hosts_token_endpoint",
    ):
        assert got[check_id] == pa.NO_DATA, check_id


@pytest.mark.parametrize(
    "data",
    [None, [], "text", {}, {"format": "ownvitals-probe-2"}, {"format": None}],
)
def test_anything_but_a_probe_report_is_refused(data: object) -> None:
    with pytest.raises(pa.NotAProbeReport):
        pa.evaluate(data)


def test_malformed_sections_do_not_crash() -> None:
    data = {
        "format": FORMAT,
        "calls": [],
        "measures": "x",
        "ecg": {"listed": True, "sampling_rate": {"300": "many", "500": -1}, "arrays": None},
    }
    got = verdicts(data)
    assert got["ecg_a3_rate"] == pa.NO_DATA
    assert got["ecg_a7_groups"] == pa.NO_DATA  # a boolean is not a count


def sentinel_report() -> dict[str, Any]:
    """Every value the probe could print is a sentinel that must never reach the output."""
    report = good_report()
    report.measures.meastypes.update({987001: 987002})
    report.measures.attrib.update({987003: 987004})
    report.measures.group_fields = {"sentinelgroupfield"}
    report.measures.measure_fields = {"sentinelmeasurefield"}
    report.ecg.list_fields = {"sentinellistfield"}
    report.ecg.lengths = Counter({9000: 987005})
    report.calls["v2/sentinel endpoint"] = "permanent sentinelcategory api_status 987006"
    report.capabilities = {"sentinelstream": "sentinelstate"}
    report.notify.fields = {"sentinelnotifyfield"}
    return as_dict(report)


SENTINELS = ("987", "sentinel", str(SYNTHETIC_CONNECTION), "c0de", "9000")


@pytest.mark.parametrize("as_json", [False, True])
def test_no_input_value_reaches_the_output(as_json: bool) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(["--json"] if as_json else [], io.StringIO(json.dumps(sentinel_report())), out, err)
    assert code == 0
    text = out.getvalue() + err.getvalue()
    for sentinel in SENTINELS:
        assert sentinel not in text, sentinel
    if as_json:
        assert json.loads(out.getvalue())["format"] == "ownvitals-assumptions-1"
    else:
        lines = out.getvalue().splitlines()
        assert lines[0].startswith("# ownvitals assumptions")
        assert all(line.startswith("assumption ") for line in lines[1:])


def test_cli_exit_codes_and_refusal_text() -> None:
    out, err = io.StringIO(), io.StringIO()
    assert main([], io.StringIO(json.dumps(as_dict(good_report()))), out, err) == 0
    out, err = io.StringIO(), io.StringIO()
    assert main([], io.StringIO('{"sentinel": 987'), out, err) == 2
    assert err.getvalue() == "error: not an ownvitals probe report\n"
    assert out.getvalue() == ""
    big = io.StringIO(" " * (2_000_000))
    assert main([], big, io.StringIO(), err := io.StringIO()) == 2
    assert "sentinel" not in err.getvalue()
