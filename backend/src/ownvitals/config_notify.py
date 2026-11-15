"""The notification settings (#195): kept out of ``ownvitals.config`` (a separate, small class)."""

from datetime import timedelta

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NotifySettings(BaseSettings):
    """``OWNVITALS_NOTIFY_HOLD_HOURS``: the bound of ADR 0006 section 11's E4/E6 notification
    hold, counted from the finding's first revision (default 24, at most 24)."""

    model_config = SettingsConfigDict(
        env_prefix="OWNVITALS_", env_ignore_empty=True, extra="ignore"
    )

    notify_hold_hours: float = Field(default=24.0, gt=0, le=24)

    @property
    def hold_bound(self) -> timedelta:
        return timedelta(hours=self.notify_hold_hours)
