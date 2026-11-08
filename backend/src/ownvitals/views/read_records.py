"""The plain records the read API's reader returns (PLAN section 8). Pure data.

Split from ``views.reads`` (no behaviour change). Nothing here carries a Withings identifier:
``source_grpid``, the hashed device id and the connection id stay in the reader's SQL.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from ownvitals.views.states import ConnectionFacts, StreamFacts


@dataclass(frozen=True, slots=True)
class DailyRecord:
    metric_key: str
    day: date
    value: Decimal
    method: str
    n: int
    minimum: Decimal
    maximum: Decimal
    mad: Decimal
    algo_version: str
    input_revision: int


@dataclass(frozen=True, slots=True)
class DayReading:
    id: UUID
    ts: datetime
    source_tz: str
    value: Decimal
    quality_flags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReadingDetail:
    id: UUID
    metric_key: str
    ts: datetime
    source_ts: datetime
    source_tz: str
    value: Decimal
    value_raw: int
    unit_exponent: int
    conversion: str
    quality_flags: tuple[str, ...]
    algo_version: str
    input_revision: int
    input_fingerprint: bytes
    attrib: int | None
    category: int | None
    device_model: int | None
    device_type: str | None
    device_firmware: str | None


@dataclass(frozen=True, slots=True)
class StatusFacts:
    connection: ConnectionFacts | None
    streams: dict[str, StreamFacts] = field(default_factory=dict)
    last_reading_at: datetime | None = None
    last_sync_at: datetime | None = None
