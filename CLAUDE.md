@AGENTS.md

## Before any `gh pr merge` (read this first)

- No `Agent review (<model>): PASS <sha>` comment for the PR's **current head commit** → **do
  not merge.** Run the review: spawn a fresh agent for the first review, or resume the PR's own reviewer for a delta (the `reviewer` role in `.claude/agents/`;
  Opus for the first review of security-critical and security-adjacent PRs, Sonnet for the rest and for most deltas, see AGENTS.md), never this session. Running the review is
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

## Code structure and token economy

Applies to source code under `backend/src/` and `frontend/src/`. Tests, migrations, SQL,
generated files, fixtures and docs are exempt from the line caps.

1. **Small, cohesive modules.** A new source file has ONE responsibility and stays under about
   300 lines (about 150 for pure rule modules). Never split a cohesive function artificially to
   hit a cap: cohesion comes before the line count.
2. **Split on touch.** A change to an existing file over 300 lines must not just add code to it.
   In the same PR, move one cohesive part into its own module so the big file ends the PR at
   net ≤ 0 lines. Do the move as its own behaviour-neutral commit (no logic change), so the
   Reviewer can check it with `git diff -M`. Files in "Shared-file locks" (`config.py`, …) keep
   their lock; the extracted module is new and needs none.
3. **Pure core first.** Rules, policies and calculations (`*_policy`, `*_model`, `*_calc`) are
   separate from side effects (database, network, filesystem, UI). A pure module never imports
   I/O: no SQLAlchemy sessions, HTTP clients, `os`/`pathlib` I/O, clock or randomness (pass
   them in). This sits inside the existing rules: egress only through `ownvitals.egress`, owned
   rows only through `UserContext`; a split never moves a security check out of its guard.
4. **Token-lean checks.** Never read a whole large file when `grep -n` or a line range finds
   what you need. While working, run only the test files that cover the files you changed; CI
   runs the full suite.

## Commits

- Conventional Commits 1.0.0 (see AGENTS.md).
- **No `Co-Authored-By` trailer.**
- Never commit `.env`, tokens, keys or anything under `secrets/`.

## This machine

Windows with Git Bash and Docker Desktop. Shell scripts are LF-only (`.gitattributes`).
Worktrees go in `../ownvitals-wt/` (`D:/DeveloperArea/Repos/ownvitals-wt/`). The specification
is frozen: never edit `docs/PLAN.md`, `docs/PLAN-operations.md` or `docs/withings-findings.md`.
