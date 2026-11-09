"""The ECG PDF (ADR 0006 section 3): A4 landscape at true scale, built from ``ecg_layout``.

Renderer: reportlab (BSD-3-Clause), a server-side library with no network access of its own.
It draws the operations of the layout in millimetres, with the built-in Helvetica fonts (no
font file, no external reference, no script, no link). ``build_pdf`` takes Withings' values and
the stored signal only; it has no parameter for an ownvitals estimate, so it can never draw one
(``tests/test_reports_ecg_pdf.py``). The bytes are deterministic (``invariant``).
"""

from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

from ownvitals.reports.ecg_layout import (
    PAGE_H,
    PAGE_W,
    Op,
    Page,
    Polyline,
    ReportFacts,
    Segments,
    TooLong,
    layout,
)

PAGE_SIZE = (PAGE_W * mm, PAGE_H * mm)  # A4 landscape


def local_time_label(at: datetime, zone: str) -> str:
    """The recording time in its own zone with the zone shown: ``2026-10-01 08:15:30 CEST
    (Europe/Stockholm)``; UTC when the zone is not known."""
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        tz, zone = ZoneInfo("UTC"), "UTC"
    return f"{at.astimezone(tz):%Y-%m-%d %H:%M:%S %Z} ({zone})"


def _draw(canvas: Canvas, op: Op) -> None:
    if isinstance(op, Segments):
        canvas.setStrokeColorRGB(*op.rgb)
        canvas.setLineWidth(op.width_mm * mm)
        path = canvas.beginPath()
        for x1, y1, x2, y2 in op.lines:
            path.moveTo(x1 * mm, y1 * mm)
            path.lineTo(x2 * mm, y2 * mm)
        canvas.drawPath(path, stroke=1, fill=0)
    elif isinstance(op, Polyline):
        canvas.setStrokeColorRGB(*op.rgb)
        canvas.setLineWidth(op.width_mm * mm)
        canvas.setLineJoin(1)
        path = canvas.beginPath()
        first, *rest = op.points
        path.moveTo(first[0] * mm, first[1] * mm)
        for x, y in rest:
            path.lineTo(x * mm, y * mm)
        canvas.drawPath(path, stroke=1, fill=0)
    else:
        canvas.setFillColorRGB(0, 0, 0)
        canvas.setFont("Helvetica-Bold" if op.bold else "Helvetica", op.size_pt)
        canvas.drawString(op.x * mm, op.y * mm, op.text)


def render(pages: tuple[Page, ...], title: str) -> bytes:
    out = BytesIO()
    canvas = Canvas(out, pagesize=PAGE_SIZE, invariant=1, pageCompression=1)
    canvas.setTitle(title)
    canvas.setAuthor("ownvitals")
    canvas.setCreator("ownvitals")
    for page in pages:
        for op in page.ops:
            _draw(canvas, op)
        canvas.showPage()
    canvas.save()
    return out.getvalue()


def build_pdf(facts: ReportFacts) -> bytes:
    """The PDF of one recording. Raises ``TooLong`` for a strip over ``MAX_SECONDS``."""
    return render(layout(facts), f"ECG {facts.recording_id[:8]}")


__all__ = ["PAGE_SIZE", "TooLong", "build_pdf", "local_time_label", "render"]
