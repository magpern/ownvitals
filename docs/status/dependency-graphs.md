# Status detail: dependency graphs

Moved out of [docs/status.md](../status.md) so the index stays short; the text is unchanged apart from relative links. Read it only when the work needs it.

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
| #42 | CI dependency audits (pip-audit, npm audit, Trivy repo scan); remove the skip steps | Sonnet | #2, #3; the `ci.yml` lock (released by #96) | 2 |
| #49 | ADR follow-ups from the #41 review (webhook-secret lifecycle, re-pointing, definer-function role, partitions for past months) | Opus | #41 | 2 |
| #52 | Frontend test follow-ups from the #48 review (guard `afterEach`, template URLs) | Sonnet | #45 | 3 |
| #57 | Release follow-ups from the #53 review (deploy recipe, guard tests in CI, version check, Dependabot cooldown and titles, base-image digests) | Opus | #7; the `ci.yml` (after #42) and `Dockerfile` locks | 3 |
| #72 | ADR follow-ups from the #58 review: mail outbox (ADR 0007), webhook-secret lifecycle split into ADR 0008, function input rules, single flight, partitions | Opus | #49 | 3 |
| #75 | Role-check follow-ups from the #65 review (login role, guard 5 constructions, ownership test, startup error) | Opus | #5 | 3 |
| #77 | Wiring after #8, #9 and #6: worker allowlist and KEK ring, guard 2 learns the queue grantee, web's queue check, `transaction_timeout` on runtime roles; web's `install_egress_hook("web")` and standard-loop check, and single-process uvicorn settings (ADR 0009); the unreachable refused-login branch in `queue_check.py` (remove or test; #115 N1) | Opus | #6 (#8, #9 and #75 merged) | 4 |
| #78 | ADR follow-ups from the #73 review; the `transaction_timeout` decision | Opus | #72 | 4 |
| #89 | Role-check and guard 5 follow-ups from the #81 and #82 reviews (flag 8 pinned, a refused login fails closed, guard 5 hardening, `crypto.md` wording, a refused-login test) | Opus | #75, #8 (both merged) | 4 |
| #101 | `db-functions` follow-ups from the #96 review (escaping test, exact volumes, digest pin, shellcheck of `docker/postgres`) | Opus | #9 (merged); holds the `ci.yml`, `compose.yaml`, `Dockerfile`, `AGENTS.md` and `deploy/` locks (#118) | 5 |

## M1 dependency graph

| Issue | What | Owner model | Depends on |
|---|---|---|---|
| #18 | Jobs for Withings: system/webhook kinds, lanes, singletons, retry budget, dead-letter | Opus | #9, #12 |
| #19 | In-process fake-withings and the synthetic data generator (split 2026-10-31) | Sonnet + Explorer | #2 |
| #36 | fake-withings e2e service (split from #19) | Sonnet | #19, #14 |
| #66 | Fake tightening from the #63 review (POST-only secrets, 2xx webhooks, callback rules) | Sonnet | #19 |
| #20 | Withings API client, error taxonomy, models | Opus | #6, #19, #66 |
| #21 | Raw Withings data model, forced RLS, partitions | Opus | #5, #13 |
| #22 | Connect Withings, crash-safe token rotation | Opus | #18, #20, #21, #66 |
| #23 | Rate limiter | Opus | #18, #21 |
| #24 | Webhook receiver | Opus | #18, #21, #22, #66 |
| #25 | Sync engine | Opus | #20, #21, #22, #23 |
| #26 | Reconciliation, deletions, subscription drift | Opus | #24, #25 |
| #27 | Disconnect and account delete with upstream revoke | Opus | #22, #25 |
| #28 | Observability (Prometheus) | Sonnet | #23, #24, #25, #26 |
| #29 | Probe CLI (shape-only) | Opus | #20, #21, #25 |
| #30 | M1 done-when: e2e, failure injection, RLS contexts | Opus | #15, #19, #36, #24–#29 |

## Pulled forward (2026-10-31, owner go-ahead to widen the graph)

Pure libraries with synthetic-data tests; no database, I/O or Withings imports (purity guard in
#37). Wiring into `observations`, `metric_daily`, findings and jobs stays in their milestones.
Some rows are not libraries: #39, #88 and #110 are ADR work, #84 is M2's deploy script and #98 is
M6's frontend shell.

| Issue | What | Owner model | Depends on |
|---|---|---|---|
| #37 | M4 analysis core: quality gates, daily values, minimum-data policy | Sonnet | #2 |
| #38 | M4 analysis methods: robust baseline, gap-weighted EWMA, Theil–Sen + modified MK, PELT | Sonnet | #2 |
| #39 | ADR 0006: ECG subsystem spec (PLAN §7.3: spec before code) | Opus | — |
| #40 | M5 ECG signal-processing core: filters, R-peaks, SQI gate, HRV | Sonnet | #2, #39 |
| #56 | Analysis follow-ups from the #54 review (purity-guard gaps, nerve baseline span) | Sonnet | #37; after #38 merges (both touch `test_analysis_purity.py` and `policy.py` semantics) |
| #68 | Autocorrelation-robust trend test and change points (follow-up from #38; before trend or change-point findings go live) | Sonnet | #38 |
| #95 | Calibration and test follow-ups from the #87 review | Sonnet | #68 |
| #99 | Purity guard follow-ups from the #94 review | Sonnet | #76 |
| #98 | M6 app shell, tokens, theme, Plex fonts and design primitives (merged as #105) | Sonnet | #3, #85 |
| #76 | Purity guard and policy follow-ups from the #74 review | Sonnet | #56 |
| #84 | M2 forced-command deploy script, the release trust boundary (merged as #100; installing it is the owner's) | Opus | #7 |
| #110 | M4 ADR 0010: v1 finding rules, parameters, definitions, confidence (merged as #112; accepted in #116, #117) | Opus | none (the libraries it names are merged) |
| #88 | ADR follow-ups from the #46 and #83 reviews (ADR 0006 amendment; ADRs 0002, 0004, 0005, 0007 and the threat model) | Sonnet (Opus reviewer) | #46, #83 |

Also created early in M5: #47, the owner-run `ownvitals ecg validate` command (ADR 0006 §14;
Opus; depends on #39, #40, #21, #25).

Scheduling: at most 5 Developers at once; the M0 critical path (#5 → #8 → #10 → #11 → #13 →
#15 → #16) and #6 come first, the pulled-forward issues fill free slots.

