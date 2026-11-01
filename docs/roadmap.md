# Roadmap: the frozen specification

**Revision 3 is frozen at commit `635901dd167c0c3fd2dbb6ccdc504386ad7b69f3` (2026-10-31).** These
three files, as they are at that commit, are the authoritative specification:

| File | Holds |
|---|---|
| [PLAN.md](PLAN.md) | Decisions D1–D11, architecture, classbeat reuse, the Withings integration (§4), data model (§5), identity (§6), analysis (§7), MVP UI (§8), milestones M0–M8 (§11), risks (§12) |
| [PLAN-operations.md](PLAN-operations.md) | Environments and hosting (§9.1–9.2), legal (§9.3), Withings terms (§9.4), agentic development: roles, merge gate, verification, status levels, GitHub (§10) |
| [withings-findings.md](withings-findings.md) | Reverse-engineering evidence and the public API facts the integration relies on |

The files are never edited: the `checks` CI job fails if any differs from the frozen commit.
[AGENTS.md](../AGENTS.md) says how agents work; where it and the specification differ, the
specification wins.

## Amendment rule

The architecture is not redesigned without a demonstrated technical blocker. An amendment:

- is added below, newest first, dated, numbered, with the section it changes, the change, and
  the **evidence** (a failing test, provider behaviour, a library limit) or the owner's decision;
- has an ADR when it changes a technical decision;
- comes from the Architect (technical blockers only) or the owner (product, scope, legal, gates).

Routine choices the specification leaves open are not amendments: they go into ADRs.

## Amendments

### Amendment 5 (2026-11-01, owner): Cursor security roles use Grok 4.7 high

- **Changes:** Amendment 4's Cursor security-critical Developer, production security
  decisions and ADRs, security-critical Reviewer, and a review after a second FAIL, use
  Grok 4.7 high. The model id is `grok-4.7-high`. The security roles stay separate from the
  routine Developer, Explorer and Reviewer. Codex stays Astra, Sol and Luna. Claude Code
  stays the Opus and Sonnet names in AGENTS.md.
- **Evidence:** the owner, 2026-11-01: Opus 5.5 cannot be used in Cursor. The pin
  `claude-opus-5-5[effort=high]` was not selectable, and a client substitution is not
  authority to take the role. The owner assigns Grok 4.7 high instead.
- The security classification list, fresh independent reviewer, current-head PASS, green
  `ci` and `pr-title`, and the Architect integration review stay mandatory. A Cursor
  security verdict is `Agent review (Grok 4.7): PASS <sha>` or `FAIL`. An unavailable
  model, or any model other than Grok 4.7 high, still blocks that Cursor role.
- Past Opus verdicts remain valid for the SHA they name. No application model route is
  enabled. The frozen specification files stay unchanged.

### Amendment 4 (2026-11-01, owner): Codex and Cursor development alternatives

- **Changes:** PLAN-operations §10.1–10.2's development model names and attribution gain
  optional tool profiles. Codex: Astra/high Architect and security-critical Developer/Reviewer,
  Sol/high routine Developer/Reviewer, Luna/medium Explorer. Cursor: Grok 4.7/high/standard
  Architect and routine Reviewer, Composer 2.5/standard routine Developer/Explorer, Opus 5.5/high
  security-critical Developer/Reviewer and production security decisions/ADRs.
- **Evidence:** the owner requested equivalent Codex and economical Cursor support after
  Classbeat's alternatives (ownvitals issue #130). This is a tooling decision, not a product,
  architecture, verification-status or data-sharing change.
- All shared security classifications, frozen-spec guard, file locks, isolated worktrees,
  independent current-head PASS, CI and Architect integration review remain mandatory.
  After a second FAIL use Astra (Codex) or Opus (Cursor). Both profiles keep the security
  Reviewer for all security-critical deltas, including tests/docs-only changes.
- Verdict comments name the actual model. Codex/Cursor PRs use their own attribution footer
  as documented in [dev/agent-tools.md](dev/agent-tools.md); no Co-Authored-By trailer.
  Unavailable/substituted models block that role, never authorize silent downgrades.
- The no-real-health-data rule, owner-only integration/production/legal gates, health-claims
  requirements and all external-action permissions remain unchanged. No application model
  route/provider is enabled and no real account is accessed by this change.
- Configuration: `.codex/` and `.cursor/`; onboarding: [dev/agent-tools.md](dev/agent-tools.md).
  Existing Claude Code definitions and the frozen three specification files stay unchanged.

### Amendment 3 (2026-11-01, Architect): `findings.tier` gains `system`

- **Changes:** PLAN §5's `findings.tier` (`device|trend|experimental`) gains a fourth value,
  `system`, used by rule Q1 only (no weigh-in, low battery, connection needs reconnect, import
  incomplete). Q1 findings are about the data and the connection, never about the body; every
  other rule keeps its PLAN §7.2 tier.
- **Evidence:** a conflict inside the frozen spec. PLAN §7.2 gives Q1 the tier "system", and
  PLAN §5's enumeration has no such value, so the `findings` schema can't hold Q1 as specified.
  The owner-accepted design already has a System tier (`docs/design/README.md`). Raised by the
  first pass of ADR 0010 (#110).
- **ADR:** ADR 0010 records the decision and Q1's rule; the M4 wiring issue adds the value to
  the schema.

### Amendment 2 (2026-10-31, Architect): keys of partitioned owned tables

- **Changes:** PLAN §5 ("Every owned table has `owner_id`, `UNIQUE (owner_id, id)` and
  owner-inclusive composite foreign keys") is refined for the monthly-partitioned tables
  (`observations`, `sleep_series`, `intraday`): their key is
  `PRIMARY KEY (owner_id, id, <partition key>)`; they must never be the target of a foreign key
  (a guard enforces it); their own foreign keys stay owner-inclusive
  (e.g. `(owner_id, connection_id) → connections(owner_id, id)`). Every other owned table keeps
  `UNIQUE (owner_id, id)`.
- **Evidence:** PostgreSQL requires every unique constraint and primary key on a partitioned
  table to include all partition-key columns, so `UNIQUE (owner_id, id)` cannot exist there. The
  rule's purpose, owner-inclusive foreign keys pointing at a row, is kept: the partitioned tables
  are leaf tables nothing references. Raised by the review of ADR 0002 (PR #41).
- **ADR:** ADR 0002 records the key rule and the guard 1 extension (#21).

### Amendment 1 (2026-10-31, Architect): the authorization-code exchange runs in the web process

- **Changes:** PLAN §2 ("The worker owns every outbound call") gains one Withings exception: the
  web process makes the single authorization-code exchange inside the OAuth callback request,
  `POST https://wbsapi.withings.net/v2/oauth2` with `action=requesttoken` and
  `grant_type=authorization_code`, through a narrow, web-only egress entry limited to exactly that
  request. Token refresh (`grant_type=refresh_token`), revoke and every other Withings call stay
  in the worker. The call counts against the app's rate budget like any other.
- **Web's outbound connections** are then exactly: the Google sign-in hosts and the configured
  SMTP server, which the classbeat port already places in web (#6, ADR 0003; sign-in and email
  flows run in the request), plus this one Withings request. Nothing else; #6 and #22 test
  both processes' lists.
- **Evidence:** a conflict inside the frozen spec. §4.3 requires the code to be exchanged "in
  the request (it's valid for 30 s)", and withings-findings §2 says the callback "must exchange
  it immediately (server-side, not via a queued job)". The callback request runs in web.
  Raised in ADR 0004 (PR #41, issue #4).
- **ADR:** ADR 0004 records the decision; #6 (egress) and #22 (connect) implement and test it.

## Pinned sources

- classbeat, the platform this ports from: `magpern/classbeat` at
  `d80605100992f85f8f02672b9b5f885ed6166214` (recorded again in ADR 0001).
