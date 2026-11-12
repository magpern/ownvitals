"""The per-area reason codes of HIS (ADR 0012 §1; G4: codes only). Synthetic inputs."""

from ownvitals.analysis.scores.his_reasons import area_reason, vertical_reasons

SYNTHETIC = True


def test_the_actionable_reason_wins() -> None:
    assert area_reason(["no_data", "insufficient_data", "no_band_row"]) == "insufficient_data"
    assert area_reason(["no_height", "insufficient_data"]) == "no_height"
    assert area_reason(["birth_year_unset", "no_height"]) == "birth_year_unset"


def test_provider_codes_map_to_the_area_vocabulary() -> None:
    assert area_reason(["ecg_not_supported"]) == "not_supported"
    assert area_reason(["input_not_wired", "no_data", None]) == "not_available"
    assert area_reason([None]) == "not_available"
    assert area_reason([]) == "not_available"


def test_a_scored_area_has_no_reason_and_each_unscored_one_has_its_own() -> None:
    criteria = {
        "bmi": {"reason": "no_height"},
        "night_hr": {"reason": "insufficient_data"},
        "daily_steps": {},
        "sleep_ahi": {"reason": "no_band_row"},
    }
    areas = {"bmi": "body", "night_hr": "heart", "daily_steps": "activity", "sleep_ahi": "sleep"}
    scores = {"body": None, "heart": None, "activity": 80.0, "sleep": None}
    assert vertical_reasons(areas, criteria, scores) == {
        "body": "no_height",
        "heart": "insufficient_data",
        "activity": None,
        "sleep": "no_band_row",
    }
