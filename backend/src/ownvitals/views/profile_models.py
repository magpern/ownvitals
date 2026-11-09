"""The shapes of ``GET /api/profile`` and ``PUT /api/profile`` (issue #203; PLAN section 8
"Settings"; the ``user_profiles`` table of #181).

Pure: validation only, no database, no clock (the caller passes the current year). Every value is
checked here before it reaches a column, and the columns repeat the same limits as CHECK
constraints. A body with an unknown key is refused, so a client cannot write a column this API
does not own.
"""

from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sex = Literal["female", "male"]
DayAssignment = Literal["reading_tz", "fixed_tz"]
Theme = Literal["system", "light", "dark"]
WeightUnit = Literal["kg", "lb", "st"]
TemperatureUnit = Literal["c", "f"]
DatetimeFormat = Literal["d_mon_y", "mon_d_y", "iso"]
ChartRange = Literal["30d", "90d", "6mo", "1y"]
Area = Literal["body", "heart", "nerve", "activity"]
Tier = Literal["device", "trend", "experimental", "system"]

FIRST_BIRTH_YEAR = 1900


def known_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError, OSError):
        return False
    return True


class ProfileIn(BaseModel):
    """What the Settings screen saves: every field, as one document."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    birth_year: int | None = Field(ge=FIRST_BIRTH_YEAR, le=2100)
    sex: Sex | None
    day_assignment: DayAssignment
    day_tz: str | None = Field(max_length=64)
    theme: Theme
    weight_unit: WeightUnit
    temperature_unit: TemperatureUnit
    datetime_format: DatetimeFormat
    chart_range: ChartRange
    overview_order: list[Area] = Field(max_length=4)
    show_unsupported: bool
    show_experimental: bool
    notify_email: bool
    notify_weekly: bool
    notify_tiers: list[Tier] = Field(max_length=4)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.day_assignment == "reading_tz" and self.day_tz is not None:
            raise ValueError("day_tz is only for a fixed zone")
        if self.day_assignment == "fixed_tz" and (
            self.day_tz is None or not known_zone(self.day_tz)
        ):
            raise ValueError("a fixed zone needs a known IANA zone name")
        if len(set(self.overview_order)) != len(self.overview_order):
            raise ValueError("overview_order repeats an area")
        if len(set(self.notify_tiers)) != len(self.notify_tiers):
            raise ValueError("notify_tiers repeats a tier")
        return self


class ProfileOut(ProfileIn):
    """The stored profile (the defaults when none is stored yet) and two read-only states: the
    experimental switch and AI insights as they stand for this account."""

    experimental_available: bool
    ai_available: bool


DEFAULTS = {
    "birth_year": None,
    "sex": None,
    "day_assignment": "reading_tz",
    "day_tz": None,
    "theme": "system",
    "weight_unit": "kg",
    "temperature_unit": "c",
    "datetime_format": "d_mon_y",
    "chart_range": "90d",
    "overview_order": ["body", "heart", "nerve", "activity"],
    "show_unsupported": True,
    "show_experimental": False,
    "notify_email": False,
    "notify_weekly": False,
    "notify_tiers": ["device", "trend", "experimental", "system"],
}


def moves_the_analysis(old: ProfileIn, new: ProfileIn) -> bool:
    """Whether the change alters what the daily and rule passes compute: sex (score bands, E4)
    or the day assignment. Display and notification choices never do."""
    return (old.sex, old.day_assignment, old.day_tz) != (new.sex, new.day_assignment, new.day_tz)
