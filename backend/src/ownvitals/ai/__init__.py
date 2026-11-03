"""AI insights (PLAN section 7.4, D11, ADR 0014).

So far only the per-user monthly cost cap: the pricing table and the request bound
(``ownvitals.ai.pricing``) and the reservations against both caps (``ownvitals.ai.budget``).
The model client, consent and the jobs come with their own issues.
"""
