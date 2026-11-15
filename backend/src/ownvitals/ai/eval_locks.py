"""The model evaluation's locks and database URL rules (ADR 0014 sections 1 and 9; #527).
No I/O but the one read of the key file (``gates.read_api_key``); a refusal is a closed code.

The evaluation has locks of its own and bypasses none of a user's for user data (it serves no user):

- ``OWNVITALS_AI_EVAL=on`` (``ai_eval``, default off, set by the owner for one run);
- ``ai_provider_review_ref`` recorded (lock 2), ``ai_model`` set and priced, the API key file
  and ``app_secret_key`` (the audit key) present;
- ``ai_eval_cap_microusd`` above 0.

It never reads ``ai_eval_ref`` (lock 3: the run is what produces its value) or a user's consent
(it seeds consent for its own accounts), and it does not need ``ai_insights``: ``run_settings``
hands the client a **copy** of the settings in which those two hold, the caps are its own, and
the database URL is the evaluation database's. The copy gates nothing else and is never stored.

The database: ``ai_eval_database_url`` (app role) and ``ai_eval_migrations_database_url`` (owner
role) must name one database whose name ends in ``_ai_eval`` and that is **not** the app's
configured one (not by host, port and name, not by name alone). In prod a URL may not embed a
password (as the app's own).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy.engine import URL, make_url

from ownvitals.ai import audit
from ownvitals.ai.gates import ModelRefused, read_api_key
from ownvitals.ai.pricing import PRICES, Price
from ownvitals.ai.requests import eval_ref
from ownvitals.auth.synthetic import is_eval_database_name
from ownvitals.config import Settings


class EvalRefused(Exception):
    """The run does not start (or stops); ``code`` is closed and holds no value of a setting."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvalUrls:
    app: URL
    owner: URL


def check_locks(
    settings: Settings,
    prices: Mapping[str, Price] = PRICES,
    secrets_directory: Path | None = None,
) -> str:
    """The model id; ``EvalRefused`` unless every lock of the module's docstring holds."""
    if settings.ai_eval is not True:
        raise EvalRefused("eval_off")
    if not settings.ai_provider_review_ref.strip():
        raise EvalRefused("ai_provider_review_missing")
    model = settings.ai_model
    if not model:
        raise EvalRefused("ai_model_unset")
    if model not in prices:
        raise EvalRefused("ai_model_unpriced")
    if settings.ai_eval_cap_microusd <= 0:
        raise EvalRefused("eval_cap_zero")
    try:
        read_api_key(secrets_directory)
        audit.audit_key(settings)
    except (ModelRefused, audit.AuditNotConfigured):
        raise EvalRefused("ai_not_configured") from None
    return model


def _same_place(a: URL, b: URL) -> bool:
    return (a.host, a.port, a.database) == (b.host, b.port, b.database)


def _parse(secret: SecretStr | None) -> URL:
    if secret is None or not secret.get_secret_value().strip():
        raise EvalRefused("eval_database_unset")
    return make_url(secret.get_secret_value())


def check_urls(settings: Settings) -> EvalUrls:
    """The evaluation database's two URLs, or ``EvalRefused``."""
    app, owner = (
        _parse(settings.ai_eval_database_url),
        _parse(settings.ai_eval_migrations_database_url),
    )
    configured = settings.sqlalchemy_url()
    if _same_place(app, configured) or app.database == configured.database:
        raise EvalRefused("eval_database_is_app")
    if not is_eval_database_name(app.database or "") or not is_eval_database_name(
        owner.database or ""
    ):
        raise EvalRefused("eval_database_name")
    if not _same_place(app, owner):
        raise EvalRefused("eval_database_mismatch")
    if settings.environment == "prod" and (app.password or owner.password):
        raise EvalRefused("eval_url_password")
    return EvalUrls(app, owner)


def run_settings(settings: Settings, model: str, *, ceiling: int) -> Settings:
    """The copy the run's engines, seeder and model client use (see the module's docstring).

    ``ceiling`` is the most the evaluation database's ledger may hold this month, in micro-USD: what
    it held when the run started plus ``ai_eval_cap_microusd``, so the cap is per run."""
    return settings.model_copy(
        update={
            "database_url": settings.ai_eval_database_url,
            "migrations_database_url": settings.ai_eval_migrations_database_url,
            "maint_database_url": None,
            "ai_insights": True,
            "ai_eval_ref": eval_ref(model),
            "ai_user_monthly_cap_microusd": ceiling,
            "ai_global_monthly_cap_microusd": ceiling,
        }
    )
