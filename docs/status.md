# Status

Kept by the Architect and updated on every merge and every orchestration cycle. The
specification is [roadmap.md](roadmap.md); GitHub milestones and issues hold the detail. Status
levels (D10): agents set **IMPLEMENTED** only; **INTEGRATION_VERIFIED** and
**PRODUCTION_READY** are the owner's.

## Milestones

| # | Milestone | State | Level |
|---|---|---|---|
| M0 | Platform | in progress: #2 (#35), #3 (#34), #4 (#41), #5 (#65), #7 (#53), #8 (#82), #9 (#96), #42 (#106), #45 (#48), #49 (#58), #52 (#64), #57 (#111), #72 (#73), #75 (#81) and #78 (#83) merged; #6 (#55, ADR 0009 runtime egress enforcement), #101 (#118), #10 (#125), #11 (#132), #12 (#129, admin gate, audit log and stored flags), #122 (#123, static egress guard), #140 (#142, step-up window bounds), #89 (#115, guard 5 follow-ups), #143 (#145, pending-session expiry) and #146 (#147, purge and session-list bounds) merged; ready: #13, #14, #18, #77, #124, #126 and #127 | — |
| M1 | Withings core | #19 (#63), #66 (#71) and #20 (#128, the API client, error taxonomy and response models) merged; backlog #18, #21–#30, #36 (dependency graph in [status/dependency-graphs.md](status/dependency-graphs.md)) | — |
| M2 | Home deploy (early) | #84 (#100, the forced-command deploy script, pulled forward) merged: IMPLEMENTED, not installed (installing it is the owner's); #101 (#118, the deploy-side follow-up) merged; the rest are mostly owner actions (see "Blocked on the owner") | — |
| M3 | Identity additions | not started | — |
| M4 | Analysis v1 | pulled forward: #37 (#54), #38 (#67), #56 (#74), #68 (#87), #76 (#94), #99 (#102) and #95 (#109) merged; ADR 0010, the v1 finding rules (#110, #112; accepted in #116, #117) and roadmap Amendment 3 merged | — |
| M5 | ECG & nerve health | ADR 0006 (#46) and its follow-ups (#88, #97) merged; #40 (#114, the signal-processing core, with its ADR 0006 amendments) and its follow-ups #119 (#120) merged; #47 waits for #21 and #25 | — |
| M6 | MVP UI | the UI design is chosen ([docs/design/](design/README.md), #85) and aligned with the spec (#91); #98 (#105: shell, tokens, theme, fonts and primitives; pulled forward) merged | — |
| M7 | AI & reports | not started | — |
| M8 | Beyond-household gate | owner-gated | — |

## Active

| Issue | What | Developer | PR | State |
|---|---|---|---|---|
| #13 | Connections layer, rewrap and keys CLI | Grok 4.7 | — | ready; starting. Holds `connections/**`, `keys_cli.py`, `cli.py`, migration `0007`, `connections.md`, the connections install in `web.py`, and `auth/owners.py` `connection_refs` |
| #14 | E2E harness: Playwright, fake-oidc, auth flows and image scan | Composer | — | ready; starting. Holds `e2e/**`, `compose.e2e.yaml`, `Dockerfile`, `ci.yml`, `scripts/ci-gate.sh`, `scripts/test-ci-gate.sh`, `dependabot.yml`, `docs/dev/e2e.md` |
| #124 | Egress hook exemptions and psycopg entries after #118 | Grok 4.7 | — | ready; starting. Holds `docs/dev/deploy.md`, `docs/dev/egress.md`, ADR 0009 and the egress guard tests. Depends on #118, #123 and #115, all merged |
| #18 | Jobs for Withings: system and webhook kinds, lanes, singletons, retry budget, dead-letter | Grok 4.7 | — | ready. Waits: `cli.py` is held by #13, and `worker/**` will be #77's |
| #77 | Wire the KEK ring, egress allowlist and queue checks | Grok 4.7 | — | ready. Waits: `web.py` is held by #13 for the connections install |
| #126 | Wire the maint role, its password and the admin URL | Grok 4.7 | — | ready (#12 merged). Waits: `Dockerfile` and the CI files are held by #14 |
| #127 | Admin users view and account helpers | Grok 4.7 | — | ready (#11 and #12 merged). Waits: `web.py` is held by #13 |

Merged since the last reconcile: #89 (#115) is on `main` at `e434f49`, #143 (#145) at `bff8eb4` and #146 (#147) at `4e10259`, all IMPLEMENTED. `main` is `4e10259`.
On GitHub #15, #16, #21–#30, #36, #47, and no longer #13, #14, #18, #77, #124, #126 or #127, carry `blocked-deps`. #124, #126 and #127 are ready but waiting on file locks. At most five Developers work on issues at
once; a fix round on an open PR may run while five are busy, and no new issue starts until fewer
than five are.

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
| `compose.yaml`, `AGENTS.md`, `deploy/`, `scripts/ci-changes.sh`, `scripts/test-release.sh`, `backend/pyproject.toml`, `backend/uv.lock` | — (#118, #55 and #134 released them). #14 holds `ci.yml`, `Dockerfile`, `scripts/ci-gate.sh` and `scripts/test-ci-gate.sh` |
| `backend/src/ownvitals/worker/__main__.py`, `backend/src/ownvitals/__main__.py`, `backend/src/ownvitals/mail.py`, `backend/src/ownvitals/egress/**` except `egress/hook.py`, `docs/dev/stack.md`, `docs/dev/jobs.md`, `docs/dev/release.md` | — (#55 and #118 released them). #13 holds `cli.py`. #124 holds `docs/dev/deploy.md` |
| `backend/src/ownvitals/db/engine.py`, `db/role_check.py`, `egress/hook.py`, `backend/tests/user_context_*.py`, `backend/tests/conftest.py`, `docs/dev/backend.md`, `docs/dev/isolation.md`, `docs/dev/isolation-guard5.md`, `docs/dev/crypto.md`, `docs/adr/0002-tenant-isolation.md` | — (#115 released them). #77 is next for `conftest.py`, `isolation.md` and `role_check.py` once `web.py` is free |
| `backend/tests/egress_*.py`, `backend/tests/test_egress_*.py` except `test_egress_hook.py`, `docs/dev/egress.md`, `docs/adr/0009-runtime-egress-enforcement.md`, `docs/dev/deploy.md` | #124 |
| `backend/src/ownvitals/connections/**`, `backend/src/ownvitals/keys_cli.py`, `backend/src/ownvitals/cli.py`, `backend/src/ownvitals/web.py`, `backend/src/ownvitals/auth/owners.py`, `docs/dev/connections.md`, migration `0007` | #13 |
| `e2e/**`, `compose.e2e.yaml`, `Dockerfile`, `.github/workflows/ci.yml`, `scripts/ci-gate.sh`, `scripts/test-ci-gate.sh`, `.github/dependabot.yml`, `docs/dev/e2e.md` | #14 |
| `docs/status.md`, `docs/status/completed.md`, `docs/status/decisions-log.md` | the status reconcile after #147 |
| `backend/src/ownvitals/admin/**`, `backend/src/ownvitals/flags/**`, `db/tables.py`, `backend/tests/role_guard.py`, `session_scan.py`, `auth_tables_scan.py`, `admintools.py`, `backend/migrations/versions/0006_admin.py`, `backend/tests/test_migrations.py`, `test_admin_*.py`, `test_flags*.py`, `test_maint_privileges.py`, `test_session_guard.py`, `docker/postgres/init/10-roles.sql`, `docker/postgres/maint/30-maint.sql`, `docs/dev/admin.md` | — (#129 released them). #13 holds `web.py` for the connections install |
| `docs/roadmap.md`, `.cursor/**`, `docs/dev/agent-tools.md`, `docs/adr/README.md` | — (#139 released them) |
| `.codex/**` | — (#131 released it) |
| `backend/src/ownvitals/db/queue_check.py` | — (#77 next) |
| `backend/src/ownvitals/config.py`, `backend/tests/dbtools.py`, `test_web.py`, `rls_guard.py`, `test_config_milestone.py` | — (#8, #82 and #11 released them) |
| `scripts/ci-uv-version.sh`, `.trivyignore` | — (#106 released them). #14 holds `scripts/ci-gate.sh` |
| `frontend/**`, `docs/dev/frontend.md` | — (#98 and #105 released them) |

Alembic migrations: a PR takes the next free revision number when it opens; if another
migration merges first, it renumbers and re-chains on its update, which needs a fresh scoped
review.

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
| Threat model O15: confirm or reject the risks the threat model proposes as accepted | Before the M2 real-data gate | Review the "Accepted risks" section of `docs/security/threat-model.md` (merged with #41; risks 20 and 21 are new; #49, #72, #78 and #88 extend 21, including the ways code with the app role could stall a rotation) |
| ECG (ADR 0006, merged in #46): whether our ECG estimates may be shown to anyone but the owner; whether public research ECG datasets may ever be used | M5 | Decide when M5 reaches them |
| Receiving mail through Proton Mail Bridge (IMAP, or a `mail-worker` that posts to a REST API): production sending through Bridge on the owner's own domain is decided, receiving may come later | Not in the frozen spec; not before M2 mail works | A product decision; then the Architect writes a roadmap amendment and a threat-model entry for the new inbound path |
| ADR 0010 open items: the C2 score-card band, the HIS table, Readiness and Body Path | M4 wiring, M6 | Decide when the wiring issue or the screen reaches them |

## Assumptions (unverified; each needs the probe or the owner)

| Assumption | Relied on by | Settled by |
|---|---|---|
| Withings' time-zone fields are IANA zone names (PLAN §5 `source_tz`) | #37 day assignment | the owner's first real sync (M1); the probe's shape-only scope (PLAN-operations §10.4) doesn't cover it |
| `attrib = 1` means "device, ambiguous (could be another household member)" (findings §2), so #37 excludes it as unconfirmed | #37 quality gate | the probe (attrib values seen) |
| The API hosts are `wbsapi.withings.net` and `account.withings.com`, and the token endpoint is `POST /v2/oauth2` with `action=requesttoken` (findings §2) | #6 egress lists and code exchange | the owner's first real connect (M1) |
| The fake's readings where the public spec is silent (the module docstrings of `backend/tests/withingscore.py`, `withingswire.py`, `withingsnotify.py`, and the #63 PR body for the full list): status 2553 for a bad refresh token and 214 for a missing scope; the OAuth scope each data endpoint needs (`DATA_ROUTES`, now in `withingsstore.py`); the signed string of `revoke` and its answer for an unknown user | #19's fake, so every M1 client test (#20, #22, #24) | the owner's first real connect and sync (M1) |
| The fake's stricter readings from #66 (#71; the docstrings of `backend/tests/withingsrules.py`, `withingsnotify.py`, `withingscore.py`, `withingswire.py` and the #71 PR body): a non-POST API call answers 2554 and a secret in the query string 503 (no documented status); webhook success means a 2xx within 3 s (the short reading of "a few seconds"); callback URLs must be HTTPS on port 443 with a domain name, else 293 (the stricter reading); the rate limit is a token bucket of 120 a minute per application, burst 120, and the 601 carries no `wait_seconds`; a new authorization drops all earlier tokens; refreshing with the current token ends an older grace token; the scope check runs before the first-use mark | the M1 client tests (#20, #22, #24, #25) | the owner's first real connect and sync (M1); the probe for the token behaviours |
| ECG A1: ECG integers are µV with no further scale factor (public spec) | #40's amplitude limits (ADR 0006 §7), the viewer | OC1 (#47): the owner compares R amplitudes with the Withings app |
| ECG A2: the ECG lead is unknown and shown as "lead unverified" | the viewer's label only | the probe (`wearposition`); OC2 |
| ECG A3: `sampling_frequency` is 300 or 500 Hz | #40 (other rates are stored, not analysed) | the probe (sampling rate) |
| ECG A4: the stored ECG signal may already be band-limited by the device | interval estimates (ADR 0006 §12) | OC3 |
| ECG A5: one ECG array per recording | analysis uses the first array; six-lead stays off | the probe (array count and lengths); OC4 if more than one |
| ECG A6: ECG strips last about 30 s | the gate's 20-s beat span (ADR 0006 §7) | the probe (array length and rate) |
| ECG A7: `getmeas` groups 135–138 and 130 belong to the ECG with the same device and timestamp | the strip link for the viewer and the §11 overlay | the probe (counts); OC5 |
| ECG A8: `heart list`'s `heart_rate` is the strip's mean rate | V3's comparison only | V3 (ADR 0006 §14) |
| ECG A9: Withings lists an ECG in `heart list` by the time its 135–138 group is in `getmeas` | ADR 0006 §11's notification hold | OC5's late-link count, once the M6/M7 hold records its end reasons (#88) |

M1 adds a row per Withings behaviour the code relies on but no real account has confirmed.

## External dependencies

| Dependency | Pinned / state |
|---|---|
| classbeat platform source | `d80605100992f85f8f02672b9b5f885ed6166214` (ADR 0001) |
| Withings public API (`wbsapi.withings.net`) | documented behaviour in [withings-findings.md](withings-findings.md); unverified until the probe |
| GitHub-hosted runners | public repository, free minutes |

## Milestone acceptance evidence

Recorded here when a milestone's "done when" (PLAN §11) is checked: CI runs, test names, e2e
runs, and what remains unverified.
