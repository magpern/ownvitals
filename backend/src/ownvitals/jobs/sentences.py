"""One plain sentence per failure category (ADR 0005 section 6): what the user sees of a failed
job. Never error, exception or provider text. ``tests/test_jobs_errors.py`` checks that every
category has one."""

from typing import Final

SENTENCES: Final[dict[str, str]] = {
    "upstream_unavailable": "The service did not answer in time. Try again later.",
    "rate_limited": "Too many requests were made for now. Try again later.",
    "lease_lost": "The job stopped unexpectedly. Try again.",
    "not_connected": "Connect your Withings account first.",
    "needs_reconnect": "Your Withings connection needs to be renewed. Reconnect it.",
    "key_unavailable": "Your connection cannot be used right now. Try again later.",
    "decrypt_failed": "Your connection could not be read. Reconnect your Withings account.",
    "invalid_input": "The request for this job was not valid.",
    "account_disabled": "Your account is disabled.",
    "outbound_blocked": "The job tried to reach a service it may not use.",
    "internal": "Something went wrong on our side.",
}


def failure_sentence(state: str, category: str | None) -> str | None:
    """The sentence for a failed job, or ``None``."""
    if state != "failed" or category is None:
        return None
    return SENTENCES.get(category, SENTENCES["internal"])
