# Status

Kept by the Architect and updated on every merge and every orchestration cycle. The
specification is [roadmap.md](roadmap.md); GitHub milestones and issues hold the detail. Status
levels (D10): agents set **IMPLEMENTED** only; **INTEGRATION_VERIFIED** and
**PRODUCTION_READY** are the owner's.

## Milestones

| # | Milestone | State | Level |
|---|---|---|---|
| M0 | Platform | in progress: #2 (#35), #3 (#34), #4 (#41), #7 (#53), #45 (#48) merged; #5 (#65), #52 (#64) in review; #6 (#55) and #49 (#58) in fix and delta rounds; #42 and #57 wait for the `ci.yml` lock | — |
| M1 | Withings core | #19 (in-process fake) in review (#63); backlog #18, #20–#30, #36 (dependency graph below) | — |
| M2 | Home deploy (early) | not started; mostly owner actions (see "Blocked on the owner") | — |
| M3 | Identity additions | not started | — |
| M4 | Analysis v1 | pulled forward: #37 merged (#54); #38 in progress; #56 after #38 | — |
| M5 | ECG & nerve health | ADR 0006 (#46) in a fix round after the root-cause change (read-time gate overlay); #40 after it; #47 created | — |
| M6 | MVP UI | not started | — |
| M7 | AI & reports | not started | — |
| M8 | Beyond-household gate | owner-gated | — |

## Active

| Issue | What | Developer | PR | State |
|---|---|---|---|---|
| #5 | Tenant isolation: UserContext, forced RLS, roles, guards (+ #35 review follow-ups) | Opus | #65 | Opus review |
| #6 | Egress allowlist and mail | Opus | #55 | Opus FAIL (guard gaps); fix round |
| #49 | ADR follow-ups from the #41 review | Opus | #58 | fix round pushed (`a86f571`); fresh Opus delta review |
| #39 | ADR 0006: ECG subsystem spec | Opus | #46 | root cause changed by the Architect after three FAILs; fourth FAIL (B5, stale gate version); fix round, then a fresh Opus review |
| #19 | In-process fake-withings and synthetic data generator | Sonnet | #63 | Sonnet review |
| #38 | Analysis methods (pulled forward) | Sonnet | — | in progress |
| #52 | Frontend test follow-ups (from the #48 review) | Sonnet | #64 | Sonnet review |
| — | Dependabot: TypeScript 7 | — | #59 | to be ignored (typescript-eslint doesn't support it yet) |
| #61 | Status after the wave 2 merges (this update) | Architect | #62 | in review |

At most five Developers work on issues at once; a fix round on an open PR may run while five
are busy, and no new issue starts until fewer than five are.

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
| #42 | CI dependency audits (pip-audit, npm audit, Trivy repo scan); remove the skip steps | Sonnet | #2, #3; the `ci.yml` lock (#5) | 2 |
| #49 | ADR follow-ups from the #41 review (webhook-secret lifecycle, re-pointing, definer-function role, partitions for past months) | Opus | #41 | 2 |
| #52 | Frontend test follow-ups from the #48 review (guard `afterEach`, template URLs) | Sonnet | #45 | 3 |
| #57 | Release follow-ups from the #53 review (deploy recipe, guard tests in CI, version check, Dependabot cooldown and titles, base-image digests) | Opus | #7; the `ci.yml` (#5) and `Dockerfile` locks | 3 |

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
| #56 | Analysis follow-ups from the #54 review (purity-guard gaps, nerve baseline span) | Sonnet | #37; after #38 merges (both touch `test_analysis_purity.py` and `policy.py` semantics) |

Also created early in M5: #47, the owner-run `ownvitals ecg validate` command (ADR 0006 §14;
Opus; depends on #39, #40, #21, #25).

Scheduling: at most 5 Developers at once; the M0 critical path (#5 → #8 → #10 → #11 → #13 →
#15 → #16) and #6 come first, the pulled-forward issues fill free slots.

## Decisions log

| Date | Decision | Where |
|---|---|---|
| 2026-10-31 | numpy and scipy join #2's dependencies so #37, #38 and #40 don't need the `pyproject.toml` lock | #2 |
| 2026-10-31 | Roadmap Amendment 2: partitioned owned tables keyed `(owner_id, id, partition key)`, never FK targets | roadmap, ADR 0002 |
| 2026-10-31 | Webhook secrets are derived, never stored: `secret = HMAC-SHA256(seed[seed_id], "ownvitals-webhook-v1" ‖ app_id ‖ secret_id)`; each entry keeps `{secret_id, seed_id, pepper_id, hmac, active_from, retire_at}` with hmac = HMAC(pepper, secret) (PLAN §4.4, §5). The worker holds seed and pepper; web holds only the pepper. A seed rotation is a secret rotation. After a seed compromise (or a compromise of the KEK ring holding it), subscriptions whose tokens were wiped keep the old URL until reconnect; the retired secret gets the generic 200 and is ignored: accepted risk 20 | ADR 0004, #8, #24 |
| 2026-10-31 | Roadmap Amendment 1: the code exchange in the OAuth callback runs in web through a web-only egress entry | roadmap, ADR 0004 |
| 2026-10-31 | Partitioned owned tables allowed only with forced RLS on parent and partitions, no app-role privileges on partitions, partitions created by one reviewed `SECURITY DEFINER` function; guard 1 extended by #21 | ADR 0002 |
| 2026-10-31 | Reversible default until the owner says otherwise (ADR 0004 had left it to the owner): disconnect keeps imported data, with an explicit delete option; linking another Withings account requires disconnecting first; a failed upstream revoke still deletes local secrets | ADR 0004, #27 |
| 2026-10-31 | After a KEK compromise, upstream revocation is the owner's runbook step (rotate the Withings client secret); stolen access tokens live up to 3 h: accepted risk | ADR 0004 |
| 2026-10-31 | Reading of PLAN §6's "fresh TOTP": a recovery code counts as a fresh second factor; "disable 2FA" returns the account to enrolment; replacing an authenticator needs step-up | ADR 0003 |
| 2026-10-31 | Session defaults: idle 72 h, absolute 30 days (settings) | ADR 0003 |
| 2026-10-31 | `webhook_events` retained 30 days | ADR 0002 |
| 2026-10-31 | Job queue: only claim, reap and stats are `SECURITY DEFINER`; heartbeat and finish are fenced writes in the owner's session (#9 aligned) | ADR 0005 |
| 2026-10-31 | Admins cannot read health data | ADR 0002/0005, #12 |
| 2026-10-31 | Licence rule (MIT, ISC, Apache-2.0, OFL) covers direct dependencies and everything shipped; transitive dev-only tooling under other permissive or weak-copyleft licences (BSD, MPL-2.0 unmodified, MIT-0, CC0, BlueOak, CC-BY-4.0 data) is accepted, as in classbeat | #34 |
| 2026-10-31 | npm audit gates at `high`; moderate dev-only findings (ESLint chain) don't fail CI | #42 |
| 2026-10-31 | `withings_apps` writes: app configuration only by #22's CLI under the owner role (one-off operator containers), with its audit row; the app role has column-level SELECT only. "Rotate" sets `rotation_requested_at`; the worker's owner-less maintenance loop derives the secret and writes entries through one narrow `SECURITY DEFINER` function (single caller, guard); each `notify update` is an owned job. Code execution in web could call that function: accepted risk 21 | ADR 0002, 0004, 0005, #21, #22, #24 |
| 2026-10-31 | #41 review follow-ups: entries deleted once no subscription uses them, pepper retired when unreferenced; old seed kept worker-side until re-pointing ends (≤ 14 days); `active_from` ≥ web cache TTL; `notify list` on (re)connect revokes stale subscriptions; webhook-secret function owned by a dedicated `NOLOGIN` role with input rules; client-secret rewrap is an operator step; `ownvitals_maint` can't write `withings_apps`; `ensure_month_partitions(table, from, to)` also called by backfill | #49, #21, #22, #24, #25 |
| 2026-10-31 | ECG (ADR 0006 at #46's head `b67c99d`, root-cause change after three FAILs): findings E1–E6 are stored from Withings values only; our SQI gate is a **read-time overlay**, never stored in a finding: one function reads the linked recording's current `estimated` and hides E4/E6 only on an explicit failure ("not evaluated by ownvitals" otherwise), never hides E1–E3 and shows our gate on them only behind the owner flag; every consumer (dashboard, API, notifications, export, doctor PDF, AI context) goes through it. E4/E6 notifications are held up to 24 h (a setting) for the gate and never recalled. The E5 index is out of v1; E4 with unknown sex uses only the QTc > 500 ms band; SQI components, HR, HRV and markers only behind the owner flag; writing `estimated` bumps no `data_revisions` and re-evaluates nothing | ADR 0006 (#46), #25 |
| 2026-10-31 | ECG, Architect decisions for #46's fourth fix round (sent to the developer; recorded here until the ADR carries them): an `estimated` whose fingerprint **or** gate-module versions (`filters`, `rpeaks`, `sqi`) differ from the running ones counts as absent; the A7 link is resolved at read time and a soft-deleted recording is no link; an E6 over several recordings is hidden when at least half failed explicitly; the JSON/CSV export carries every finding with its overlay state, the doctor PDF and AI context leave hidden ones out; no reason code is a display label; the hold decides once and restarts on a new revision | #46 (round 4) |
| 2026-10-31 | From the #35 review: prod also refuses a DSN `password`/`passfile` query parameter; the test-DSN check refuses an empty host and `service=` | #5 |
| 2026-10-31 | Egress (#6): `ownvitals.egress` is a package; the web-only code exchange is its own module; the Withings and model clients refuse to build or send unless the worker allowlist is installed; SMTP has a 20 s total deadline (moves to a setting in #10); web installs a deny-all urllib opener in #10; the guard denies network-capable imports outside egress and mail (from the #55 review) | #6, #10, #23 |
| 2026-10-31 | Release trust boundary: the `v*` tag ruleset stops moving and deleting tags, not creating them; the deploy is the boundary (tag commit in `refs/heads/main`, attestation with cert identity, `--source-ref`, `--source-digest`). No tag `creation` rule: it adds nothing while the owner's token is the only pusher | AGENTS.md, #57, T62 (#49) |
| 2026-10-31 | From the #58 review: compromise rotation retires first, then appends (routine: append, re-point, retire); `withings_apps.rotation_reason` (`routine`/`compromise`); the webhook-secret role gets `USAGE` on `public`, column-level SELECT it needs, no client-secret or token column; guard 11 uses `has_any_column_privilege`; `ensure_month_partitions` takes an advisory lock and accepts months from 13 months back to 3 ahead; owner notices from the worker are in-app only (SMTP never from the worker; the operator CLI may mail); `notify list` clean-up: #22 on (re)connect, #26 after the 14-day deadline, #27 on disconnect | #49, #21, #22, #24–#27 |
| 2026-10-31 | Guard 5 (ADR 0002 §1 "anywhere"): `object.__setattr__(self, …)` is allowed only inside a frozen dataclass's `__post_init__` on the bare `self`; every other form stays refused (it can't build a `UserContext`) | #5 (PR #65 body, `docs/dev/isolation.md`) |
| 2026-10-31 | Analysis core (#37): plausibility ranges are labelled engineering bounds unless findings cite one; a metric without a range is refused; values are `Decimal`, floats refused | #37 |
| 2026-10-31 | Dependabot: TypeScript 7 to be ignored until typescript-eslint supports it (`@dependabot ignore typescript major version` requested on #59; if Dependabot doesn't act, #57 adds the ignore to `dependabot.yml`); Dependabot titles over the 100-character header are shortened through group names (#57) or retitled by the Architect before merge | #59, #57 |

## Architect tasks

- When #46 merges: record ADR 0006's assumptions A1–A8 below with their settling items; add the
  0006 row to `docs/adr/README.md` (noting that 0006 and its tests file use "§n" for their own
  sections); re-check the ECG decisions row.
- The M4 wiring issue (to create) reconciles `metric_daily` (morning, mean, min, max, n; PLAN §5)
  with #37's `DailyValue` (morning or median, min, max, MAD), and keys duplicates of segmental
  types 173–175 on `(grpid, position)`.
- TypeScript 7: re-enable when typescript-eslint supports it (`@dependabot unignore` or a manual
  bump issue).
- Removing the "Is there a backend/frontend yet?" skip steps is part of #42.
- Dependabot PRs go through the same gate with a fresh Sonnet reviewer.

## Shared-file locks

No two open PRs change the same file. Files many issues need are locked here by the Architect;
an issue that needs a locked file waits for the holder's merge, then updates its branch.

| File | Held by |
|---|---|
| `.github/workflows/ci.yml` | #5 (backend job); #42 next |
| `backend/src/ownvitals/config.py` | #5 |
| `backend/pyproject.toml`, `backend/uv.lock` | — |
| `backend/src/ownvitals/cli.py` | — |
| `compose.yaml` | #5 |
| `Dockerfile` | — |
| `AGENTS.md` | #61 |
| `backend/src/ownvitals/web.py` | #5 |
| `backend/src/ownvitals/db/engine.py` | #5 |
| `backend/tests/dbtools.py` | #5 |
| `backend/tests/conftest.py`, `backend/tests/test_web.py` | #5 |
| `docs/dev/backend.md`, `docs/dev/stack.md` | #5 |

Alembic migrations: a PR takes the next free revision number when it opens; if another
migration merges first, it renumbers and re-chains on its update, which needs a fresh scoped
review.

## Completed

- 2026-10-31: Dependabot #60 (pydantic-settings 2.16.2, `uv.lock` only) merged (Sonnet PASS `55f0657`).
- 2026-10-31: #7 release workflow merged as #53 (Opus PASS `e693f54`, merge check `f21b511`) → IMPLEMENTED; it has never run: the first real tag exercises the attestation push and self-verification.
- 2026-10-31: #37 analysis core merged as #54 (Sonnet PASS `93d6312`, merge check `2cef0fa`) → IMPLEMENTED.
- 2026-10-31: #50 status at the start of wave 2 merged as #51 (Sonnet PASS `cf94a4b`, merge check `13f96d1`).
- 2026-10-31: #45 frontend test hardening merged as #48 (Sonnet PASS `83eaa73`, merge check `98820be`) → IMPLEMENTED.
- 2026-10-31: #4 ADRs 0001–0005 and the threat model merged as #41 (Opus PASS `04d634d`, merge check `f1580ec`).
- 2026-10-31: #2 backend skeleton merged as #35 (Opus PASS `4ffaef8`, merge check `5c22d98`) → IMPLEMENTED.
- 2026-10-31: #43 status after wave 1 and roadmap Amendments 1–2 merged as #44 (Sonnet PASS `a21399c`, merge check `f7ef585`).
- 2026-10-31: specification Revision 3 frozen at `635901d` ([roadmap.md](roadmap.md)).
- 2026-10-31: #3 frontend skeleton merged as #34 (Opus PASS `0e41e7c`, merge check `fc8641f`) → IMPLEMENTED.
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
| Threat model O6: SWAG settings for ownvitals as the threat model lists them (no buffering for SSE, the webhook location's access log without the URI, `CF-Connecting-IP` honoured only on connections from Cloudflare's ranges (SWAG's real-IP settings), port 8096 firewalled to SWAG only) | M2 | Confirm or apply (192.168.1.151 and 192.168.1.222) |
| Threat model O12: disk encryption on 192.168.1.222; whether Cloudflare proxies `withings.pernemark.se` | M2 | Decide and tell the Architect |
| Threat model O15: confirm or reject the risks the threat model proposes as accepted | Before the M2 real-data gate | Review the "Accepted risks" section of `docs/security/threat-model.md` (merged with #41; risks 20 and 21 are new, #49 extends 21) |
| ECG (ADR 0006, in review in #46): whether our ECG estimates may be shown to anyone but the owner; whether public research ECG datasets may ever be used | M5 | Decide when M5 reaches them |

## Assumptions (unverified; each needs the probe or the owner)

| Assumption | Relied on by | Settled by |
|---|---|---|
| Withings' time-zone fields are IANA zone names (PLAN §5 `source_tz`) | #37 day assignment | the owner's first real sync (M1); the probe's shape-only scope (PLAN-operations §10.4) doesn't cover it |
| `attrib = 1` means "device, ambiguous (could be another household member)" (findings §2), so #37 excludes it as unconfirmed | #37 quality gate | the probe (attrib values seen) |
| The API hosts are `wbsapi.withings.net` and `account.withings.com`, and the token endpoint is `POST /v2/oauth2` with `action=requesttoken` (findings §2) | #6 egress lists and code exchange | the owner's first real connect (M1) |

ADR 0006's ECG assumptions A1–A8 are added when #46 merges. M1 adds a row per Withings behaviour
the code relies on but no real account has confirmed.

## External dependencies

| Dependency | Pinned / state |
|---|---|
| classbeat platform source | `d80605100992f85f8f02672b9b5f885ed6166214` (ADR 0001) |
| Withings public API (`wbsapi.withings.net`) | documented behaviour in [withings-findings.md](withings-findings.md); unverified until the probe |
| GitHub-hosted runners | public repository, free minutes |

## Milestone acceptance evidence

Recorded here when a milestone's "done when" (PLAN §11) is checked: CI runs, test names, e2e
runs, and what remains unverified.
