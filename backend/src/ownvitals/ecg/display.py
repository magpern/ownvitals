"""What the ECG screen and the ECG PDF call Withings' own values (ADR 0006 section 3, PLAN 4.2).

Pure: tables and small functions, no I/O. A documented code gets its meaning from Withings'
public documentation (withings-findings section 3); an undocumented one is shown as
"Withings code n" and never given a meaning; a missing value reads "not reported", never 0 or
normal. The words here are the PDF's; the page holds its own and uses the meaning tokens.
"""

from decimal import Decimal
from typing import Final

# withings-findings section 3: ECG ``afib``.
AFIB_MEANING: Final[dict[int, str]] = {0: "negative", 1: "positive", 2: "inconclusive"}
# withings-findings section 3: ``afib_details``.
DETAILS_MEANING: Final[dict[int, str]] = {
    -1: "not processed",
    0: "negative",
    1: "positive",
    2: "inconclusive",
    5: "noise",
    6: "low heart rate",
    7: "high heart rate",
    9: "negative, normal heart rate",
    10: "negative, high heart rate",
    11: "positive, normal heart rate",
    12: "positive, high heart rate",
    13: "no diagnosis",
}
# withings-findings section 3: device models; only the names it lists.
MODEL_NAME: Final[dict[int, str]] = {
    6: "Body Cardio",
    9: "Body Pro",
    10: "Body Scan",
    18: "Body Comp",
    44: "BPM Core",
    93: "ScanWatch",
    94: "ScanWatch 2",
}
# ADR 0006 A7: the intervals Withings reports with an ECG (measure types 135 to 138), in ms.
INTERVALS: Final[dict[int, tuple[str, str]]] = {
    135: ("qrs_duration", "QRS duration"),
    136: ("pr_interval", "PR interval"),
    137: ("qt_interval", "QT interval"),
    138: ("qtc_interval", "QTc"),
}
NOT_REPORTED: Final = "not reported"
LEAD_UNVERIFIED: Final = "lead unverified"  # A2: the lead's identity is not known
NOT_A_MEDICAL_DEVICE: Final = (
    "ownvitals is not a medical device. The classification above is Withings' own result; "
    "ownvitals did not produce it. This is not a diagnosis."
)


def afib_meaning(code: int | None) -> str | None:
    """The documented meaning of ``afib``, or ``None`` for a missing or undocumented code."""
    return None if code is None else AFIB_MEANING.get(code)


def details_meaning(code: int | None) -> str | None:
    return None if code is None else DETAILS_MEANING.get(code)


def model_name(model_id: int | None) -> str | None:
    return None if model_id is None else MODEL_NAME.get(model_id)


def classification_text(code: int | None) -> str:
    """The attributed sentence for ``afib`` (PLAN 7.2)."""
    if code is None:
        return f"Withings classification: {NOT_REPORTED}"
    meaning = afib_meaning(code)
    shown = meaning if meaning is not None else f"Withings code {code}"
    return f"Withings classified this ECG as: {shown}"


def details_text(code: int | None) -> str | None:
    if code is None:
        return None
    meaning = details_meaning(code)
    return f"Withings detail: {meaning if meaning is not None else f'Withings code {code}'}"


def lead_label(lead: int, leads: int) -> str:
    """A2: never a lead name; several arrays are numbered rows."""
    return LEAD_UNVERIFIED if leads == 1 else f"{LEAD_UNVERIFIED} (array {lead + 1} of {leads})"


def interval_value(value_raw: int, unit: int) -> float:
    """A Withings measure: ``value_raw`` times ten to the ``unit``."""
    return float(Decimal(value_raw).scaleb(unit))


__all__ = [
    "AFIB_MEANING",
    "DETAILS_MEANING",
    "INTERVALS",
    "LEAD_UNVERIFIED",
    "MODEL_NAME",
    "NOT_A_MEDICAL_DEVICE",
    "NOT_REPORTED",
    "afib_meaning",
    "classification_text",
    "details_meaning",
    "details_text",
    "interval_value",
    "lead_label",
    "model_name",
]
