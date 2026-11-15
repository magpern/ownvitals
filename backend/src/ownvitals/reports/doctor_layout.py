"""The doctor PDF's own pages as drawing operations (ADR 0013 section 6). Pure geometry.

Same units and operations as ``ecg_layout`` (millimetres from the bottom-left of an A4
landscape page; ``Segments``, ``Polyline``, ``Text``), so one renderer (``ecg_pdf.render``)
draws both and the ECG strips keep their true scale. Pages: the header and contents, one page
per metric (chart, typical range, monthly table), the device findings, and the footer on every
page, ECG pages included (``with_footer``). Daily points the user typed in (``manual``) are
hollow octagons (role ``manual_point``); measured points are filled squares.
"""

import textwrap
from collections.abc import Sequence
from datetime import date
from math import cos, pi, sin
from typing import Final

from ownvitals.reports.doctor_model import (
    FOOTER,
    RANGE_LABEL,
    FindingLine,
    MonthRow,
    Point,
    Range,
    Report,
    Section,
    shown,
)
from ownvitals.reports.ecg_layout import GRID_X0, PAGE_W, Op, Page, Polyline, Segments, Text

X0: Final = GRID_X0
X1: Final = PAGE_W - GRID_X0
CHART_TOP: Final = 182.0
CHART_BOTTOM: Final = 124.0
MAX_GAP_DAYS: Final = 7  # the line is broken across a longer gap
TABLE_TOP: Final = 108.0
TABLE_STEP: Final = 4.4
LINES_PER_PAGE: Final = 24
FOOTER_Y: Final = (24.0, 20.5, 17.0)
FOOTER_WIDTH: Final = 125
INK: Final = (0.0, 0.0, 0.0)
SERIES: Final = (0.13, 0.33, 0.6)
BAND: Final = (0.72, 0.82, 0.92)
MEDIAN: Final = (0.45, 0.45, 0.45)
FRAME: Final = (0.6, 0.6, 0.6)
COLUMNS: Final = (("Month", 0.0), ("Median", 38.0), ("Minimum", 76.0), ("Maximum", 114.0))
COLUMNS_END: Final = (("Days", 152.0), ("Entered manually", 176.0))


def footer_ops() -> list[Op]:
    lines = textwrap.wrap(FOOTER, FOOTER_WIDTH)
    return [Text(X0, y, line, 7) for y, line in zip(FOOTER_Y, lines, strict=False)]


def with_footer(page: Page) -> Page:
    return Page((*page.ops, *footer_ops()))


def cover(report: Report) -> list[Op]:
    ops: list[Op] = [
        Text(X0, 190.0, "Health data report", 20, bold=True),
        Text(X0, 180.0, f"Period: {report.start.isoformat()} to {report.end.isoformat()}", 11),
        Text(X0, 174.0, f"Generated: {report.generated.isoformat()}", 11),
    ]
    y = 168.0
    if report.birth_year is not None:
        ops.append(Text(X0, y, f"Birth year: {report.birth_year}", 11))
        y -= 6.0
    ops.append(Text(X0, y, "Name: " + "_" * 40, 11))
    y -= 14.0
    ops.append(Text(X0, y, "Contents", 12, bold=True))
    y -= 7.0
    for section in report.sections:
        ops.append(Text(X0, y, f"{section.label} ({section.unit})", 10))
        y -= 5.0
    ops.append(Text(X0, y - 2.0, "Observations reported by the Withings device", 10))
    ops.append(Text(X0, y - 7.0, "ECG recordings at true scale (25 mm/s, 10 mm/mV)", 10))
    return ops


def _scale(points: Sequence[Point], typical: Range | None) -> tuple[float, float]:
    values = [p.value for p in points]
    if typical is not None:
        values += [typical.low, typical.high]
    low, high = (min(values), max(values)) if values else (0.0, 1.0)
    pad = (high - low) * 0.08 or 1.0
    return low - pad, high + pad


def _octagon(x: float, y: float, radius: float = 0.9) -> Polyline:
    ring = tuple(
        (round(x + radius * cos(k * pi / 4), 4), round(y + radius * sin(k * pi / 4), 4))
        for k in range(9)
    )
    return Polyline(ring, 0.25, SERIES, "manual_point")


def _runs(points: Sequence[Point]) -> list[list[Point]]:
    runs: list[list[Point]] = []
    for point in points:
        if runs and (point.day - runs[-1][-1].day).days <= MAX_GAP_DAYS:
            runs[-1].append(point)
        else:
            runs.append([point])
    return runs


def chart(section: Section, start: date, end: date) -> list[Op]:
    low, high = _scale(section.points, section.typical)
    span = max(1, (end - start).days)
    height = CHART_TOP - CHART_BOTTOM

    def at_x(day: date) -> float:
        return round(X0 + (day - start).days / span * (X1 - X0), 4)

    def at_y(value: float) -> float:
        return round(CHART_BOTTOM + (value - low) / (high - low) * height, 4)

    frame = Segments(
        (
            (X0, CHART_BOTTOM, X1, CHART_BOTTOM),
            (X0, CHART_BOTTOM, X0, CHART_TOP),
            (X1, CHART_BOTTOM, X1, CHART_TOP),
            (X0, CHART_TOP, X1, CHART_TOP),
        ),
        0.2,
        FRAME,
    )
    ops: list[Op] = [frame]
    ops += [
        Text(X0 - 10.0, CHART_TOP - 2.5, shown(high), 6),
        Text(X0 - 10.0, CHART_BOTTOM + 0.8, shown(low), 6),
        Text(X0, CHART_BOTTOM - 4.0, start.isoformat(), 7),
        Text(X1 - 17.0, CHART_BOTTOM - 4.0, end.isoformat(), 7),
    ]
    if section.typical is not None:
        top, bottom = at_y(section.typical.high), at_y(section.typical.low)
        hatch = []
        y = bottom
        while y <= top:
            hatch.append((X0, round(y, 4), X1, round(y, 4)))
            y += 0.6
        ops.append(Segments(tuple(hatch), 0.5, BAND))
        middle = at_y(section.typical.median)
        ops.append(Segments(((X0, middle, X1, middle),), 0.25, MEDIAN))
    for run in _runs(section.points):
        if len(run) >= 2:
            line = tuple((at_x(p.day), at_y(p.value)) for p in run)
            ops.append(Polyline(line, 0.3, SERIES, "series"))
    filled = tuple(
        (at_x(p.day), at_y(p.value) - 0.4, at_x(p.day), at_y(p.value) + 0.4)
        for p in section.points
        if not p.manual
    )
    if filled:
        ops.append(Segments(filled, 0.8, SERIES))
    ops += [_octagon(at_x(p.day), at_y(p.value)) for p in section.points if p.manual]
    return ops


def _table(months: Sequence[MonthRow]) -> list[Op]:
    ops: list[Op] = []
    for name, dx in (*COLUMNS, *COLUMNS_END):
        ops.append(Text(X0 + dx, TABLE_TOP, name, 8, bold=True))
    for k, row in enumerate(months, start=1):
        y = TABLE_TOP - k * TABLE_STEP
        cells = (
            (0.0, row.month),
            (38.0, shown(row.median)),
            (76.0, shown(row.minimum)),
            (114.0, shown(row.maximum)),
            (152.0, str(row.days)),
            (176.0, str(row.manual_days)),
        )
        ops += [Text(X0 + dx, y, text, 8) for dx, text in cells]
    return ops


def metric_page(section: Section, start: date, end: date) -> list[Op]:
    ops: list[Op] = [Text(X0, 196.0, f"{section.label} ({section.unit})", 14, bold=True)]
    if not section.points:
        return [*ops, Text(X0, 180.0, "No readings in this period.", 10)]
    ops += chart(section, start, end)
    legend = RANGE_LABEL if section.typical is not None else "No typical range yet: too few days."
    ops.append(Text(X0, 113.0, legend, 8))
    ops.append(Text(X0 + 150.0, 113.0, "Hollow point: entered manually", 8))
    return ops + _table(section.months)


def _finding_lines(findings: Sequence[FindingLine], notes: Sequence[str]) -> list[str]:
    lines = [
        f"{f.first.isoformat()} to {f.last.isoformat()}   {f.rule_id}   {f.text}" for f in findings
    ]
    if not lines:
        lines = ["No device observations in this period."]
    return [*lines, *notes]


def findings_pages(report: Report) -> list[list[Op]]:
    notes = [f"ECG {n.recorded} is not printed: {n.reason}" for n in report.ecg_notes]
    lines = _finding_lines(report.findings, notes)
    pages: list[list[Op]] = []
    for first in range(0, len(lines), LINES_PER_PAGE):
        title = "Observations reported by the Withings device" + (" (continued)" if first else "")
        ops: list[Op] = [Text(X0, 196.0, title, 14, bold=True)]
        for k, line in enumerate(lines[first : first + LINES_PER_PAGE]):
            ops.append(Text(X0, 184.0 - k * 6.0, line, 9))
        pages.append(ops)
    return pages


def report_pages(report: Report) -> tuple[Page, ...]:
    """Header, one page per metric, the findings; each with the footer and its page number."""
    bodies = [cover(report)]
    bodies += [metric_page(s, report.start, report.end) for s in report.sections]
    bodies += findings_pages(report)
    count = len(bodies)
    return tuple(
        with_footer(Page((*ops, Text(X1 - 15.0, 12.0, f"Page {number} of {count}", 7))))
        for number, ops in enumerate(bodies, start=1)
    )
