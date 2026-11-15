"""The AI evaluation's start locks and its run settings (ADR 0014 section 9; #527). Pure: no I/O.

``refusal(settings)`` is the first check of the evaluation's process, before it reads a key,
opens the database or allows a host: a closed code, or ``None`` when every lock is open.

- ``ai_eval`` is on (``OWNVITALS_AI_EVAL``; off by default): ``evaluation_off``;
- ``ai_eval_cap_microusd`` is above 0, the run's whole budget: ``cap_unset``;
- a model is set and priced, and the provider review is recorded: the model client's own codes
  (``ai_model_unset``, ``model_unpriced``, ``ai_provider_review_missing``);
- the database's name ends in ``_ai_eval``: ``not_an_evaluation_database``;
- the process holds no owner or admin credential (the owner's prepare step is a separate
  command with its own environment): ``owner_credentials_present``.

``run_settings(settings)`` is the copy the run's model calls use: the ``ai_insights`` switch
and the evaluation reference that the client's locks ask for (the run is what produces that
reference, so it cannot need it), the run's cap as both monthly caps, and the experimental tier
off. The copy lives only in the run.
"""

from collections.abc import Mapping

from sqlalchemy.engine import make_url

from ownvitals.ai.pricing import PRICES, ModelUnpriced, Price
from ownvitals.ai.requests import eval_ref
from ownvitals.auth.synthetic import is_eval_database_name
from ownvitals.config import Settings


def _database_name(settings: Settings) -> str:
    try:
        return make_url(settings.database_url.get_secret_value()).database or ""
    except Exception:
        return ""


def _owner_credentials(settings: Settings) -> bool:
    if settings.database_owner_password is not None:
        return True
    if settings.database_maint_password is not None or settings.maint_database_url is not None:
        return True
    try:
        return bool(make_url(settings.migrations_database_url.get_secret_value()).password)
    except Exception:
        return True


def refusal(settings: Settings, prices: Mapping[str, Price] = PRICES) -> str | None:
    """``None`` when the evaluation may start; else the first closed lock's code."""
    if settings.ai_eval is not True:
        return "evaluation_off"
    if settings.ai_eval_cap_microusd <= 0:
        return "cap_unset"
    if not settings.ai_model:
        return "ai_model_unset"
    if settings.ai_model not in prices:
        return ModelUnpriced.code
    if not settings.ai_provider_review_ref.strip():
        return "ai_provider_review_missing"
    if not is_eval_database_name(_database_name(settings)):
        return "not_an_evaluation_database"
    if _owner_credentials(settings):
        return "owner_credentials_present"
    return None


def run_settings(settings: Settings) -> Settings:
    """The run's settings (see the module docstring); only after ``refusal`` is ``None``."""
    cap = settings.ai_eval_cap_microusd
    return settings.model_copy(
        update={
            "ai_insights": True,
            "ai_eval_ref": eval_ref(settings.ai_model),
            "ai_user_monthly_cap_microusd": cap,
            "ai_global_monthly_cap_microusd": cap,
            "ai_include_experimental": False,
            "experimental_findings": False,
        }
    )
