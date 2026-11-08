"""Response models of the read API (``/api/status``, ``/api/metrics``, ``/api/days``,
``/api/readings``, ``/api/nerve/summary``). No Withings identifier, token or hashed device id is
a field of any of them. A day with no value is ``value: null``, never ``0``; a stream that is not
supported (or not shared, not available, ...) carries that state and no count.
"""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from ownvitals.views.findings_routes import FindingOut


class ProgressOut(BaseModel):
    """Progress toward the minimum-data policy (the design's "insufficient data" state)."""

    days_with_data: int
    days_required: int
    span_days: int
    span_required: int | None
    shortfalls: list[str]
    fraction: float


class StreamStatusOut(BaseModel):
    stream: str
    state: str
    capability: str | None
    progress_percent: int | None  # importing and incomplete imports
    last_ok_at: datetime | None
    skipped_out_of_range: int


class StatusOut(BaseModel):
    """The freshness line: last reading, last sync, imports, broken connections."""

    as_of: datetime
    connection: str  # not_connected | connected | needs_reconnect | withings_plus_needed
    last_reading_at: datetime | None
    last_sync_at: datetime | None
    streams: list[StreamStatusOut]
    importing: list[str]
    incomplete: list[str]
    broken_connections: list[str]


class BandOut(BaseModel):
    """The display band (ADR 0010 section 2): median plus or minus ``half_width``."""

    median: float
    low: float
    high: float
    half_width: float
    k: float
    window_days: int
    as_of: date


class LatestOut(BaseModel):
    day: date
    value: float
    n: int
    method: str


class SparkPoint(BaseModel):
    day: date
    value: float | None  # null on a day without a value


class MetricTileOut(BaseModel):
    key: str
    area: str
    unit: str
    stream: str
    state: str
    progress_percent: int | None
    progress: ProgressOut | None
    latest: LatestOut | None
    typical_range: BandOut | None
    sparkline: list[SparkPoint]


class MetricsOut(BaseModel):
    as_of: datetime
    metrics: list[MetricTileOut]


class SeriesPoint(BaseModel):
    day: date
    value: float | None  # a gap is null, never 0
    n: int
    method: str | None
    minimum: float | None
    maximum: float | None


class UnconfirmedDay(BaseModel):
    day: date
    n: int


class TrendOut(BaseModel):
    """Theil-Sen slope with the Mann-Kendall test (AR(1)-corrected); the slope is per day."""

    state: str  # ok | insufficient_data
    progress: ProgressOut | None = None
    direction: str | None = None
    significant: bool | None = None
    slope_per_day: float | None = None
    slope_low: float | None = None
    slope_high: float | None = None
    p_value: float | None = None
    alpha: float | None = None
    n_days: int | None = None
    correction: str | None = None
    algo_version: str | None = None


class ChangePointOut(BaseModel):
    day: date
    previous_day: date
    mean_before: float
    mean_after: float
    shift: float
    shift_sigmas: float | None


class ChangePointsOut(BaseModel):
    """Only on a series that passes the minimum-data policy; otherwise ``insufficient_data``."""

    state: str  # ok | insufficient_data
    progress: ProgressOut | None = None
    searched: bool = False
    points: list[ChangePointOut] = []
    algo_version: str | None = None


class MethodOut(BaseModel):
    code: str  # a response code of the ``views`` family (``ownvitals.response_codes``)
    parameters: dict[str, Any]
    algo_versions: dict[str, str]


class ProvenanceOut(BaseModel):
    algo_versions: list[str]
    input_revision: int | None
    current_revision: int
    recompute_pending: bool


class MetricDetailOut(BaseModel):
    key: str
    area: str
    unit: str
    stream: str
    state: str
    progress_percent: int | None
    range: str
    start: date
    end: date
    latest: LatestOut | None
    series: list[SeriesPoint]
    unconfirmed: list[UnconfirmedDay]
    band: BandOut | None
    band_progress: ProgressOut | None
    trend: TrendOut
    change_points: ChangePointsOut
    data_quality: dict[str, Any] | None
    method: MethodOut
    provenance: ProvenanceOut


class DayReadingOut(BaseModel):
    id: UUID
    at: datetime
    local_time: str
    source_tz: str
    value: float
    quality_flags: list[str]
    used: bool  # false for an unconfirmed, implausible or duplicate reading


class DayDailyOut(BaseModel):
    value: float
    method: str
    n: int
    minimum: float
    maximum: float
    mad: float
    algo_version: str


class AlsoRecordedOut(BaseModel):
    metric_key: str
    n_readings: int


class DayOut(BaseModel):
    key: str
    day: date
    unit: str
    daily: DayDailyOut | None
    readings: list[DayReadingOut]
    also_recorded: list[AlsoRecordedOut]


class GateOut(BaseModel):
    gate: str  # confirmed | plausible | not_duplicate | no_jump
    passed: bool
    advisory: bool


class RawOut(BaseModel):
    """The stored value is ``value_raw x 10^unit_exponent`` (Withings' own encoding)."""

    value_raw: int
    unit_exponent: int
    conversion: str


class DeviceOut(BaseModel):
    model_id: int | None
    type: str | None
    firmware: str | None


class ReadingProvenanceOut(BaseModel):
    algo_version: str
    input_revision: int
    input_fingerprint: str  # hex
    recompute_pending: bool


class ReadingOut(BaseModel):
    id: UUID
    metric_key: str
    unit: str
    at: datetime
    source_at: datetime
    source_tz: str
    local_day: date
    value: float
    value_exact: str
    raw: RawOut
    attrib: int | None
    attrib_key: str | None
    category: int | None
    device: DeviceOut | None
    quality_flags: list[str]
    gates: list[GateOut]
    used_in_daily: bool
    provenance: ReadingProvenanceOut


class NerveMetricMonth(BaseModel):
    metric_key: str
    days_with_data: int
    median: float | None
    minimum: float | None
    maximum: float | None


class NerveMonthOut(BaseModel):
    month: str  # YYYY-MM
    metrics: list[NerveMetricMonth]
    findings: list[FindingOut]


class NerveMetricOut(BaseModel):
    key: str
    unit: str
    state: str
    progress_percent: int | None
    progress: ProgressOut | None
    latest: LatestOut | None


class NerveSummaryOut(BaseModel):
    """ADR 0010 section 8: the monthly summary of the nerve metrics and of N2 and N3."""

    as_of: datetime
    metrics: list[NerveMetricOut]
    months: list[NerveMonthOut]
