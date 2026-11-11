# Status

Kept by the Architect and updated on every merge and every orchestration cycle. The
specification is [roadmap.md](roadmap.md); GitHub milestones and issues hold the detail. Status
levels (D10): agents set **IMPLEMENTED** only; **INTEGRATION_VERIFIED** and
**PRODUCTION_READY** are the owner's.

## Milestones

| # | Milestone | State | Level |
|---|---|---|---|
| M0 | Platform | in progress: #2 (#35), #3 (#34), #4 (#41), #5 (#65), #7 (#53), #8 (#82), #9 (#96), #42 (#106), #45 (#48), #49 (#58), #52 (#64), #57 (#111), #72 (#73), #75 (#81) and #78 (#83) merged; #6 (#55, ADR 0009 runtime egress enforcement), #101 (#118), #10 (#125), #11 (#132), #12 (#129, admin gate, audit log and stored flags), #122 (#123, static egress guard), #140 (#142, step-up window bounds), #89 (#115, guard 5 follow-ups), #143 (#145, pending-session expiry) and #146 (#147, purge and session-list bounds) merged; #13 (#151), #18 (#153), #77 (#155), #124 (#149), #126 (#150) and #127 (#152) merged (2026-11-02, Cursor Grok 4.7 Developers); #14 (#169, the e2e harness and the CI `E2E` job), #15 (#170, RLS tests in every execution context, after four review rounds), #163 (#171, disconnect and account delete on the revoke-first worker job) and #164 (#173, the Withings job kinds in the worker, the webhook target check and the `queue_schedule` scheduler function) merged (2026-11-03, Claude Developers); #16 (#223, the post-port threat-model review and the M0 test mapping), #218 (#225, "delete imported data" also removes derived rows) and #194 (#220, the mail outbox and web sender, ADR 0007) merged; the M0 done-when evidence is under "Milestone acceptance evidence" | — |
| M1 | Withings core | #19 (#63), #66 (#71) and #20 (#128, the API client, error taxonomy and response models) merged; #21 (#154), #22 (#156), #23 (#157), #24 (#158), #25 (#159), #26 (#160) and #27 (#161) merged (IMPLEMENTED, synthetic only); #28 (#166, the metrics registry; the endpoint and wiring are #167) and #29 (#172, the shape-only probe CLI: the owner runs it, INTEGRATION_VERIFIED is the owner's) merged; #36 (#221, the fake-withings e2e service) merged; open: #30 (the M1 done-when e2e), #167 (dependency graph in [status/dependency-graphs.md](status/dependency-graphs.md)) | — |
| M2 | Home deploy (early) | #84 (#100, the forced-command deploy script, pulled forward) merged: IMPLEMENTED, not installed (installing it is the owner's); #101 (#118, the deploy-side follow-up) merged; the rest are mostly owner actions (see "Blocked on the owner") | — |
| M3 | Identity additions | ADR 0011 (#177, passkeys and QR approve-login; #212) merged as Proposed; #178 (#226, passkeys: registration, sign-in and management) and #179 (#232, QR approve-login backend) merged (IMPLEMENTED, synthetic only; notes under "QR login (#232)"); open: #180 (the passkey and QR UI with an e2e per flow) | — |
| M4 | Analysis v1 | pulled forward: #37 (#54), #38 (#67), #56 (#74), #68 (#87), #76 (#94), #99 (#102) and #95 (#109) merged; ADR 0010, the v1 finding rules (#110, #112; accepted in #116, #117) and roadmap Amendment 3 merged; ADR 0012 (#182, scores; #213) merged as Proposed; merged: #183 (#215, rule core, body and heart rules), #181 (#216, analysis schema), #184 (#222, nerve-health and system rules), #185 (#227, measures into observations), #190 (#228, the one findings read function, the ECG gate overlay and the findings API), #229 (#230, sleep and activity streams; scope ownership; serialised passes) #186 (#231, daily values into `metric_daily` with staleness recompute), #189 (#233, the read API: status, metrics, days, readings and the nerve summary) and #187 (#234, the rule evaluation job, finding episodes, `ownvitals analysis check` and migration 0016; roadmap Amendment 6); backlog #188; since 2026-11-10: #343 (#324, segmental muscle mass tiles in the metrics read API), #366 (#303, metrics follow-ups) and #373 (#370, the rate limiter lanes follow live demand, roadmap Amendment 10) merged | — |
| M5 | ECG & nerve health | ADR 0006 (#46) and its follow-ups (#88, #97) merged; #40 (#114, the signal-processing core, with its ADR 0006 amendments) and its follow-ups #119 (#120) merged; #47 (#162, the owner `validate` command) merged; backlog #191, #192; since 2026-11-10: #361 (#307, `after_ecg` tests and rule gaps) and #367 (#315, exact V4 percent, OC3 mains not observable) merged (IMPLEMENTED, synthetic only) | — |
| M6 | MVP UI | the UI design is chosen ([docs/design/](design/README.md), #85) and aligned with the spec (#91); #98 (#105: shell, tokens, theme, fonts and primitives; pulled forward) merged; #193 (#217, chart primitives, value styles and the data layer) and #194 (#220, mail outbox) merged; backlog #195–#203; since 2026-11-10: #345 (#325, body composition and findings test gaps), #358 (#304, outcome notice, QR expiry race, approve-login load failure), #359 (#308, connect route tests) and #360 (#283, i18n follow-ups) merged | — |
| M7 | AI & reports | ADR 0013 (reports and export) and ADR 0014 (AI insights) (#204; #214) merged as Proposed; #205 (#224, the per-user monthly AI cost cap that holds under concurrency) merged; backlog #206–#211; since 2026-11-10: #344 (#333, cross-user purge, `export_ready` mail and export events in activity), #346 and #348 (export e2e on the fake account), #347 (#208, chat assistant with privacy and prompt-injection evals), #355 (#209, weekly digest) and #371 (conversation-creation rate limit) merged (IMPLEMENTED, synthetic only); #211 (the export UI) and the AI owner gates remain | — |
| M8 | Beyond-household gate | owner-gated; prerequisites P1–P3 (roadmap Amendment 7) | — |
| G | Groundwork (Amendment 7, ADR 0015): `config.py` split, multilingual en/sv | all 7 issues merged on 2026-11-08 and IMPLEMENTED (synthetic tests only): G1 #245 (#259), G2 #246 (#260), G3 #249 (#264), G4 #250 (#263), G5 #253 (#265), G6 #254 (#268), G7 #255 (#269). The Swedish health wording stays on the M8 medical-claims review list (ADR 0015). Next migration number: 0020 | IMPLEMENTED |
| H | Hardening from classbeat (Amendment 7) | all 4 issues merged on 2026-11-08 and IMPLEMENTED (synthetic tests only; all security-critical): H1 #247 (#262), H2 #248 (#261), H3 #251 (#266, migration 0019), H4 #252 (#267, migration 0018). Next migration number: 0020 | IMPLEMENTED |

### Start order (Amendment 7, from 2026-11-09): waves 1-4 done, all merged on 2026-11-08; waves A to C merged 2026-11-09

Waves keep the file sets disjoint: no file is in two locks of the same wave. A wave starts its
issues in parallel within the Developer limit of AGENTS.md (about three). Wave 1 has four, so H2 or
H1 starts when G1, a short move, merges. The catalogue is split by area (`frontend/src/i18n/<locale>/<area>.ts`,
G2), and each issue owns its own area file.

| Wave | Issues | Locks taken (all under `backend/src/ownvitals/` or `frontend/` unless a path says otherwise) | Notes |
|---|---|---|---|
| 1 | **G1** #245 (Sonnet, Opus first review), **G2** #246 (Sonnet), **H1** #247 (Opus), **H2** #248 (Opus) | G1: `config.py`, the new `config_*.py` beside the existing `config_secrets.py` and `config_net.py` (from #242; `config.py` is still 493 lines), the config tests, `docs/dev/backend.md`. G2: `src/i18n/**`, `src/lib/format.ts`, `src/pages/**`, `src/components/**`, `src/api/messages.ts`, `src/main.tsx`, `index.html`, `public/locale-init.js`, `docs/dev/frontend.md`. H1: `auth/oauth_outcomes.py`, `auth/oauth_client.py`, `auth/oauth_routes.py`, `auth/oauth_idtoken.py`, the fake-oidc test support, the ADR 0003 amendment, the new `docs/dev/auth-reauth.md`. H2: `auth/sessions.py` (302 lines, must not grow), `auth/session_manage.py`, `auth/session_model.py`, `auth/signin_routes.py`, `auth/reset_routes.py`, `auth/accounts.py`, `auth/admin_accounts.py`, `auth/routes.py`, `auth/oauth_store.py`, `docs/dev/auth.md` | G1 merges first so the others add settings to small modules. H1 is backend only: its new reason code is worded by G4. If H1 needs `start_session` or `oauth_store.py`, it waits for H2's merge. |
| 2 | **G3** #249 (Opus), **G4** #250 (Sonnet, Opus first review) | G3: the next migration number, `auth/accounts.py` (sign-up paths), `auth/oauth_store.py`, `auth/routes.py` (session payload), the G1 module that holds the locale settings, `src/i18n/resolve*`, `src/i18n/*/account.ts`, `src/api/auth.ts` (session type), `src/auth/SessionContext.tsx`, `src/pages/SecurityPage.tsx`, `docs/dev/auth.md`, `docs/dev/frontend.md` (free once G2 has merged). G4: the text fields of `views/**`, the user-facing sentence sites in `jobs/` (never lease, fencing or queue SQL) and `connections/`, `src/api/**` except `src/api/auth.ts`, `src/i18n/*/codes.ts` and `*/health.ts`, `docs/dev/i18n.md` | After G1, G2 and H2 (G4 also after H1, for its code) |
| 3 | **G5** #253 (Opus), **H3** #251 (Opus), **H4** #252 (Opus); #180 takes H4's e2e | G5: `notify/**`, `auth/recipients.py` (the send-time locale lookup, inside guard 8), `auth/mails.py`, `auth/notices.py`, the `Content-Language` header in `mail.py`, the mail tests and golden files. H3: `auth/events.py`, the logout paths in `auth/routes.py`, the new `auth/activity_routes.py`, `docs/dev/auth.md` (it calls the existing notice functions without editing them). H4: `auth/passkey*.py`, `auth/sessions.py` if needed, the ADR 0011 amendment, `docs/dev/auth-passkeys.md` | After G3 (H3 also after H2) |
| 4 | **G6** #254 (Sonnet translator, Opus Reviewer), then **G7** #255 (Sonnet) | G6: `src/i18n/sv/**`, the `sv` mail tables and golden files, the glossary in `docs/dev/i18n.md`. G7: the new guard tests, `e2e/specs/i18n.spec.ts`, the checklist section of `docs/dev/i18n.md` | After G4 and G5. M6 screen issues may start once G2 and G3 have merged. |
| later | P1–P3 (Amendment 7) | — | After M6, before M8; never enabled without the owner |

## Active

Reconciled 2026-11-11 against `git log origin/main` (`main` at `199074b`); the open PRs were not queried in this docs PR.

| Issue | What | Developer | PR | State |
|---|---|---|---|---|
| — | Delegated owner decisions and status drift for #375 to #388 (this PR, docs only) | Developer | this PR | open |

No issue is in progress. The Architect picks the next ones from the open issues (labels are not authoritative; this table is). Hardening issues from the earlier reviews (#277 to #284, #294, #301 to #305) may be partly done by the PRs below; check each issue before starting it. Reconciled 2026-11-11 against `git log origin/main` (`main` at `199074b`): #365, #314, #277 and #282 are decided (delegated, provisional; see "Blocked on the owner") and merged (#375, #378, #379, #380, #388), #352 and #353 are finished (#377, #384, #387); open follow-up issue #389. Still to check before starting: #280 (webhook receiver hardening; #321 and #318 covered most of it) and #211 (export UI; #332 and #344 are merged).

Merged on 2026-11-10 and 11 (`main` now `199074b`), after #373, all IMPLEMENTED (synthetic tests only; verdict comments are on the PRs): #375 (admin account changes commit with their `ok` audit row; #314, #277 item 1), #376 (batched docs follow-ups), #377 (AI lock-wait timeouts), #378 (security notices in the recipient's language), #379 (TOTP enrolment message), #380 (revoke the Withings grant before an admin delete; #365), #384 (reports.md follow-ups), #385 (trailing-dot localhost RP ID refused in production), #386 (batched test follow-ups of #361-#378), #387 (AI hardening follow-ups of #352) and #388 (revoke for a disabled account before delete; issue #383). Open follow-up issue: #389. The shared-file locks below are released.

Merged on 2026-11-10 (`main` now `511d8e9`), #343 to #373, all IMPLEMENTED (synthetic tests only; verdict comments are on the PRs): #343 (segmental muscle mass tiles), #344 (cross-user purge, export mail and activity events), #345 (UI test gaps), #346 and #348 (export e2e), #347 (chat assistant, evals), #350 and #368 (ingest waits out a refused rate-limit claim; claim backoff tests), #354 (docs: a `PASS` only on green `ci` and `pr-title`), #355 (weekly digest), #356 (`rate.sql` in the CI db-functions step), #357 (notify checks every connection of a grpid), #358 (UI follow-ups), #359 (connect tests), #360 (i18n follow-ups), #361 and #367 (ECG follow-ups), #363 (decrypt-failure flag guard), #364 (admin delete purges webhook events), #366 (metrics refresh after the queue check, keyset webhook-age watermark), #369 (backfill fan-out through the real limiter), #371 (AI conversation-creation rate limit), #372 (auth hardening follow-ups) and #373 (limiter lanes by live demand, Amendment 10). There are no PRs numbered #349, #351, #353, #362, #365, #370: they are issues.

Merged since `b2e6558` (all IMPLEMENTED, synthetic tests only; verdict comments are on the PRs): #298 (ECG list and viewer), #299 (ECG rules E1-E4, E6), #300 (Withings connect web routes), #309 (findings inbox), #310 (token writes on disconnected rows), #312 (sync ranges, ECG leads, callback URL), #313 (admin enable refusal), #316 (finding notifications), #318 (queue follow-ups), #320 (AI consent and tool set), #321 (webhook uniform answers and quotas), #323 (body composition page), #326 (nerve health page), #328 (saved display settings), #332 (JSON/CSV export with a single-use download), #327 (worker-only model client, redacted audit log, fake model API), #334 (compose mounts for the AI key and app secret, worker only), #336 (12-month `ai_audit` retention purge), #337 (metrics list honours range), #335 (doctor PDF), #338 (scores input revision digest), #339 (egress chain scan, hook installed in migrate), #340 (webhook secret cache refresh after startup) and #341 (ECG PDF off the event loop, clipped traces, image/link guard). Per-PR detail is in [status/completed.md](status/completed.md) where recorded.


Merged on 2026-11-09 (`main` now `b2e6558`), all IMPLEMENTED (synthetic tests only; verdict comments are on the PRs): #285 (docs, reconcile and the #165 split), #286 (#257, hardening from the #242 review), #287 (#197, overview dashboard), #288 (#198, metric detail and day view), #289 (#203, settings), #290 (#167, `/metrics` from the worker), #291 (M1 done-when flow, failure injection, RLS context tests), #292 (#188, scores job), #293 (#180, passkey and QR approve-login UI), #295 (onboarding and connections screen) and #297 (#192, ECG list and signal API, ECG PDF). Their assumptions are in the table below and the non-blocking findings are #294, #296 and #301-#305.


Merged on 2026-11-08 (`main` now `a47e1ef`): groundwork G and H, 11 PRs, see "Groundwork G/H done-when" below and [status/completed.md](status/completed.md).

Merged on 2026-11-08 (`main` now `7987369`): #256 (roadmap Amendment 7 and ADR 0015, the G and H plan, docs), #243 (the `python:3.12.16-slim-bookworm` digest for the apt CVE-2026-109518 fix, so the E2E Trivy scan passes again) and #242 (the behaviour-neutral split of source files over 300 lines, with the egress, crypto, auth, withings and worker guards repointed; Opus `PASS` on `d69e56e`; it left `crypto/keks.py`, `findings_read.py`, `passkey_ceremonies.py`, `withings/client.py` and `worker/loop.py` whole on purpose). #242 had no issue; the findings that remain are #257.

Merged before that (`main` was `0621653`, then past `6079898`): #233, #234, #235 (the security-adjacent tier and Sonnet delta reviews in AGENTS.md), #219 (this reconcile), the Dependabot PR #175 (`@playwright/test` 1.64.1), #215, #217, #223, #216, #221, #220, #222, #225, #224, #227, #228, #226, #230, #231, #232 and the Dependabot PRs #174 (docker group) and #176 (tzdata 2026.6), all IMPLEMENTED (synthetic only; docs PRs have no status). There is no #229 PR: #229 is the issue that #230 closes. Per-PR lines are in [status/completed.md](status/completed.md); the verdict comments are on the PRs. Rule observed on 2026-11-03: `main` requires branches up to date, so every merge makes the other open PRs stale; the Architect merges one at a time and records a merge check for each update.
At most five Developers work on issues at once; a fix round on an open PR may run while five are busy.

## Detail files

Read these only when the work needs them; the index above is what every cycle reads.

| File | Holds |
|---|---|
| [status/dependency-graphs.md](status/dependency-graphs.md) | M0 and M1 dependency graphs, pulled-forward issues |
| [status/decisions-log.md](status/decisions-log.md) | Dated Architect decisions, with their evidence |
| [status/architect-tasks.md](status/architect-tasks.md) | Open Architect tasks |
| [status/completed.md](status/completed.md) | Merged work, by PR |

## Shared-file locks

No two open PRs change the same file. Files many issues need are locked here by the Architect;
an issue that needs a locked file waits for the holder's merge, then updates its branch.

| File | Held by |
|---|---|
| `backend/pyproject.toml`, `uv.lock`, `compose.yaml`, `Dockerfile`, `.github/workflows/ci.yml`, `frontend/package.json` and lock, `frontend/src/main.tsx`, `config.py`, `config_secrets.py`, `web.py`, `appPages.tsx`, `worker/kinds.py` | — (free now; #372 was the last holder of `config.py`, #371 of `auth/ratelimit.py` and `auth/deps.py`, #373 of `rate.sql`: all merged on 2026-11-10); the issues that need them take the lock when they start (planner's note: `appPages.tsx` takes one line per screen issue; `web.py` for routes; `jobs/kinds.py` and `worker/**` for job kinds; the next free migration number is 0027; 0026 is the weekly digest) |
| `docs/status.md`, `docs/status/completed.md` | Architect |
| everything else | — (released) |

Alembic migrations: a PR takes the next free revision number when it opens; if another
migration merges first, it renumbers and re-chains on its update, which needs a fresh scoped
review.

## Groundwork G/H done-when

Merged on 2026-11-08, IMPLEMENTED with synthetic tests only (Amendment 7, ADR 0015): the `config.py` split (G1), the frontend i18n runtime (G2), the account language and sign-up locale (G3), stable response codes (G4), mail in the recipient's language (G5), the Swedish catalogues (G6), the half-wired-language guard, e2e and the add-a-language checklist (G7); Google re-authentication (H1), reset race windows (H2), sign-out and security-activity events (H3, migration 0019) and shorter cross-device passkey sessions (H4, migration 0018). Nothing is INTEGRATION_VERIFIED or PRODUCTION_READY; the owner's gates are untouched: the Swedish health wording is still listed for the M8 medical-claims review, and the ADR 0015 owner questions stay open.

### Backlog from the G/H reviews

Batched into hardening issues on 2026-11-09: auth side #282, i18n side #283; the earlier `#165` items are #277 to #281 and #284 (the umbrella #165 is closed and its comment lists them). The non-blocking findings of the wave A to C reviews (#287 to #297) were batched on 2026-11-09 into #301 (ECG PDF), #302 (scores), #303 (metrics), #304 (UI: outcome notice, QR panel, approve-login) and #305 (UI: saved display settings, range to `/api/metrics`, `sv` native review); #294 (backfill fan-out) and #296 (connect routes) came from #291 and #295. The ADR 0015 owner questions and the native-Swedish review of the health wording stay the owner's.

## Blocked on the owner

Nothing blocks M0 or any open issue. In 2026-11 the owner delegated the product and policy questions below marked DELEGATED to the Architect ("figure out the questions without me"). Each was settled with the safe default, is **PROVISIONAL** and is re-checked at the M8 human review; none is an owner sign-off or a passed gate. Still owner-only: the Withings and Google apps, SWAG and Cloudflare, the deploy, backups, the M8 reviews and the threat-model items O6, O12 and O15 (later: the live Withings, home-server and real-account steps, which agents work past with fakes and the assumptions below).

| Item | Needed for | Owner action |
|---|---|---|
| Withings developer app (callback `https://ownvitals.eu/...` for production, `https://ownvitals.pernemark.se/...` for staging, HTTPS on 443) | M1 live, M2 | Register the app; put the client id and secret on the home server only |
| Google OAuth client for sign-in | M2 live sign-in | Create the OAuth client; secret on the home server only |
| SWAG `site-confs/ownvitals.conf`, Cloudflare DNS, SSL Full (strict), WAF skip rule for `/api/webhooks/withings/*` | M2 | Apply on 192.168.1.151 and Cloudflare |
| `ownvitals-deploy` user with the forced-command key on 192.168.1.222 | M2 | Create the user and install the reviewed deploy script |
| Backups and a restore drill | M2 real-data gate | Approve the backup target; run the drill |
| Connect your Withings account and run `ownvitals probe` on the home server | M1 → INTEGRATION_VERIFIED | Run it; paste the shape-only output into the issue |
| Written clarification from Withings on Developer Agreement §4 and D2 (BYO, user cap) | M8 | Contact Withings |
| Human security review, legal checklist (§9.3), medical-claims review | M8 | Owner or a trusted reviewer |
| Threat model O6: SWAG settings for ownvitals as the threat model lists them (no buffering for SSE, the webhook location's access log without the URI, `CF-Connecting-IP` honoured only on connections from Cloudflare's ranges (SWAG's real-IP settings); Amendment 9: no host firewall, no trusted proxies, web on 8097/8098) | M2 | Confirm or apply (192.168.1.151 and 192.168.1.222) |
| Threat model O12: disk encryption on 192.168.1.222; whether Cloudflare proxies `ownvitals.eu` and `ownvitals.pernemark.se` (both are, 2026-11-09) | M2 | Decide and tell the Architect |
| Threat model O15: confirm or reject the risks the threat model proposes as accepted | Before the M2 real-data gate | Review the "Accepted risks" section of `docs/security/threat-model.md` (merged with #41; risks 20 and 21 are new; #49, #72, #78 and #88 extend 21, including the ways code with the app role could stall a rotation) |
| ECG (ADR 0006, merged in #46): whether our ECG estimates may be shown to anyone but the owner; whether public research ECG datasets may ever be used | M5 | Decide when M5 reaches them |
| Receiving mail through Proton Mail Bridge (IMAP, or a `mail-worker` that posts to a REST API): production sending through Bridge on the owner's own domain is decided, receiving may come later | Not in the frozen spec; not before M2 mail works | A product decision; then the Architect writes a roadmap amendment and a threat-model entry for the new inbound path |
| ADR 0011 (passkeys, QR login): **DELEGATED 2026-11, PROVISIONAL, re-check at the M8 human review**: a passkey is not the second factor (`passkey_satisfies_second_factor` stays false); `geo_source=none` ("Place unknown"); QR approve-login for household accounts only (`qr_login_enabled=false` is the safe-off) | M3, M8 | Re-check at M8 |
| ADR 0012 (scores): **DELEGATED 2026-11, PROVISIONAL, re-check at the M8 human review**: the Health Improvement Score band table stays unset, so the score shows "not set" (the Dashboard mock-up's 71/100 differs until a table with a source and licence under §9.4 exists) | M4, M6 | Re-check at M8 |
| ADR 0013 (reports, export): **DELEGATED 2026-11, PROVISIONAL, re-check at the M8 human review**: doctor-PDF variant none; FIT streams off; ECG estimates withheld from an unflagged account's export | M7 | Re-check at M8 |
| ADR 0014 (AI insights): **DELEGATED 2026-11, PROVISIONAL, re-check at the M8 human review**: provider data-handling review not recorded, so AI stays off; per-user and global caps 0; experimental content not sent to the model; retention 90 days (conversations) and 12 months (audit rows); `ai_eval_ref` unset; the 30/hour conversation-creation limit (#371, kept in #387) stays | M7 | Re-check at M8; AI is enabled only after the review and the eval reference are recorded |
| The loading state of the UI (design has none; plain "Loading…" line used) and tick formats | M6 | Say if you want a skeleton design when the dashboard screens land |
| ADR 0010 open items: the C2 score-card band, the HIS table, Readiness and Body Path | M4 wiring, M6 | Decide when the wiring issue or the screen reaches them |
| ADR 0015 (multilingual): **DELEGATED 2026-11, PROVISIONAL, re-check at the M8 human review**: languages en and sv; the other defaults of the ADR stand; the Swedish health wording stays on the M8 medical-claims review list | G, M6, M7 | Re-check at M8 |
| #277 item 1 / #314 (admin account change and its audit row in one transaction): **DELEGATED 2026-11, PROVISIONAL, re-check at the M8 human review**: the new NOLOGIN owner role with the ADR 0002 section 5 amendment, as merged in #375 (#314) | #314, #277 | Re-check at M8 |
| #365 (admin delete does not revoke the Withings grant first, PLAN section 4.3): **DELEGATED 2026-11, PROVISIONAL, re-check at the M8 human review**: option A, implemented in #380, with #388 (issue #383: revoke for a disabled target) | #365 (security-critical) | Re-check at M8 |
| Digest design sign-off (#355) and its English-only assumption (the digest text is generated in English whatever the user's locale); whether the weekly digest mail needs its own `notify_email` setting or follows the existing one | Enabling the digest | Approve or amend |
| Unevaluated digest prompt text: `ai_eval_ref` stays unset until the digest prompt has an eval run, so AI stays off for the digest | M7 AI enabled | Run the eval or accept |
| The #347 rail placement of the chat assistant (design covers no AI rail) | M6/M7 UI | Say where it goes |
| #211 session binding: a download token is bound to the cookie plus a fresh step-up, not strictly to one session (needs an ADR 0013 section 4 amendment) | #211 | Accept or ask for the amendment |
| #315 item 1: `marked_for_resubscribe` has no writer; build one or delete the column | Webhook resubscribe | Decide |
| ADR 0004 amendment for #311 item 3 | #311 follow-up | Decide, or the Architect amends |
| 30 per hour AI conversation creation (#371) is a Developer choice with no ADR | AI cost and abuse limit | Confirm, or record the number in ADR 0014 section 8 |

## Assumptions (unverified; each needs the probe or the owner)

| Assumption | Relied on by | Settled by |
|---|---|---|
| Withings' time-zone fields are IANA zone names (PLAN §5 `source_tz`) | #37 day assignment | the owner's first real sync (M1); the probe's shape-only scope (PLAN-operations §10.4) doesn't cover it |
| `attrib = 1` means "device, ambiguous (could be another household member)" (findings §2), so #37 excludes it as unconfirmed | #37 quality gate | the probe (attrib values seen) |
| The API hosts are `wbsapi.withings.net` and `account.withings.com`, and the token endpoint is `POST /v2/oauth2` with `action=requesttoken` (findings §2) | #6 egress lists and code exchange | the owner's first real connect (M1) |
| The fake's readings where the public spec is silent (the module docstrings of `backend/tests/withingscore.py`, `withingswire.py`, `withingsnotify.py`, and the #63 PR body for the full list): status 2553 for a bad refresh token and 214 for a missing scope; the OAuth scope each data endpoint needs (`DATA_ROUTES`, now in `withingsstore.py`); the signed string of `revoke` and its answer for an unknown user | #19's fake, so every M1 client test (#20, #22, #24) | the owner's first real connect and sync (M1) |
| The fake's stricter readings from #66 (#71; the docstrings of `backend/tests/withingsrules.py`, `withingsnotify.py`, `withingscore.py`, `withingswire.py` and the #71 PR body): a non-POST API call answers 2554 and a secret in the query string 503 (no documented status); webhook success means a 2xx within 3 s (the short reading of "a few seconds"); callback URLs must be HTTPS on port 443 with a domain name, else 293 (the stricter reading); the rate limit is a token bucket of 120 a minute per application, burst 120, and the 601 carries no `wait_seconds`; a new authorization drops all earlier tokens; refreshing with the current token ends an older grace token; the scope check runs before the first-use mark | the M1 client tests (#20, #22, #24, #25) | the owner's first real connect and sync (M1); the probe for the token behaviours |
| `blocked_by_provider_plan`: how the API signals an account without Withings+ (after 13 Oct 2026) is unknown; `provider_plan_blocked` returns false for every status (#159) | the sync engine's capability states | the probe / the owner's first real sync |
| The fake's readings used by #156, #160 and #161: refresh grace and lost-response behaviour, callback URL rules, `notify` list/update/revoke semantics, `revoke` for an unknown user, `getmeas` reports no deletions (findings §5 question 3), the 14-day re-pointing mark | M1 token, reconcile and disconnect code | the owner's first real connect and sync |
| The webhook form is exactly `userid`, `appli`, `startdate`, `enddate`; an extra field is refused (#158, stricter than the public docs). Entry-cache TTL 60 s is a constant | the receiver | the owner's first real webhook; loosen if real notifications are refused |
| ECG owner thresholds chosen in #162 where ADR 0006 is silent: OC3 mains line (±1 Hz power ≥ 10 dB over the side bands and ≥ 1 % of 5–15 Hz power), OC4 unsupported rate counts as not simultaneous, V3 prints no bpm | `ecg validate` | the owner's OC/V run |
| ECG A1: ECG integers are µV with no further scale factor (public spec) | #40's amplitude limits (ADR 0006 §7), the viewer | OC1 (#47): the owner compares R amplitudes with the Withings app |
| ECG A2: the ECG lead is unknown and shown as "lead unverified" | the viewer's label only | the probe (`wearposition`); OC2 |
| ECG A3: `sampling_frequency` is 300 or 500 Hz | #40 (other rates are stored, not analysed) | the probe (sampling rate) |
| ECG A4: the stored ECG signal may already be band-limited by the device | interval estimates (ADR 0006 §12) | OC3 |
| ECG A5: one ECG array per recording | analysis uses the first array; six-lead stays off | the probe (array count and lengths); OC4 if more than one |
| ECG A6: ECG strips last about 30 s | the gate's 20-s beat span (ADR 0006 §7) | the probe (array length and rate) |
| ECG A7: `getmeas` groups 135–138 and 130 belong to the ECG with the same device and timestamp | the strip link for the viewer and the §11 overlay | the probe (counts); OC5 |
| `attrib = 15` marks a guided reading, so N2 splits guided and routine readings by it (#222; `docs/dev/analysis-rules.md`) | N2:guided and N2:routine | the probe (attrib values seen) |
| Foot measure types 158/159 and 197/198 are the left/right pairs N5 compares (`nerve_foot_158_159`, `nerve_foot_197_198`), paired by `grpid` (#222) | rule N5 | the probe (meastypes and counts) |
| Withings sleep-summary field names (`SLEEP_FIELDS`, `sleep_hr_average`, `sleep_apnea_hypopnea_index`, `sleep_total_sleep_time`) are those of the public summary shape, and a summary carries `rmssd`, although the public shape lists no HRV field; stream units are stored as `unknown` (`e<unit>:unknown`) because the findings document none (#230, `docs/dev/derived.md`) | `SCORE_INPUTS` of ADR 0012 (Readiness, night heart rate, AHI), #188 | the owner's first real sync; wrong names show as skipped or `invalid` counts |
| ECG A7 (above) is also relied on by the ECG gate overlay of the findings read function (#228, `docs/dev/views.md`): a `grpid` shared by two connections links both groups | `read_findings` evidence and overlay | the probe (counts); OC5 |
| ECG A8: `heart list`'s `heart_rate` is the strip's mean rate | V3's comparison only | V3 (ADR 0006 §14) |
| ECG A9: Withings lists an ECG in `heart list` by the time its 135–138 group is in `getmeas` | ADR 0006 §11's notification hold | OC5's late-link count, once the M6/M7 hold records its end reasons (#88) |
| Read API (#233): a stream is `stale` after 48 h without a successful sync; `importing` versus `incomplete` is decided by a queued or running `withings_backfill` job (not scoped to the connection); `withings_plus_needed` shows when `connections.last_error` or a cursor's `last_error_category` is `blocked_by_provider_plan`, which nothing writes yet; ECG intervals 135–138 take their capability from the `ecg` stream; the metric detail returns one display band as of the window's end | #189 `views/*` (`docs/dev/views.md`) | the owner's first real sync; the Withings+ signal needs the probe or the owner |
| Rule evaluation (#234): the `getdevice` battery enum is `high`/`medium`/`low`; attrib 15 means a guided measurement; per-foot types 158/159/197/198 | rules Q1, N2, N5 | the probe (capability states, attrib values, meastypes) |
| M1 done-when flow (#291, `backend/tests/test_m1_flow.py`): the token lifecycle steps 1 to 3 as the fake models them; the status codes the wire helper assumes (`withingswire`); the answer to `revoke` for an unknown user; `enqueue(origin="scheduler")` for the connect, poll and reconcile kinds, backfill progress events and `backfill_state->>'status'` = `complete` (could not all be run); the webhook e2e asserts only receiver statuses, and its 503 case is weak; the rate limiter is bypassed in the test, so the real limiter path is not covered (#294) | the M1 done-when evidence, the e2e spec | the owner's first real connect, sync and webhook; #294 for the limiter |
| Metrics (#290, `docs/dev/metrics.md`): the definitions of the subscription states (`failed`, `pending`, `active` = a notification within 7 days, `missing`) and of completeness (`not_available`, `complete`, `backfilling`, `partial`) are derived, since nothing stores a subscription state; webhook ages are read from `webhook_events.received_at` and `enddate` from the worker's start time; a late-committed row with an earlier time is not counted | the `/metrics` gauges | the owner's first real deployment and Prometheus scrape (the listener is off by default) |
| QR and passkey UI (#293): ADR 0011 section 5 and section 8 amended in the PR (the QR nonce comes from the desktop's `qr/start` response, and the approve code goes in the approve body; recorded in `dev/auth-qrlogin.md`); the owner has not signed off the design of the phone approval screen or the desktop QR panel (no mock-up covers them); hybrid "Use a phone" is not exercised in CI | `QrLoginPanel`, `ApproveLoginPage`, `e2e/specs` | the owner's design sign-off; a real phone |
| Scores (#292, ADR 0012): the Health Improvement Score is always `not_set` (default: no band table registered, owner questions 1 to 3 open); sleep and activity field names and units (`total_sleep_time` seconds, moderate and intense seconds) are assumed; BMI is `no_height`; `afib_ecg_count` and `high_bp_ratio` are `input_not_wired` | the scores job and the dashboard score card | the owner's ADR 0012 answers; the probe (field names, units) |
| Onboarding and connections UI (#295): the contract `POST /api/connections/withings/start` returns `{authorize_url}` and the callback returns to `/onboarding?withings=<outcome>` is assumed and no route exists yet (#296); the design has gaps (Devices list, Sync now and Import more have no API, so Devices shows "Coming later"; the loading state has no design) | the connections screen | #296 builds the routes to the assumed contract; the owner's design answers; the owner's first real connect |

Added 2026-11-09 (after #335 to #341). All IMPLEMENTED with fakes and synthetic data only; nothing here is INTEGRATION_VERIFIED.

| Assumption | Relied on by | Settled by |
|---|---|---|
| Withings answers a bad or revoked refresh token with `status` 2553 (the fake's reading; the public spec is silent) | the refresh path and disconnect handling | the owner's first real token refresh failure, or the probe |
| The connect-scope fake (`scope` values and the authorize/callback flow of `fake-withings`) matches the real API | #300 connect routes, #296 | the owner's first real connect (M1) |
| `xmax = 0` (insert-versus-update detection in upserts) was checked on the local PostgreSQL only | the derived-data writers and report-update counts (#312) | CI on the production PostgreSQL major version, then the first real sync |
| #195 Withings assumptions for finding notifications (notify timing, the E4/E6 gate hold interplay with late ECG links) | #316 | the owner's first real webhook and ECG (OC5) |
| Whether Withings retries a webhook on a 5xx answer is unknown; the receiver answers uniformly (#321) and does not rely on retries | the webhook receiver, queue debounce (#318) | the owner's first real webhook failure, or Withings support |
| #206 consent-UI design: no design covers the AI consent and withdrawal screens beyond the data-minimised tool set; the wording and layout are the developer's | #320 and the later UI | the owner's design decision |
| #207 provider assumptions: the model API shape (messages, tool calls, token counts, errors) is the fake's reading of the public docs | #327 | the first AI-enabled run with the owner's key |
| #211 export UI states (step-up prompt, single-use link used or expired, empty export) are not in the mock-ups | #332 | the owner's design decision |
| #210 Settings report-row layout and the ECG "PDF for a doctor" button placement are not in the mock-ups | #335, #341 | the owner's design decision |
| ADR session binding: a download token is bound to the session cookie plus a fresh step-up, not strictly to one session id | #332, #335 | the Reviewer of the next auth-adjacent change, or an ADR amendment |
| FIT output stays off (ADR 0013 default); no FIT writer is reachable | #332 | the owner's ADR 0013 answer |

Added 2026-11-10 (after #343 to #373). All IMPLEMENTED with fakes and synthetic data only.

| Assumption | Relied on by | Settled by |
|---|---|---|
| A withdrawal during answer storage ends the AI job `failed`, not `cancelled` (the store's `NOWAIT` refuses and the runner records a failure) | #347 behaviour; #352 stays open | A change in `jobs/runner.py` (a cancelled outcome for a refused store) |
| The `ecg_recompute` job is claimed on the scheduler's lane (3), not the backfill lane (5): `queue_enqueue` maps only `withings_backfill` to lane 5, and mapping more kinds changes a security-critical SQL function (`docs/dev/ecg.md` known gaps, `docs/dev/derived.md`); the nightly reconciliation is the only ECG sweep trigger (no per-connection sweep, none at worker start); an ECG-only account (no `measures` revision row) gets no E1 to E6 until a `measures` row exists (`docs/dev/ecg.md`) | #299, #307, #367 | A Developer issue for the lane and the sweep; the probe for the ECG-only case |
| ECG rule E1 (afib) tightening is open (ADR 0006 section 11) | #299 | The owner's ECG answers; an ADR 0006 amendment |
| The limiter's M1 flow takes about 49 s, which is the budget the limiter spacing sets (roadmap Amendment 10, #373) | #291, #373 | Real timing from the owner's probe |
| #282 skipped item 1 (security-notice mail callers passed no locale; ADR 0015 section 7): done in #378 | #360, #372, #378 | — |
| #282 H1 N4 (enrolment wording of the TOTP step): **DELEGATED 2026-11, PROVISIONAL, re-check at the M8 human review**: the clearer message of #379 stands | #282, #379 | Re-check the wording at M8 |
| Deploy: `db-functions` and the worker image ship together (#366 changed `metrics_webhook_ages` to two arguments); the `withings_rate_claim` v2 change of #373 also ships via `db-functions` | The next deploy | The deploy runbook (owner runs it) |
| Lock order for AI stores: the job row first, then the consent row `FOR SHARE NOWAIT`; `ownvitals_app` has `lock_timeout` 5 s, which gives the same `55P03` | #347, #355 | Recorded in ADR 0014 section 3 (this PR) |

Known gaps of #233 and #234 (all in #165): blocked streams with stored data still return values (docs say they should not); each evaluation pass replays the owner's whole history (about 3 s CPU for two years of seven daily metrics); the Settings writer (M6) must queue a daily pass when `fixed_tz` changes; the "all imported data" UI copy has no screen yet; an import never normalised reports `current`, not `behind`.

Known gaps of #335 (doctor PDF; not blocking; the doctor audit kinds are now in the activity allowlist and doctor rows are covered by the cross-user purge, #344): tidy-audited events carry `export_kind` "data" for doctor rows; the ECG picker offers only the first page of recordings; `doctor_routes.py` imports private helpers from `export_routes.py` (to be made public or moved).

## Owner-side steps after #334 to #341

- The staging host needs the secret file `secrets/anthropic_api_key` (an empty file is fine while AI is off) before the next `up`, since #334 mounts it in the worker.
- Local checkouts need `scripts/worktree.sh init` again.
- Release note: stored scores read stale until the next scores pass runs (#338 changed the input revision digest).

M1 adds a row per Withings behaviour the code relies on but no real account has confirmed.

## QR login (#232): notes

Backend only (IMPLEMENTED, synthetic); the desktop panel, the phone screen and `qr-login.spec.ts` are #180. Detail and the deviations table: [dev/auth-qrlogin.md](dev/auth-qrlogin.md); the deviations are recorded as an amendment to [ADR 0011](adr/0011-passkeys-and-qr-login.md).

- A QR `complete` does not run the `ADMIN_EMAILS` grant (it would revoke the approving phone's session); a password, Google or passkey sign-in does. An admin on a QR-born session gets no admin role until a TOTP is verified in that session.
- Each web process holds a second permanent `LISTEN qr_logins` connection (a second `EventHub` beside the jobs hub), so two pooled connections are held for the life of the process.
- The deviations from ADR 0011 section 4 and following (pre-session rotated in place, `presession_id` replacing `presession_hash`, `approver_session_id` `SET NULL` ending the flow on rotation, the new audit kinds `qr_login_started` and `qr_login_refused`) are in the amendment at the end of the ADR.
- `geo_source=none` is the default; the place is display text only. The sessions list does not show it yet (#180).
- SWAG: the owner's location for `/api/auth/qr/` must not buffer (threat model O6).

## Decisions since the last reconcile

2026-11-09 (owner): the `ownvitals probe` output is confirmed and the Withings integration is verified, said in the session prompt. Agents do not write INTEGRATION_VERIFIED; the owner records it. The unverified-assumptions table below stays until the owner settles each row, so the code keeps its current readings.

2026-11-08 (owner, roadmap Amendment 7): groundwork before the M6 screens: G (`config.py` split, multilingual en/sv, ADR 0015) and H (auth fixes from classbeat after the pinned port: Google re-auth `auth_time`, reset race windows and row locks, sign-out events and security activity, passkey follow-ups). P1–P3 (Turnstile, session anomaly signal and abuse view, privacy, terms and cookie notice) are required before M8. Not adopted: language in the URL, plan quotas, the image-only install.

The dated rows are in [status/decisions-log.md](status/decisions-log.md) (2026-11-03): household rule is the switch alone until M8; a `grpid` repeated across connections links both; the Withings OAuth state lives in the web process; the AI caps come from settings; the mail outbox and AI ledger survive "delete imported data"; passkey attestation is `none` only; advisory-lock namespace 6 belongs to the normaliser.

## External dependencies

| Dependency | Pinned / state |
|---|---|
| classbeat platform source | `d80605100992f85f8f02672b9b5f885ed6166214` (ADR 0001) |
| Withings public API (`wbsapi.withings.net`) | documented behaviour in [withings-findings.md](withings-findings.md); unverified until the probe |
| GitHub-hosted runners | public repository, free minutes |

## Milestone acceptance evidence

Recorded here when a milestone's "done when" (PLAN §11) is checked: CI runs, test names, e2e
runs, and what remains unverified.

### M0 Platform (PLAN §11: CI green; e2e login + TOTP; guards pass; threat model updated)

Evidence from the checklist of PR #223 (#16), taken at `5efe73c` (#215). IMPLEMENTED on synthetic data; nothing here is verified against Withings, and no production or legal gate is passed. The checklist's boxes were left for the Architect: this section is the record, the owner's gates are unchanged.

- **CI green on `main`.** Run 41629133957 on `5efe73c`: `ci`, Checks, Backend and E2E passed; Frontend was skipped by the change filter. The last run with every job, Frontend included: run 41615509288 on #169 (#14), all green. A scheduled full run on `main` at `76ca57f`: run 41440781757, success.
- **e2e login + TOTP.** E2E job 125893991129 (on #215) and 125847924670 (on #169): `e2e/specs/auth-password.spec.ts` (sign-up, forced enrolment, sign-in with a code), `auth-google.spec.ts` (Google through fake-oidc, enrolment, code), `step-up.spec.ts`, `password-reset.spec.ts`; the same job runs `e2e/scripts/stack-checks.sh` (non-root, no secret in `docker inspect`) and the Trivy image scan.
- **Guards pass** (Backend job): guard 1 `test_rls_guard.py`; guard 2 `test_role_guard.py`, `test_roles_rerun.py`; guards 3 and 4 `test_isolation.py`; guard 5 `test_user_context_guard.py` and `test_user_context_*.py`; guard 6 `test_role_check*.py`; guard 7 `test_jobs_api.py`, `test_jobs_stream.py`, `test_auth_sessions.py`, `test_auth_oauth_link.py`; guard 8 `test_auth_tables_guard.py`; guard 9 `test_queue_guard.py`; guard 11 `test_maint_privileges.py`; the contexts guard `test_isolation_contexts_guard.py` and its six files; `test_session_guard.py`; `test_crypto_guard.py`; `test_egress_guard.py`, `test_egress_runtime.py`, `test_egress_hook.py`; `test_auth_mandatory_totp.py`; `test_netguard.py`; frontend `sources.test.ts` and `guards.test.tsx`.
- **Threat model updated** in #223 (`docs/security/threat-model.md`: the post-port review, the Enforced-by column naming merged tests, open items O7, O9, O13 and O14 closed). Gaps G1 to G6 are open and none blocks M0: G1 to G5 are in #165; G6 (production compose ports, T63) is the owner's under O6. The per-item PLAN §4.4 mapping is #30's.
- **Unverified:** everything is synthetic. The probe, the real-account sync and the deploy are the owner's.

### M1, M4, M5 and the M7 backend: IMPLEMENTED on synthetic data (checked 2026-11-11 against `origin/main` at `199074b`)

Taken from the merged issues and the test files in the repository; no CI run IDs are recorded here and nothing is verified against Withings, a real account or a provider. No production or legal gate is passed.

- **M1** (PLAN §11: the §4.4 checklist → IMPLEMENTED): the M1 issues listed in the table above are merged, and #291 added the done-when flow (`backend/tests/test_m1_flow.py`), failure injection and the RLS context tests. Not checked here: the per-item PLAN §4.4 mapping (open as #30), so the claim is "the M1 issues are merged with tests", not "every §4.4 item is mapped". INTEGRATION_VERIFIED waits for the owner's probe.
- **M4** (rules tested on synthetic irregular and sparse series): the rule, score, daily-value, pipeline and read-API issues listed in the table are merged, with synthetic series in `backend/tests/analysis/synthetic_readings.py` and the rule and score tests under `backend/tests/analysis/`. "Reproducible on your data" is the owner's.
- **M5** (ECG and nerve): the ECG core, rules, list and viewer, PDF and nerve rules are merged with synthetic signals (`backend/tests/ecg/`, `ecgsynth.py`, `test_ecg_pdf_guard.py`). "Validated on your history" and the review of the experimental tier are the owner's; the owner `validate` command (#162) exists for that.
- **M7 backend** (consent, privacy and prompt-injection evals, cap under concurrency): consent (`test_ai_consent*.py`), the evals (`backend/tests/ai_evals/`), the cost cap with `test_fifty_concurrent_reservations_admit_exactly_ten` and the global-cap test (`test_ai_cost_cap.py`), the chat assistant (#347), the digest (#355), the export (#332, #344) and the doctor PDF are merged. The "provider review" part of the done-when is **not** met: it is not recorded, so AI is off (ADR 0014, delegated and provisional above). The M7 UI parts are not claimed.
- **Not claimed:** M2, M3 (its Opus review and e2e were not re-checked here), M6 and M8.
