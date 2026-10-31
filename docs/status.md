# Status

Kept by the Architect and updated on every merge and every orchestration cycle. The
specification is [roadmap.md](roadmap.md); GitHub milestones and issues hold the detail. Status
levels (D10): agents set **IMPLEMENTED** only; **INTEGRATION_VERIFIED** and
**PRODUCTION_READY** are the owner's.

## Milestones

| # | Milestone | State | Level |
|---|---|---|---|
| M0 | Platform | in progress: #2 (#35), #3 (#34), #4 (#41), #5 (#65), #7 (#53), #45 (#48), #49 (#58), #52 (#64), #72 (#73) merged; #6 (#55) in a fix round after its second FAIL; #8, #9 and #75 in progress; #77 and #78 created; #42 and #57 wait for the `ci.yml` lock (held by #9) | — |
| M1 | Withings core | #19 (#63) and #66 (#71) merged; backlog #18, #20–#30, #36 (dependency graph below) | — |
| M2 | Home deploy (early) | not started; mostly owner actions (see "Blocked on the owner") | — |
| M3 | Identity additions | not started | — |
| M4 | Analysis v1 | pulled forward: #37 (#54), #38 (#67) and #56 (#74) merged; #68 in progress; #76 ready | — |
| M5 | ECG & nerve health | ADR 0006 (#46) in a fix round after its sixth FAIL (root cause of the trigger rule changed); #40 after it; #47 created | — |
| M6 | MVP UI | not started; the UI design is chosen ([docs/design/](design/README.md), #85; spec corrections in #91) | — |
| M7 | AI & reports | not started | — |
| M8 | Beyond-household gate | owner-gated | — |

## Active

| Issue | What | Developer | PR | State |
|---|---|---|---|---|
| #6 | Egress allowlist and mail | Opus | #55 | second FAIL (`efae7bb`); root cause changed: network capability derived from the locked environment and one reviewed table; fix round |
| #39 | ADR 0006: ECG subsystem spec | Opus | #46 | sixth FAIL (`222579e`, B7); root cause changed: the trigger is the provenance of the triggering value; fix round |
| #8 | Envelope encryption and KEK ring | Opus | — | in progress |
| #91 | Align the design mock-ups with the spec (follow-ups from the #86 review) | Architect | #92 | fresh Sonnet review |
| #9 | Job queue, leases, fencing and worker | Opus | — | in progress |
| #75 | Role-check follow-ups from the #65 review | Opus | — | in progress |
| #68 | Autocorrelation-robust trend test and change points | Sonnet | — | in progress |
| #79 | Status after the #58, #65, #71, #73 and #74 merges (this update) | Architect | this PR | in review |

Ready, waiting for a free slot: #76 (Sonnet), #78 (Opus). At most five Developers work on
issues at once; a fix round on an open PR may run while five are busy, and no new issue starts
until fewer than five are.

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
| #42 | CI dependency audits (pip-audit, npm audit, Trivy repo scan); remove the skip steps | Sonnet | #2, #3; the `ci.yml` lock (#9) | 2 |
| #49 | ADR follow-ups from the #41 review (webhook-secret lifecycle, re-pointing, definer-function role, partitions for past months) | Opus | #41 | 2 |
| #52 | Frontend test follow-ups from the #48 review (guard `afterEach`, template URLs) | Sonnet | #45 | 3 |
| #57 | Release follow-ups from the #53 review (deploy recipe, guard tests in CI, version check, Dependabot cooldown and titles, base-image digests) | Opus | #7; the `ci.yml` (#9, then #42) and `Dockerfile` locks | 3 |
| #72 | ADR follow-ups from the #58 review: mail outbox (ADR 0007), webhook-secret lifecycle split into ADR 0008, function input rules, single flight, partitions | Opus | #49 | 3 |
| #75 | Role-check follow-ups from the #65 review (login role, guard 5 constructions, ownership test, startup error) | Opus | #5 | 3 |
| #77 | Wiring after #8, #9 and #6: worker allowlist and KEK ring, guard 2 learns the queue grantee, web's queue check, `transaction_timeout` on runtime roles | Opus | #6, #8, #9, #75 | 4 |
| #78 | ADR follow-ups from the #73 review; the `transaction_timeout` decision | Opus | #72 | 4 |

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

| Issue | What | Owner model | Depends on |
|---|---|---|---|
| #37 | M4 analysis core: quality gates, daily values, minimum-data policy | Sonnet | #2 |
| #38 | M4 analysis methods: robust baseline, gap-weighted EWMA, Theil–Sen + modified MK, PELT | Sonnet | #2 |
| #39 | ADR 0006: ECG subsystem spec (PLAN §7.3: spec before code) | Opus | — |
| #40 | M5 ECG signal-processing core: filters, R-peaks, SQI gate, HRV | Sonnet | #2, #39 |
| #56 | Analysis follow-ups from the #54 review (purity-guard gaps, nerve baseline span) | Sonnet | #37; after #38 merges (both touch `test_analysis_purity.py` and `policy.py` semantics) |
| #68 | Autocorrelation-robust trend test and change points (follow-up from #38; before trend or change-point findings go live) | Sonnet | #38 |
| #76 | Purity guard and policy follow-ups from the #74 review | Sonnet | #56 |

Also created early in M5: #47, the owner-run `ownvitals ecg validate` command (ADR 0006 §14;
Opus; depends on #39, #40, #21, #25).

Scheduling: at most 5 Developers at once; the M0 critical path (#5 → #8 → #10 → #11 → #13 →
#15 → #16) and #6 come first, the pulled-forward issues fill free slots.

## Decisions log

| Date | Decision | Where |
|---|---|---|
| 2026-10-31 | Owner: the UI design is "Quiet instrument", exactly as in the mock-ups in `docs/design/mockups/` (IBM Plex self-hosted, the health score shown with its components, range bars on the phone, a System/Light/Dark theme setting). The mock-ups bind every UI change; gaps go to the Architect, design changes to the owner | #85, `docs/design/README.md`, AGENTS.md "UI design" |
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
| 2026-10-31 | ECG (ADR 0006, **#46 is open**; the root-cause change after three FAILs): findings E1–E6 are stored from Withings values only; our SQI gate is a **read-time overlay**, never stored in a finding: one function reads the linked recording's current `estimated` and hides E4/E6 only on an explicit failure ("not evaluated by ownvitals" otherwise), never hides E1–E3 and shows our gate on them only behind the owner flag; every consumer (dashboard, API, notifications, export, doctor PDF, AI context) goes through it. E4/E6 notifications are held up to 24 h (a setting) for the gate and never recalled. The E5 index is out of v1; E4 with unknown sex uses only the QTc > 500 ms band; SQI components, HR, HRV and markers only behind the owner flag; writing `estimated` bumps no `data_revisions` and re-evaluates nothing | ADR 0006 (#46), #25 |
| 2026-10-31 | ECG, Architect decisions for #46's fix rounds 4–6 (sent to the developer; **#46 is open**, so they are recorded here until the ADR carries them): an `estimated` whose fingerprint or gate-module versions (`input`, `filters`, `rpeaks`, `sqi`) differ from the running ones counts as absent; the link is resolved at read time and a soft-deleted recording is no link; a finding's **trigger is the provenance of the value that made it fire** (the source groups the value-producing function used: a morning daily value's one group, a median day's groups), and baseline recordings never count; E4/E6 are hidden when at least half of the trigger recordings **with a gate result** failed explicitly, recordings without a result are left out, and none with a result means "not evaluated by ownvitals"; a group that is both trigger and baseline counts as a trigger; HR and HRV only from up-to-date estimates in every output; a finding with no linkable recording ends its hold at once ("no linkable recording" needs an `ecg` sync that completed successfully after the trigger was stored); the JSON/CSV export carries every finding with its overlay state, the doctor PDF and AI context leave hidden ones out; no reason code is a display label; the hold decides once and restarts on a new revision | #46 (rounds 4–6) |
| 2026-10-31 | From the #35 review: prod also refuses a DSN `password`/`passfile` query parameter; the test-DSN check refuses an empty host and `service=` | #5 |
| 2026-10-31 | Egress (#6, **#55 is open**): `ownvitals.egress` is a package; the web-only code exchange is its own module; the Withings and model clients refuse to build or send unless the worker allowlist is installed; SMTP has a 20 s total deadline (moves to a setting in #10); web installs a deny-all urllib opener in #10; the guard denies network-capable imports outside egress and mail (from the #55 review) | #6, #10, #23 |
| 2026-10-31 | Release trust boundary: the `v*` tag ruleset stops moving and deleting tags, not creating them; the deploy is the boundary (tag commit in `refs/heads/main`, attestation with cert identity, `--source-ref`, `--source-digest`). No tag `creation` rule: it adds nothing while the owner's token is the only pusher | AGENTS.md, #57, T62 (#49) |
| 2026-10-31 | Webhook-secret rotation (ADRs 0004 and 0008, #58 and #73): **compromise** retires every entry at once, then appends, re-points and deletes, and cancels a routine rotation's queued `notify update` jobs; **routine** appends, waits for `active_from`, re-points, retires and deletes. `withings_apps.rotation_reason` is `routine` or `compromise`, and the CLI never downgrades a pending compromise (one conditional `UPDATE`). The function compares and clears the request under its row lock, refuses a routine step while a compromise is pending, refuses a stale request on retire-all and the clearing append, runs at READ COMMITTED and serialises on advisory lock 4; its `unretired_entries` and `seed_reused` rules guard an honest worker, not code with the app role (risk 21). Its role has `USAGE` on `public` and the column grants it needs, no client-secret or token column. Guard 11 checks with `has_any_column_privilege` and `has_table_privilege` that `ownvitals_app` and `ownvitals_maint` can't write `withings_apps`. A web cache that is stale or not yet loaded answers `POST` with 503 and `HEAD` with 200. `notify list` clean-up: #22 on (re)connect, #26 after the 14-day deadline, #27 on disconnect | ADR 0004, 0008, #21, #22, #24–#27 |
| 2026-10-31 | Advisory-lock registry (ADR 0005 §9): namespace 1 job creation, 2 token refresh, 3 partitions, 4 webhook-secret rotation | ADR 0005 |
| 2026-10-31 | Partitions (ADR 0002, 0005; #58, #73): floor 2009-01-01, ceiling now + 3 months, created lazily for the months actually written; every writer of a partitioned table calls `ensure_month_partitions` in its own short transaction before inserting; a row outside the range is not written but counted and recorded as skipped (out of range), never as missing | ADR 0002, 0005, #21, #25 |
| 2026-10-31 | Mail (ADR 0003 §6, ADR 0007): SMTP never from the worker. Mail that originates in the worker goes to an outbox (one owned table, one system table): a row holds a fixed `kind`, the recipient id (or a system row for an owner notice) and a fixed route, never an address or free text; web resolves the address and renders a fixed template, reading and marking rows only through definer functions, and the app role has no grants on the system table. The outbox is built in M6/M7; in M1, PLAN §4.3's owner notice is in-app only | ADR 0003, 0007, #22 |
| 2026-10-31 | Tenant isolation (#65): runtime roles may be members of no role, directly or indirectly, whatever the `INHERIT` or `SET` option and are refused superuser, `BYPASSRLS` and `REPLICATION` on every reachable role; the production database URL takes an allowlist of query parameters; any `app.*` default is refused at every level; guard 5 also refuses `__class__` assignment and explicit `__post_init__`/`__init__` calls | #5 (#65), #75 |
| 2026-10-31 | Analysis span convention (#74): a span is elapsed days (`(last − first).days`), `span_fraction` is elapsed days over `window_days` (so at most (w−1)/w); gaps and `window_days` stay inclusive day counts; the nerve baseline needs 28 days, so it is never sufficient while N1 isn't | #56 (#74) |
| 2026-10-31 | Analysis methods (#67): the Hamed–Rao modified Mann–Kendall lowers but doesn't remove false trends on autocorrelated data, and PELT finds spurious change points on it; #68 must fix both before trend or change-point findings go live | #38 (#67), #68 |
| 2026-10-31 | Guard 5 (ADR 0002 §1 "anywhere"): `object.__setattr__(self, …)` is allowed only inside a frozen dataclass's `__post_init__` on the bare `self`; every other form stays refused (it can't build a `UserContext`) | #5 (PR #65 body, `docs/dev/isolation.md`) |
| 2026-10-31 | Analysis core (#37): plausibility ranges are labelled engineering bounds unless findings cite one; a metric without a range is refused; values are `Decimal`, floats refused | #37 |
| 2026-10-31 | Dependabot: TypeScript 7 ignored until typescript-eslint supports it (Dependabot closed #59 after `@dependabot ignore typescript major version`); Dependabot titles over the 100-character header are shortened through group names (#57) or retitled by the Architect before merge | #59, #57 |
| 2026-10-31 | Frontend licence closure (from the #64 review; #52 item 4 reverted): an optional peer dependency is never skipped as dev-only. npm marks such a package `devOptional`, not `dev`, and a GPL package that Vite ships would hide behind the skip | #52, #64 |

## Architect tasks

- Split M6 into issues by screen group from `docs/design/README.md` ("Building it"): shell and
  tokens first. Point the UI parts of #10, #11 and #12 at the `Auth`, `Security` and `Admin`
  mock-ups.
- When #46 merges: record ADR 0006's assumptions A1–A8 (and the new one next to A7: Withings
  lists the ECG by the time an `ecg` sync completes) below with their settling items; add the
  0006 row to `docs/adr/README.md` (noting that 0006 and its tests file use "§n" for their own
  sections); re-check the ECG decisions row.
- The M4 wiring issue (to create) reconciles `metric_daily` (morning, mean, min, max, n; PLAN §5)
  with #37's `DailyValue` (morning or median, min, max, MAD), and keys duplicates of segmental
  types 173–175 on `(grpid, position)`.
- TypeScript 7: re-enable when typescript-eslint supports it (`@dependabot unignore` or a manual
  bump issue).
- Removing the "Is there a backend/frontend yet?" skip steps is part of #42.
- Dependabot PRs go through the same gate with a fresh Sonnet reviewer.
- Frontend licence closure tests: add an `optionalDependencies` row (a mutation survived in the
  #64 review; the same gap is on `main`). It goes into the next frontend issue.
- After #46 and #55 merge: move or confirm the decisions they carry (the ECG rows above; the
  egress API names for #9, #10, #22 and #29 and the closed-world network classification).
- After #8 merges: record its decisions (web and worker refuse to start when the KEK check can't
  run, including on an unmigrated database; the seed and pepper ids are settings).
- The M4 wiring issue also carries a daily value's source groups as provenance (ADR 0006's
  trigger rule, #46) and uses `from_daily`, not `from_instants`, which counts UTC days.

## Shared-file locks

No two open PRs change the same file. Files many issues need are locked here by the Architect;
an issue that needs a locked file waits for the holder's merge, then updates its branch.

| File | Held by |
|---|---|
| `.github/workflows/ci.yml` | #9 (backend job); #42, then #57 |
| `backend/src/ownvitals/config.py` | #8 |
| `backend/pyproject.toml`, `backend/uv.lock` | — |
| `backend/src/ownvitals/cli.py` | #9 |
| `compose.yaml` | #9 |
| `Dockerfile` | — (#57 next) |
| `AGENTS.md` | — |
| `backend/src/ownvitals/web.py` | #8 |
| `backend/src/ownvitals/db/engine.py` | — |
| `backend/src/ownvitals/db/tables.py` | #8 |
| `backend/tests/dbtools.py` | — |
| `backend/tests/conftest.py`, `backend/tests/test_web.py` | #8 |
| `backend/tests/role_guard.py`, `docker/postgres/init/10-roles.sql` | #8 |
| `backend/tests/session_scan.py` | #9 (one `system_session` entry by #8) |
| `backend/src/ownvitals/db/role_check.py`, `backend/tests/user_context_scan.py`, `docs/dev/isolation.md` | #75 (one test function in `test_role_check.py` by #8) |
| `backend/tests/test_migrations.py` | #8 (head pin); #9 moves it after #8 if it merges second (the migration rule below) |
| `docs/dev/backend.md` | #8 |
| `docs/dev/stack.md` | — |

Alembic migrations: a PR takes the next free revision number when it opens; if another
migration merges first, it renumbers and re-chains on its update, which needs a fresh scoped
review.

## Completed

- 2026-10-31: #85 the chosen UI design and its mock-ups merged as #86 (Sonnet PASS `d01ff96`, merge check `2bb7dc4`); the review's spec findings go into #91.
- 2026-10-31: #72 ADR hardening merged as #73 (Opus PASS `293d818` after FAIL `cb9f7f6`, merge check `5fc8520`); ADRs 0007 and 0008 added.
- 2026-10-31: #56 analysis follow-ups merged as #74 (Sonnet PASS `ce28f28`, merge check `8f483aa`) → IMPLEMENTED.
- 2026-10-31: #5 tenant isolation merged as #65 (Opus PASS `5a994d1` after FAIL `46264a4`) → IMPLEMENTED.
- 2026-10-31: #66 fake tightening merged as #71 (Sonnet PASS `b53ee8f`, merge check `9825aa0`) → IMPLEMENTED; synthetic only.
- 2026-10-31: #69 status merged as #70 (Sonnet PASS `58f8a55` after FAIL `cf7b21a`, merge check `8fc632f`).
- 2026-10-31: #49 ADR follow-ups merged as #58 (Opus PASS `f15a46d` after FAILs `a18bcd0` and `a86f571`, merge check `555a1f7`).
- 2026-10-31: #38 analysis methods merged as #67 (Sonnet PASS `9a622df`, merge check `6b71a18`) → IMPLEMENTED.
- 2026-10-31: #52 frontend test follow-ups merged as #64 (Sonnet PASS `12851d6`) → IMPLEMENTED.
- 2026-10-31: #19 in-process fake-withings and synthetic data generator merged as #63 (Sonnet PASS `61b1d18`, merge check `8394ea6`) → IMPLEMENTED; synthetic only, nothing verified against Withings.
- 2026-10-31: #61 status after the wave 2 merges merged as #62 (Sonnet PASS `d55e6db`, merge check `5be3d07`).
- 2026-10-31: Dependabot #59 (TypeScript 7) closed by Dependabot after the major-version ignore.
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
| Threat model O15: confirm or reject the risks the threat model proposes as accepted | Before the M2 real-data gate | Review the "Accepted risks" section of `docs/security/threat-model.md` (merged with #41; risks 20 and 21 are new; #49 and #72 extend 21, including the ways code with the app role could stall a rotation) |
| ECG (ADR 0006, in review in #46): whether our ECG estimates may be shown to anyone but the owner; whether public research ECG datasets may ever be used | M5 | Decide when M5 reaches them |

## Assumptions (unverified; each needs the probe or the owner)

| Assumption | Relied on by | Settled by |
|---|---|---|
| Withings' time-zone fields are IANA zone names (PLAN §5 `source_tz`) | #37 day assignment | the owner's first real sync (M1); the probe's shape-only scope (PLAN-operations §10.4) doesn't cover it |
| `attrib = 1` means "device, ambiguous (could be another household member)" (findings §2), so #37 excludes it as unconfirmed | #37 quality gate | the probe (attrib values seen) |
| The API hosts are `wbsapi.withings.net` and `account.withings.com`, and the token endpoint is `POST /v2/oauth2` with `action=requesttoken` (findings §2) | #6 egress lists and code exchange | the owner's first real connect (M1) |
| The fake's readings where the public spec is silent (the module docstrings of `backend/tests/withingscore.py`, `withingswire.py`, `withingsnotify.py`, and the #63 PR body for the full list): status 2553 for a bad refresh token and 214 for a missing scope; the OAuth scope each data endpoint needs (`DATA_ROUTES`, now in `withingsstore.py`); the signed string of `revoke` and its answer for an unknown user | #19's fake, so every M1 client test (#20, #22, #24) | the owner's first real connect and sync (M1) |
| The fake's stricter readings from #66 (#71; the docstrings of `backend/tests/withingswire.py`, `withingsrules.py`, `withingsreceiver.py`, `withingsstore.py` and the #71 PR body): a non-POST API call answers 2554 and a secret in the query string 503 (no documented status); webhook success means a 2xx within 3 s (the short reading of "a few seconds"); callback URLs must be HTTPS on port 443 with a domain name, else 293 (the stricter reading); the rate limit is a token bucket of 120 a minute per application, burst 120, and the 601 carries no `wait_seconds`; a new authorization drops all earlier tokens; refreshing with the current token ends an older grace token; the scope check runs before the first-use mark | the M1 client tests (#20, #22, #24, #25) | the owner's first real connect and sync (M1); the probe for the token behaviours |

ADR 0006's ECG assumptions A1–A8 are added when #46 merges. M1 adds a row per Withings
behaviour the code relies on but no real account has confirmed.

## External dependencies

| Dependency | Pinned / state |
|---|---|
| classbeat platform source | `d80605100992f85f8f02672b9b5f885ed6166214` (ADR 0001) |
| Withings public API (`wbsapi.withings.net`) | documented behaviour in [withings-findings.md](withings-findings.md); unverified until the probe |
| GitHub-hosted runners | public repository, free minutes |

## Milestone acceptance evidence

Recorded here when a milestone's "done when" (PLAN §11) is checked: CI runs, test names, e2e
runs, and what remains unverified.
