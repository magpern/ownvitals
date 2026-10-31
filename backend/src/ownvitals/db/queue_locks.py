"""The advisory-lock namespaces of ADR 0005 section 9: one registry, so features never collide.

Every advisory lock uses the two-key form ``(namespace, key)`` with two ``int4``s and takes its
namespace from here; the SQL of ``db-functions`` uses the same numbers. A UUID key is hashed to
``int4`` with ``hashtext(id::text)``; a hash collision only makes one caller wait. A new advisory
lock adds its constant here, and its row to ADR 0005 section 9, in the PR that introduces it.
"""

from typing import Final

# 1: job creation, ``pg_advisory_xact_lock``, keyed by the owner id (ADR 0005 section 2).
JOB_CREATION: Final = 1
# 2: token refresh, ``pg_advisory_lock`` (session-scoped), keyed by the connection id (ADR 0004
# section 6; used from #22).
TOKEN_REFRESH: Final = 2
# 3: partitions, ``pg_advisory_xact_lock``, keyed by the parent table's oid (ADR 0002 section 2;
# used by #21's ensure_month_partitions).
PARTITIONS: Final = 3
# 4: webhook-secret rotation, ``pg_advisory_xact_lock``, keyed by the ``withings_apps`` id (ADR
# 0008 section 2, ADR 0004 section 4; used from M1).
WEBHOOK_SECRET_ROTATION: Final = 4

NAMESPACES: Final = {
    "job_creation": JOB_CREATION,
    "token_refresh": TOKEN_REFRESH,
    "partitions": PARTITIONS,
    "webhook_secret_rotation": WEBHOOK_SECRET_ROTATION,
}
