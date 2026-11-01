"""Database flags and settings with the stricter-wins rule (ADR 0011 section 7)."""

from ownvitals.flags import effective
from ownvitals.flags.core import BANNER_MAX_CHARS, Banner, TrialSetting

__all__ = ["BANNER_MAX_CHARS", "Banner", "TrialSetting", "effective"]
