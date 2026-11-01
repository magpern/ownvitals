# Status detail: completed

Moved out of [docs/status.md](../status.md) so the index stays short; the text is unchanged apart from relative links. Read it only when the work needs it.

## Completed

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

