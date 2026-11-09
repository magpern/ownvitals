"""The score settings (ADR 0012): kept out of ``ownvitals.config`` (a separate, small class)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ScoreSettings(BaseSettings):
    """``OWNVITALS_HIS_BAND_TABLE``: the file of the Health Improvement Score's band table. Unset,
    or a file that is not registered (``analysis.scores.his_table.REGISTERED``), leaves the score
    ``not_set``."""

    model_config = SettingsConfigDict(
        env_prefix="OWNVITALS_", env_ignore_empty=True, extra="ignore"
    )

    his_band_table: Path | None = None
