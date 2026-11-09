"""The checks before a model call that need no database (ADR 0014 sections 1 and 7; #207), and
the client's two outcomes besides an answer. Pure but for ``read_api_key``, which reads the
worker's secret file.

- ``check_locks``: the owner's locks that live in the settings: ``ai_insights`` on (the
  environment side; the stored flag is checked with consent, in the job's transaction), a pinned
  ``ai_model``, a recorded ``ai_provider_review_ref`` (lock 2), an ``ai_eval_ref`` that names
  exactly this model, prompt version and tool-set version (lock 3), and the model priced.
- ``read_api_key``: the secret file ``anthropic_api_key`` (mounted into the worker only); without
  it ``ai_not_configured``. The key is never a setting, so it is never on a ``Settings`` object.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

from ownvitals.ai import budget
from ownvitals.ai.audit import ErrorCategory
from ownvitals.ai.pricing import PRICES, ModelUnpriced, Price
from ownvitals.ai.requests import eval_ref
from ownvitals.config import Settings, read_secret_files, secrets_dir

if TYPE_CHECKING:
    import httpx

API_KEY_FILE: Final = "anthropic_api_key"
RETRY_AFTER_MAX_SECONDS: Final = 30.0


class ModelRefused(Exception):
    """No request was sent, and nothing stays reserved. ``code`` is a closed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ModelFailed(Exception):
    """A request was made and failed; the reservation is settled or released as the rule says.
    ``code`` is the audit's error category; ``retry_after`` a hint in seconds, if any."""

    def __init__(self, code: ErrorCategory, retry_after: float | None = None) -> None:
        super().__init__(code)
        self.code: ErrorCategory = code
        self.retry_after = retry_after


def check_locks(settings: Settings, prices: Mapping[str, Price] = PRICES) -> str:
    """The owner's locks that live in the settings; the model id. Pure."""
    if settings.ai_insights is not True:
        raise ModelRefused("ai_not_enabled")
    model = settings.ai_model
    if not model:
        raise ModelRefused("ai_model_unset")
    if not settings.ai_provider_review_ref.strip():
        raise ModelRefused("ai_provider_review_missing")
    if settings.ai_eval_ref != eval_ref(model):
        raise ModelRefused("ai_eval_mismatch")
    if model not in prices:
        raise ModelRefused(ModelUnpriced.code)
    return model


def read_api_key(directory: Path | None = None) -> str:
    """The provider key from the worker's secret file; ``ai_not_configured`` without it."""
    found = read_secret_files(directory or secrets_dir(), (API_KEY_FILE,)).get(API_KEY_FILE, "")
    if not found.strip():
        raise ModelRefused("ai_not_configured")
    return found


def retry_after_of(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    if seconds is None or not 0 <= seconds <= RETRY_AFTER_MAX_SECONDS:
        return None
    return seconds


def category_of(status: int) -> ErrorCategory:
    if status == 429:
        return "rate_limited"
    if status == 529:
        return "overloaded"
    if status in budget.RELEASABLE_STATUSES:
        return "refused"
    return "server_error" if status >= 500 else "http_other"
