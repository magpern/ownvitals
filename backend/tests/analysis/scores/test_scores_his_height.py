"""The HIS height bound (#492): an engineering bound, not a health range. Synthetic values."""

import math
from datetime import date

import pytest

from ownvitals.analysis.scores.his_inputs import HEIGHT_M_MAX, HEIGHT_M_MIN, bmi, pick_height

SYNTHETIC = True


@pytest.mark.parametrize("metres", [0.5, 1.75, 2.6])  # both edges inclusive
def test_an_in_bound_height_is_used(metres: float) -> None:
    assert pick_height([("2026-01-02", metres)]) == (("2026-01-02", metres), False)


@pytest.mark.parametrize("metres", [0.17, 0.499, 2.601, 17.5, 0.0, math.nan, math.inf])
def test_an_out_of_bound_height_is_refused(metres: float) -> None:
    assert pick_height([("2026-01-02", metres)]) == (None, True)


def test_a_bad_newer_height_never_hides_a_good_older_one() -> None:
    newer_first = [("2026-02-01", 17.5), ("2026-01-15", 1.7), ("2026-01-01", 1.6)]
    assert pick_height(newer_first) == (("2026-01-15", 1.7), True)


def test_no_height_at_all_is_not_a_rejection() -> None:
    assert pick_height([]) == (None, False)


def test_the_bound_constants() -> None:
    assert (HEIGHT_M_MIN, HEIGHT_M_MAX) == (0.5, 2.6)


def test_bmi_names_why_there_is_no_height() -> None:
    day = date(2026, 1, 31)
    assert bmi([], None, day).reason == "no_height"
    assert bmi([], None, day, True).reason == "implausible_height"
