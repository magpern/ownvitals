"""The feature-flag and AI cost-cap settings of ``ownvitals.config`` (fields only)."""

from pydantic import Field
from pydantic_settings import BaseSettings


class AiSettings(BaseSettings):
    """Feature-flag and AI monthly-cap fields (the cross-field cap check stays in ``Settings``)."""

    # The environment side of the stored feature flags (the stricter of the two wins): both are
    # off by default and a stored flag can only keep them off. ``ai_insights`` also gates the
    # model host on the worker's egress allowlist.
    experimental_findings: bool = False
    ai_insights: bool = False
    # The AI cost cap (D11, ADR 0014 section 7), integer micro-USD per calendar month in UTC: per
    # user, and for all users together (the owner's total exposure). Both 0 until the owner sets
    # them (ADR 0014 open question 2): no request can be reserved. These settings are the
    # authoritative caps; the worker passes them on every reservation and nothing stores them.
    ai_user_monthly_cap_microusd: int = Field(default=0, ge=0, le=10_000_000_000_000)
    ai_global_monthly_cap_microusd: int = Field(default=0, ge=0, le=10_000_000_000_000)
    # Experimental findings and scores (the HIS) to the model (ADR 0014 section 4, open question
    # 4): only for an account that passes the experimental gate (the owner-only switch) **and**
    # only when this is true. Default false: the owner opts in explicitly.
    ai_include_experimental: bool = False
    # The locks the owner sets (ADR 0014 sections 1 and 5; agents never set them). Each is empty
    # by default, so no model call is made: the pinned model id (open question 3), the reference
    # of the recorded provider review (lock 2) and the evaluation reference (lock 3), exactly
    # ``<model>/<prompt version>/<tool-set version>`` as the owner's evaluation run passed it
    # (``ownvitals.ai.requests.eval_ref``).
    ai_model: str = Field(default="", max_length=100, pattern=r"^([a-z0-9][a-z0-9.\-]*)?$")
    ai_provider_review_ref: str = Field(default="", max_length=500)
    ai_eval_ref: str = Field(default="", max_length=200)
