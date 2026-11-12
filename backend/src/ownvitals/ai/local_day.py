"""The pure rule for the user's "today" in the AI tools' periods (issue #443).

Recording days are local (the profile's fixed zone, else each reading's own zone), so a period
anchored on ``now``'s UTC date leaves out the last hours of a local day that is ahead of UTC.
``local_today`` is the rule; ``local_day_read.today_of`` reads the zone. No I/O here.
"""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


def local_today(now: datetime, zone: str | None) -> date:
    """``now``'s date in ``zone``; the UTC date when the zone is absent or unknown."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    try:
        return now.astimezone(ZoneInfo(zone) if zone else UTC).date()
    except (ValueError, KeyError, OSError):
        return now.astimezone(UTC).date()
