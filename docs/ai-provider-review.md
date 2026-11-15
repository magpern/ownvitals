# AI provider review record

Referenced by the setting `ai_provider_review_ref` (ADR 0014 §1 lock 2, §2). This file records
the owner's decision. It is not a legal or production gate and says nothing about verification.

## 2026-11-15

- Provider: Anthropic (public API, `api.anthropic.com`).
- Model: `claude-sonnet-5-5` (prices: `backend/src/ownvitals/ai/pricing.py`).
- Owner's statement, in a session with the Architect: "i accept the terms" (the provider's current
  terms and usage policies), and that the Architect records it.
- Checklist rows (ADR 0014 §2) not individually checked by the owner: all; the owner accepted the
  terms as a whole. The consent text shown to users (§3) must keep stating retention truthfully;
  a change of model or of the provider's terms needs a new dated entry here.
- A data processing agreement is still required before any non-household user (PLAN-operations §9.3).

Reference value for the setting: `docs/ai-provider-review.md@2026-11-15`
