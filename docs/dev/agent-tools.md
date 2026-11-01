# Developing OwnVitals with Codex or Cursor

These optional profiles implement the owner's request for alternatives to Claude Code.
They change development tools only. Roadmap Amendment 4 authorizes the model names and PR
attribution; AGENTS.md, CLAUDE.md, the frozen specification and all safety gates still apply.
Nothing here enables an application AI provider, sends health data to a model or changes egress.

## Models and roles

| Role | Codex | Economical Cursor |
|---|---|---|
| Architect / Orchestrator (primary) | GPT-6 Astra, high | Grok 4.7, high, standard speed |
| Routine Developer | GPT-6.1 Sol, high | Composer 2.5, standard speed |
| Explorer | GPT-6 Luna, medium | Composer 2.5, standard speed |
| Routine Reviewer | GPT-6.1 Sol, high | Grok 4.7, high, standard speed |
| Security-critical Developer / decisions | GPT-6 Astra, high | Claude Opus 5.5, high |
| Security-critical Reviewer; review after second FAIL | GPT-6 Astra, high | Claude Opus 5.5, high |

Risk classification is OwnVitals' own list, not Classbeat's: vault/rotation, OAuth state,
webhooks, RLS/roles/definer functions, jobs/leases/rate limiter, auth/session/TOTP/step-up/
passkeys/QR login, export/delete, deployment and trusted attestations, AI egress and keys.
When uncertain, use the security role. The Cursor Grok Architect schedules security work;
its production security decisions and ADRs go to Opus and a separate Opus Reviewer.
Both profiles deliberately use the security reviewer even for tests/docs-only deltas on a
security-critical PR, a stricter choice than the shared gate permits.

This is a starting mapping, not a demonstrated quality equivalence. Evaluate routine PRs by
acceptance-criterion coverage, findings, fix rounds and total usage before expanding roles.
Keep explicit model selections; an unavailable or substituted model blocks that role.

## Codex setup

- Use a current Codex client with access to Astra, Sol and Luna; sign in outside the repo.
  Trust this checkout so .codex/config.toml and its project agents load.
- The project config selects Astra/high for the Architect and an instruction budget of
  65536 bytes (AGENTS.md currently fits). It explicitly directs the session to this guide.
  Do not use an AGENTS.override.md that would mask the shared rulebook.
- Confirm all five codex-* agents in .codex/agents/ are discovered. Invoke those names,
  not similarly named roles discovered from another tool. Custom agents pin their model
  and reasoning effort; their files are instructions, not an executable scheduler.
- On interfaces that ignore project configuration, explicitly read the shared rules and
  this guide, select the prescribed model, and start equivalent separate role sessions.
  If the required model or independent review is unavailable, leave the PR open.
- Coding-agent authentication stays outside the checkout and application secrets.
  No OpenAI API key or application model route is needed for synthetic-only development.

## Cursor setup

- Select Grok 4.7, high effort, standard speed for the primary Architect in the model picker.
  The always-applied .cursor/rules/ownvitals.mdc directs sessions to this guide.
- Confirm all five cursor-* agents in .cursor/agents/ are visible and invoke them by name.
  Cursor also discovers Claude/Codex agents, so these names are deliberately unique.
- Composer is pinned with composer-2.5[fast=false]; Grok reviews use
  grok-4.7[reasoning_effort=high,fast=false]. Standard speed is deliberate: Fast consumes more usage.
  Opus roles use claude-opus-5-5[effort=high]. Parameter names are model-specific:
  Grok 4.7 uses reasoning_effort; Opus 5.5 uses effort.
- Check the actual model on each task card/usage row. Cursor may substitute models because
  of plan limits, admin restrictions or legacy Max Mode settings. A fallback is not authority
  to continue as the intended role, especially a security-critical role.
- The complete profile requires access to third-party Opus. Included model allowances are
  finite; Opus draws from third-party usage. Check current pricing/limits in the client.
  Changing a paid plan/service still requires specific owner authorization.

## Environment and handoff

Use Git, Bash (Git Bash on Windows), Docker Desktop/Engine with Compose v2, uv with Python 3.12,
and Node/npm matching CI. Backend commands and the pinned uv version are in
[backend.md](backend.md); frontend commands in [frontend.md](frontend.md); isolated Compose,
secrets and worktrees in [stack.md](stack.md). Follow those maintained files rather than
copying version pins into agent instructions. GitHub access needs repository/issue/check reads,
issue branch pushes, PR creation and review comments; authenticate gh outside the repository.

Start by reading AGENTS.md, CLAUDE.md, roadmap/status, open issues/PR heads/checks/verdicts,
worktrees and running stacks. Respect the active file sets and shared-file locks before
starting a writer. One issue, one branch, one PR, one writing agent. No two tools write to
the same branch. `scripts/worktree.sh new <issue> <slug>` creates
`../ownvitals-wt/<issue>-<slug>`
with isolated ports/project/database/secrets. Do not use a hardcoded Windows path on another OS.

Developers run lint/type checks and targeted tests/guards. CI runs the full suite.
Use only synthetic labelled fixtures and fake Withings/Google/model APIs, local Postgres
and isolated stacks. Never retrieve real health data, connect a real Withings account or
ask the owner to paste real values. The owner alone runs the shape-only real-account probe.
No Withings or Google production credentials are needed for the fake-only workflow.

A hosted environment without Docker can prepare a PR and run available checks; report the
missing checks accurately and rely on CI for the stack suite. Never weaken a guard or widen
host permissions to work around a missing capability. No client smoke test is implied by
syntactically valid configuration.

## Review, integration and merge

A fresh reviewer receives the PR, exact head SHA, issue and repository evidence, without
the author's or Architect's conversation or conclusions. Codex must launch without history
inheritance; Cursor documents clean subagent contexts. If an interface cannot provide that
isolation, use a separate fresh session. An in-session self-review is never independent PASS.

Use all of AGENTS.md's checks, including units/time zones, forced RLS across execution contexts,
log redaction, synthetic data, rule tiers/sources, experimental flags and design screenshots.
Run targeted tests and about eight risk-based mutations, not the full suite. Review deltas
and verify previous findings. Security-critical reviews, their deltas and a review after a second FAIL go to
codex-security-reviewer (Astra) or cursor-security-reviewer (Opus). A routine reviewer hands that
work to that agent and does not PASS it. Security-critical implementation goes to
codex-security-developer or cursor-security-developer.
Only PASS with no blocking findings and green ci/pr-title on that exact SHA.

Record verdicts as PR comments using the actual model:
- `Agent review (GPT-6 Astra): PASS <full head SHA>`
- `Agent review (GPT-6.1 Sol): FAIL <full head SHA>`
- `Agent review (Grok 4.7): PASS <full head SHA>`
- `Agent review (Opus 5.5): FAIL <full head SHA>`
Never submit an approving review, write Human review:, override FAIL or claim a substituted
model's identity. New pushes invalidate verdicts except the shared mechanical merge exception.

After independent PASS, the Architect performs AGENTS.md item 7's integration review:
issue closed by the PR, scope correct, compatible with intervening merges, status updated
when a milestone changes, and no claim beyond IMPLEMENTED. Only then merge with:
`gh pr merge <n> --squash --match-head-commit <reviewed-sha> --delete-branch`
Branches must satisfy the repository's up-to-date checks. Keep the existing merge-only
carry-forward/scoped review rules. Never --auto, --admin or ruleset changes to bypass a gate.

## Status, attribution and boundaries

Do not edit docs/PLAN.md, docs/PLAN-operations.md or docs/withings-findings.md. They remain
frozen at the specified commit. No redesign without the roadmap's demonstrated-blocker rule
or owner decision. Agents set IMPLEMENTED only after gated merge; INTEGRATION_VERIFIED,
PRODUCTION_READY and all legal/production gates are owner-only.
UI changes follow docs/design/ and its mockups; analysis rules keep their tier and source.

PRs have scope, acceptance criteria and their validation, `Closes #<issue>`, Tested and Notes
for reviewer. Conventional Commits, no Co-Authored-By trailer. Footer by authoring tool:
- Codex: 🤖 Generated with [Codex](https://developers.openai.com/codex/)
- Cursor: 🤖 Generated with [Cursor](https://cursor.com/)
- Claude Code retains its existing footer.

Deployment, SWAG/Cloudflare/DNS, Withings/Google apps, real-account actions, paid services,
contacting people and expanding registration retain the owner's specific authorization
requirements. Do not send health data or secrets to any development model.
Continue independent work while an external step is blocked and record assumptions as the
shared workflow requires.

## Official references and verification

- [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Codex custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents)
- [Codex instruction discovery](https://learn.chatgpt.com/docs/agent-configuration/agents-md)
- [Cursor custom agents/parameters](https://cursor.com/docs/subagents)
- [Cursor rules](https://cursor.com/docs/rules)
- [Composer](https://cursor.com/docs/models/cursor-composer-2-5)
- [Grok](https://cursor.com/docs/models/grok-4-7)
- [Opus](https://cursor.com/docs/models/claude-opus-5-5)

Configuration validation covers syntax, mappings and references. Each installation still
needs a smoke check of rule/agent discovery, account model access, actual model/speed/effort
selection and isolated reviewer behaviour. No live-account test is part of that smoke check.
