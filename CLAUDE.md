@AGENTS.md

## Before any `gh pr merge` (read this first)

- No `Agent review (<model>): PASS <sha>` comment for the PR's **current head commit** → **do
  not merge.** Run the review: spawn a fresh agent (the `reviewer` role in `.claude/agents/`;
  Opus for security-critical PRs, Sonnet for the rest), never this session. Running the review is
  the default; don't ask whether to skip it.
- Your own check, a review skill in this session, green CI and passing tests are not a `PASS`.
  "I reviewed it" in chat is not one either: the owner can merge the PR themselves on GitHub.
- Any push after a `PASS` invalidates it. A merge-only update of `main` with no hand-resolved
  conflict is covered by the Architect's merge check (AGENTS.md review gate, item 3); any other
  push needs a fresh (delta) review.
- The Architect's integration review (AGENTS.md review gate, item 7) comes before the merge.
- Merge only with `gh pr merge <n> --squash --match-head-commit <reviewed-sha> --delete-branch`.
- Never `--auto`, never `--admin`, never an approving review, never a ruleset change to get a
  merge through, never a comment starting `Human review:`.

## Status and data

- Agents set IMPLEMENTED only. Never write INTEGRATION_VERIFIED or PRODUCTION_READY, and never
  mark a production or legal gate passed: those are the owner's.
- No real health data anywhere an agent works: synthetic fixtures and fakes only.

## Commits

- Conventional Commits 1.0.0 (see AGENTS.md).
- **No `Co-Authored-By` trailer.**
- Never commit `.env`, tokens, keys or anything under `secrets/`.

## This machine

Windows with Git Bash and Docker Desktop. Shell scripts are LF-only (`.gitattributes`).
Worktrees go in `../ownvitals-wt/` (`D:/DeveloperArea/Repos/ownvitals-wt/`). The specification
is frozen: never edit `docs/PLAN.md`, `docs/PLAN-operations.md` or `docs/withings-findings.md`.
