# AGENTS.md: how agents build ownvitals

ownvitals is a free, multi-user website that pulls each user's Withings data through the public
API (webhooks plus scheduled sync), stores it, analyses it and shows it on a dashboard. It is
built by agents. The owner (`@magpern`) makes product decisions, gives external approvals and
sets the integration, production and legal gates; agents do everything else.

**[docs/roadmap.md](docs/roadmap.md) freezes the specification: Revision 3 of
[docs/PLAN.md](docs/PLAN.md), [docs/PLAN-operations.md](docs/PLAN-operations.md) and
[docs/withings-findings.md](docs/withings-findings.md) at commit `635901d`, plus the dated
amendments listed in the roadmap. If this file and the specification differ, the specification
wins.** Section numbers (§4.3, D9, M2 …) refer to it. The three files are never edited; CI fails
if they change. A change goes in as a roadmap amendment (see "Decisions and ADRs").

## Roles

| Role | Model class | Owns | Must not |
|---|---|---|---|
| Owner | — | Product decisions; external approvals (Withings developer apps, Cloudflare, SWAG, deploys); the real-account probe; INTEGRATION_VERIFIED, PRODUCTION_READY and the legal gates | — |
| Architect / Orchestrator | Opus (the main session) | Milestones → issues (one per PR, acceptance criteria, "Depends on #n"), scheduling, merge order, ADRs, amendments, `docs/status.md`, the integration review of every PR before merge | Normally write production code |
| Developer | Sonnet; **Opus for security-critical issues** | One issue → one PR in its own branch and worktree: code, tests, CI fixes, **all** blocking findings | Change architecture or acceptance criteria without the Architect |
| Explorer | Haiku | Bounded work: searches, synthetic fixtures, `fake-withings` payloads from the public spec, docs research | Decide security, auth, isolation, crypto or health-claims questions |
| Reviewer | A **fresh context per review**: Opus for security-critical PRs, Sonnet otherwise | The diff, acceptance criteria, tests, CI output, spec and ADR compliance → `PASS` or `FAIL` | Fix its own findings; review an ADR it wrote; share context with the Developer |

Model *classes*, not versions: use the current generation of each tier. A substitution never
widens a role's authority. Role definitions live in `.claude/agents/`; when a session cannot
spawn them by name, it spawns a general agent with the role's model and tells it to follow the
role file.

**Security-critical** (PLAN-operations §10.1), so an Opus Developer and an Opus Reviewer:
token vault and rotation, OAuth state; the webhook receiver; RLS, roles and `SECURITY DEFINER`
functions; jobs, leases and the rate limiter; auth (passwords, sessions, TOTP, step-up, passkeys,
QR login); export and delete; the deploy mechanism; AI egress; encryption and key handling. The
issue carries the `security-critical` label and says so in its "Owner:" line. When in doubt, it
is security-critical.

## Unit of work: one issue, one PR

- Every milestone is a GitHub milestone (M0 … M8). The Architect splits it into issues, one per
  PR, each with scope, acceptance criteria, the spec sections it implements, and dependencies
  (`Depends on #n`). Labels: `ready` (dependencies merged), `blocked-deps`, `blocked-owner`,
  `in-progress`, `security-critical`.
- A PR closes one issue (`Closes #n`) and states scope, acceptance criteria and how each was tested.
- No two Developers change the same files at the same time. Each issue lists the files it owns;
  the Architect checks the file sets of parallel issues before starting them, and overlap means
  one waits. Files many issues need (`ci.yml`, `config.py`, `pyproject.toml`/`uv.lock`,
  `cli.py`, `compose.yaml`, `Dockerfile`, `AGENTS.md`) are locked to one open PR at a time in
  `docs/status.md` ("Shared-file locks").
- Alembic migrations take the next free revision when the PR opens; if another migration merges
  first, the PR renumbers and re-chains on its update (a fresh scoped review).
- A milestone is done when all its issues are closed and its "done when" (PLAN §11) holds, with
  the evidence recorded in `docs/status.md`.

## PR loop

```
issue → Developer (+ Explorer) in its own worktree → PR → CI
      → fresh Reviewer → PASS → Architect integration review → merge
                       → FAIL → Developer fixes every blocking finding → CI → delta review
```

If the same blocking finding survives three cycles, the Architect re-assesses the root cause,
revises the issue or writes an ADR, and the loop restarts with a fresh Reviewer.

The Orchestrator keeps going without routine human input: it does not stop for a `FAIL`, after
a merge, or at the end of a milestone. It escalates only genuine blockers ("Escalation" below)
and carries on with independent work meanwhile.

## Review gate (mandatory for every PR an agent merges)

This gate is enforced by the agents, not by GitHub (PLAN-operations §10.2). It relies on the
deterministic CI security checks (§10.3) and on the owner's human security review at M8.

1. The PR has a Reviewer `PASS` for its **current head commit**, recorded as a PR comment that
   starts `Agent review (<model>): PASS <head SHA>`, followed by the non-blocking findings. A
   `FAIL` is recorded the same way: `Agent review (<model>): FAIL <head SHA>`, blocking findings
   first, each with file and line.
2. The Reviewer was a fresh context: Opus for security-critical PRs, Sonnet for the rest (UI,
   docs, tooling, tests). When in doubt, Opus.
3. **A new head SHA needs a new review**:
   - *Merge-only update:* the only change since the reviewed head is a merge of `main` with no
     hand-resolved conflict, and CI is green on the new head. The Architect checks that
     `git diff <reviewed>..<head>` is exactly `main`'s change and records
     `Agent review (merge check): PASS <head SHA> (carries <reviewed SHA>)`. If `main` changed a
     module the PR changes or calls, or a conflict was resolved by hand, a fresh Reviewer checks
     the merge.
   - *Fix round after a `FAIL`:* a delta review of the diff since the last reviewed head that
     verifies each earlier finding. Fresh Opus for a behaviour change on a security-critical PR
     or after a second `FAIL`; a fresh Sonnet may review a tests- or docs-only delta that loosens
     no guard, and must check each earlier finding by mutating the code the new test protects.
   Reviewers rely on CI for the full suite. They run only the targeted tests and at most about 8
   mutation checks, chosen by risk, and say which they ran.
4. Not a `PASS`: the implementing session's own check, a review run in the same context, green
   CI, passing tests, or "it's fine" in chat.
5. Agents record verdicts only as comments. They never submit an approving review and never
   write a comment starting `Human review:`.
6. A `FAIL` is never overridden by an agent.
7. **Architect integration review** before every merge: the PR closes its issue, stays in scope,
   fits what merged since it was planned, updates `docs/status.md` if it changes a milestone's
   state, and claims no status beyond IMPLEMENTED.

**A human may merge any PR on GitHub regardless of the verdict.** That is the owner's decision,
never an agent's: an agent never merges without the gate, even when told to skip it.

## Merging

Agents merge only with:

```bash
gh pr merge <n> --squash --match-head-commit <reviewed-sha> --delete-branch
```

and only when the required checks (`ci`, `pr-title`) are green on that SHA, the `PASS` (or the
merge check) is for that SHA, and no blocking comment is open. `--match-head-commit` makes GitHub
refuse the merge if the head moved after the review. The squash title is the PR title plus
` (#<n>)`, so the PR title must be the Conventional Commits subject. Never `--auto`, never
`--admin`, never an approving review, never a ruleset change to get a merge through.

The `main` ruleset (PLAN-operations §10.5): pull request required, required checks `ci` and
`pr-title` with branches up to date, squash only, linear history, no direct pushes, no force
pushes, zero required approvals. Protected `v*` tags. `CODEOWNERS` is informational. When `main`
moved, the Developer (or the Architect) updates the branch with
`gh pr update-branch <n>` and the merge-only rule in item 3 applies.

## Branches, worktrees and commits

- Every task is a branch `<issue>-<slug>` and a PR. One writing agent per branch.
- Each Developer works in its own worktree outside the main checkout:
  `git worktree add ../ownvitals-wt/<issue>-<slug> -b <issue>-<slug> origin/main`. Remove it
  after the merge (`git worktree remove ../ownvitals-wt/<issue>-<slug>`). Once
  `scripts/worktree.sh` exists it does this and gives each worktree its own compose project,
  database and ports.
- The main checkout (`D:/DeveloperArea/Repos/withings`) stays on `main` for the Architect.
- When a dependency merges first, the dependent branch is updated from `main` (review rule 3).
- **Conventional Commits 1.0.0** for commits, PR titles and squash subjects: `feat(auth): …`,
  `fix(withings): …`, `refactor(db): …`, `test(security): …`, `ci: …`, `docs: …`, `build: …`,
  `chore: …`. Keep titles under about 90 characters (the ` (#n)` suffix must fit in 100).
- **No `Co-Authored-By` trailer** in commits. PR descriptions end with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.

## Status levels (D10, PLAN-operations §10.4)

| Level | Means | Set by |
|---|---|---|
| IMPLEMENTED | Code and synthetic tests complete (fakes, demo account), merged through the gate | An agent, after the PASS merge |
| INTEGRATION_VERIFIED | Provider behaviour confirmed with a real account, from the owner's probe output | **Owner only** |
| PRODUCTION_READY | Security, operations and deployment acceptance complete | **Owner only**, at the milestone gate |

Agents never write INTEGRATION_VERIFIED or PRODUCTION_READY for anything, never mark a legal or
production gate passed, and never describe synthetic results as verified against Withings. Work
past a blocked external step uses fakes, contract tests and **documented assumptions**, each
listed in `docs/status.md` with what would verify it.

## Health data

- **No real health data** in development environments, agent contexts, fixtures, test data,
  logs, PRs, issues or comments. Fixtures are synthetic and say so (`synthetic: true` or a
  `synthetic_` file prefix). The local stack only uses the demo account and the fakes.
- Agents never connect a real Withings account, never read the owner's data and never ask the
  owner to paste real values. The probe (`ownvitals probe`, §10.4) prints shapes only (meastypes
  and counts, array lengths, sampling rate, attrib and position values, capability states): no
  values, no timestamps. The owner runs it on the home server.
- Logs never contain measurement values, ECG samples, tokens, webhook secrets or full webhook
  paths (log-redaction tests enforce it).
- Every rule and finding has a tier (D9) and cites its source; the experimental tier is behind
  the owner-only flag.

## Safety envelope

**Agents may, without asking:** create branches, PRs, issues, labels and milestones; review and
merge per the gate; run local Docker Desktop stacks with fakes; cut releases and push images to
`ghcr.io/magpern/ownvitals` once `release.yml` exists; set repository rulesets and settings that
match PLAN-operations §10.5.

**Only with the owner's authorization for that specific action:** deploying to the home server
(the forced-command deploy key, §9.2); SWAG, Cloudflare and DNS changes; registering or changing
Withings developer apps and Google OAuth apps; anything touching a real Withings account; paid
services; emails to real people; contacting Withings; opening registration beyond the household.

## Releasing

Deploying is not part of a release and needs the owner's go-ahead.

1. `main` is green and holds what the release should contain. Pick the next SemVer `vX.Y.Z`.
2. `git fetch origin main && git tag -a vX.Y.Z -m "vX.Y.Z" origin/main && git push origin vX.Y.Z`.
3. `release.yml` (M0) verifies the tag is on `main`, builds and pushes the image to GHCR and
   creates a build-provenance attestation. The deploy mechanism (§9.2) only accepts an attested
   digest from a protected `v*` tag.
4. Never move or delete a pushed tag: fix forward with a new version.

## Security rules

- Never commit, print or log secrets: Withings client secrets, access and refresh tokens,
  webhook secrets, OAuth client secrets, session cookies, TOTP seeds, KEKs, signed URLs. `.env`
  and `secrets/` are never committed; `.env.example` holds placeholders only.
- Public Withings API only (D1). Never the app's private API, never credentials from the app.
- Tokens and secrets never go to a model. Model output is a proposal; the backend authorizes
  every action against the authenticated user.
- Tests and CI never touch the network or real accounts: fakes for Withings, Google and model
  APIs. The backend suite enforces it with a network guard; only a test marked `allow_network`
  is exempt, and none is expected.
- Every owned row carries `owner_id` and is read through `UserContext` under forced RLS. Cross-user
  work (webhooks, scheduler, purge, admin) goes through reviewed `SECURITY DEFINER` functions or
  the issuer pattern of §5; RLS tests run in every execution context.
- The worker owns every outbound call through the egress allowlist (`wbsapi.withings.net`,
  `account.withings.com`, and `api.anthropic.com` only when AI is enabled). The list is code.
- Webhook secrets are stored as HMACs; bodies over 2 KB are refused; the payload is only a
  trigger to fetch (§4.4).

## Decisions and ADRs

Routine technical decisions never go to the owner. When a fact is unknown, the Architect picks
the safest reversible default, isolates it behind an interface or setting, and records it in
`docs/adr/NNNN-title.md` (Context, Decision, Alternatives, Consequences, Status). A fresh
Reviewer reviews the ADR in its PR.

The architecture is frozen. The Architect may amend the specification only for a demonstrated
technical blocker (a failing test, a provider behaviour, a library limit), recorded as a dated
entry in `docs/roadmap.md` with its evidence and an ADR. Product, scope, legal and gate changes
need the owner.

## Escalation to the owner

Only for: external permissions and agreements, credentials agents can't provision safely,
real-account verification, product decisions, and human security decisions. Record the question
in `docs/status.md` under "Blocked on the owner", label the issue `blocked-owner`, carry on with
independent work, and resume when it's answered.

## Durable state and resuming

The repository and GitHub are the source of truth, never chat history: `docs/roadmap.md`,
`docs/status.md`, `docs/adr/`, GitHub milestones, issues (with their dependencies) and PRs.

Every orchestration cycle, and always after a restart:

1. read `CLAUDE.md`, `AGENTS.md`, `docs/roadmap.md` and `docs/status.md`;
2. reconcile: `git fetch`, open PRs with their head SHA, CI and verdict comments, open issues and
   their labels, `git worktree list`, running stacks;
3. fix any drift in `docs/status.md`, then continue with the next unblocked issue without
   re-planning finished work. A PR whose head has no verdict gets a fresh Reviewer; a branch
   with no PR goes back to its Developer.

## Developer commands

Each area documents its commands and settings in its own file in [docs/dev/](docs/dev/),
written by the issue that creates the area, so parallel issues never edit the same file:
`backend.md` and `stack.md` (skeleton, compose, worktrees), `frontend.md`, `isolation.md`,
`egress.md`, `crypto.md`, `jobs.md`, `auth.md`, `admin.md`, `connections.md`, `e2e.md`; later
milestones add `withings.md`, `analysis.md` and others. Read the files for the area you work in.

## CI

| Workflow / job | Runs | What it does |
|---|---|---|
| `ci` / `checks` | every PR, weekly, manual | Frozen-spec guard (the three spec files equal `635901d`); gitleaks over the PR's own commits (weekly: whole history); `bash -n` and shellcheck on `scripts/*.sh`; actionlint |
| `ci` / `ci` | always | The required check: fails unless every needed job succeeded |
| `pr-title` | every PR title change | commitlint (`commitlint.config.cjs`) on the title with ` (#<n>)` appended, after a self-test |

Every GitHub Action is pinned by full commit SHA with the version in a comment; tools downloaded
in CI are checksum-verified. Dependabot keeps Actions current (and uv, npm and Docker once their
manifests exist). Jobs added later (backend, frontend, e2e, image scan, audits) join `needs` of
the `ci` gate and this table.

New advisory: **upgrade first**. Only if no fix exists or the code path is unreachable, add a
dated ignore with a reason comment. Never ignore without a reason.
