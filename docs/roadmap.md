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

### Amendment 7 (2026-11-08, owner): groundwork and hardening carried over from classbeat

- **Changes:** PLAN §11 gains two milestones that run **before the M6 screens**: **G
  Groundwork** (multilingual support, plus the `config.py` split that the new settings need)
  and **H Hardening from classbeat** (auth fixes found in classbeat after the pinned port). It
  also lists the **P** items that must be done before M8 opens registration beyond the
  household. PLAN §8 gains "every page is shown in the user's language (English and Swedish)".
  M6 does not start a screen issue until G2 and G3 are merged. From then on, every M6, M7 and
  M3-UI issue builds its text through the catalogue, and the guard in G2 enforces it.
- **Evidence:** the owner's decision of 2026-11-08 ("plan what needs to be planned … ground work
  first"), after a scan of the classbeat issues closed since the pinned source
  (`d806051`). For H, the evidence is a defect: classbeat fixed bugs in code that ownvitals ported
  unchanged. For example, `auth/oauth_outcomes.py` refuses a Google re-authentication whose ID
  token has no fresh `auth_time` (lines 131–135). classbeat #522 showed that Google sends no such
  claim, so the refusal happens every time. The frontend is still small (about 3,400 lines,
  sign-in and security pages only), so translation costs least now, before M6 adds about ten
  screens.
- **ADR:** [ADR 0015](adr/0015-multilingual.md) (multilingual; Proposed with this amendment). An
  H issue that changes a recorded decision amends ADR 0003 or ADR 0011 in its own PR. P1 brings
  its own ADR, and a roadmap amendment for the web egress list (Amendment 1's list gains the
  Turnstile verification host only then).

**G Groundwork** (done when: English and Swedish switch everywhere except the admin area. The
guards refuse hard-coded text, a missing key and a half-added language. Mail goes out in the
recipient's language. `docs/dev/i18n.md` has the add-a-language checklist.)

| Id | Scope | Tier | Ports (classbeat) | Depends on |
|---|---|---|---|---|
| G1 (#245) | Split the **non-secret** `Settings` groups out of `config.py` (553 lines) into modules. The secret-file loading, the `app_secret_key` validator, `connection_kek_id` and the `passkey_satisfies_second_factor` refusal stay in `config.py`, unchanged. No behaviour change; one move commit, checked with `git diff -M`. Releases the `config.py` lock sooner for everyone. | security-adjacent (moves code next to key handling: Opus first review) | #764, #803, #812 | — |
| G2 (#246) | Frontend runtime: in-house typed catalogue `frontend/src/i18n/`, split by area (`en/<area>.ts` is the source, `sv/<area>.ts` `satisfies` its shape, so parallel issues own separate files), `t()` with named placeholders, `plural()` on `Intl.PluralRules`, a `Trans` helper (no markup in strings). Also `format.ts` on `Intl` (numbers, dates and durations; days and reading times follow the account's day setting), the locale manifest, `<html lang>`, a picker on the signed-out pages, and every existing page moved to the catalogue. Guards: hard-coded string guard with a shrink-only allowlist, catalogue key and placeholder parity. | ordinary | #663, #665, #667 | — |
| G3 (#249) | Language preference: `users.locale` (migration, next free number), `PUT /api/account/locale`, returned with the session; a bounded `Accept-Language` parse; a new account stores the language in effect at sign-up (email and Google paths, including the OAuth flow row); backend reads the same manifest. | security-critical (auth flows, OAuth state) | #664 | G1, G2, H2 |
| G4 (#250) | Codes, not sentences: every English sentence the backend sends to the page (errors, job and sync states, view `reason` strings, notices) becomes a code plus parameters that the frontend words, with the English text as the fallback for an unknown code. Findings already carry structured `evidence`. The rule texts are worded in the frontend from `rule_id` and evidence. | security-adjacent | #666, #699 | G2, H1 |
| G5 (#253) | Mail in the recipient's language. The outbox row is **unchanged** (ADR 0007 §1, T59). Web's sender looks up the recipient's locale through `ownvitals.auth` at send time, as it does for the address; owner notices are in English. `notify/templates` and the auth mails gain one fixed table per locale, golden files per kind and locale, and `Content-Language`. Month names come from the catalogue, never `setlocale`. | security-critical (the sender's lookup through guard 8, T59) | #671 (layout of #652) | G3 |
| G6 (#254) | Swedish catalogues for the app and mail: a Sonnet translator, an independent Opus-class Reviewer (ADR 0015 §8). The health wording (findings, tiers, disclaimers, score text) is listed in `docs/dev/i18n.md` for the owner's medical-claims review at M8. | ordinary (translation PR) | #672 | G2, G4, G5 |
| G7 (#255) | Done-when suite: guards so a new language cannot be half-wired, the add-a-language checklist, e2e that switches language on sign-in and Settings. | ordinary | #673, #756, #761 | G6 |

**H Hardening from classbeat** (all security-critical, so an Opus Developer and an Opus first
Reviewer). Each issue first reproduces the classbeat defect in ownvitals with a failing test. If
the defect cannot be reproduced, the issue closes with that test kept as a regression guard.

| Id | Scope | Ports (classbeat) | Depends on |
|---|---|---|---|
| H1 (#247) | Google re-authentication: decide how freshness is proven without `auth_time` (fail closed; never weaken step-up's 10 minutes). Log one WARNING line for every refused OAuth callback (provider, purpose, outcome code, never a token, code, state or email). Backend only: the page gets a reason code, and G4 words it ("use your password or an authenticator code instead"). Includes 2FA activation behind a Google re-auth. | #522 (PR #563), #687 | — |
| H2 (#248) | Race windows: a password sign-in racing a reset, and an identity linked during a reset, must not outlive it. Take the user-row lock (`FOR UPDATE`) before log-out-everywhere and the MFA reset revoke sessions, and in admin disable and delete. Same lock order everywhere, with race tests. `auth/sessions.py` (529 lines) is split on touch. | #639 (PR #642), #643, #675 | — (coordinate files with H1) |
| H3 (#251) | Sign-out recorded in `auth_events`. A user-facing security activity API (sign-ins, sign-outs, method changes, other sessions signed out) with an event when another session is signed out; its screen is the Security tab of #203. | #646, #678 | H2, G3 |
| H4 (#252) | Passkeys: a shorter session after a cross-device (hybrid) passkey sign-in (an ADR 0011 amendment). Apply classbeat's passkey hardening list (#795, #797) where it fits. #180 gains classbeat's e2e with a virtual authenticator (#778). | #776, #778, #795 | — (H4's UI part is in #180) |

**P Before M8** (registration beyond the household). Built after M6, never enabled without the
owner:

| Id | Scope | Tier | Ports (classbeat) |
|---|---|---|---|
| P1 | Cloudflare Turnstile on sign-up, sign-in and reset, off by default. Backend verification first, then the widget, then the CSP and privacy text. The install check refuses "on" without keys. Own ADR and an egress amendment. | security-critical (egress, auth) | #721, #724, #726, #798, #805 |
| P2 | Session anomaly signal, security log and an admin abuse view (scripted use of a stolen session). | security-critical | #577, #632, #750 |
| P3 | Privacy and terms pages and a cookie notice in English and Swedish. The texts are drafts for the owner's §9.3 legal checklist; strictly necessary cookies only until P1 adds Turnstile's. | ordinary | #715, #718, #727 |

**M6 additions:** #195 and #203 also take email preferences and a signed unsubscribe link
(classbeat #655, security-adjacent).

**Considered and not adopted:**
- language in the URL (`/en`, `/sv`): ownvitals is a private signed-in app with no search
  engine need;
- plan quotas and prices: D11 keeps ownvitals free;
- classbeat's image-only install: ownvitals' forced-command deploy (#84) is a different
  mechanism, to be compared at M2;
- the path-filtered Playwright job: ownvitals' CI already has the `stack` filter.

### Amendment 6 (2026-11-03, Architect): `findings.rule_id` and `tier` checks take the rule-id shapes the rules use

- **Changes:** PLAN §5's `findings` checks (`rule_id` as a letter plus one or two digits, and
  `tier = 'system'` exactly when `rule_id = 'Q1'`) are relaxed by migration
  `0016_findings_rule_ids`. `rule_id` may carry the variant suffixes the PLAN §7.2 rules already
  use: `^[A-Z][0-9]{1,2}(x|:[a-z][a-z_]{0,23})?$` (for example `C1x`, `N2:guided`,
  `Q1:no_weigh_in`), and `tier = 'system'` holds exactly when the id matches `^Q1(:|$)`. Both
  columns stay NOT NULL, so the checks cannot evaluate to NULL. No new tier, rule or column.
- **Evidence:** a failing insert. The merged rule specs (and the findings read function's
  experimental list) use ids such as `C1x` and `N2:guided`, which migration 0011's checks refuse,
  so the rule evaluation job (#187) could not write them. Found by the #187 Developer; fixed in
  PR #234 with a test that every rule id in the specs satisfies the checks and malformed ids and
  a system-tier row with a non-Q1 id are refused.
- **ADR:** none new; the rule ids are ADR 0010's. The shapes are documented in
  `docs/dev/derived.md`.

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
