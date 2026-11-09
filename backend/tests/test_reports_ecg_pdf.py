"""The true-scale ECG PDF (ADR 0006 section 3; issue #192). Synthetic signals only.

The geometry is read off the layout's drawing operations (the pulse and the trace) and again off
the PDF's own path operations, so the drawing units are what a printer at 100 % scales.
"""

import dataclasses
import inspect
import math
import re
from itertools import pairwise

import pytest

import pdftools
from ownvitals.reports import ecg_layout, ecg_pdf
from ownvitals.reports.ecg_layout import (
    GRID_X0,
    MM_PER_MV,
    MM_PER_S,
    ROW_H,
    ROW_W,
    Polyline,
    ReportFacts,
    Segments,
    Text,
    TooLong,
    layout,
)

SYNTHETIC = True
RATE = 250  # synthetic Hz; any positive integer works


def facts(leads: tuple[tuple[int, ...], ...], **changes: object) -> ReportFacts:
    base = ReportFacts(
        recording_id="00000000-0000-4000-8000-000000000192",
        recorded_local="2026-10-01 08:15:30 CEST (Europe/Stockholm)",
        device="ScanWatch",
        sampling_hz=RATE,
        lead_label="lead unverified",
        wear_position="code 3",
        classification="Withings classified this ECG as: inconclusive",
        details="Withings detail: noise",
        heart_rate="61 bpm",
        intervals=(("QRS duration", "92 ms"), ("PR interval", "not reported")),
        leads=leads,
        disclaimer="ownvitals is not a medical device.",
    )
    return dataclasses.replace(base, **changes)


def strip(seconds: float, *, peak_uv: int = 1000) -> tuple[int, ...]:
    """A synthetic strip: a flat 500 uV offset with a 1 Hz sine of ``peak_uv``."""
    n = int(seconds * RATE)
    return tuple(500 + round(peak_uv * math.sin(2 * math.pi * i / RATE)) for i in range(n))


def polylines(page: ecg_layout.Page, role: str) -> list[Polyline]:
    return [op for op in page.ops if isinstance(op, Polyline) and op.role == role]


def test_the_calibration_pulse_is_10_mm_high_and_200_ms_wide() -> None:
    page = layout(facts((strip(10),)))[0]
    pulse = polylines(page, "calibration")[0]
    ys = [y for _, y in pulse.points]
    assert max(ys) - min(ys) == pytest.approx(1.0 * MM_PER_MV) == pytest.approx(10.0)
    top = [x for x, y in pulse.points if y == max(ys)]
    assert max(top) - min(top) == pytest.approx(0.2 * MM_PER_S) == pytest.approx(5.0)
    # it sits on the baseline of its row: the trace's median level
    trace = polylines(page, "trace")[0]
    assert min(ys) == pytest.approx(210.0 - 42.0 - 20.0)  # row top 168 mm, baseline 20 mm below
    assert trace.points[0][0] == pytest.approx(GRID_X0)


def test_one_second_is_25_mm_and_ten_seconds_fill_the_250_mm_row() -> None:
    page = layout(facts((strip(10),)))[0]
    xs = [x for x, _ in polylines(page, "trace")[0].points]
    assert xs[RATE] - xs[0] == pytest.approx(25.0)  # sample index RATE is exactly 1 s
    # 2500 samples cover 10 s: the last one sits one sample period before the row's end
    assert xs[-1] - xs[0] == pytest.approx(ROW_W - MM_PER_S / RATE)
    assert xs[2 * RATE] - xs[RATE] == pytest.approx(25.0)


def test_one_millivolt_in_the_signal_is_10_mm_and_only_the_median_is_removed() -> None:
    samples = (500,) * RATE + (1500,) * RATE + (500,) * (8 * RATE)  # a 1000 uV step
    page = layout(facts((samples,)))[0]
    ys = [y for _, y in polylines(page, "trace")[0].points]
    assert max(ys) - min(ys) == pytest.approx(10.0)  # 1 mV = 10 mm
    base = 210.0 - 42.0 - 20.0
    assert min(ys) == pytest.approx(base)  # the median (500) sits on the baseline
    assert max(ys) == pytest.approx(base + 10.0)  # never rescaled or clipped


def test_a_trace_beyond_2_mv_is_clipped_to_its_own_row_in_the_pdf() -> None:
    samples = (500,) * RATE + (3500,) * RATE + (500,) * (18 * RATE)  # a 3 mV step, two rows
    page = layout(facts((samples,)))[0]
    base = 210.0 - 42.0 - 20.0
    ys = [y for _, y in polylines(page, "trace")[0].points]
    assert max(ys) - base == pytest.approx(30.0)  # the layout never rescales (3 mV = 30 mm) ...
    stream = pdftools.streams(ecg_pdf.build_pdf(facts((samples,))))[0]
    clips = re.findall(r"^n (-?[\d.]+) (-?[\d.]+) ([\d.]+) ([\d.]+) re W\* n$", stream, re.M)
    assert len(clips) == 2  # ... the PDF cuts each trace at its own row: one clip per row
    x, y, w, h = (float(v) / pdftools.PT_PER_MM for v in clips[0])
    assert (x, w, h) == pytest.approx((GRID_X0, ROW_W, ROW_H))
    assert y == pytest.approx(base - ROW_H / 2)  # plus and minus 2 mV around the baseline
    assert stream.count("\nq\n") >= 2  # each clip is inside a saved state, so it ends with it
    assert stream.count("\nQ\n") >= 2


def test_the_grid_is_1_mm_with_5_mm_majors_over_the_whole_row() -> None:
    page = layout(facts((strip(10),)))[0]
    minor, major = [op for op in page.ops if isinstance(op, Segments)][:2]
    vertical = sorted({x for x1, _, x2, _ in minor.lines + major.lines for x in (x1,) if x1 == x2})
    assert len(vertical) == 251
    assert all(b - a == pytest.approx(1.0) for a, b in pairwise(vertical))
    major_x = sorted({x1 for x1, _, x2, _ in major.lines if x1 == x2})
    assert all(b - a == pytest.approx(5.0) for a, b in pairwise(major_x))
    assert major_x[0] == pytest.approx(GRID_X0)


def test_rows_are_10_s_three_to_a_page_with_a_pulse_in_each() -> None:
    pages = layout(facts((strip(30),)))
    assert len(pages) == 1  # 30 s is three rows
    assert len(polylines(pages[0], "calibration")) == 3
    traces = polylines(pages[0], "trace")
    assert [t.points[0][0] for t in traces] == [pytest.approx(GRID_X0)] * 3
    two = layout(facts((strip(30), strip(30))))  # six rows: two pages of three
    assert [len(polylines(p, "trace")) for p in two] == [3, 3]
    assert "Page 2 of 2" in [op.text for op in two[1].ops if isinstance(op, Text)]


def test_a_partial_last_row_is_drawn_to_scale_and_a_long_strip_is_refused() -> None:
    page = layout(facts((strip(12),)))[0]
    last = polylines(page, "trace")[1]
    assert last.points[-1][0] - last.points[0][0] == pytest.approx(2 * MM_PER_S - MM_PER_S / RATE)
    with pytest.raises(TooLong):
        layout(facts((strip(ecg_layout.MAX_SECONDS + 1),)))


def test_the_page_is_a4_landscape_and_the_pdf_draws_in_true_millimetres() -> None:
    pdf = ecg_pdf.build_pdf(facts((strip(10),)))
    width, height = pdftools.media_box(pdf)
    assert (width, height) == (
        pytest.approx(297 * pdftools.PT_PER_MM, abs=0.01),
        pytest.approx(210 * pdftools.PT_PER_MM, abs=0.01),
    )
    paths = pdftools.paths_mm(pdf)
    pulse = next(p for p in paths if len(p) == 6)  # the six-point calibration polyline
    ys = [y for _, y in pulse]
    assert max(ys) - min(ys) == pytest.approx(10.0, abs=0.01)
    top = [x for x, y in pulse if y == max(ys)]
    assert max(top) - min(top) == pytest.approx(5.0, abs=0.01)
    trace = max(paths, key=len)
    assert trace[RATE][0] - trace[0][0] == pytest.approx(25.0, abs=0.01)
    assert trace[-1][0] - trace[0][0] == pytest.approx(250.0 - 25.0 / RATE, abs=0.01)


def test_it_shows_withings_values_as_withings_and_what_is_missing_as_not_reported() -> None:
    shown = pdftools.text(ecg_pdf.build_pdf(facts((strip(10),))))
    for needle in (
        "2026-10-01 08:15:30 CEST",
        "Europe/Stockholm",
        "ScanWatch",
        f"Sampling rate: {RATE} Hz",
        "lead unverified",
        "Withings classified this ECG as: inconclusive",
        "Reported by Withings: heart rate 61 bpm",
        "QRS duration 92 ms",
        "PR interval not reported",
        "not a medical device",
        "Print at 100 %",
    ):
        assert needle in shown, needle


def test_the_pdf_has_no_estimate_and_no_way_to_pass_one() -> None:
    # No field or parameter can carry an ownvitals estimate or anything experimental.
    assert {f.name for f in dataclasses.fields(ReportFacts)} == {
        "recording_id", "recorded_local", "device", "sampling_hz", "lead_label", "wear_position",
        "classification", "details", "heart_rate", "intervals", "leads", "disclaimer",
    }  # fmt: skip
    assert list(inspect.signature(ecg_pdf.build_pdf).parameters) == ["facts"]
    shown = pdftools.text(ecg_pdf.build_pdf(facts((strip(10),)))).lower()
    for word in ("estimated", "ownvitals estimate", "experimental", "hrv", "rmssd", "r-peak"):
        assert word not in shown, word


def test_the_pdf_is_self_contained_and_repeatable() -> None:
    pdf = ecg_pdf.build_pdf(facts((strip(10),)))
    for marker in (
        b"/URI",
        b"/JavaScript",
        b"/JS",
        b"/Launch",
        b"/EmbeddedFile",
        b"http://",
        b"https://",
    ):
        assert marker not in pdf, marker
    assert pdf.startswith(b"%PDF-")
    assert ecg_pdf.build_pdf(facts((strip(10),))) == pdf  # deterministic bytes


def test_local_time_names_the_zone_and_falls_back_to_utc() -> None:
    from datetime import UTC, datetime

    at = datetime(2026, 10, 1, 6, 15, 30, tzinfo=UTC)
    assert ecg_pdf.local_time_label(at, "Europe/Stockholm") == (
        "2026-10-01 08:15:30 CEST (Europe/Stockholm)"
    )
    assert ecg_pdf.local_time_label(at, "Not/AZone") == "2026-10-01 06:15:30 UTC (UTC)"


def test_the_repr_of_the_facts_holds_no_values() -> None:
    assert repr(facts((strip(1),))) == "ReportFacts redacted"


def test_no_text_lies_inside_a_rows_grid_on_any_page() -> None:
    """Header, captions' neighbours, footer and page number stay out of every grid; only the
    row's own 1 mV label and caption (drawn inside it) are exempt."""
    for pages in (layout(facts((strip(100),))), layout(facts((strip(30), strip(30), strip(20))))):
        for page in pages:
            rows = len(polylines(page, "trace"))
            tops = [ecg_layout.GRID_TOP - k * ecg_layout.ROW_H for k in range(rows)]
            for op in page.ops:
                if not isinstance(op, Text) or op.text.endswith(" s") or op.text == "1 mV":
                    continue
                for top in tops:
                    assert not (top - ecg_layout.ROW_H <= op.y <= top), op.text


def test_multi_array_rows_are_captioned_by_array() -> None:
    pages = layout(facts((strip(10), strip(10))))
    captions = [
        op.text for p in pages for op in p.ops if isinstance(op, Text) and op.text.endswith(" s")
    ]
    assert captions == ["array 1, 0-10 s", "array 2, 0-10 s"]
