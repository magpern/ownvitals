"""What the JSON/CSV export holds, as code (ADR 0013 section 1; #211). Pure: no I/O.

The builder exports a column only when it is listed here. Every owned table is classified:

- ``GENERIC``: read by ``ownvitals.reports.export_read`` under forced RLS, exactly the listed
  columns, one file per table (``data/<file>.json`` and ``csv/<file>.csv``);
- ``SPECIAL``: owned tables read by their own module (the ECG recordings, with their signal, by
  ``ownvitals.reports.export_ecg``; the rule findings only through the one read function,
  ``views.findings_read``, ADR 0006 section 11), with their columns listed in ``SPECIAL_COLUMNS``;
- ``NOT_EXPORTED_TABLES``: owned tables the export leaves out, each with its reason.

Every column of an exported table that is not exported is in ``NOT_EXPORTED_COLUMNS`` with its
reason. ``tests/test_reports_export_allowlist.py`` reads the migrated catalog and fails when a
table or column is in none of these lists, so a new one fails until it is classified. The account
tables (system tables, ADR 0003 section 1) are classified the same way in ``ownvitals.auth.
exports``, the only module that reads them for the export.

Never exported (ADR 0013 section 1): tokens, client or webhook secrets, any hash, HMAC or
ciphertext, job rows and events, rate-limit buckets, system tables, other users' anything.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

# The two owned tables read by their own modules, named once (``tests/test_findings_read_guard.py``
# pins each string here, with the estimates' column and label below).
ECG_TABLE: Final = "ecg_recordings"
FINDINGS_TABLE: Final = "findings"
_HASH: Final = "a hash of the stored payload or inputs (ADR 0013 section 1: no hash)"
_OWNER: Final = "the account's own id, given once in manifest.json"


@dataclass(frozen=True, slots=True)
class TableSpec:
    """One exported owned table: its file stem, the columns in file order, and whether a
    ``value`` (``value_raw * 10 ** unit``, the real value) is added after them."""

    file: str
    table: str
    columns: tuple[str, ...]
    real_value: bool = False


GENERIC: Final[tuple[TableSpec, ...]] = (
    TableSpec("user_profiles", "user_profiles", (
        "id", "birth_year", "sex", "day_assignment", "day_tz", "theme", "weight_unit",
        "temperature_unit", "datetime_format", "chart_range", "overview_order",
        "show_unsupported", "show_experimental", "notify_email", "notify_weekly",
        "notify_tiers", "updated_at",
    )),
    TableSpec("connections", "connections", (
        "id", "provider", "status", "scopes", "connected_at", "updated_at", "withings_userid",
    )),
    TableSpec("devices", "devices", (
        "id", "connection_id", "hash_deviceid", "model_id", "type", "fw", "battery",
        "last_session_at", "source_tz",
    )),
    TableSpec("measure_groups", "measure_groups", (
        "id", "connection_id", "grpid", "measured_at", "source_tz", "attrib", "category",
        "model_id", "hash_deviceid", "created_at", "modified_at", "deleted_at", "raw",
    )),
    TableSpec("measures", "measures", (
        "id", "measure_group_id", "type", "value_raw", "unit", "position",
    ), real_value=True),
    TableSpec("observations", "observations", (
        "id", "metric_key", "ts", "source_ts", "source_tz", "value", "source_value_raw",
        "source_unit", "conversion", "quality_flags", "source_grpid", "source_type",
        "source_position", "algo_version", "input_revision",
    )),
    TableSpec("metric_daily", "metric_daily", (
        "id", "metric_key", "day", "method", "value", "morning", "mean", "min", "max", "mad",
        "n", "source_groups", "algo_version", "input_revision",
    )),
    TableSpec("sleep_summaries", "sleep_summaries", (
        "id", "connection_id", "day", "start_at", "end_at", "source_tz", "model_id", "raw",
    )),
    TableSpec("sleep_series", "sleep_series", (
        "id", "connection_id", "ts", "source_tz", "series", "value_raw", "unit",
    ), real_value=True),
    TableSpec("activity_daily", "activity_daily", (
        "id", "connection_id", "day", "start_at", "source_tz", "raw",
    )),
    TableSpec("workouts", "workouts", (
        "id", "connection_id", "workout_id", "start_at", "end_at", "source_tz", "category", "raw",
    )),
    TableSpec("intraday", "intraday", (
        "id", "connection_id", "ts", "source_tz", "metric", "value_raw", "unit",
    ), real_value=True),
    TableSpec("scores", "scores", (
        "id", "kind", "day", "value", "components", "algo_version", "input_revision",
    )),
    TableSpec("ai_consents", "ai_consents", (
        "id", "consent_version", "locale", "given_at", "withdrawn_at",
    )),
    TableSpec("ai_conversations", "ai_conversations", ("id", "created_at", "updated_at")),
    TableSpec("ai_messages", "ai_messages", (
        "id", "conversation_id", "role", "content", "created_at",
    )),
)  # fmt: skip

# Owned tables read by their own module, with the columns that module exports.
SPECIAL: Final[Mapping[str, str]] = {
    ECG_TABLE: "ownvitals.reports.export_ecg: Withings' values and the decoded signal",
    FINDINGS_TABLE: "views.findings_read (consumer export): every finding with its overlay state",
}
SPECIAL_COLUMNS: Final[Mapping[str, tuple[str, ...]]] = {
    ECG_TABLE: (
        "id", "connection_id", "signalid", "recorded_at", "source_tz", "model_id", "afib",
        "heart_rate", "sampling_hz", "wearposition", "reported", "afib_details", "signal",
    ),
    FINDINGS_TABLE: (
        "id", "rule_id", "rule_version", "tier", "severity", "confidence", "data_quality",
        "origin", "metric_keys", "subject", "window_start", "window_end", "reference_day",
        "evidence", "status", "first_seen", "last_seen", "ended_at", "notify_state",
        "notified_at", "seen_at",
    ),
}  # fmt: skip

NOT_EXPORTED_TABLES: Final[Mapping[str, str]] = {
    "jobs": "job rows (ADR 0013 section 1)",
    "job_events": "job events (ADR 0013 section 1)",
    "sync_cursors": "sync bookkeeping of the Withings streams, no data of the user",
    "data_revisions": "revision counters of the analysis inputs, no data of the user",
    "notify_holds": "notification bookkeeping (rule ids and times), no data of the user",
    "mail_outbox": "delivery state of notification mails (a fixed kind and route), no content",
    "ai_spend": "the AI cost cap's accounting (micro-USD), no data of the user",
    "ai_reservations": "the AI cost cap's reservations, no data of the user",
    "exports": "the export's own record",
    "export_chunks": "the bytes of exports themselves",
}

NOT_EXPORTED_COLUMNS: Final[Mapping[str, Mapping[str, str]]] = {
    "user_profiles": {"owner_id": _OWNER},
    "connections": {
        "owner_id": _OWNER,
        "last_error": "an internal error category of the connection",
        "public": "provider bookkeeping of the token (no data of the user)",
        "secret": "the sealed tokens (ciphertext)",
        "secret_nonce": "a nonce of the sealed tokens",
        "wrapped_dek": "a wrapped data key (ciphertext)",
        "dek_nonce": "a nonce of the wrapped data key",
        "key_id": "the key-encryption key's id (key handling)",
        "token_generation": "token rotation bookkeeping",
        "refresh_in_flight": "token rotation bookkeeping",
        "refresh_intent_at": "token rotation bookkeeping",
        "expires_at": "the access token's expiry (token bookkeeping)",
        "last_used_at": "token bookkeeping",
        "app_id": "the id of the system's Withings app (a system table)",
    },
    "devices": {"owner_id": _OWNER},
    "measure_groups": {"owner_id": _OWNER, "payload_hash": _HASH},
    "measures": {"owner_id": _OWNER},
    "observations": {"owner_id": _OWNER, "input_fingerprint": _HASH},
    "metric_daily": {"owner_id": _OWNER, "input_fingerprint": _HASH},
    "sleep_summaries": {"owner_id": _OWNER, "payload_hash": _HASH},
    "sleep_series": {"owner_id": _OWNER},
    "activity_daily": {"owner_id": _OWNER, "payload_hash": _HASH},
    "workouts": {"owner_id": _OWNER, "payload_hash": _HASH},
    "intraday": {"owner_id": _OWNER},
    "scores": {"owner_id": _OWNER, "input_fingerprint": _HASH},
    "ai_consents": {"owner_id": _OWNER, "text_sha256": "a hash of the consent text shown"},
    "ai_conversations": {"owner_id": _OWNER},
    "ai_messages": {"owner_id": _OWNER},
    ECG_TABLE: {
        "owner_id": _OWNER,
        "payload_hash": _HASH,
        "estimated": "ownvitals' estimates: only up to date, through the read function, in "
        "ecg_estimates (ADR 0006 section 9)",
    },
    FINDINGS_TABLE: {
        "owner_id": _OWNER,
        "input_revision": "the rules engine's input bookkeeping",
        "input_fingerprint": _HASH,
    },
}

# The estimates' own file (ADR 0013 section 2): its header and manifest entry.
ESTIMATES_FILE: Final = "ecg_estimates"
ESTIMATES_LABEL: Final = "Estimated by ownvitals from the raw signal; not a Withings measurement"
# What an account without the experimental tier does not receive, and why.
WITHHELD_WITHOUT_TIER: Final = ("ecg_estimates", "experimental_findings")
WITHHELD_REASON: Final = "experimental_tier_not_enabled"


def generic_tables() -> frozenset[str]:
    return frozenset(spec.table for spec in GENERIC)


def exported_columns(table: str) -> tuple[str, ...]:
    """The exported columns of an owned table (``()`` for one that is not exported)."""
    for spec in GENERIC:
        if spec.table == table:
            return spec.columns
    return SPECIAL_COLUMNS.get(table, ())
