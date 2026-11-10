"""The rate functions', the mail outbox's, the AI cost cap's and the export purge's expectations
of the queue check (``ownvitals.db.queue_check``, ADR 0005 section 3, guard 9): names, versions,
owners and pins of the functions admitted beside the queue. Split from
``ownvitals.db.queue_expected``; no logic change. ``ownvitals.db.queue_check`` re-exports every
public name here.
"""

SEARCH_PATH = "search_path=pg_catalog, pg_temp"  # same value as queue_expected.SEARCH_PATH

# Admitted with the Withings functions (20-queue.sql, rate.sql). Absent until the migration
# and rate.sql have run; once present, pinned the same way.
RATE_FUNCTION_NAMES = (
    "withings_rate_claim",
    "withings_rate_release",
    "withings_rate_pause",
)
RATE_VERSION = "ownvitals-rate v1"
_RATE_CONFIG = "search_path=pg_catalog, pg_temp;TimeZone=UTC"
_RATE_ACL = "ownvitals_app=X/ownvitals_owner,ownvitals_owner=X/ownvitals_owner"
RATE_PINS: dict[str, tuple[str, str, str, str]] = {
    "withings_rate_claim": (
        "p_app_id integer, p_account_id uuid, p_lane integer, p_lease_seconds integer",
        "ownvitals_owner",
        _RATE_CONFIG,
        _RATE_ACL,
    ),
    "withings_rate_release": ("p_lease_id uuid", "ownvitals_owner", _RATE_CONFIG, _RATE_ACL),
    "withings_rate_pause": (
        "p_app_id integer, p_wait_seconds double precision",
        "ownvitals_owner",
        _RATE_CONFIG,
        _RATE_ACL,
    ),
}
# The mail outbox's family (docker/postgres/notify/50-outbox.sql, ADR 0007 section 3; #194),
# owned by its own NOLOGIN role. Absent until db-functions has run that script; once present,
# pinned the same way.
OUTBOX_ROLE = "ownvitals_mail_outbox"
OUTBOX_FUNCTION_NAMES = (
    "mail_outbox_claim",
    "mail_outbox_mark",
    "owner_notice_queue",
)
OUTBOX_VERSION = "ownvitals-outbox v1"
_OUTBOX_ACL = f"ownvitals_app=X/{OUTBOX_ROLE},{OUTBOX_ROLE}=X/{OUTBOX_ROLE}"
OUTBOX_PINS: dict[str, tuple[str, str, str, str]] = {
    "mail_outbox_claim": (
        "p_limit integer, p_lease_seconds integer",
        OUTBOX_ROLE,
        "search_path=pg_catalog, pg_temp",
        _OUTBOX_ACL,
    ),
    "mail_outbox_mark": (
        "p_id uuid, p_outcome text",
        OUTBOX_ROLE,
        "search_path=pg_catalog, pg_temp",
        _OUTBOX_ACL,
    ),
    "owner_notice_queue": (
        "p_kind text",
        OUTBOX_ROLE,
        "search_path=pg_catalog, pg_temp",
        _OUTBOX_ACL,
    ),
}
# The AI cost cap's family (docker/postgres/ai/50-ai.sql, ADR 0014 section 7; #205), owned by
# its own NOLOGIN role, with the audit log's retention purge (ADR 0014 section 6; #331). Absent
# until db-functions has run that script; once one is present, all five must be, each pinned (its
# digest in queue_digests.py with the others).
AI_ROLE = "ownvitals_ai"
AI_FUNCTION_NAMES = ("ai_reserve", "ai_settle", "ai_release", "ai_settle_lost", "ai_audit_purge")
AI_VERSION = "ownvitals-ai v1"
# Sorted as the query sorts the entries: "ownvitals_ai" comes before "ownvitals_app".
_AI_ACL = f"{AI_ROLE}=X/{AI_ROLE},ownvitals_app=X/{AI_ROLE}"
AI_PINS: dict[str, tuple[str, str, str, str]] = {
    "ai_reserve": (
        "p_amount bigint, p_job_id uuid, p_user_cap bigint, p_global_cap bigint",
        AI_ROLE,
        SEARCH_PATH,
        _AI_ACL,
    ),
    "ai_settle": ("p_id uuid, p_actual bigint", AI_ROLE, SEARCH_PATH, _AI_ACL),
    "ai_release": ("p_id uuid", AI_ROLE, SEARCH_PATH, _AI_ACL),
    "ai_settle_lost": ("p_limit integer", AI_ROLE, SEARCH_PATH, _AI_ACL),
    "ai_audit_purge": ("p_months integer, p_limit integer", AI_ROLE, SEARCH_PATH, _AI_ACL),
}
# The export's cross-user purge (docker/postgres/exports/60-exports.sql, ADR 0013 section 4;
# #333), owned by its own NOLOGIN role. Absent until db-functions has run that script; once
# present, pinned the same way (its digest in queue_digests.py with the others).
EXPORTS_ROLE = "ownvitals_exports"
EXPORTS_FUNCTION_NAMES = ("exports_purge_expired",)
EXPORTS_VERSION = "ownvitals-exports v1"
# Sorted as the query sorts the entries: "ownvitals_app" comes before "ownvitals_exports".
_EXPORTS_ACL = f"ownvitals_app=X/{EXPORTS_ROLE},{EXPORTS_ROLE}=X/{EXPORTS_ROLE}"
EXPORTS_PINS: dict[str, tuple[str, str, str, str]] = {
    "exports_purge_expired": (
        "OUT expired integer, OUT chunks integer, OUT deleted integer",
        EXPORTS_ROLE,
        SEARCH_PATH,
        _EXPORTS_ACL,
    ),
}
