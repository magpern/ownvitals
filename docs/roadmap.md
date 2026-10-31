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

- **Changes:** PLAN §2 ("The worker owns every outbound call") gains one exception: the web
  process may make the single `oauth2 requesttoken` call that exchanges an authorization code
  inside the OAuth callback request, through a narrow, web-only egress entry limited to that
  call and host. Token refresh, revoke and every other Withings call stay in the worker.
- **Evidence:** a conflict inside the frozen spec. §4.3 requires the code to be exchanged "in
  the request (it's valid for 30 s)", and the callback request runs in web; withings-findings §2
  records the 30 s validity. Handing the code to a worker job and waiting would race that window.
  Raised in ADR 0004 (PR #41, issue #4).
- **ADR:** ADR 0004 records the decision; the egress guard (#6) and the connect issue (#22)
  implement and test that web can reach nothing else.

## Pinned sources

- classbeat, the platform this ports from: `magpern/classbeat` at
  `d80605100992f85f8f02672b9b5f885ed6166214` (recorded again in ADR 0001).
