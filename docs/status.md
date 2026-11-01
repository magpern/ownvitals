# Status

Kept by the Architect and updated on every merge and every orchestration cycle. The
specification is [roadmap.md](roadmap.md); GitHub milestones and issues hold the detail. Status
levels (D10): agents set **IMPLEMENTED** only; **INTEGRATION_VERIFIED** and
**PRODUCTION_READY** are the owner's.

## Milestones

| # | Milestone | State | Level |
|---|---|---|---|
| M0 | Platform | in progress: #2 (#35), #3 (#34), #4 (#41), #5 (#65), #7 (#53), #8 (#82), #9 (#96), #42 (#106), #45 (#48), #49 (#58), #52 (#64), #57 (#111), #72 (#73), #75 (#81) and #78 (#83) merged; #6 (#55, ADR 0009 runtime egress enforcement) has five Opus FAILs, the latest on `d957d45`, and a fix round pushed as `61d7c30`; #89 (#115) has five, the latest on `bee21cf`, and #101 (#118) one on `818a0df`, each with the Architect's fix list posted; #77 and #10 wait for #6 | — |
| M1 | Withings core | #19 (#63) and #66 (#71) merged; backlog #18, #20–#30, #36 (dependency graph below) | — |
| M2 | Home deploy (early) | #84 (#100, the forced-command deploy script, pulled forward) merged: IMPLEMENTED, not installed (installing it is the owner's); #101 (#118) is the deploy-side follow-up; the rest are mostly owner actions (see "Blocked on the owner") | — |
| M3 | Identity additions | not started | — |
| M4 | Analysis v1 | pulled forward: #37 (#54), #38 (#67), #56 (#74), #68 (#87), #76 (#94), #99 (#102) and #95 (#109) merged; ADR 0010, the v1 finding rules (#110, #112; accepted in #116, #117) and roadmap Amendment 3 merged | — |
| M5 | ECG & nerve health | ADR 0006 (#46) and its follow-ups (#88, #97) merged; #40 (#114, the signal-processing core, with its ADR 0006 amendments) and its follow-ups #119 (#120) merged; #47 waits for #21 and #25 | — |
| M6 | MVP UI | the UI design is chosen ([docs/design/](design/README.md), #85) and aligned with the spec (#91); #98 (#105: shell, tokens, theme, fonts and primitives; pulled forward) merged | — |
| M7 | AI & reports | not started | — |
| M8 | Beyond-household gate | owner-gated | — |

## Active

| Issue | What | Developer | PR | State |
|---|---|---|---|---|
| #6 | Egress allowlist and mail (ADR 0009, runtime egress enforcement) | Opus | #55 | five Opus FAILs, the latest on `d957d45` (`_posixsubprocess.fork_exec` raises no audit event); the Architect's fix list (the static refusal of `_posixsubprocess`, `multiprocessing` and `ProcessPoolExecutor`, `pyyaml` in the dev group, and non-blocking items) is posted. The head is `61d7c30` (fix commits and a merge of `main`) and has no verdict; its `Backend` job failed when this update was written (not investigated); a fresh Opus delta review follows once CI is green |
| #89 | Role-check and guard 5 follow-ups from the #81 and #82 reviews | Opus | #115 | five Opus FAILs, the latest on `bee21cf` (the head, CI green; a string starting with `*` still reaches `get_type_hints`); the Architect's fix list (a fail-closed type grammar, one blocking item and five non-blocking) is posted; the next review is a fresh Opus delta review |
| #101 | `db-functions` follow-ups from the #96 review (functions installed from the release image) | Opus | #118 | Opus FAIL `818a0df` (the CI filter must run the guards on Dockerfile and compose changes; compose read as YAML); the head `4ab9f85` (CI green) has no verdict yet; a fresh Opus delta review follows |
| — | Status reconcile to 2026-11-01 (this update) | Developer (Sonnet) | this PR | in review |

Ready, waiting for a free slot: none. #77 and #10 wait for #6; #11, #12 and #13 follow #10. At
most five Developers work on issues at once; a fix round on an open PR may run while five are
busy, and no new issue starts until fewer than five are.

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

## Decisions log

| Date | Decision | Where |
|---|---|---|
| 2026-11-01 | Owner: Google-branded sign-in button. Only the Google button of the `Auth` mock-up changes, to follow Google's branding guidelines: the "G" logo and an approved label, light and dark variants, a self-hosted SVG from Google's branding download (CSP stays `'self'`, nothing loads from Google until the click), a test for logo, label and local asset, and one line in the `Auth` row of `docs/design/README.md` | #10 |
| 2026-11-01 | Owner: #113 (a sign-in look-and-feel reference with Google first, no Apple, large pill buttons) is withdrawn and closed as not planned; #10 builds from the existing `Auth` mock-up, as before | #113, #10 |
| 2026-10-31 | Owner: in production, mail goes through the owner's Proton Mail Bridge ([magpern/proton-bridge](https://github.com/magpern/proton-bridge), `ghcr.io/magpern/proton-bridge`) on the owner's own domain. Bridge is a container on a private Docker network (SMTP on port 2025, STARTTLS, self-signed certificate), and it is the one SMTP host in web's and the CLI's resolvable set (ADR 0009). The mail module never turns TLS verification off; the deploy issue supplies Bridge's certificate as a pinned CA file or fingerprint setting. The Bridge SMTP password is a secret file under `secrets/`, never an environment value in the compose file. No change to #55's scope. Receiving mail is an open owner question (see "Blocked on the owner") | #6 |
| 2026-11-01 | Guard 5 threat model (#89, **#115 is open**; written down after the second FAIL on the same class of finding, as for #55; five Opus FAILs so far): the scan stops plausible and review-visible ways of forging a `UserContext` (mistakes, shortcuts, copied snippets). It is not a sandbox against adversarial code in the process; code review of every PR, RLS (the database enforces `owner_id` whatever Python builds) and the role checks cover that. `isolation.md` says so in a "Threat model of guard 5" paragraph, and ADR 0002 §1's AST-guard bullet gets one sentence. A reviewer who finds a further reflection-class route to the token records it as non-blocking, for "Known limits", unless it is a plain import or a call that review could miss. Annotation allow-list grammar (the root-cause decision after the fourth FAIL, `c196da5`; it fails closed): one recursive checker covers every annotation position (arguments, returns, variables, `TypeAlias` values, PEP 695 aliases and bounds). A type expression may hold only names and dotted names (no `_` part, no token names), subscripts and tuples of type expressions, `X | Y`, constants, the constant arguments of `Literal[...]` (never parsed) and strings, which are parsed and checked recursively with the module's own import table, so an aliased `UserContext`, `vars(ownvitals.context)` or `dataclasses.replace` inside a string is refused as it is outside one. Anything else is refused: a call, a lambda, a comprehension, a walrus, `await`, `yield`. `Annotated` metadata (everything after the first element) is the only place calls are allowed and is scanned as ordinary code. After the fifth FAIL (`bee21cf`): a string in a type position is accepted only if it parses and the grammar accepts it; a string starting with `*` is parsed the way 3.12's `ForwardRef` does (drop the `*`, check the rest), so `"*Ts"` passes and `"*[getattr(...)]"` is refused; a string that doesn't parse is refused; the string rules (the `ownvitals.context` key, the token names) run before the string is parsed; `TypeAlias` counts under any alias imported from `typing` or `typing_extensions`; a plain `S = "..."` string alias used as a type, and reaching `eval` without its name (`getattr(builtins, …)`, `sys.modules`, `importlib`, `__import__`, `timeit`, `code`, `runpy`), are Known limits under the threat model; private library types in annotations are refused. `eval`, `exec` and `compile` are refused as names, as imports from `builtins` and through `builtins.` in `backend/src`. The bound: a route through string evaluation outside annotations, other than those three, is reflection and goes to Known limits. Also: a plain `import ownvitals.context` is allowed (it binds only the package, and every non-annotation use of `ownvitals.context` is refused) while every import that binds the module object is refused; star imports from `ownvitals` and its subpackages are refused; `__kwdefaults__` and `__defaults__` are refused names; a missing database (SQLSTATE 3D000) is a refused login; the server must use English messages (`lc_messages` `en_US` or `C`) for refusals to be classified, and a deferral on an unrecognised `FATAL:` answer logs one warning with the SQLSTATE class and no message text | #89 (#115), ADR 0002, `docs/dev/isolation.md` |
| 2026-11-01 | `db-functions` (#101, **#118 is open**): the one-shot runs the release's app image, which carries the SQL and the installer, so nothing is read from a host path; the deploy runs it on the release digest, so a deploy installs that release's functions and a rollback re-installs the previous release's. The only host SQL mount allowed is `db`'s init directory, matched by its exact source and target; it runs once on an empty data directory and holds no application functions. The superuser password stays in a `PGPASSFILE` on a tmpfs (#9). From the Opus FAIL `818a0df`: CI sets `backend=true` for a change to `Dockerfile`, `.dockerignore`, `compose*.yaml`, `deploy/**` or `docker/**`, so the guards run on the PRs they guard; the host-SQL check reads compose as YAML (short and long volume syntax); the runner refuses every `PG*` variable except `PGPASSFILE` and a script with its own transaction control; the deploy checks with `docker compose config` that `db-functions` has no volume but its tmpfs. #101 also carries a `python3 -I` test for `release.yml`'s version check, a test that the Dockerfile keeps its `@sha256:` pins, the pre-release spelling sentence in `release.md`, shellcheck of `deploy/ownvitals-deploy`, and the `deploy.md` notes on sudo-rs 0.2.2 (a global `env_keep` fallback) and on recovering from a failed rollback | #101 (#118), #100, #111 |
| 2026-11-01 | Egress, #55 decisions (**#55 is open**): the hook refuses the process-creation routes that raise audit events (`subprocess.Popen`, `os.system`, `os.exec*`, `os.posix_spawn*`, `os.fork`/`os.forkpty`, `_winapi.CreateProcess`) in all three process kinds, and refuses the `socket.__new__` event for any family but AF_INET, AF_INET6 and AF_UNIX. `_posixsubprocess.fork_exec` raises no audit event and refusing its import would break `subprocess`, so that control is static: the guard refuses `_posixsubprocess`, `_winapi`, `multiprocessing` and its submodules, and `ProcessPoolExecutor` in `backend/src`, under any alias, and ADR 0009's limits name `fork_exec`, the `multiprocessing` `spawn` and `forkserver` methods and `ctypes`. Web stays one uvicorn process (no `workers > 1`, no `--reload` outside development; #77 tests the production settings). A task or `to_thread` started inside an egress scope keeps it, documented as a limit. `pyyaml` joins the dev dependency group for #118's compose tests (today it reaches the lock only through `uvicorn[standard]`, which #55 drops) | #6 (#55), #77, #101 (#118) |
| 2026-11-01 | ADR 0009 (runtime egress enforcement; **to be written in #55's PR**, Accepted when it merges), amended before implementation after a Linux probe: web runs on the standard asyncio loop (`server_config` sets `loop="asyncio"`, pinned by a test), because under uvloop asyncio connections and lookups raise no socket audit events; uvloop leaves the lock (uvicorn without `[standard]`, any extra named, and a test that `uvloop` is neither in `uv.lock` nor importable); `install_egress_hook` raises if `uvloop` is importable; web, the worker and the CLI refuse to run on a non-stdlib loop (a startup check; web's call is #77's); `socket.getnameinfo` is refused like `gethostbyaddr`; the mail session context is set inside `_send_blocking`, in the thread that connects; the probe (standard loop and uvloop: what fires, in which thread) is recorded in the ADR and a Linux-only loopback subprocess test, and a switch to another event loop must re-run it first; the 58 findings from #96's worker and jobs code are classified in the static guard. Web's install moved to #77 because #82 held `web.py` while #55 was written | #6 (#55), #77 |
| 2026-10-31 | Egress, root-cause change after three FAILs on B1 (#6, **#55 is open**): a static scan can't prove a library never reaches the network (lazy fetchers such as numpy's `DataSource`), so a PEP 578 audit hook in every process (web, worker, CLI) is the authoritative control. It resolves only the process kind's hosts (its egress hosts, the database host, and SMTP in web and the CLI), refuses every reverse lookup (the mailer passes a fixed `local_hostname`), and allows AF_INET and AF_INET6 connects only inside an egress-transport or mail context. The static closed-world guard stays the early warning for direct imports. Out of the hook's reach: C extensions with their own sockets (libpq, by design) and `ctypes` | #6 (#55), #77 |
| 2026-11-01 | ADR 0010 (v1 finding rules, #110, merged as #112, accepted in #117): two bands from the same median and scaled MAD over 90 days: the display band at k 1.5 (as the design computes it) and the finding threshold at k 2.5 (Leys et al. 2013), preceding and evaluated at an episode's first day (E6, N2, B1's ECW), so a finding value always lies outside the displayed band; the metric's BASELINE policy gates both, `min_scale` is one display step, and `DEFAULT_K` isn't used by findings or the display. C1 trend: on each of at least 3 consecutive days, value at least median + 10 and above the display high; resting HR is type 11, #37's daily value, a standing pulse and not a clinical resting HR; the experimental < 50 or > 100 bpm rule also needs 3 consecutive days. C2 v1 is "rising" only; B1 v1 is the 3-day arm only; N4 is not in v1 until a primary source is verified; B3 stays experimental (owner-only flag) with the manufacturer's 0.40 cut named as device-specific; the monthly summary is a view; N5 uses the symmetric index on two pairs; confidence and severity as proposed (device and system findings stored as high, severity constant per rule). One active finding per (rule, subject), updated while the rule fires and ended when it stops; the wiring issue stores episodes. Trend-test rules have a persistence rule, because daily re-testing gave 60–65 % of no-change years a false "rising" episode: an episode opens only after the condition holds for L = 21 consecutive daily evaluations (14 gave 15 % false years at φ 0.6) and ends after 7 failing days; B2 has 0 % false episodes within 60 days by definition. Open items are under "Blocked on the owner" | #110 (#112), #116 (#117) |
| 2026-11-01 | Roadmap Amendment 3: `findings.tier` gains `system`, used by rule Q1 only (no weigh-in, low battery, connection needs reconnect, import incomplete). Evidence: PLAN §5's enum and §7.2 contradict each other, so the schema couldn't hold Q1. ADR 0010 is the amendment's ADR; the M4 wiring issue adds the value to the schema | roadmap, ADR 0010 |
| 2026-11-01 | ECG, Architect decisions on #40 and #114 (amendments file): baseline wander below 50 bpm may pass or fail the gate (34/40 and 38/40 strips pass at 300 and 500 Hz; every failure is the clipping rule), and at 50 bpm and above it stays at least 95 %; the clipping rule is unchanged, and a run entered and left by a step is the candidate for a later SQI version if §14 V2 shows low-rate clipping failures on real strips. The distorted-beat segment is the QRS, R ± 100 ms, against the window's median beat, with `DISTORTED_R = 0.85` (0.10 below the clean minimum, derived from the clean and ectopic rows only); a distorted beat within 5 s of the verified span's start or end removes every beat before or after it, the span is recomputed, and fewer than 20 s left fails the gate as `too_short`. New rule `missed_beat`: on a strip that isn't AF-like, an RR of at least 1.75 × the median of its 8 neighbouring intervals leaves both HR and NN and counts toward the excluded fraction (a major bump of `hrv` and the pipeline). A strip whose other exclusions exceed 20 % (AF-like) gets HR null with reason `irregular_rhythm`, so no unchecked ownvitals HR is shown (Withings' own HR stays the device value); this withdraws the Architect's earlier acceptance of the AF-like per-strip floor on the claim that its HR was within tolerance, which had not been measured, and the 94.5 % floor at 500 Hz stays a detection metric only. A 135 bpm ectopic strip with a true interval within ± 5 ms of 272.7 ms is left out of the HR assertion and reported in `docs/dev/ecg.md`. An ECG suite of about 96 s in CI is accepted (revisit if the backend job passes about 5 minutes) | #40 (#114), #119 (#120) |
| 2026-11-01 | Envelope encryption (#8, merged as #82): web and the worker refuse to start when the KEK check can't run, including on an unmigrated database; the seed and pepper ids are settings (as the Architect recorded when #8 was scheduled; not re-checked against the code here). Follow-ups: `crypto.md` wording and a refused-login test go to #89, the `0004` downgrade lock to #13, and a distinct retired webhook-seed type to #24 | #8 (#82), #89, #13, #24 |
| 2026-10-31 | M6 shell (#98, merged as #105): the reviewer's screenshots stay outside the repository; `isAdmin` lives in `auth/access.ts`; the theme flash on a cold load is accepted (the Settings issue decides on a second pinned same-origin script); the Settings tabs are `/settings/profile`, `/settings/security`, `/settings/notifications` and `/settings/data`, and `/settings` shows the first (the IA line in `docs/design/README.md`); Admin on a phone is in the account menu; the freshness line, the range selector, the rail's status marks and "This device since" wait for their data; `build.test.ts` allows `.woff` and `.woff2` under `assets/`; `index.html` asks for `viewport-fit=cover` and both frames pad by the safe-area insets. From the review: the sticky rail is accepted; the lock button's `aria-haspopup` goes to #11; the per-selector inset in `viewport.test` and an `optionalDependencies` licence row go to the next M6 frontend issue | #98 (#105), #11 |
| 2026-10-31 | ECG (#40, merged as #114; Architect decisions on five assertions the specified algorithms can't meet, carried by ADR 0006 amendments in [the amendments file](adr/0006-ecg-subsystem-amendments.md)): the generator scales each beat's event amplitudes by ω_k/ω̄, so R amplitude doesn't follow RR; the premature interval is max(60–70 % of the sinus interval, 250 ms); downstream beats are confirmed beats (a Pan–Tompkins detection matched by Elgendi within ± 50 ms; unmatched ones are dropped and their intervals deleted as `unconfirmed_beat`); the 20 % rule holds except within ± 0.5 % of its limit. White noise at 12 dB (revised after re-measurement: 0/15 strips at 300 Hz, 9/15 at 500 Hz): below 50 bpm it may pass or fail, at 50 bpm and above the gate passes at ≥ 95 %, and clean strips at 40–49 bpm pass at ≥ 95 %; the PR reports gate pass by heart-rate band, and the amendment records the known limitation (such strips yield no ownvitals values and hide E4/E6 when they are the trigger; §14 V2 measures it). HRV on accepted motion-burst strips: a confirmed beat whose Pearson r with its window's median beat is below a threshold taken from the clean strips (below their 0.5th percentile) is distorted, and its intervals leave the NN intervals as `distorted_beat` (HR keeps its definition; `hrv` `ALGO_VERSION` bumps). An accepted strip's floors and HR/HRV tolerances are never loosened: it meets them or its HRV is null | #40 |
| 2026-11-01 | Deploy (#84, merged as #100; IMPLEMENTED, not installed): `deploy` refuses a SemVer lower than the recorded current tag, and the same version with another digest; `rollback` (one step, through every check) is the only way back; pre-release tags are refused, as in `release.yml`; the script lives in `deploy/`; the state is recorded after migrations and before `up -d`. From the Opus FAIL `4373d2c`: the override sets `scale: 0` on `migrate`, so `up -d` and a rollback run no migration (a deploy migrates only through `run --rm migrate`); the highest deployed release is recorded, and a deploy needs a SemVer at least as high with a tag commit equal to or descending from that release's; the deploy refuses unless the tag ruleset reports `current_user_can_bypass == "never"` (and fails closed if the field is missing) | #84 (#100) |
| 2026-10-31 | Owner notices (#97): the owner-notice write takes only a `kind`; its route comes from a fixed per-kind mapping inside the definer function, and the notifications issue keeps at most one pending row per kind (bounds the backlog, not the send rate). Owned outbox rows still hold a route key from the fixed list | ADR 0007 (#97) |
| 2026-10-31 | Job queue (#96): classbeat's `expire` step is dropped (it served approvals), so the reaper is the only maintenance step; the advisory-lock registry in `queue_locks.py` is tested against ADR 0005 §9's table; until #77, guard 2 can't see the queue, web doesn't refuse a drifted queue, and the worker loads neither the allowlist nor the key ring, each seam failing closed and pinned by a test | #9 (#96), #77 |
| 2026-10-31 | Purity guard (#94): `ownvitals.config`, `admin`, `flags`, `cli`, `log`, `headers`, `auth`, `context` and `connections`, `sqlalchemy`, `psycopg` and `requests` are forbidden in `ownvitals.analysis` and `ownvitals.ecg`. There, `patch`, `run_module`, `run_path` and `resolve_name` are refused as names, `exec`, `eval` and `compile` as bare names (or on `builtins` or a computed base), and all nine with `import_module` and `__import__` as exact strings; `re.compile`, and `import_module` or `__import__` called directly with a clean literal, are allowed (#99 added the planted tests) | #76 (#94), #99 (#102) |
| 2026-10-31 | Analysis methods (#87): Theil–Sen with an AR(1) variance correction for the trend test (`trend/2`) and PELT with a long-run noise scale from a robust variogram (`changepoint/2`). The power lost on short series is accepted. At φ 0.6 and fewer than 20 points the false-trend rate is about 6 % (5.8–6.4 % on 42,000 series per cell), not 5 %, so the M4 wiring issue treats such trends as low-confidence; #95 restates the documented rates. `shift_sigmas` is in long-run noise units, and φ above 0.7 reads as level changes | #68 (#87), #95 |
| 2026-10-31 | ECG, Architect decisions on the #46 review (ADR 0006 as amended by #88, merged in #97): the noisy RMSSD tolerance at 60–99 bpm widens from ± 6.5 to ± 7.0 ms (the 20 % margin rule, applied as for clean 60–99 bpm); every listed recording gets an `estimated`, and one stored with `signal` NULL gets `signal_missing` (no gate result), so a hold on it ends at once; A9 stays open until the M6/M7 hold records its end reasons, and OC5 reports its late-link count as not applicable until then; the M4 wiring carries the groups the value came from (the morning group, or the readings the median used), not the day's fingerprint inputs | #88 (#97) |
| 2026-10-31 | `transaction_timeout` (#83): a 5-minute role default on `ownvitals_app` (#77) and `ownvitals_maint` (#12). It bounds role-default transactions only: a session can set it to 0, and a session-level lock 4 outlives it. It ends the whole connection, not just the transaction (ADR 0002 §5 as amended by #88) | ADR 0002 §5, #77, #12, #88 (#97) |
| 2026-10-31 | Role check (#81): it judges the login role (`session_user`), not `current_user`; a login that Postgres refuses (SQLSTATE class 28 or 42501) will fail closed with `DatabaseRoleError` instead of reading as "database unreachable" | #75, #89 |
| 2026-10-31 | Compose (#9, merged in #96): the one-shot `db-functions` holds the superuser's password, read into a `PGPASSFILE` on a tmpfs at run time (never an environment variable, argument or URL), because it owns the `SECURITY DEFINER` functions and runs the functions script on every deploy; web and the worker wait for it with `service_completed_successfully` | #9 (Architect comment of 2026-10-31) |
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
| 2026-10-31 | ECG (ADR 0006, merged in #46; the root-cause change after three FAILs): findings E1–E6 are stored from Withings values only; our SQI gate is a **read-time overlay**, never stored in a finding: one function reads the linked recording's current `estimated` and hides E4/E6 only on an explicit failure ("not evaluated by ownvitals" otherwise), never hides E1–E3 and shows our gate on them only behind the owner flag; every consumer (dashboard, API, notifications, export, doctor PDF, AI context) goes through it. E4/E6 notifications are held up to 24 h (a setting) for the gate and never recalled. The E5 index is out of v1; E4 with unknown sex uses only the QTc > 500 ms band; SQI components, HR, HRV and markers only behind the owner flag; writing `estimated` bumps no `data_revisions` and re-evaluates nothing | ADR 0006 (#46), #25 |
| 2026-10-31 | ECG, Architect decisions for #46's fix rounds 4–6 (carried by ADR 0006 as merged): an `estimated` whose fingerprint or gate-module versions (`input`, `filters`, `rpeaks`, `sqi`) differ from the running ones counts as absent; the link is resolved at read time and a soft-deleted recording is no link; a finding's **trigger is the provenance of the value that made it fire** (the source groups the value-producing function used: a morning daily value's one group, a median day's groups), and baseline recordings never count; E4/E6 are hidden when at least half of the trigger recordings **with a gate result** failed explicitly, recordings without a result are left out, and none with a result means "not evaluated by ownvitals"; a group that is both trigger and baseline counts as a trigger; HR and HRV only from up-to-date estimates in every output; a finding with no linkable recording ends its hold at once ("no linkable recording" needs an `ecg` sync that completed successfully after the trigger was stored); the JSON/CSV export carries every finding with its overlay state, the doctor PDF and AI context leave hidden ones out; no reason code is a display label; the hold decides once and restarts on a new revision | #46 (rounds 4–6) |
| 2026-10-31 | From the #35 review: prod also refuses a DSN `password`/`passfile` query parameter; the test-DSN check refuses an empty host and `service=` | #5 |
| 2026-10-31 | Egress (#6, **#55 is open**; the ADR 0009 rows above supersede the static-guard-only reading): `ownvitals.egress` is a package; the web-only code exchange is its own module; the Withings and model clients refuse to build or send unless the worker allowlist is installed; SMTP has a 20 s total deadline (moves to a setting in #10); web installs a deny-all urllib opener in #10; the guard denies network-capable imports outside egress and mail (from the #55 review) | #6, #10, #23 |
| 2026-10-31 | Release trust boundary: the `v*` tag ruleset stops moving and deleting tags, not creating them; the deploy is the boundary (tag commit in `refs/heads/main`, attestation with cert identity, `--source-ref`, `--source-digest`). No tag `creation` rule: it adds nothing while the owner's token is the only pusher | AGENTS.md, #57, T62 (#49) |
| 2026-10-31 | Webhook-secret rotation (ADRs 0004 and 0008, #58 and #73): **compromise** retires every entry at once, then appends, re-points and deletes, and cancels a routine rotation's queued `notify update` jobs; **routine** appends, waits for `active_from`, re-points, retires and deletes. `withings_apps.rotation_reason` is `routine` or `compromise`, and the CLI never downgrades a pending compromise (one conditional `UPDATE`). The function compares and clears the request under its row lock, refuses a routine step while a compromise is pending, refuses a stale request on retire-all and the clearing append, runs at READ COMMITTED and serialises on advisory lock 4; its `unretired_entries` and `seed_reused` rules guard an honest worker, not code with the app role (risk 21). Its role has `USAGE` on `public` and the column grants it needs, no client-secret or token column. Guard 11 checks with `has_any_column_privilege` and `has_table_privilege` that `ownvitals_app` and `ownvitals_maint` can't write `withings_apps`. A web cache that is stale or not yet loaded answers `POST` with 503 and `HEAD` with 200. `notify list` clean-up: #22 on (re)connect, #26 after the 14-day deadline, #27 on disconnect | ADR 0004, 0008, #21, #22, #24–#27 |
| 2026-10-31 | Advisory-lock registry (ADR 0005 §9): namespace 1 job creation, 2 token refresh, 3 partitions, 4 webhook-secret rotation | ADR 0005 |
| 2026-10-31 | Partitions (ADR 0002, 0005; #58, #73): floor 2009-01-01, ceiling now + 3 months, created lazily for the months actually written; every writer of a partitioned table calls `ensure_month_partitions` in its own short transaction before inserting; a row outside the range is not written but counted and recorded as skipped (out of range), never as missing | ADR 0002, 0005, #21, #25 |
| 2026-10-31 | Mail (ADR 0003 §6, ADR 0007): SMTP never from the worker. Mail that originates in the worker goes to an outbox (one owned table, one system table): a row holds a fixed `kind`, the recipient id (or a system row for an owner notice) and a fixed route, never an address or free text; web resolves the address and renders a fixed template, reading and marking rows only through definer functions, and the app role has no grants on the system table. The outbox is built in M6/M7; in M1, PLAN §4.3's owner notice is in-app only | ADR 0003, 0007, #22 |
| 2026-10-31 | Tenant isolation (#65): runtime roles may be members of no role, directly or indirectly, whatever the `INHERIT` or `SET` option, and are refused superuser, `BYPASSRLS` and `REPLICATION` on every reachable role; the production database URL takes an allowlist of query parameters; any `app.*` default is refused at every level; guard 5 also refuses `__class__` assignment and explicit `__post_init__`/`__init__` calls | #5 (#65), #75 |
| 2026-10-31 | Analysis span convention (#74): a span is elapsed days (`(last − first).days`), `span_fraction` is elapsed days over `window_days` (so at most (w−1)/w); gaps and `window_days` stay inclusive day counts; the nerve baseline needs 28 days, so it is never sufficient while N1 isn't | #56 (#74) |
| 2026-10-31 | Analysis methods (#67): the Hamed–Rao modified Mann–Kendall lowers but doesn't remove false trends on autocorrelated data, and PELT finds spurious change points on it; #68 must fix both before trend or change-point findings go live | #38 (#67), #68 |
| 2026-10-31 | Guard 5 (ADR 0002 §1 "anywhere"): `object.__setattr__(self, …)` is allowed only inside a frozen dataclass's `__post_init__` on the bare `self`; every other form stays refused (it can't build a `UserContext`) | #5 (PR #65 body, `docs/dev/isolation.md`) |
| 2026-10-31 | Analysis core (#37): plausibility ranges are labelled engineering bounds unless findings cite one; a metric without a range is refused; values are `Decimal`, floats refused | #37 |
| 2026-10-31 | Dependabot: TypeScript 7 ignored until typescript-eslint supports it (Dependabot closed #59 after `@dependabot ignore typescript major version`); Dependabot titles over the 100-character header are shortened through group names (#57) or retitled by the Architect before merge | #59, #57 |
| 2026-10-31 | Frontend licence closure (from the #64 review; #52 item 4 reverted): an optional peer dependency is never skipped as dev-only. npm marks such a package `devOptional`, not `dev`, and a GPL package that Vite ships would hide behind the skip | #52, #64 |

## Architect tasks

- Split M6 into issues by screen group from `docs/design/README.md` ("Building it"): shell and
  tokens are done (#98); the rest follow. Point the UI parts of #10, #11 and #12 at the `Auth`,
  `Security` and `Admin` mock-ups. The next M6 frontend issue carries the `viewport.test`
  per-selector inset and the `optionalDependencies` licence-closure row (a mutation survived in
  the #64 review; the same gap is on `main`).
- The M6/M7 notifications issue (to create): at most one pending outbox row per owner-notice kind
  (#83 finding 1) on the `kind`-only write with its per-kind route mapping, tested by its signature
  and mapping (#97, N8), and the hold records how it ended as a reason code (ADR 0006 A9, OC5).
- The next change to the analysis purity guard carries the #102 review's nits: `ownvitals.worker`
  on `FORBIDDEN` (the dynamic check catches it today through `db`), and the docs' "refused as an
  identifier" wording (a name that is defined and never used isn't flagged). The next analysis
  touch carries the #109 nits: the margin wording, the suite time, SE rounding, and
  `methods/__init__.py:6`, which still names Hamed–Rao as the correction (the default is AR(1)).
- After #100 (merged): align threat model T62 ("an optional pre-release") with the deploy
  script's refusal, note `deploy/` in ADR 0001's layout, and fix the ADR 0007 wording N6 and N7
  from the #97 review. #101 shellchecks `deploy/ownvitals-deploy` in the `checks` job.
- The M4 wiring issue (to create) reconciles `metric_daily` (morning, mean, min, max, n; PLAN §5)
  with #37's `DailyValue` (morning or median, min, max, MAD), keys duplicates of segmental types
  173–175 on `(grpid, position)`, adds the `system` tier to the `findings` schema (Amendment 3),
  stores ADR 0010's episodes, treats a trend on under 20 points with φ above 0.5 as
  low-confidence (#87), labels `shift_sigmas` as long-run noise units or reports the shift in
  metric units, carries a daily value's source groups as provenance (ADR 0006's trigger rule,
  #46), and uses `from_daily`, not `from_instants`, which counts UTC days.
- Proton Mail Bridge (owner decision of 2026-10-31): a setting for Bridge's pinned certificate (a
  CA file or fingerprint), supplied by the M2 deploy or mail issue if #6 doesn't have it.
- TypeScript 7: re-enable when typescript-eslint supports it (`@dependabot unignore` or a manual
  bump issue). Check the Dependabot tab for the cooldown and the group `major` (from #111).
- Dependabot PRs go through the same gate with a fresh Sonnet reviewer.
- After #55 merges: move or confirm the decisions it carries (the egress API names for #9, #10,
  #22 and #29 and the closed-world network classification), and confirm the ADR 0009 row in
  `docs/adr/README.md`.
- `ci-uv-version.sh` may change for a digest-pinned uv; its cases are in the uv section of
  `scripts/test-ci-gate.sh`. Other docs that mention `uv@0.13.6` (`analysis.md`,
  `analysis-methods.md`, `stack.md`) aren't checked by the script (a note only).
- #14, when it creates `e2e/`, makes its `npm audit` required, so that deleting `e2e/` later fails
  CI instead of skipping the audit (from the #106 review).

## Shared-file locks

No two open PRs change the same file. Files many issues need are locked here by the Architect;
an issue that needs a locked file waits for the holder's merge, then updates its branch.

| File | Held by |
|---|---|
| `.github/workflows/ci.yml`, `compose.yaml`, `Dockerfile`, `AGENTS.md`, `deploy/` | #101 (#118) |
| `scripts/ci-changes.sh`, `scripts/test-ci-gate.sh`, `scripts/test-release.sh` | #101 (#118) |
| `backend/tests/test_compose_roles.py`, `docs/dev/stack.md`, `docs/dev/deploy.md`, `docs/dev/jobs.md`, `docs/dev/release.md` | #101 (#118) |
| `backend/pyproject.toml`, `backend/uv.lock` | #6 (#55: uvloop out, `pyyaml` into the dev group) |
| `backend/src/ownvitals/cli.py`, `backend/src/ownvitals/worker/__main__.py`, `backend/src/ownvitals/__main__.py` | #6 (#55: ADR 0009's install calls, `loop="asyncio"`) |
| `backend/src/ownvitals/egress/**`, `backend/src/ownvitals/mail.py`, `docs/dev/egress.md`, `docs/adr/README.md` | #6 (#55) |
| `backend/tests/conftest.py`, `docs/dev/backend.md` | #6 (#55) and #89 (#115), in different hunks; the second to merge updates from `main` |
| `backend/src/ownvitals/db/engine.py`, `backend/src/ownvitals/db/role_check.py`, `backend/tests/user_context_scan.py`, `backend/tests/user_context_rules.py`, `backend/tests/user_context_types.py`, `docs/dev/isolation.md`, `docs/dev/crypto.md`, `docs/adr/0002-tenant-isolation.md` | #89 (#115), with `test_role_check*.py`, `test_user_context_*.py` |
| `backend/src/ownvitals/web.py` | — (free until #77) |
| `backend/src/ownvitals/db/queue_check.py` | — (#77 next) |
| `backend/src/ownvitals/config.py`, `db/tables.py`, `backend/tests/dbtools.py`, `test_web.py`, `role_guard.py`, `session_scan.py`, `rls_guard.py`, `test_migrations.py`, `test_config_milestone.py`, `docker/postgres/init/10-roles.sql` | — (#8 and #82 released them) |
| `scripts/ci-uv-version.sh`, `.trivyignore`, `scripts/ci-gate.sh` | — (#106 released them) |
| `frontend/**`, `docs/dev/frontend.md` | — (#98 and #105 released them) |

Alembic migrations: a PR takes the next free revision number when it opens; if another
migration merges first, it renumbers and re-chains on its update, which needs a fresh scoped
review.

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
