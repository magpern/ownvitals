#!/usr/bin/env bash
# Keeps the owner's local real-account work out of git (#450, AGENTS.md "Health data",
# docs/dev/local-real-account.md). Path rules only: content is gitleaks' job in CI, and a real
# value cannot be told from a synthetic one by its look, so nothing real may be written outside
# the git-ignored places in the first place.
#   local-real-guard.sh staged         refuse staged paths that must never be committed (pre-commit)
#   local-real-guard.sh tracked        refuse such paths anywhere in the index (CI, the guard test)
#   local-real-guard.sh paths <p>...   check the given paths (tests)
#   local-real-guard.sh pre-push       refuse any push from a local real-account worktree
#   local-real-guard.sh install        install the pre-commit and pre-push hooks (refuses to replace others)
#   local-real-guard.sh seal           break every remote's push URL for this worktree only, so a
#                                      `git push --no-verify` from the real-account worktree fails too
set -euo pipefail

die() { echo "local-real-guard: $*" >&2; exit 1; }

# A path that must never be committed. Lower-cased before matching.
#   .env and .env.<anything> but .env.example, and direnv's .envrc; secret/ and secrets/
#   anywhere; the local-real/ output directory anywhere; probe output; database dumps and their
#   compressed forms; archives (the app's own data export is a .zip); CSV and HAR exports.
FORBIDDEN='(^|/)\.env$|(^|/)\.env\.|(^|/)\.envrc$|(^|/)secrets?(/|$)|(^|/)local-real(/|$)|(^|/)probe-output|\.(dump|pgdump|pg_dump|backup|bak)$|\.sql\.(gz|bz2|xz|zst|zip|7z)$|(^|/)[^/]*dump[^/]*\.sql$|\.(tar|tgz|tbz2|txz|zip|har)$|\.tar\.(gz|bz2|xz|zst)$'
ALLOWED='(^|/)\.env\.example$'
# Plain .sql is source only under docker/postgres/<area>/ (the dump rule above still applies
# there); anywhere else it is taken for a dump. A .csv is a synthetic fixture (synthetic_ prefix)
# or an export.
SQL_HOME='^docker/postgres/[a-z0-9_-]+/[a-z0-9_.-]+\.sql$'
CSV_OK='(^|/)synthetic_[^/]*\.csv$'

refused() {
  local lower="$1"
  if [[ "$lower" =~ $ALLOWED ]]; then return 1; fi
  if [[ "$lower" =~ $FORBIDDEN ]]; then return 0; fi
  if [[ "$lower" == *.sql && ! "$lower" =~ $SQL_HOME ]]; then return 0; fi
  if [[ "$lower" == *.csv && ! "$lower" =~ $CSV_OK ]]; then return 0; fi
  return 1
}

check_paths() {
  local bad=0 path lower
  for path in "$@"; do
    lower="$(printf '%s' "$path" | tr '[:upper:]' '[:lower:]')"
    if refused "$lower"; then
      # The path is ours (a file name), never file content.
      echo "local-real-guard: refused: $path" >&2
      bad=1
    fi
  done
  return "$bad"
}

# The marker that makes a worktree a local real-account one (git-ignored, under local-real/).
is_real_worktree() {
  local top
  top="$(git rev-parse --show-toplevel)"
  [ -e "$top/local-real/REAL-ACCOUNT" ]
}

cmd_install() {
  local hooks name mode
  hooks="$(git rev-parse --git-path hooks)"  # honours core.hooksPath; shared by all worktrees
  mkdir -p "$hooks"
  for name in pre-commit pre-push; do
    if [ "$name" = pre-commit ]; then mode=staged; else mode=pre-push; fi
    if [ -e "$hooks/$name" ] && ! grep -q 'local-real-guard' "$hooks/$name"; then
      die "$hooks/$name exists and is not ours; add 'scripts/local-real-guard.sh $mode' to it by hand"
    fi
    # A checkout from before the guard has no script: the hook then lets git go on, unless the
    # worktree is a real-account one (its marker exists), which never commits or pushes.
    # shellcheck disable=SC2016 # the hook expands these when it runs, not now
    printf '%s\n' '#!/usr/bin/env bash' '# installed by scripts/local-real-guard.sh install' \
      'top="$(git rev-parse --show-toplevel)" || exit 1' \
      'guard="$top/scripts/local-real-guard.sh"' \
      'if [ ! -f "$guard" ]; then' \
      '  [ ! -e "$top/local-real/REAL-ACCOUNT" ] && exit 0' \
      '  echo "local-real-guard: refused: a local real-account worktree without the guard script" >&2' \
      '  exit 1' \
      'fi' \
      "exec bash \"\$guard\" $mode" > "$hooks/$name"
    chmod +x "$hooks/$name"
  done
  echo "installed pre-commit and pre-push in $hooks"
}

# A push URL no transport serves: git fails on it before connecting anywhere.
NO_PUSH_URL='local-real-no-push://this-worktree-never-pushes'

cmd_seal() {
  local common remote remotes=()
  is_real_worktree || die "no local-real/REAL-ACCOUNT here; seal only the real-account worktree"
  common="$(git rev-parse --git-common-dir)"
  # With worktreeConfig on, a core.worktree in the shared config would apply to every worktree.
  if git config --file "$common/config" core.worktree > /dev/null; then
    die "core.worktree is set in $common/config; move it to config.worktree first (git help worktree)"
  fi
  git config extensions.worktreeConfig true
  mapfile -t remotes < <(git remote)
  for remote in "${remotes[@]}"; do
    git config --worktree "remote.$remote.pushurl" "$NO_PUSH_URL"
  done
  echo "sealed ${#remotes[@]} remote(s) for this worktree; seal again after adding a remote"
}

main() {
  local mode="${1:-}"
  [ $# -gt 0 ] && shift
  case "$mode" in
    staged)
      local files=()
      mapfile -d '' files < <(git diff --cached --name-only --no-renames --diff-filter=ACMRT -z)
      check_paths "${files[@]}" || die "unstage these; they stay on this machine (docs/dev/local-real-account.md)"
      if is_real_worktree; then
        die "this is a local real-account worktree: it never commits (docs/dev/local-real-account.md)"
      fi
      ;;
    tracked)
      local files=()
      mapfile -d '' files < <(git ls-files -z)
      check_paths "${files[@]}" || die "these paths must never be tracked"
      ;;
    paths) check_paths "$@" || die "refused" ;;
    pre-push)
      if is_real_worktree; then
        die "this is a local real-account worktree: it never pushes (docs/dev/local-real-account.md)"
      fi
      ;;
    install) cmd_install ;;
    seal) cmd_seal ;;
    *) die "usage: local-real-guard.sh staged|tracked|paths <p>...|pre-push|install|seal" ;;
  esac
}

main "$@"
