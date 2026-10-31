#!/usr/bin/env bash
# Per-worktree dev stacks for ownvitals.
#   worktree.sh init [issue]        .env and secrets/ for the current checkout (if missing)
#   worktree.sh new <issue> <slug>  worktree ../ownvitals-wt/<issue>-<slug> from origin/main (or $WORKTREE_BASE_REF), then init
#   worktree.sh rm <issue>          stop the stack (volumes too), remove the worktree, delete its branch if merged
#   worktree.sh ls                  worktrees with project name and ports
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SECRETS=(postgres_password database_owner_password database_password database_maint_password
  app_secret_key connection_kek)

die() { echo "worktree.sh: $*" >&2; exit 1; }

MAX_ISSUE=9999

# Digits, no leading zero, 1..MAX_ISSUE (ports are 20000/30000/40000 + issue).
check_issue() {
  case "$1" in
    ""|0*|*[!0-9]*) die "issue must be a number without leading zeros" ;;
  esac
  [ "$1" -le "$MAX_ISSUE" ] || die "issue must be at most $MAX_ISSUE"
}

repo_root() { git rev-parse --show-toplevel; }

# Directory holding the worktrees: ../ownvitals-wt of the main checkout.
wt_base() {
  local parent
  parent="$(dirname "$(repo_root)")"
  if [ "$(basename "$parent")" = "ownvitals-wt" ]; then echo "$parent"; else echo "$parent/ownvitals-wt"; fi
}

# $1 = number of random bytes (hex-encoded: twice as many characters).
random_secret() { head -c "${1:-32}" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

cmd_init() {
  local issue="${1:-}" root
  root="$(repo_root)"
  cd "$root"
  if [ -n "$issue" ]; then
    check_issue "$issue"
  fi

  if [ ! -f .env ]; then
    cp .env.example .env
    if [ -n "$issue" ]; then
      sed -i \
        -e "s/^COMPOSE_PROJECT_NAME=.*/COMPOSE_PROJECT_NAME=ownvitals-$issue/" \
        -e "s/^WEB_PORT=.*/WEB_PORT=$((20000 + issue))/" \
        -e "s/^DB_PORT=.*/DB_PORT=$((30000 + issue))/" \
        -e "s/^MAILPIT_PORT=.*/MAILPIT_PORT=$((40000 + issue))/" .env
    fi
    echo "created .env"
  fi

  mkdir -p secrets
  chmod 700 secrets 2>/dev/null || true
  local name bytes
  for name in "${SECRETS[@]}"; do
    if [ ! -f "secrets/$name" ]; then
      # app_secret_key feeds the key derivation of the accounts (at least 32 bytes); 64 here.
      # connection_kek is exactly 32 bytes: 64 lowercase hex characters.
      if [ "$name" = "app_secret_key" ]; then bytes=64; else bytes=32; fi
      printf '%s' "$(random_secret "$bytes")" > "secrets/$name"
      # The container user (uid 10001) must read the bind-mounted file; secrets/ itself is 0700.
      chmod 644 "secrets/$name" 2>/dev/null || true
      echo "created secrets/$name"
    fi
  done
}

cmd_new() {
  local issue="${1:-}" slug="${2:-}"
  [ -n "$slug" ] || die "usage: worktree.sh new <issue> <slug>"
  check_issue "$issue"
  case "$slug" in
    ""|-*|*-|*[!a-z0-9-]*) die "slug may only contain a-z, 0-9 and -, and not start or end with -" ;;
  esac
  local branch="$issue-$slug" dir
  dir="$(wt_base)/$branch"
  [ ! -e "$dir" ] || die "$dir already exists"
  mkdir -p "$(wt_base)"
  git fetch origin main --quiet
  if git show-ref --verify --quiet "refs/heads/$branch"; then
    git worktree add "$dir" "$branch"
  else
    git worktree add -b "$branch" "$dir" "${WORKTREE_BASE_REF:-origin/main}"
  fi
  (cd "$dir" && "$SELF" init "$issue")
  echo "worktree: $dir"
}

# Only worktrees directly under ../ownvitals-wt/ are matched.
find_worktree() {
  local issue="$1" path base
  base="$(wt_base)"
  while read -r path; do
    case "$path" in "$base"/"$issue"-*) echo "$path"; return 0 ;; esac
  done < <(git worktree list --porcelain | sed -n 's/^worktree //p')
  return 1
}

cmd_rm() {
  local issue="${1:-}" dir branch project declared
  check_issue "$issue"
  dir="$(find_worktree "$issue")" || die "no worktree for issue $issue under $(wt_base)"
  # Empty on a detached HEAD.
  branch="$(git -C "$dir" symbolic-ref --short -q HEAD || true)"
  # Only ever this issue's own project: whatever the worktree's .env says is not trusted, so
  # `down -v` can never reach another stack or its volumes.
  project="ownvitals-$issue"
  [ -z "$(git -C "$dir" status --porcelain)" ] \
    || die "$dir has uncommitted or untracked work; commit or discard it first (nothing was changed)"
  if [ -f "$dir/compose.yaml" ]; then
    if [ ! -f "$dir/.env" ]; then
      echo "warning: $dir has no .env; trying project $project anyway so a stack started there is not orphaned" >&2
    else
      declared="$(env_value "$dir" COMPOSE_PROJECT_NAME)"
      if [ "$declared" != "$project" ]; then
        echo "warning: .env in $dir sets COMPOSE_PROJECT_NAME='${declared}'; ignoring it and using $project" >&2
      fi
    fi
    if ! (cd "$dir" && docker compose -p "$project" down -v --remove-orphans --rmi local); then
      echo "worktree.sh: docker compose down failed in $dir; the worktree and branch were kept." >&2
      echo "  Some containers, networks, volumes or images of project $project may already be gone and some may remain." >&2
      echo "  Inspect: docker ps -a --filter label=com.docker.compose.project=$project" >&2
      echo "           docker volume ls --filter label=com.docker.compose.project=$project" >&2
      echo "           docker network ls --filter label=com.docker.compose.project=$project" >&2
      echo "  Clean up: (cd $dir && docker compose -p $project down -v --remove-orphans --rmi local), then run rm again." >&2
      exit 1
    fi
  fi
  # No --force: git refuses a worktree with uncommitted or untracked work (ignored .env and
  # secrets/ do not count).
  git worktree remove "$dir" \
    || die "git refused to remove $dir (uncommitted work?). Commit or discard it, then run rm again"
  if [ -z "$branch" ]; then
    echo "worktree was on a detached HEAD; no branch to delete"
    local leftover
    leftover="$(git for-each-ref --format='%(refname:short)' "refs/heads/$issue-*")"
    if [ -n "$leftover" ]; then
      echo "branch(es) not touched: $(echo "$leftover" | tr '\n' ' ')"
      echo "If a branch is no longer needed (e.g. the one 'new' created), delete it with: git branch -D <branch>"
    fi
  elif git branch -d "$branch" 2>/dev/null; then
    echo "deleted branch $branch"
  else
    echo "kept branch $branch: git does not see it as merged (squash merges never are)."
    echo "If the PR is merged, delete it with: git branch -D $branch"
  fi
}

env_value() { sed -n "s/^$2=//p" "$1/.env" 2>/dev/null | head -n 1; }

cmd_ls() {
  local path
  printf '%-45s %-18s %-6s %-6s %s\n' WORKTREE PROJECT WEB DB MAILPIT
  while read -r path; do
    printf '%-45s %-18s %-6s %-6s %s\n' "$(basename "$path")" \
      "$(env_value "$path" COMPOSE_PROJECT_NAME)" "$(env_value "$path" WEB_PORT)" \
      "$(env_value "$path" DB_PORT)" "$(env_value "$path" MAILPIT_PORT)"
  done < <(git worktree list --porcelain | sed -n 's/^worktree //p')
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  new) shift; cmd_new "$@" ;;
  rm) shift; cmd_rm "$@" ;;
  ls) cmd_ls ;;
  *) die "usage: worktree.sh init [issue] | new <issue> <slug> | rm <issue> | ls" ;;
esac
