# Architecture decision records

One file per decision: `NNNN-title.md`, with **Context**, **Decision**, **Alternatives**,
**Consequences** and **Status** (Proposed, Accepted, Superseded by NNNN). An ADR that leaves a
question open says so in an **Open questions** section and names who answers it.

- ADRs **record and detail** the frozen specification ([roadmap.md](../roadmap.md): PLAN.md,
  PLAN-operations.md and withings-findings.md at Revision 3). They never contradict it. A
  technical decision the specification leaves open goes into an ADR; a change to the
  specification itself is a dated roadmap amendment plus an ADR (AGENTS.md, "Decisions and
  ADRs").
- ADRs 0001 to 0005 adapt classbeat's ADRs of the same numbers at the pinned commit
  `d80605100992f85f8f02672b9b5f885ed6166214` (ADR 0001 §5). Each says what it keeps, what it
  drops and what changes for health data, and why. "classbeat ADR 0003 §5" means that section at
  the pinned commit.
- A fresh Reviewer reviews each ADR in its PR (Opus for security content). Section references
  such as §4.3 or D9 without a file name point into PLAN.md; §9.x and §10.x point into
  PLAN-operations.md.
- An accepted ADR changes only through a later ADR or a dated amendment section at its end;
  status lines are updated by the issue that changes the decision (for 0001 to 0005 after the
  port: #16).

| # | Decision | Status |
|---|---|---|
| [0001](0001-stack-and-layout.md) | Stack, repository layout and the classbeat port rule | Accepted |
| [0002](0002-tenant-isolation.md) | Tenant isolation: UserContext, forced RLS, roles and execution contexts | Accepted |
| [0003](0003-accounts-and-sessions.md) | Accounts, sessions, mandatory TOTP and step-up | Accepted |
| [0004](0004-connections-and-encryption.md) | Provider connections, envelope encryption and the KEK ring | Accepted |
| [0005](0005-jobs-and-leases.md) | Jobs, leases, fencing and the worker | Accepted |
| [0008](0008-webhook-secret-lifecycle.md) | Webhook-secret lifecycle, rotations and subscriptions (moved from ADR 0004 §4) | Accepted |

The health-data threat model that these ADRs feed is
[docs/security/threat-model.md](../security/threat-model.md).
