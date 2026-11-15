"""Pure assembly of the segmental tiles (issue #324): ``segmental_muscle_mass:<position>``.

Segmental values have no daily value, so no typical range and no sparkline: the tile carries the
latest reading and the data state of the ``measures`` stream, like any other tile. The reading is
kept while the stream is blocked, as ``assemble.tile`` does (the page decides by state whether to
show it). A position with a reading is listed; one without is listed only while the stream is
blocked, so the page says "not supported", "not shared" and so on instead of "not available"
(the page reads an unlisted position as not available). No database, clock or Withings.
"""

from collections.abc import Sequence
from typing import Final

from ownvitals.views import states
from ownvitals.views.read_records import SegmentalRecord
from ownvitals.views.schemas import LatestOut, MetricTileOut

SEGMENT_KEY: Final = "segmental_muscle_mass"
STREAM: Final = "measures"
UNIT: Final = "kg"
# The positions the body page shows; a blocked stream lists these even without a reading.
PAGE_POSITIONS: Final = ("left_arm", "right_arm", "left_leg", "right_leg")


def _tile(key: str, record: SegmentalRecord | None, stream: states.StreamState) -> MetricTileOut:
    return MetricTileOut(
        key=key,
        area="body",
        unit=UNIT,
        stream=STREAM,
        state=stream.state,
        progress_percent=stream.progress_percent,
        progress=None,
        latest=None
        if record is None
        else LatestOut(day=record.day, value=float(record.value), n=1, method="reading"),
        typical_range=None,
        sparkline=[],
    )


def tiles(records: Sequence[SegmentalRecord], stream: states.StreamState) -> list[MetricTileOut]:
    by_key: dict[str, SegmentalRecord | None] = {r.metric_key: r for r in records}
    if stream.state in states.BLOCKED:
        for position in PAGE_POSITIONS:
            by_key.setdefault(f"{SEGMENT_KEY}:{position}", None)
    return [_tile(key, by_key[key], stream) for key in sorted(by_key)]
