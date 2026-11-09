# Status detail: completed

Moved out of [docs/status.md](../status.md) so the index stays short; the text is unchanged apart from relative links. Read it only when the work needs it.

## Completed

- 2026-11-09: merged to `main` between `a47e1ef` and `b2e6558`, all IMPLEMENTED (synthetic only; verdicts are on the PRs; the non-blocking findings are #294, #296 and #301-#305): #285 (docs, reconcile and the #165 split), #286 (#257), #287 (#197, overview), #288 (#198, metric detail and day view), #289 (#203, settings), #290 (#167, worker `/metrics`), #291 (M1 done-when flow, failure injection, RLS contexts), #292 (#188, scores job), #293 (#180, passkey and QR approve-login UI), #295 (onboarding and connections screen), #297 (#192, ECG list and signal API, ECG PDF). Dependency PRs #274 to #276 and the docs PRs #271 and #273 (Amendments 8 and 9) merged the same day.
- 2026-11-08: groundwork G and H merged through the review gate, all IMPLEMENTED (synthetic only; `main` `a47e1ef`; the non-blocking findings are in the backlog of docs/status.md): #259 (G1 #245, `config.py` split), #260 (G2 #246, i18n runtime), #262 (H1 #247, Google re-auth), #261 (H2 #248, auth reset races), #263 (G4 #250, response codes), #264 (G3 #249, account locale), #265 (G5 #253, mail locale, `4bfa064`), #267 (H4 #252, passkey cross-device sessions, migration 0018, `31aec87`), #266 (H3 #251, auth activity and sign-out events, migration 0019, `c8ee21b`), #268 (G6 #254, Swedish translation, `4ffac1a`), #269 (G7 #255, locale guard, e2e, add-a-language checklist, `a47e1ef`).
- 2026-11-03 (later): merged to `main` between `0621653` and `7e4d464`, all IMPLEMENTED (synthetic only; verdict comments are on the PRs; the non-blocking findings go to #165): #183 (#215, v1 rule core, body and heart rules), #193 (#217, chart primitives and the data layer), #16 (#223, post-port threat-model review and M0 test mapping; docs), #181 (#216, analysis schema, migration `0011`), #36 (#221, fake-withings e2e service), #194 (#220, mail outbox and web sender, ADR 0007), #184 (#222, nerve-health and system rules N1-N3, N5, Q1), #218 (#225, "delete imported data" also removes derived rows), #205 (#224, per-user monthly AI cost cap under concurrency), #185 (#227, measures into observations with provenance), #190 (#228, the one findings read function, ECG gate overlay, findings API), #178 (#226, passkeys), #229 (#230, sleep and activity streams; scope ownership; serialised passes), #186 (#231, daily values into `metric_daily`), #179 (#232, QR approve-login backend, migration `0015`). Dependabot #176 (tzdata 2026.6) and #174 (docker group) merged; #175 (`@playwright/test` 1.64.1) is open. M0's done-when evidence is in `docs/status.md`.
- 2026-11-03: #14 (#169), #15 (#170), #163 (#171), #164 (#173), #28 (#166), #29 (#172) merged through the review gate by Claude Developers and Reviewers → IMPLEMENTED (synthetic only). #177 (#212), #182 (#213) and #204 (#214): ADR 0011, 0012, 0013 and 0014 merged as Proposed. The non-blocking findings are collected in #165.
- 2026-11-02: #13 (#151), #18 (#153), #21 (#154), #77 (#155), #22 (#156), #23 (#157), #24 (#158), #25 (#159), #26 (#160), #27 (#161), #47 (#162), #124 (#149), #126 (#150), #127 (#152) merged through the review gate by Cursor Grok 4.7 Developers and Reviewers → IMPLEMENTED (synthetic only). Their non-blocking findings are collected in #165.
- 2026-11-01: #146 purge and session-list future-proof bounds merged as #147 (Grok 4.7 PASS `686d656`) → IMPLEMENTED.
- 2026-11-01: #143 pending-session expiry merged as #145 (Grok 4.7 PASS `5527006`, merge check `8362769`) → IMPLEMENTED. Follow-up #146 is merged as #147.
- 2026-11-01: #89 role-check and guard 5 follow-ups merged as #115 (Grok 4.7 PASS `42126ca`, merge check `780bc9f`) → IMPLEMENTED. `__slots__` is allowed only as a class-body assignment in `admin/db.py`.
- 2026-11-01: #140 step-up window bounds merged as #142 (Grok 4.7 PASS `83164b7`, merge check `c751275`) → IMPLEMENTED. A future step-up or MFA time is outside the window. Follow-up #143: `pending_expired` still has no lower bound.
- 2026-11-01: #122 static egress guard merged as #123 (Grok 4.7 PASS `bab31a2`, merge review PASS `4206536`) → IMPLEMENTED. `egress.md` does not list the `fastapi.routing` allow; the guard reads the Python table.
- 2026-11-01: #12 admin gate, audit log and stored flags merged as #129 (Opus PASS `b6d69c3`, Grok 4.7 delta PASS `f631a9b` after the #132 merge; Architect integration review PASS) → IMPLEMENTED. Synthetic data only. Follow-ups are #140.
- 2026-11-01: #119 ECG follow-ups from the #114 review merged as #120 (Sonnet PASS `a0b7629`) → IMPLEMENTED.
- 2026-11-01: #40 ECG signal-processing core merged as #114 (Opus delta PASS `308e54b` after FAIL `b40a263`; Architect integration review PASS) → IMPLEMENTED; synthetic data only, nothing verified against Withings. Its follow-ups are #119.
- 2026-11-01: #116 ADR 0010 corrections and acceptance merged as #117 (Sonnet PASS `bcd3685`, merge check `11f4939`); ADR 0010 is Accepted.
- 2026-11-01: #84 forced-command deploy script merged as #100 (Opus delta PASS `fa43404` after FAIL `4373d2c`, merge check `690b6eb`) → IMPLEMENTED; not installed or used (installing it is the owner's step). Its follow-ups go to #101, the next docs PR and #118.
- 2026-11-01: #110 ADR 0010, the v1 finding rules, merged as #112 (Opus delta PASS `408863d` after FAIL `809b1a8`, merge check `822e343`), with roadmap Amendment 3.
- 2026-11-01: #57 release follow-ups merged as #111 (Opus PASS `03c19b2`, merge check `4ddb9d8`) → IMPLEMENTED; its follow-ups went to #101.
- 2026-11-01: #8 envelope encryption and the KEK ring merged as #82 (Opus delta PASS `0399575` after FAIL `5679e7d`, merge check `0b38853`) → IMPLEMENTED. Its follow-ups went to #89, #13 and #24.
- 2026-10-31: #95 calibration and test follow-ups merged as #109 (Sonnet PASS `c72672e`, merge check `b76c0ff`) → IMPLEMENTED; its nits wait for the next analysis touch.
- 2026-10-31: #42 CI dependency audits merged as #106 (Sonnet PASS `abc9a4b`, merge check `b5cd338`) → IMPLEMENTED.
- 2026-10-31: #107 status merged as #108 (Sonnet PASS `d44a7a1`, merge check `7c65068`); its drift findings are fixed here.
- 2026-10-31: #98 app shell, tokens, theme, fonts and design primitives merged as #105 (Sonnet PASS `0a6efb4`, merge check `02dcfce`) → IMPLEMENTED.
- 2026-10-31: #99 purity guard follow-ups merged as #102 (Sonnet PASS `83ad6b2`, merge check `09f3c4d`) → IMPLEMENTED; its two nits wait for the next analysis-guard change.
- 2026-10-31: #103 status merged as #104 (Sonnet PASS `8491382`); its drift findings are fixed here.
- 2026-10-31: #88 ADR follow-ups merged as #97 (Opus delta PASS `0155ddc` after FAILs `80a0606` and `cfb5039`, merge check `eec8f60`).
- 2026-10-31: #9 job queue, leases, fencing and the worker merged as #96 (Opus PASS `8fe99e3`, merge check `71ff8fb`) → IMPLEMENTED; its seams wait for #77.
- 2026-10-31: #76 purity guard and policy follow-ups merged as #94 (Sonnet PASS `99f64f1`, merge check `50adba8`) → IMPLEMENTED.
- 2026-10-31: #90 status merged as #93 (Sonnet delta PASS `9db9cce` after FAIL `2c2ccb5`).
- 2026-10-31: #68 autocorrelation-robust trend test and change points merged as #87 (Sonnet PASS `0a246b5`, merge check `b015be7`) → IMPLEMENTED; not wired into findings until M4.
- 2026-10-31: #91 design mock-ups aligned with the spec, merged as #92 (Sonnet PASS `05e6d57`).
- 2026-10-31: #75 role-check follow-ups merged as #81 (Opus PASS `db494a2`, merge check `5681e0e`) → IMPLEMENTED.
- 2026-10-31: #85 the chosen UI design and its mock-ups merged as #86 (Sonnet PASS `d01ff96`, merge check `2bb7dc4`); the review's spec findings go into #91.
- 2026-10-31: #78 ADR follow-ups merged as #83 (Opus PASS `6095563`, merge check `59154ea`).
- 2026-10-31: #39 ADR 0006, the ECG subsystem spec, merged as #46 (Opus PASS `01d0fa3` after six FAILs, the last `222579e`).
- 2026-10-31: #79 status merged as #80 (Sonnet PASS `3848623` after FAIL `aac3440`).
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
- 2026-10-31: specification Revision 3 frozen at `635901d` ([roadmap.md](../roadmap.md)).
- 2026-10-31: #3 frontend skeleton merged as #34 (Opus PASS `0e41e7c`, merge check `fc8641f`) → IMPLEMENTED.
- 2026-10-31: #32 bootstrap review follow-ups merged as #33; Dependabot #31 merged.
- 2026-10-31: #1 bootstrap merged as #17 (Opus PASS `31fc85c`). Rulesets set: `main` (PR, `ci` + `pr-title` strict, squash only, linear history, no force push or deletion, 0 approvals) and `release tags` (`v*` protected); squash-only merges, branches deleted on merge; secret scanning and push protection on.

