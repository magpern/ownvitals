"""The words for Withings' own ECG values: documented codes get a meaning, others are named by
code, a missing value is "not reported", and ``ecg_read`` never names the estimates column."""

import ast
from pathlib import Path

import pytest

from ownvitals.ecg import display
from ownvitals.views import ecg_read

SYNTHETIC = True


def test_a_documented_code_has_its_meaning_and_an_unknown_one_is_named_by_code() -> None:
    assert display.classification_text(1) == "Withings classified this ECG as: positive"
    assert display.classification_text(42) == "Withings classified this ECG as: Withings code 42"
    assert display.classification_text(None) == "Withings classification: not reported"
    assert display.afib_meaning(42) is None
    assert display.details_text(None) is None
    assert display.details_text(77) == "Withings detail: Withings code 77"


def test_the_lead_is_never_named() -> None:
    assert display.lead_label(0, 1) == "lead unverified"
    assert display.lead_label(1, 2) == "lead unverified (array 2 of 2)"


@pytest.mark.parametrize(("raw", "unit", "value"), [(95, 0, 95.0), (950, -1, 95.0), (1, 2, 100.0)])
def test_interval_values_apply_the_exponent(raw: int, unit: int, value: float) -> None:
    assert display.interval_value(raw, unit) == value


def test_the_reader_names_no_estimate_in_a_string() -> None:
    tree = ast.parse(Path(ecg_read.__file__).read_text(encoding="utf-8"))
    words = [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    assert not [w for w in words if "estimated" in w.lower() and not w.startswith("The ")]
