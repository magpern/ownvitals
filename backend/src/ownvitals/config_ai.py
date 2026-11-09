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
