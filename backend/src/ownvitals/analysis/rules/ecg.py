"""ECG rules E1 to E4 and E6 (ADR 0006 section 11; ADR 0010 section 7, "ECG"; PLAN §7.2). Pure.

Same contract as ``body`` and ``heart``: a ``Finding`` proposal, a ``NoFinding`` or
``Insufficient`` for one evaluation ``day``. Every rule reads **Withings-reported values only**;
no finding stores our gate outcome (the read function's overlay brings it, ADR 0006 section 11).

* **E1** (device, urgent): a recording with ``afib`` 1 or ``afib_details`` 1, 11 or 12.
* **E2** (device): at least 3 of the user's last 10 recordings (by ``recorded_at``) with
  ``afib_details`` 2 or 5; with fewer than 10, the same count over those that exist.
* **E3** (device): a recording with ``afib_details`` 6 or 7 (low or high HR reported).
* **E4** (experimental): a reported interval above its band. QTc (138) more than 450 ms (men),
  460 ms (women) or 500 ms (anyone; with the sex unknown only this one); PR (136) more than
  200 ms; QRS (135) 120 ms or more. Each interval is its own subject; the trigger is the reading's
  own group.
* **E6** (trend): a QTc or QRS daily value above the finding threshold at its day (ADR 0010 §2:
  k 2.5, the 90 days before, BASELINE policy). Window and reference day: that day; the trigger is
  the daily value's provenance.

**Only documented codes trigger, and a missing value never counts as negative.** A code that is
NULL or not in withings-findings §2's table is missing. A day on which a missing value could
change E1's, E2's or E3's outcome is not an evaluation day (``decided_days``): it neither opens
nor ends an episode. E4 and E6 are evaluated only on days with a reading or a daily value of
their own subject, so a missing interval is never a "within range".

E1 to E3 have one subject, the ``ecg`` stream; their trigger groups are the measure groups A7
links to the recordings that fired (the caller resolves them when it reads the recordings; the
read function links them again at read time).
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from ownvitals.analysis.rules.core import (
    Confidence,
    Insufficient,
    NoFinding,
    RuleResult,
    make_finding,
)
from ownvitals.analysis.rules.ecg_intervals import E4_KEYS, E6_KEYS, Sex, e4, e6

SUBJECT: Final = "ecg"  # E1 to E3: the stream (ADR 0010 §4)

# withings-findings §2, "Values and enums": afib 0 negative, 1 positive, 2 inconclusive;
# afib_details -1 not processed, 0, 1, 2, 5 noise, 6 low HR, 7 high HR, 9, 10, 11, 12, 13.
AFIB_CODES: Final = frozenset({0, 1, 2})
DETAIL_CODES: Final = frozenset({-1, 0, 1, 2, 5, 6, 7, 9, 10, 11, 12, 13})
E1_AFIB: Final = frozenset({1})
E1_DETAILS: Final = frozenset({1, 11, 12})
E2_DETAILS: Final = frozenset({2, 5})
E2_LAST: Final = 10
E2_MIN: Final = 3
E3_DETAILS: Final = frozenset({6, 7})


@dataclass(frozen=True, slots=True)
class Recording:
    """The Withings-reported fields of one ECG recording that E1 to E3 read. ``day`` is its
    calendar date in the day zone; ``groups`` the measure groups A7 links to it now."""

    signalid: int
    recorded_at: datetime
    day: date
    afib: int | None
    afib_details: int | None
    groups: tuple[int, ...] = ()

    def __repr__(self) -> str:
        return "Recording redacted"

    @property
    def order(self) -> tuple[datetime, int]:
        return (self.recorded_at, self.signalid)


def _afib(r: Recording) -> int | None:
    return r.afib if r.afib in AFIB_CODES else None


def _details(r: Recording) -> int | None:
    return r.afib_details if r.afib_details in DETAIL_CODES else None


def _on(recordings: Iterable[Recording], day: date) -> list[Recording]:
    return sorted((r for r in recordings if r.day == day), key=lambda r: r.order)


def _undecided(rule_id: str, day: date) -> Insufficient:
    return Insufficient(rule_id, SUBJECT, day, "a missing code could change the outcome")


def _recording_finding(
    rule_id: str, day: date, fired: list[Recording], *, start: date | None = None, **extra: object
) -> RuleResult:
    return make_finding(
        rule_id,
        SUBJECT,
        day,
        window_start=day if start is None else start,
        window_end=day,
        reference_day=day,
        confidence=Confidence.HIGH,
        evidence={
            **extra,
            "recordings": [
                {"signalid": r.signalid, "afib": r.afib, "afib_details": r.afib_details}
                for r in fired
            ],
        },
        data_quality=None,
        trigger_groups=(g for r in fired for g in r.groups),
    )


def e1(recordings: Sequence[Recording], day: date) -> RuleResult:
    """E1: a recording of ``day`` with ``afib`` 1 or ``afib_details`` 1, 11 or 12. Not firing
    only when every recording of the day carries a documented code."""
    found = _on(recordings, day)
    fired = [r for r in found if _afib(r) in E1_AFIB or _details(r) in E1_DETAILS]
    if fired:
        return _recording_finding("E1", day, fired)
    if found and all(_afib(r) is not None or _details(r) is not None for r in found):
        return NoFinding("E1", SUBJECT, day, "no positive AFib result")
    return _undecided("E1", day)


def e2(recordings: Sequence[Recording], day: date) -> RuleResult:
    """E2: at least 3 of the last 10 recordings up to ``day`` with ``afib_details`` 2 or 5. Not
    firing only when the recordings with no documented ``afib_details`` could not reach 3."""
    last = sorted((r for r in recordings if r.day <= day), key=lambda r: r.order)[-E2_LAST:]
    if not last:
        return _undecided("E2", day)
    fired = [r for r in last if _details(r) in E2_DETAILS]
    if len(fired) >= E2_MIN:
        return _recording_finding(
            "E2", day, fired, start=last[0].day, of_last=len(last), minimum=E2_MIN
        )
    missing = sum(1 for r in last if _details(r) is None)
    if len(fired) + missing < E2_MIN:
        return NoFinding("E2", SUBJECT, day, "fewer than 3 inconclusive or noisy")
    return _undecided("E2", day)


def e3(recordings: Sequence[Recording], day: date) -> RuleResult:
    """E3: a recording of ``day`` with ``afib_details`` 6 or 7. Not firing only when every
    recording of the day carries a documented ``afib_details``."""
    found = _on(recordings, day)
    fired = [r for r in found if _details(r) in E3_DETAILS]
    if fired:
        return _recording_finding("E3", day, fired)
    if found and all(_details(r) is not None for r in found):
        return NoFinding("E3", SUBJECT, day, "no low or high HR reported")
    return _undecided("E3", day)


def decided_days(
    run: Callable[[Sequence[Recording], date], RuleResult], recordings: Sequence[Recording]
) -> tuple[date, ...]:
    """The evaluation days of E1, E2 or E3: the days with a recording whose outcome no missing
    code could change."""
    days = sorted({r.day for r in recordings})
    return tuple(d for d in days if not isinstance(run(recordings, d), Insufficient))


__all__ = [
    "AFIB_CODES",
    "DETAIL_CODES",
    "E4_KEYS",
    "E6_KEYS",
    "SUBJECT",
    "Recording",
    "Sex",
    "decided_days",
    "e1",
    "e2",
    "e3",
    "e4",
    "e6",
]
