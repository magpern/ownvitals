"""The score settings (ADR 0012): kept out of ``ownvitals.config`` (a separate, small class)."""

from pathlib import Path
from typing import Any, Final

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HIS_BAND_TABLE: Final = (
    Path(__file__).parent / "analysis" / "scores" / "his_tables" / "withings_card_v1.json"
)


class ScoreSettings(BaseSettings):
    """``OWNVITALS_HIS_BAND_TABLE``: the file of the Health Improvement Score's band table. Default:
    the shipped Withings card table (owner decision 2026-11-12). Set to an empty value, or a file
    that is not registered (``analysis.scores.his_table.REGISTERED``), and the score is
    ``not_set``."""

    model_config = SettingsConfigDict(
        env_prefix="OWNVITALS_", env_ignore_empty=False, extra="ignore"
    )

    his_band_table: Path | None = DEFAULT_HIS_BAND_TABLE

    @field_validator("his_band_table", mode="before")
    @classmethod
    def _empty_is_unset(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v
