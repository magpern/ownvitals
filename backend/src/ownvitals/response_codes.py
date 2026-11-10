"""The stable codes of user-facing responses, per route family (ADR 0015 section 6).

A response never carries an English sentence: it carries a code (and numbers, ISO dates or
allowlisted enums as parameters), and the frontend words the code from its catalogue
(``frontend/src/i18n/*/codes.ts``) with an English fallback for a code it does not know. This module
is the list. ``tests/test_response_codes.py`` checks that

- every code a route family emits is listed here,
- ``frontend/src/api/codes.json`` is exactly this list (the frontend test reads that file and
  checks every code has an English entry),
- a response with a code outside its family fails ``assert_response_code``.

Pure: no I/O. Log lines, admin-only output and the CLI stay English and are not listed.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

# Answers any family may give.
COMMON: Final = frozenset({"not_found", "not_authenticated", "forbidden", "invalid_input", "busy"})

# ``detail.reason`` of /api/auth/*. The OAuth outcomes of ``?oauth=`` are a separate word list
# worded by the frontend's errors area.
AUTH: Final = frozenset(
    {
        "account_exists",
        "busy",
        "email_taken",
        "forbidden",
        "invalid_code",
        "invalid_credentials",
        "invalid_invite",
        "invalid_token",
        "invite_invalid",
        "last_sign_in_method",
        "locale_not_offered",
        "mfa_required",
        "name_invalid",
        "no_passkeys",
        "no_pending_signup",
        "not_authenticated",
        "not_found",
        "passkey_failed",
        "passkey_limit",
        "passkey_signin_failed",
        "passkeys_unavailable",
        "password_rule",
        "provider_disabled",
        "qr_approval_not_allowed",
        "rate_limited",
        "reauth_required",
        "reauth_stale",
        "reauth_use_password",
        "signups_closed",
        "step_up_required",
        "too_many_streams",
        "totp_changed",
        "totp_not_enabled",
        "two_factor_required",
        "wrong_browser",
        "wrong_password",
    }
)

# A failed job's ``error_category`` (ADR 0005 section 6; ``jobs.errors.CATEGORIES``).
JOB_FAILURES: Final = frozenset(
    {
        "upstream_unavailable",
        "rate_limited",
        "lease_lost",
        "not_connected",
        "needs_reconnect",
        "key_unavailable",
        "decrypt_failed",
        "invalid_input",
        "account_disabled",
        "outbound_blocked",
        "internal",
        "ai_not_enabled",
        "ai_budget_exhausted",
        "ai_unavailable",
    }
)

# ``detail.reason`` of /api/jobs and its event stream.
JOBS: Final = COMMON | {
    "bad_last_event_id",
    "idempotency_key_invalid",
    "idempotency_key_missing",
    "idempotency_mismatch",
    "not_cancellable",
    "too_large",
    "too_many_streams",
    "unknown_kind",
}

# ``detail.reason`` of /api/connections, a connection's ``last_error`` and the disconnect
# ``removal`` note.
CONNECTIONS: Final = (
    COMMON
    | JOB_FAILURES
    | {
        "blocked_by_provider_plan",
        "invalid_body",
        "revoke_failed",
        "revoke_incomplete",
        "step_up_required",
        "unknown_provider",
    }
)

# The ``method.code`` of a metric detail; its numbers are in ``method.parameters``.
VIEWS: Final = COMMON | {"daily_summary"}

# ``detail.reason`` of /api/ai/* (#206): AI is not enabled (the flag is off), and a consent that
# names another text than the current one. The assistant (#208): the month's budget is used up, a
# turn of the conversation is still running, the chat limit, and a malformed idempotency key.
AI: Final = COMMON | {
    "ai_not_enabled",
    "ai_consent_outdated",
    "step_up_required",
    "ai_budget_exhausted",
    "ai_turn_in_progress",
    "rate_limited",
    "idempotency_mismatch",
}

# ``detail.reason`` of /api/exports (#211, ADR 0013 section 4); the download's cross-site refusal
# is a page that names its code in ``X-Ownvitals-Reason``.
EXPORTS: Final = COMMON | {
    "export_in_progress",
    "export_not_ready",
    "export_expired",
    "export_download_spent",
    "download_same_origin_required",
    "rate_limited",
    "reauth_required",
    "step_up_required",
}

FAMILIES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "auth": AUTH,
        "jobs": JOBS,
        "job_failures": JOB_FAILURES,
        "connections": CONNECTIONS,
        "views": VIEWS,
        "ai": AI,
        "exports": EXPORTS,
    }
)

# The answer of a disconnect or an account deletion whose upstream revoke did not finish.
REMOVAL_REVOKE_INCOMPLETE: Final = "revoke_incomplete"
# ``method.code`` of the metric detail.
METHOD_DAILY_SUMMARY: Final = "daily_summary"


def all_codes() -> list[str]:
    """Every code of every family, sorted: what the frontend catalogue must word."""
    return sorted(set().union(*FAMILIES.values()))


def is_allowed(family: str, code: str) -> bool:
    return code in FAMILIES[family]
