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
