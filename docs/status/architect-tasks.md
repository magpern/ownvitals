# Status detail: Architect tasks

Moved out of [docs/status.md](../status.md) so the index stays short; the text is unchanged apart from relative links. Read it only when the work needs it.

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

