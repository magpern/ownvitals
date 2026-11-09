"""The doctor PDF (ADR 0013 section 6): the report pages, then the chosen ECG strips.

One renderer, ``ecg_pdf.render``, draws the report pages of ``doctor_layout`` and the strips of
``ecg_layout`` (ADR 0006 section 3), so the strips keep 25 mm/s and 10 mm/mV; the footer goes
on every page, strips included. The input is ``Report`` (``doctor_model``) and the strips'
``ReportFacts``: Withings' values and the user's own readings, with no field for an estimate,
a score, an experimental or trend finding or AI text. The bytes are deterministic.
"""

from collections.abc import Sequence

from ownvitals.reports.doctor_layout import report_pages, with_footer
from ownvitals.reports.doctor_model import Report
from ownvitals.reports.ecg_layout import Page, ReportFacts, TooLong, layout
from ownvitals.reports.ecg_pdf import render

TITLE = "Health data report"


def strip_pages(facts: ReportFacts) -> tuple[Page, ...] | None:
    """The pages of one strip with the footer, or ``None`` when it is too long to print."""
    try:
        return tuple(with_footer(page) for page in layout(facts))
    except TooLong:
        return None


def build_doctor_pdf(report: Report, strips: Sequence[tuple[Page, ...]]) -> bytes:
    pages = [*report_pages(report)]
    for strip in strips:
        pages.extend(strip)
    return render(tuple(pages), TITLE)
