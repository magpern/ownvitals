"""The assumption checker over the probe's JSON (#450). Synthetic reports only: every count,
code and id below is made up (synthetic: true), and no test touches a network or a database."""

from __future__ import annotations

import io
import json
import random
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


# The whole output vocabulary: each check's id with every (verdict, note) it may print. A line
# outside it would be a value from the input, or a new note that needs this review.
VOCABULARY: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("hosts_token_endpoint", pa.NO_DATA, "no call was made"),
        ("hosts_token_endpoint", pa.FAIL, "the probe stopped; see its outcome line"),
        ("hosts_token_endpoint", pa.PASS, "the API answered on the listed hosts"),
        ("hosts_token_endpoint", pa.FAIL, "no call answered ok"),
        ("attrib_documented", pa.FAIL, "an undocumented attrib code was seen"),
        ("attrib_documented", pa.NO_DATA, "no measure group"),
        ("attrib_documented", pa.PASS, "every attrib code is a documented one"),
        ("attrib_1_ambiguous", pa.NO_DATA, "the code does not occur"),
        (
            "attrib_1_ambiguous",
            pa.MANUAL,
            "the code occurs; confirm it means a device reading that may be another person's",
        ),
        ("attrib_15_guided", pa.NO_DATA, "the code does not occur"),
        ("attrib_15_guided", pa.MANUAL, "the code occurs; confirm it means a guided measurement"),
        ("n5_foot_pairs", pa.NO_DATA, "no per-foot measure type"),
        ("n5_foot_pairs", pa.PASS, "each foot type comes with its partner, equal counts"),
        ("n5_foot_pairs", pa.FAIL, "a foot type has no partner or a different count"),
        ("ecg_a2_lead", pa.NO_DATA, "no wear position sent"),
        ("ecg_a2_lead", pa.MANUAL, "a wear position is sent; the lead label needs OC2"),
        ("ecg_a3_rate", pa.NO_DATA, "no ECG signal fetched"),
        ("ecg_a3_rate", pa.FAIL, "a sampling rate other than 300 or 500 Hz"),
        ("ecg_a3_rate", pa.PASS, "every sampling rate is 300 or 500 Hz"),
        ("ecg_a5_one_array", pa.NO_DATA, "no ECG signal fetched"),
        ("ecg_a5_one_array", pa.FAIL, "a recording with more or fewer than one array"),
        ("ecg_a5_one_array", pa.PASS, "every recording has one array"),
        ("ecg_a6_strip_30s", pa.NO_DATA, "no ECG signal fetched"),
        ("ecg_a6_strip_30s", pa.MANUAL, "rates and lengths cannot be paired by shape"),
        ("ecg_a6_strip_30s", pa.PASS, "every strip lasts 25 to 35 s"),
        ("ecg_a6_strip_30s", pa.FAIL, "a strip outside 25 to 35 s"),
        ("ecg_a7_groups", pa.NO_DATA, "no ECG listed"),
        ("ecg_a7_groups", pa.MANUAL, "measure groups were cut at the page limit"),
        ("ecg_a7_groups", pa.PASS, "135 to 138 have equal counts and 130 occurs (OC5)"),
        ("ecg_a7_groups", pa.FAIL, "135 to 138 are missing, unequal, or 130 is missing"),
        ("source_tz_iana", pa.MANUAL, "local database check: time zones parse as IANA names"),
        ("refresh_status_2553", pa.MANUAL, "only a refused refresh token shows its status"),
        ("provider_plan_signal", pa.MANUAL, "only an account without Withings+ shows the signal"),
        (
            "sleep_summary_fields",
            pa.MANUAL,
            "local database check: stored sleep summary field names",
        ),
        ("battery_enum", pa.MANUAL, "local database check: device battery values"),
        ("new_grant_drops_old", pa.MANUAL, "watch the production connection after a local connect"),
        ("webhook_form", pa.MANUAL, "needs a public callback; not run on the local stack"),
        ("getmeas_deletions", pa.MANUAL, "delete a test reading in the app, then sync"),
    }
)

# Digits str.isdigit() accepts but are not ASCII: superscript two (int() refuses it), Arabic-Indic
# one and fullwidth five (int() reads them as 1 and 5).
NON_ASCII_DIGITS = (chr(0xB2), chr(0x661), chr(0xFF15))
# Keys and values a malformed or hostile report could hold: real codes, sentinels, non-ASCII
# digits and non-counts.
FUZZ_KEYS: tuple[str, ...] = (
    "0", "1", "15", "130", "135", "136", "137", "138", "158", "159", "197", "198", "300", "500",
    "9000", "3000", "absent", "other", "987001", "sentinel", "-1", "1.5", "", " 1", "1234567",
    *NON_ASCII_DIGITS,
)  # fmt: skip
FUZZ_VALUES: tuple[object, ...] = (1, 2, 3, 987002, 0, -1, True, None, "many", 1.5, [], {})
SECTION_KEYS = ("meastypes", "attrib", "sampling_rate", "arrays", "lengths", "wearposition")


def fuzz_report(rng: random.Random) -> dict[str, Any]:
    def counts() -> object:
        if rng.random() < 0.1:
            return rng.choice(FUZZ_VALUES)
        keys = rng.sample(FUZZ_KEYS, rng.randint(0, 6))
        return {k: rng.choice(FUZZ_VALUES) for k in keys}

    def section() -> object:
        if rng.random() < 0.1:
            return rng.choice(FUZZ_VALUES)
        data: dict[str, object] = {k: counts() for k in SECTION_KEYS if rng.random() < 0.7}
        data["truncated"] = rng.choice((True, False, None, "sentinel"))
        data["listed"] = rng.choice(FUZZ_VALUES)
        return data

    results = ("ok", "sentinel 987", 1)
    calls = {f"sentinel {i}": rng.choice(results) for i in range(rng.randint(0, 3))}
    return {
        "format": FORMAT,
        "outcome": rng.choice(("complete", "sentinel", None)),
        "calls": calls if rng.random() < 0.9 else "sentinel",
        "measures": section(),
        "ecg": section(),
    }


def test_every_output_line_is_from_the_fixed_vocabulary() -> None:
    rng = random.Random(452)  # noqa: S311 - seeded test data, not a secret: the same synthetic reports on every run
    lines = {f"assumption {i} {v} {n}" for i, v, n in VOCABULARY}
    seen: set[tuple[str, str, str]] = set()
    reports = [as_dict(good_report()), as_dict(Report(connection=SYNTHETIC_CONNECTION))]
    reports += [fuzz_report(rng) for _ in range(3000)]
    for data in reports:
        checks = pa.evaluate(data)
        assert [c.id for c in checks] == [c.id for c in pa.evaluate(reports[0])]
        rendered = pa.render(checks).splitlines()
        assert rendered[0].startswith("# ownvitals assumptions")
        assert set(rendered[1:]) <= lines, set(rendered[1:]) - lines
        for item in pa.as_dict(checks)["checks"]:
            assert set(item) == {"id", "verdict", "note"}
            assert (item["id"], item["verdict"], item["note"]) in VOCABULARY, item
            seen.add((item["id"], item["verdict"], item["note"]))
    assert len(seen) > len(VOCABULARY) // 2  # the fuzzing reaches most outcomes


@pytest.mark.parametrize("code", NON_ASCII_DIGITS)
def test_a_non_ascii_digit_code_is_undocumented_not_a_crash(code: str) -> None:
    data = as_dict(good_report())
    data["measures"]["attrib"] = {"0": 3, code: 2}
    assert verdicts(data)["attrib_documented"] == pa.FAIL
    out, err = io.StringIO(), io.StringIO()
    assert main([], io.StringIO(json.dumps(data)), out, err) == 1
    assert code not in out.getvalue() + err.getvalue()


class _Unreadable(io.StringIO):
    def read(self, size: int | None = -1, /) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "sentinel 987")


def _boom(_report: object) -> list[pa.Check]:
    raise RuntimeError("sentinel 987 from the input")


@pytest.mark.parametrize(
    ("stdin", "evaluate"),
    [
        (io.StringIO("[" * 200_000), None),  # json.loads: RecursionError, not ValueError
        (_Unreadable(), None),
        (io.StringIO(json.dumps(as_dict(good_report()))), _boom),
    ],
)
def test_any_failure_prints_only_the_fixed_message(
    monkeypatch: pytest.MonkeyPatch, stdin: io.StringIO, evaluate: Any
) -> None:
    if evaluate is not None:
        monkeypatch.setattr("ownvitals.withings.probe_assumptions_cli.evaluate", evaluate)
    out, err = io.StringIO(), io.StringIO()
    assert main([], stdin, out, err) == 2
    assert err.getvalue() == "error: not an ownvitals probe report\n"
    assert out.getvalue() == ""
