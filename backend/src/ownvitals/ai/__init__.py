"""AI insights (PLAN section 7.4, D11, ADR 0014).

So far: the per-user monthly cost cap (``pricing``, ``budget``; #205), consent and its
withdrawal (``consent``, ``consent_text``, ``consent_routes``, ``tables``; #206) and the
data-minimised tool set the model may call (``tools``, ``tools_model``; #206). The model client,
the jobs and the audit log come with their own issues.
"""
