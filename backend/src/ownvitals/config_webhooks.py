"""The webhook settings of ``ownvitals.config`` (fields only; validators stay in ``Settings``)."""

from pydantic import Field
from pydantic_settings import BaseSettings


class WebhookSettings(BaseSettings):
    """The webhook-secret key ids and web's entry-cache TTL."""

    # The ids of the current webhook-secret seed (worker only) and pepper (web and worker). The
    # keys are the secret files ``webhook_seed`` and ``webhook_pepper`` (read by the crypto
    # package, never the environment); retired ones are ``webhook_seed_<id>`` and
    # ``webhook_pepper_<id>``. Optional in M0, required from M1 (ADR 0004 section 4).
    webhook_seed_id: str = "seed-1"
    webhook_pepper_id: str = "pep-1"
    # How long web's copy of the webhook-secret HMACs stays usable after the read that made it;
    # past it the receiver answers 503 (ADR 0008). Web re-reads it every quarter of this. At most
    # 60 s, the lead ``append_secret`` gives a new entry (``webhook_cache.CACHE_TTL``), so every
    # web process holds an entry before it opens: it can be shortened, never lengthened.
    webhook_cache_ttl_seconds: int = Field(default=60, ge=4, le=60)
