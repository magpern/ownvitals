"""The true-scale ECG page as drawing operations (ADR 0006 section 3). Pure geometry.

Every coordinate is in millimetres from the bottom-left corner of an A4 landscape page
(297 x 210 mm), so printing at 100 % gives 25 mm/s and 10 mm/mV on paper. One row is 10 s
(250 mm wide); a trace sample at time t is at ``GRID_X0 + 25 t`` and a value of v microvolts
above the row's median at ``baseline + v / 100`` (10 mm per mV). Each row has a 1 mV x 200 ms
calibration pulse to the left of its grid, drawn from the same constants, so a test reads the
geometry off the operations. Colours are RGB in 0..1.

The input is ``ReportFacts``: Withings' own values (already worded) and the stored signal. There
is no field for an ownvitals estimate or anything experimental, so none can be drawn.
"""

import statistics
from dataclasses import dataclass
from math import ceil
from typing import Final

PAGE_W: Final = 297.0
PAGE_H: Final = 210.0
MM_PER_S: Final = 25.0
MM_PER_MV: Final = 10.0
UV_PER_MM: Final = 1000.0 / MM_PER_MV
ROW_SECONDS: Final = 10
ROW_W: Final = ROW_SECONDS * MM_PER_S  # 250 mm
ROW_H: Final = 40.0  # eight major squares: plus and minus 2 mV around the baseline
GRID_X0: Final = (PAGE_W - ROW_W) / 2
PULSE_MV: Final = 1.0
PULSE_S: Final = 0.2
PULSE_X: Final = 8.5
PULSE_LEAD: Final = 2.5  # flat line before and after the pulse
ROWS_PER_PAGE: Final = 4
GRID_TOP: Final = 168.0  # the top of the first row; the header is above it
MAX_SECONDS: Final = 120  # refuse rather than print a very long strip
MINOR: Final = (0.98, 0.78, 0.78)
MAJOR: Final = (0.92, 0.55, 0.55)
INK: Final = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class Segments:
    lines: tuple[tuple[float, float, float, float], ...]
    width_mm: float
    rgb: tuple[float, float, float]


@dataclass(frozen=True)
class Polyline:
    points: tuple[tuple[float, float], ...]
    width_mm: float
    rgb: tuple[float, float, float]
    role: str  # "trace" or "calibration"


@dataclass(frozen=True)
class Text:
    x: float
    y: float
    text: str
    size_pt: float
    bold: bool = False


Op = Segments | Polyline | Text


@dataclass(frozen=True)
class Page:
    ops: tuple[Op, ...]


@dataclass(frozen=True)
class ReportFacts:
    """Withings' values as shown text, and the stored signal. ``repr`` is fixed: no values."""

    recording_id: str
    recorded_local: str  # local time with its zone
    device: str
    sampling_hz: int
    lead_label: str
    wear_position: str
    classification: str
    details: str | None
    heart_rate: str
    intervals: tuple[tuple[str, str], ...]  # (label, shown value) of 135 to 138
    leads: tuple[tuple[int, ...], ...]
    disclaimer: str

    def __repr__(self) -> str:
        return "ReportFacts redacted"


class TooLong(ValueError):
    """The strip is longer than ``MAX_SECONDS``."""


@dataclass(frozen=True)
class _Row:
    caption: str
    samples: tuple[int, ...]
    start: int
    median: float


def _grid(top: float) -> tuple[Segments, Segments]:
    bottom = top - ROW_H
    minor: list[tuple[float, float, float, float]] = []
    major: list[tuple[float, float, float, float]] = []
    for i in range(int(ROW_W) + 1):
        x = round(GRID_X0 + i, 4)
        (major if i % 5 == 0 else minor).append((x, bottom, x, top))
    for j in range(int(ROW_H) + 1):
        y = round(bottom + j, 4)
        (major if j % 5 == 0 else minor).append((GRID_X0, y, GRID_X0 + ROW_W, y))
    return Segments(tuple(minor), 0.1, MINOR), Segments(tuple(major), 0.25, MAJOR)


def calibration(baseline: float) -> Polyline:
    """1 mV (10 mm) high and 200 ms (5 mm) wide, on the row's baseline."""
    x1 = PULSE_X
    x2 = x1 + PULSE_S * MM_PER_S
    top = baseline + PULSE_MV * MM_PER_MV
    points = (
        (x1 - PULSE_LEAD, baseline),
        (x1, baseline),
        (x1, top),
        (x2, top),
        (x2, baseline),
        (x2 + PULSE_LEAD, baseline),
    )
    return Polyline(points, 0.35, INK, "calibration")


def trace(
    samples: tuple[int, ...], start: int, stop: int, rate: int, base: float, median: float
) -> Polyline:
    """Samples ``start`` to ``stop`` (inclusive of ``stop`` when it exists), x from the row's
    start at 25 mm/s, the median subtracted for vertical placement only."""
    stop = min(stop, len(samples) - 1)
    pts = tuple(
        (
            round(GRID_X0 + (i - start) / rate * MM_PER_S, 4),
            round(base + (samples[i] - median) / UV_PER_MM, 4),
        )
        for i in range(start, stop + 1)
    )
    return Polyline(pts, 0.3, INK, "trace")


def _header(facts: ReportFacts, first: bool) -> list[Op]:
    if not first:
        return [Text(GRID_X0, 196.0, f"ECG {facts.recording_id[:8]} (continued)", 9)]
    ops: list[Op] = [
        Text(GRID_X0, 197.0, "ECG recording", 14, bold=True),
        Text(GRID_X0, 190.0, f"Recorded {facts.recorded_local}", 9),
        Text(GRID_X0, 185.0, f"Device: {facts.device}   Sampling rate: {facts.sampling_hz} Hz", 9),
        Text(GRID_X0, 180.0, f"{facts.lead_label}   Wear position: {facts.wear_position}", 9),
    ]
    ops.append(Text(150.0, 190.0, facts.classification, 9, bold=True))
    ops.append(Text(150.0, 185.0, f"Reported by Withings: heart rate {facts.heart_rate}", 9))
    if facts.details is not None:
        ops.append(Text(150.0, 180.0, facts.details, 9))
    line = "   ".join(f"{label} {value}" for label, value in facts.intervals)
    ops.append(Text(GRID_X0, 173.0, f"Reported by Withings: {line}", 9))
    return ops


def _footer(facts: ReportFacts, number: int, count: int) -> list[Op]:
    return [
        Text(GRID_X0, 12.0, "25 mm/s, 10 mm/mV. Print at 100 % (actual size); do not scale.", 7),
        Text(GRID_X0, 8.0, facts.disclaimer, 7),
        Text(PAGE_W - GRID_X0 - 15, 8.0, f"Page {number} of {count}", 7),
    ]


def layout(facts: ReportFacts) -> tuple[Page, ...]:
    """The pages: rows of 10 s, one lead after another, four rows to a page."""
    rate = facts.sampling_hz
    rows: list[_Row] = []
    for lead in facts.leads:
        if len(lead) / rate > MAX_SECONDS:
            raise TooLong
        median = float(statistics.median(lead))
        for r in range(max(1, ceil(len(lead) / (ROW_SECONDS * rate)))):
            caption = f"{r * ROW_SECONDS}-{(r + 1) * ROW_SECONDS} s"
            rows.append(_Row(caption, lead, r * ROW_SECONDS * rate, median))
    chunks = [rows[i : i + ROWS_PER_PAGE] for i in range(0, len(rows), ROWS_PER_PAGE)]
    pages: list[Page] = []
    for number, chunk in enumerate(chunks, start=1):
        ops: list[Op] = _header(facts, number == 1)
        for k, row in enumerate(chunk):
            top = GRID_TOP - k * ROW_H
            base = top - ROW_H / 2
            ops.extend(_grid(top))
            ops.append(calibration(base))
            ops.append(Text(PULSE_X - PULSE_LEAD, base + 11.0, "1 mV", 6))
            ops.append(Text(GRID_X0 + 1, top - 4.0, row.caption, 6))
            stop = row.start + ROW_SECONDS * rate
            ops.append(trace(row.samples, row.start, stop, rate, base, row.median))
        ops.extend(_footer(facts, number, len(chunks)))
        pages.append(Page(tuple(ops)))
    return tuple(pages)
