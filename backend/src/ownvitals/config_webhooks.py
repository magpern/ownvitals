"""The webhook settings of ``ownvitals.config`` (fields only; validators stay in ``Settings``)."""

from pydantic_settings import BaseSettings


class WebhookSettings(BaseSettings):
    """The webhook-secret key ids."""

    # The ids of the current webhook-secret seed (worker only) and pepper (web and worker). The
    # keys are the secret files ``webhook_seed`` and ``webhook_pepper`` (read by the crypto
    # package, never the environment); retired ones are ``webhook_seed_<id>`` and
    # ``webhook_pepper_<id>``. Optional in M0, required from M1 (ADR 0004 section 4).
    webhook_seed_id: str = "seed-1"
    webhook_pepper_id: str = "pep-1"
