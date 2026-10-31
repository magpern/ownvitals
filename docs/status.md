# Status

Kept by the Architect and updated on every merge and every orchestration cycle. The
specification is [roadmap.md](roadmap.md); GitHub milestones and issues hold the detail. Status
levels (D10): agents set **IMPLEMENTED** only; **INTEGRATION_VERIFIED** and
**PRODUCTION_READY** are the owner's.

## Milestones

| # | Milestone | State | Level |
|---|---|---|---|
| M0 | Platform | in progress: wave 1 PRs open (#35 backend, #34 frontend, #41 ADRs and threat model) | — |
| M1 | Withings core | not started; backlog #18–#30 (dependency graph below) | — |
| M2 | Home deploy (early) | not started; mostly owner actions (see "Blocked on the owner") | — |
| M3 | Identity additions | not started | — |
| M4 | Analysis v1 | analysis core pulled forward (#37, #38; pure libraries, start after #2) | — |
| M5 | ECG & nerve health | ECG spec ADR 0006 in progress (#39); signal-processing core #40 after it | — |
| M6 | MVP UI | not started | — |
| M7 | AI & reports | not started | — |
| M8 | Beyond-household gate | owner-gated | — |

## Active

| Issue | What | Developer | PR | State |
|---|---|---|---|---|
| #2 | Backend skeleton, config, Alembic baseline, local stack (+ numpy, scipy) | Sonnet | #35 | finishing |
| #3 | Frontend skeleton | Sonnet | #34 | Opus review |
| #4 | ADRs 0001–0005, health-data threat model | Opus | #41 | applying Architect decisions, then Opus review |
| #39 | ADR 0006: ECG subsystem spec (pulled forward) | Opus | — | in progress |
| #43 | Status after wave 1 (this update) | Architect | #44 | in review |

## M0 dependency graph

| Issue | What | Owner model | Depends on | Wave |
|---|---|---|---|---|
| #2 | Backend skeleton, config, Alembic baseline, local stack | Sonnet | #1 | 1 |
| #3 | Frontend skeleton | Sonnet | #1 | 1 |
| #4 | ADRs 0001–0005, health-data threat model | Opus | #1 | 1 |
| #5 | Tenant isolation: UserContext, forced RLS, roles, guards | Opus | #2 | 2 |
| #6 | Egress allowlist and mail | Opus | #2 | 2 |
| #7 | Release workflow (attestation), Dependabot ecosystems | Opus (security-critical: the deploy trusts its attestation) | #2, #3 | 2 |
| #8 | Envelope encryption, KEK ring | Opus | #5 | 3 |
| #9 | Job queue, leases, fencing, worker | Opus | #5 | 3 |
| #10 | Accounts and sign-in (classbeat port) | Opus | #5, #6, #8 | 4 |
| #11 | Mandatory TOTP, session protection, step-up | Opus | #3, #10 | 5 |
| #12 | Admin, audit log, stored flags | Opus | #10 | 5 |
| #13 | Connections layer, rewrap, keys CLI | Opus | #8, #10, #11 | 6 |
| #14 | E2E harness, auth flows, image scan | Sonnet | #3, #11 | 6 |
| #15 | RLS tests in every execution context | Opus | #9, #12, #13 | 7 |
| #16 | Threat-model update, M0 acceptance | Opus | #4, #14, #15 | 8 |
| #42 | CI dependency audits (pip-audit, npm audit, Trivy repo scan); remove the skip steps | Sonnet | #2, #3 | 2 |

## M1 dependency graph

| Issue | What | Owner model | Depends on |
|---|---|---|---|
| #18 | Jobs for Withings: system/webhook kinds, lanes, singletons, retry budget, dead-letter | Opus | #9, #12 |
| #19 | In-process fake-withings and the synthetic data generator (split 2026-10-31) | Sonnet + Explorer | #2 |
| #36 | fake-withings e2e service (split from #19) | Sonnet | #19, #14 |
| #20 | Withings API client, error taxonomy, models | Opus | #6, #19 |
| #21 | Raw Withings data model, forced RLS, partitions | Opus | #5, #13 |
| #22 | Connect Withings, crash-safe token rotation | Opus | #18, #20, #21 |
| #23 | Rate limiter | Opus | #18, #21 |
| #24 | Webhook receiver | Opus | #18, #21, #22 |
| #25 | Sync engine | Opus | #20, #21, #22, #23 |
| #26 | Reconciliation, deletions, subscription drift | Opus | #24, #25 |
| #27 | Disconnect and account delete with upstream revoke | Opus | #22, #25 |
| #28 | Observability (Prometheus) | Sonnet | #23, #24, #25, #26 |
| #29 | Probe CLI (shape-only) | Opus | #20, #21, #25 |
| #30 | M1 done-when: e2e, failure injection, RLS contexts | Opus | #15, #19, #36, #24–#29 |

## Pulled forward (2026-10-31, owner go-ahead to widen the graph)

Pure libraries with synthetic-data tests; no database, I/O or Withings imports (purity guard in
#37). Wiring into `observations`, `metric_daily`, findings and jobs stays in their milestones.

| Issue | What | Owner model | Depends on |
|---|---|---|---|
| #37 | M4 analysis core: quality gates, daily values, minimum-data policy | Sonnet | #2 |
| #38 | M4 analysis methods: robust baseline, gap-weighted EWMA, Theil–Sen + modified MK, PELT | Sonnet | #2 |
| #39 | ADR 0006: ECG subsystem spec (PLAN §7.3: spec before code) | Opus | — |
| #40 | M5 ECG signal-processing core: filters, R-peaks, SQI gate, HRV | Sonnet | #2, #39 |

Scheduling: at most 5 Developers at once; the M0 critical path (#5 → #8 → #10 → #11 → #13 →
#15 → #16) and #6 come first, the pulled-forward issues fill free slots.

## Decisions log

| Date | Decision | Where |
|---|---|---|
| 2026-10-31 | numpy and scipy join #2's dependencies so #37, #38 and #40 don't need the `pyproject.toml` lock | #2 |
| 2026-10-31 | Roadmap Amendment 1: the code exchange in the OAuth callback runs in web through a web-only egress entry | roadmap, ADR 0004 |
| 2026-10-31 | Partitioned owned tables allowed only with forced RLS on parent and partitions, no app-role privileges on partitions, partitions created by one reviewed `SECURITY DEFINER` function; guard 1 extended by #21 | ADR 0002 |
| 2026-10-31 | Disconnect keeps imported data by default, with an explicit delete option; linking another Withings account requires disconnecting first; a failed upstream revoke still deletes local secrets | ADR 0004, #27 |
| 2026-10-31 | After a KEK compromise, upstream revocation is the owner's runbook step (rotate the Withings client secret); stolen access tokens live up to 3 h: accepted risk | ADR 0004 |
| 2026-10-31 | Step-up: a recovery code counts as a fresh second factor; "disable 2FA" returns the account to enrolment; replacing an authenticator needs step-up | ADR 0003 |
| 2026-10-31 | Session defaults: idle 72 h, absolute 30 days (settings) | ADR 0003 |
| 2026-10-31 | `webhook_events` retained 30 days | ADR 0002 |
| 2026-10-31 | Job queue: only claim, reap and stats are `SECURITY DEFINER`; heartbeat and finish are fenced writes in the owner's session (#9 aligned) | ADR 0005 |
| 2026-10-31 | Admins cannot read health data | ADR 0002/0005, #12 |
| 2026-10-31 | Licence rule (MIT, ISC, Apache-2.0, OFL) covers direct dependencies and everything shipped; transitive dev-only tooling under other permissive or weak-copyleft licences (BSD, MPL-2.0 unmodified, MIT-0, CC0, BlueOak, CC-BY-4.0 data) is accepted, as in classbeat | #34 |
| 2026-10-31 | npm audit gates at `high`; moderate dev-only findings (ESLint chain) don't fail CI | #42 |

## Architect tasks

- Removing the "Is there a backend/frontend yet?" skip steps is now part of #42 (bootstrap
  review, finding 1).
- Dependabot PRs (e.g. #31) go through the same gate with a fresh Sonnet reviewer.

## Shared-file locks

No two open PRs change the same file. Files many issues need are locked here by the Architect;
an issue that needs a locked file waits for the holder's merge, then updates its branch.

| File | Held by |
|---|---|
| `.github/workflows/ci.yml` | — |
| `backend/src/ownvitals/config.py` | #2 |
| `backend/pyproject.toml`, `backend/uv.lock` | #2 |
| `backend/src/ownvitals/cli.py` | #2 |
| `compose.yaml` | #2 |
| `Dockerfile` | #2 |
| `AGENTS.md` | — |

Alembic migrations: a PR takes the next free revision number when it opens; if another
migration merges first, it renumbers and re-chains on its update, which needs a fresh scoped
review.

## Completed

- 2026-10-31: specification Revision 3 frozen at `635901d` ([roadmap.md](roadmap.md)).
- 2026-10-31: #32 bootstrap review follow-ups merged as #33; Dependabot #31 merged.
- 2026-10-31: #1 bootstrap merged as #17 (Opus PASS `31fc85c`). Rulesets set: `main` (PR, `ci` + `pr-title` strict, squash only, linear history, no force push or deletion, 0 approvals) and `release tags` (`v*` protected); squash-only merges, branches deleted on merge; secret scanning and push protection on.

## Blocked on the owner

Nothing blocks M0. These are needed later; agents work past them with fakes and record the
assumptions below.

| Item | Needed for | Owner action |
|---|---|---|
| Withings developer app (callback `https://withings.pernemark.se/...`, HTTPS on 443) | M1 live, M2 | Register the app; put the client id and secret on the home server only |
| Google OAuth client for sign-in | M2 live sign-in | Create the OAuth client; secret on the home server only |
| SWAG `site-confs/ownvitals.conf`, Cloudflare DNS, SSL Full (strict), WAF skip rule for `/api/webhooks/withings/*` | M2 | Apply on 192.168.1.151 and Cloudflare |
| `ownvitals-deploy` user with the forced-command key on 192.168.1.222 | M2 | Create the user and install the reviewed deploy script |
| Backups and a restore drill | M2 real-data gate | Approve the backup target; run the drill |
| Connect your Withings account and run `ownvitals probe` on the home server | M1 → INTEGRATION_VERIFIED | Run it; paste the shape-only output into the issue |
| Written clarification from Withings on Developer Agreement §4 and D2 (BYO, user cap) | M8 | Contact Withings |
| Human security review, legal checklist (§9.3), medical-claims review | M8 | Owner or a trusted reviewer |
| Threat model O6: SWAG settings for ownvitals (headers, body limits, the webhook location's access log without the URI) | M2 | Confirm or apply on 192.168.1.151 (ADR/threat model in #41) |
| Threat model O12: disk encryption on 192.168.1.222; whether Cloudflare proxies `withings.pernemark.se` | M2 | Decide and tell the Architect |
| Threat model O15: confirm or reject the risks the threat model proposes as accepted | Before the M2 real-data gate | Review the "Accepted risks" section once #41 merges |

## Assumptions (unverified; each needs the probe or the owner)

None recorded yet. M1 adds one row per Withings behaviour the code relies on but no real account
has confirmed (for example the `blocked_by_provider_plan` signal for post-13-Oct-2026 accounts).

## External dependencies

| Dependency | Pinned / state |
|---|---|
| classbeat platform source | `d80605100992f85f8f02672b9b5f885ed6166214` (ADR 0001) |
| Withings public API (`wbsapi.withings.net`) | documented behaviour in [withings-findings.md](withings-findings.md); unverified until the probe |
| GitHub-hosted runners | public repository, free minutes |

## Milestone acceptance evidence

Recorded here when a milestone's "done when" (PLAN §11) is checked: CI runs, test names, e2e
runs, and what remains unverified.
