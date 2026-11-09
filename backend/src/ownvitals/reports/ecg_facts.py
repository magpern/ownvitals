"""The facts printed on an ECG strip page, from a stored recording (shared by the single-ECG
PDF and the doctor PDF). Withings' values only, never an estimate."""

from ownvitals.ecg import display
from ownvitals.reports.ecg_layout import ReportFacts
from ownvitals.reports.ecg_pdf import local_time_label
from ownvitals.views.ecg_read import Recording


def facts_for(r: Recording, leads: tuple[tuple[int, ...], ...], rate: int) -> ReportFacts:
    name = display.model_name(r.model_id)
    device = display.NOT_REPORTED if r.model_id is None else name or f"Withings model {r.model_id}"
    shown = {i.key: i.value for i in r.intervals}
    return ReportFacts(
        recording_id=str(r.id),
        recorded_local=local_time_label(r.recorded_at, r.source_tz),
        device=device,
        sampling_hz=rate,
        lead_label=display.lead_label(0, len(leads)),
        wear_position=display.NOT_REPORTED if r.wearposition is None else f"code {r.wearposition}",
        classification=display.classification_text(r.afib),
        details=display.details_text(r.afib_details),
        heart_rate=(display.NOT_REPORTED if r.heart_rate is None else f"{r.heart_rate} bpm"),
        intervals=tuple(
            (
                label,
                display.NOT_REPORTED if key not in shown else f"{shown[key]:g} ms",
            )
            for key, label in display.INTERVALS.values()
        ),
        leads=leads,
        disclaimer=display.NOT_A_MEDICAL_DEVICE,
    )
