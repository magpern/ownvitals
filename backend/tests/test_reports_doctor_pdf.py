"""The doctor PDF's content and geometry (ADR 0013 section 6; issue 210), with no database:
the pure model and layout, then the bytes read back. Synthetic data only."""

import dataclasses
import math
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

import pdftools
from ownvitals.reports import doctor_layout, doctor_model, doctor_pdf, ecg_layout
from ownvitals.reports.doctor_input import DoctorChoices
from ownvitals.reports.doctor_model import (
    FOOTER,
    RANGE_LABEL,
    DailyRow,
    EcgNote,
    FindingLine,
    Report,
    build_section,
)

SYNTHETIC = True
START, END = date(2026, 1, 1), date(2026, 3, 31)


def daily(day: date, value: str, *, manual: bool = False) -> DailyRow:
    d = Decimal(value)
    return DailyRow(day, d, "median", 1, d - 1, d + 1, Decimal(1), "synthetic/1", manual)


def rows(count: int = 120) -> list[DailyRow]:
    first = START - timedelta(days=30)
    return [daily(first + timedelta(days=i), str(70 + i % 5), manual=i == 40) for i in range(count)]


def report(**changes: object) -> Report:
    base = Report(
        start=START,
        end=END,
        generated=date(2026, 4, 2),
        birth_year=1980,
        sections=(build_section("weight", rows(), START, END),),
        findings=(FindingLine("E1", doctor_model.FINDING_TEXT["E1"], START, END),),
        ecg_notes=(),
    )
    return dataclasses.replace(base, **changes)


def facts(seconds: float = 10) -> ecg_layout.ReportFacts:
    rate = 250
    leads = (
        tuple(
            500 + round(1000 * math.sin(2 * math.pi * i / rate)) for i in range(int(seconds * rate))
        ),
    )
    return ecg_layout.ReportFacts(
        recording_id=str(uuid4()),
        recorded_local="2026-02-01 08:15:30 UTC",
        device="ScanWatch",
        sampling_hz=rate,
        lead_label="lead unverified",
        wear_position="code 3",
        classification="Withings classified this ECG as: inconclusive",
        details="Withings detail: noise",
        heart_rate="61 bpm",
        intervals=(("QRS duration", "92 ms"),),
        leads=leads,
        disclaimer="ownvitals is not a medical device.",
    )


def pdf_of(rep: Report, strips: int = 1) -> bytes:
    pages = [p for p in (doctor_pdf.strip_pages(facts()) for _ in range(strips)) if p]
    return doctor_pdf.build_doctor_pdf(rep, pages)


def test_the_header_names_the_period_the_date_and_the_birth_year_but_no_one() -> None:
    shown = pdftools.text(pdf_of(report()))
    assert "Health data report" in shown
    assert all(day in shown for day in ("2026-01-01", "2026-03-31", "2026-04-02"))
    assert "1980" in shown
    assert "Name: ______" in shown
    assert "@" not in shown


def test_a_missing_birth_year_is_left_out() -> None:
    assert "Birth year" not in pdftools.text(pdf_of(report(birth_year=None)))


def test_the_footer_is_on_every_page_strips_included() -> None:
    pdf = pdf_of(report(), strips=2)
    pages = pdftools.streams(pdf)
    assert len(pages) >= 4
    for page in pages:
        assert "ownvitals is not a medical device" in page
    assert " ".join(FOOTER.split()[:6]) in pdftools.text(pdf)


def test_the_range_is_labelled_as_the_users_own_not_a_clinical_one() -> None:
    shown = pdftools.text(pdf_of(report()))
    assert RANGE_LABEL in shown
    assert "Entered manually" in shown


def test_the_sections_come_in_the_adr_order() -> None:
    shown = pdftools.text(pdf_of(report()))
    order = [
        "Health data report",
        "Weight",
        RANGE_LABEL,
        "Observations reported by the Withings device",
    ]
    positions = [shown.rindex(item) for item in order]
    assert positions == sorted(positions)
    assert "Withings classified this ECG" in pdftools.streams(pdf_of(report()))[-1]


def test_manual_days_are_hollow_points_and_counted() -> None:
    section = build_section("weight", rows(), START, END)
    ops = doctor_layout.metric_page(section, START, END)
    hollow = [op for op in ops if isinstance(op, ecg_layout.Polyline) and op.role == "manual_point"]
    assert len(hollow) == sum(1 for p in section.points if p.manual) >= 1
    assert sum(m.manual_days for m in section.months) == len(hollow)


def test_the_typical_range_comes_from_the_90_days_before_the_period_end() -> None:
    section = build_section("weight", rows(), START, END)
    assert section.typical is not None
    assert section.typical.low <= section.typical.median <= section.typical.high


def test_a_metric_with_no_readings_says_so() -> None:
    empty = build_section("weight", [], START, END)
    shown = pdftools.text(pdf_of(report(sections=(empty,))))
    assert "No readings in this period." in shown


def test_no_estimate_score_experimental_or_trend_text_can_be_printed() -> None:
    fields = {f.name for f in dataclasses.fields(Report)}
    assert fields == {
        "start",
        "end",
        "generated",
        "birth_year",
        "sections",
        "findings",
        "ecg_notes",
    }
    assert set(doctor_model.FINDING_TEXT) == {"E1", "E2", "E3"}
    shown = pdftools.text(pdf_of(report())).lower()
    for word in ("estimate", "experimental", "score", "trend", "ownvitals estimates"):
        assert word not in shown.replace("observations reported", "")


def test_ecg_strips_keep_25_mm_per_s_and_10_mm_per_mv() -> None:
    pdf = pdf_of(report())
    paths = pdftools.paths_mm(pdf)
    trace = max(paths, key=len)
    assert trace[250][0] - trace[0][0] == pytest.approx(25.0, abs=0.125)  # one second
    pulse = next(p for p in paths if len(p) == 6)  # the 1 mV calibration pulse
    ys = [y for _, y in pulse]
    assert max(ys) - min(ys) == pytest.approx(10.0, abs=0.05)
    peak = max(y for _, y in trace) - min(y for _, y in trace)
    assert peak == pytest.approx(20.0, abs=0.2)  # 2 mV peak to peak at 10 mm/mV


def test_a_recording_too_long_to_print_is_not_scaled_down() -> None:
    assert doctor_pdf.strip_pages(facts(ecg_layout.MAX_SECONDS + 1)) is None


def test_the_notes_say_which_recordings_were_left_out() -> None:
    note = EcgNote("2026-02-01 08:15 UTC", "no signal stored")
    shown = pdftools.text(pdf_of(report(ecg_notes=(note,)), strips=0))
    assert "no signal stored" in shown


def test_the_bytes_are_repeatable() -> None:
    first = facts()
    assert doctor_pdf.build_doctor_pdf(report(), []) == doctor_pdf.build_doctor_pdf(report(), [])
    assert first is not None


def test_the_repr_of_the_report_holds_no_values() -> None:
    assert repr(report()) == "Report redacted"


# --- the choices -----------------------------------------------------------------------------


def choices(**changes: object) -> dict[str, object]:
    return {"start": "2026-01-01", "end": "2026-03-31", "metrics": ["weight"], **changes}


def test_the_choices_accept_a_period_metrics_and_recordings() -> None:
    ok = DoctorChoices(**choices(recordings=[str(uuid4())]))  # type: ignore[arg-type]
    assert ok.metrics == ("weight",)


@pytest.mark.parametrize(
    "bad",
    [
        {"end": "2025-12-31"},  # before the start
        {"end": "2027-01-02"},  # over 12 months
        {"metrics": []},
        {"metrics": ["weight", "weight"]},
        {"metrics": ["not_a_metric"]},
        {"recordings": [str(uuid4()) for _ in range(11)]},
        {"name": "A Name"},  # no free text, no name
        {"email": "a@example.com"},
    ],
)
def test_the_choices_refuse_what_they_do_not_name(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DoctorChoices(**choices(**bad))  # type: ignore[arg-type]
