"""The documented Withings assumptions, checked against the probe's JSON (#450). Pure.

``docs/status.md`` lists the readings agents built on while no real account was available; the
owner's local session (``docs/dev/local-real-account.md``) settles them. This module reads the
probe's ``--json`` report (``probe_report.as_dict``, format ``ownvitals-probe-1``) and gives each
assumption one verdict:

- ``pass``: the shapes agree with the reading;
- ``fail``: the shapes contradict it (the code that relies on it needs a look);
- ``no_data``: the account has nothing the check could look at;
- ``manual``: the probe's shapes cannot settle it; the runbook names the local check.

The output holds only the fixed assumption ids, the fixed verdicts and fixed notes from this
module, never a value read from the input: not a count, a code, a field name or the connection
id (``tests/test_probe_assumptions.py`` feeds sentinel values and checks none comes out). A
verdict is not INTEGRATION_VERIFIED; the owner reads the results and decides.

No I/O, clock or randomness: ``probe_assumptions_cli.py`` reads the report from stdin.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

FORMAT: Final = "ownvitals-probe-1"  # probe_report.FORMAT, repeated so this module stays pure

PASS: Final = "pass"  # noqa: S105 - a verdict, not a password
FAIL: Final = "fail"
NO_DATA: Final = "no_data"
MANUAL: Final = "manual"
VERDICTS: Final = (PASS, FAIL, NO_DATA, MANUAL)

ECG_RATES: Final = frozenset({300, 500})  # ECG A3
STRIP_SECONDS: Final = (25.0, 35.0)  # ECG A6: "about 30 s"
FOOT_PAIRS: Final = ((158, 159), (197, 198))  # N5
ECG_INTERVALS: Final = (135, 136, 137, 138)  # ECG A7
AFIB_RESULT: Final = 130


class NotAProbeReport(ValueError):
    """The input is not the probe's JSON report. The message is fixed, never the input."""

    def __init__(self) -> None:
        super().__init__("not an ownvitals probe report")


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    verdict: str
    note: str


def _section(report: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = report.get(name)
    return value if isinstance(value, Mapping) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _codes(section: Mapping[str, Any], key: str) -> tuple[dict[int, int], bool]:
    """Integer codes with positive counts, and whether a non-integer code (``other``) was seen.
    ``absent`` (the field was not sent) is neither."""
    raw = section.get(key)
    found: dict[int, int] = {}
    other = False
    if not isinstance(raw, Mapping):
        return found, other
    for code, count in raw.items():
        n = _int(count)
        if not n:
            continue
        # ASCII only: str.isdigit() also takes superscripts and other scripts' digits, which
        # int() may refuse (and its error quotes the key).
        if isinstance(code, str) and code.isascii() and code.isdigit() and len(code) <= 6:
            found[int(code)] = n
        elif code != "absent":
            other = True
    return found, other


def _hosts(report: Mapping[str, Any]) -> Check:
    calls = _section(report, "calls")
    outcome = report.get("outcome")
    if not calls:
        return Check("hosts_token_endpoint", NO_DATA, "no call was made")
    if outcome != "complete":
        return Check("hosts_token_endpoint", FAIL, "the probe stopped; see its outcome line")
    if any(result == "ok" for result in calls.values()):
        return Check("hosts_token_endpoint", PASS, "the API answered on the listed hosts")
    return Check("hosts_token_endpoint", FAIL, "no call answered ok")


def _attrib_codes(report: Mapping[str, Any]) -> Check:
    codes, other = _codes(_section(report, "measures"), "attrib")
    if other:
        return Check("attrib_documented", FAIL, "an undocumented attrib code was seen")
    if not codes:
        return Check("attrib_documented", NO_DATA, "no measure group")
    return Check("attrib_documented", PASS, "every attrib code is a documented one")


def _attrib(code: int, check_id: str, meaning: str) -> Callable[[Mapping[str, Any]], Check]:
    def check(report: Mapping[str, Any]) -> Check:
        codes, _ = _codes(_section(report, "measures"), "attrib")
        if code not in codes:
            return Check(check_id, NO_DATA, "the code does not occur")
        return Check(check_id, MANUAL, f"the code occurs; confirm it means {meaning}")

    return check


def _foot(report: Mapping[str, Any]) -> Check:
    types, _ = _codes(_section(report, "measures"), "meastypes")
    seen = [(types.get(a, 0), types.get(b, 0)) for a, b in FOOT_PAIRS]
    if not any(left or right for left, right in seen):
        return Check("n5_foot_pairs", NO_DATA, "no per-foot measure type")
    if all(left == right for left, right in seen):
        return Check("n5_foot_pairs", PASS, "each foot type comes with its partner, equal counts")
    return Check("n5_foot_pairs", FAIL, "a foot type has no partner or a different count")


def _rates(report: Mapping[str, Any]) -> tuple[dict[int, int], bool]:
    return _codes(_section(report, "ecg"), "sampling_rate")


def _ecg_a3(report: Mapping[str, Any]) -> Check:
    rates, other = _rates(report)
    if not rates and not other:
        return Check("ecg_a3_rate", NO_DATA, "no ECG signal fetched")
    if other or not set(rates) <= ECG_RATES:
        return Check("ecg_a3_rate", FAIL, "a sampling rate other than 300 or 500 Hz")
    return Check("ecg_a3_rate", PASS, "every sampling rate is 300 or 500 Hz")


def _ecg_a5(report: Mapping[str, Any]) -> Check:
    arrays, other = _codes(_section(report, "ecg"), "arrays")
    if not arrays and not other:
        return Check("ecg_a5_one_array", NO_DATA, "no ECG signal fetched")
    if other or set(arrays) != {1}:
        return Check("ecg_a5_one_array", FAIL, "a recording with more or fewer than one array")
    return Check("ecg_a5_one_array", PASS, "every recording has one array")


def _ecg_a6(report: Mapping[str, Any]) -> Check:
    rates, rate_other = _rates(report)
    lengths, length_other = _codes(_section(report, "ecg"), "lengths")
    if not lengths and not length_other:
        return Check("ecg_a6_strip_30s", NO_DATA, "no ECG signal fetched")
    if rate_other or length_other or len(rates) != 1 or 0 in rates:
        return Check("ecg_a6_strip_30s", MANUAL, "rates and lengths cannot be paired by shape")
    (rate,) = rates
    low, high = STRIP_SECONDS
    if all(low <= length / rate <= high for length in lengths):
        return Check("ecg_a6_strip_30s", PASS, "every strip lasts 25 to 35 s")
    return Check("ecg_a6_strip_30s", FAIL, "a strip outside 25 to 35 s")


def _ecg_a7(report: Mapping[str, Any]) -> Check:
    if not _int(_section(report, "ecg").get("listed")):
        return Check("ecg_a7_groups", NO_DATA, "no ECG listed")
    measures = _section(report, "measures")
    if measures.get("truncated") is not False:
        return Check("ecg_a7_groups", MANUAL, "measure groups were cut at the page limit")
    types, _ = _codes(measures, "meastypes")
    counts = {types.get(t, 0) for t in ECG_INTERVALS}
    if len(counts) == 1 and 0 not in counts and AFIB_RESULT in types:
        return Check("ecg_a7_groups", PASS, "135 to 138 have equal counts and 130 occurs (OC5)")
    return Check("ecg_a7_groups", FAIL, "135 to 138 are missing, unequal, or 130 is missing")


def _ecg_a2(report: Mapping[str, Any]) -> Check:
    positions, other = _codes(_section(report, "ecg"), "wearposition")
    if not positions and not other:
        return Check("ecg_a2_lead", NO_DATA, "no wear position sent")
    return Check("ecg_a2_lead", MANUAL, "a wear position is sent; the lead label needs OC2")


def _manual(check_id: str, note: str) -> Callable[[Mapping[str, Any]], Check]:
    return lambda _report: Check(check_id, MANUAL, note)


CHECKS: Final[tuple[Callable[[Mapping[str, Any]], Check], ...]] = (
    _hosts,
    _attrib_codes,
    _attrib(1, "attrib_1_ambiguous", "a device reading that may be another person's"),
    _attrib(15, "attrib_15_guided", "a guided measurement"),
    _foot,
    _ecg_a2,
    _ecg_a3,
    _ecg_a5,
    _ecg_a6,
    _ecg_a7,
    _manual("source_tz_iana", "local database check: time zones parse as IANA names"),
    _manual("refresh_status_2553", "only a refused refresh token shows its status"),
    _manual("provider_plan_signal", "only an account without Withings+ shows the signal"),
    _manual("sleep_summary_fields", "local database check: stored sleep summary field names"),
    _manual("battery_enum", "local database check: device battery values"),
    _manual("new_grant_drops_old", "watch the production connection after a local connect"),
    _manual("webhook_form", "needs a public callback; not run on the local stack"),
    _manual("getmeas_deletions", "delete a test reading in the app, then sync"),
)


def evaluate(report: object) -> list[Check]:
    """Every check, in a fixed order. Refuses anything but the probe's JSON report."""
    if not isinstance(report, Mapping) or report.get("format") != FORMAT:
        raise NotAProbeReport
    return [check(report) for check in CHECKS]


def render(checks: list[Check]) -> str:
    """One ``assumption <id> <verdict> <note>`` line per check."""
    return "\n".join(
        [
            "# ownvitals assumptions: verdicts only (no values, timestamps or ids)",
            *(f"assumption {c.id} {c.verdict} {c.note}" for c in checks),
        ]
    )


def as_dict(checks: list[Check]) -> dict[str, Any]:
    return {
        "format": "ownvitals-assumptions-1",
        "checks": [{"id": c.id, "verdict": c.verdict, "note": c.note} for c in checks],
    }
