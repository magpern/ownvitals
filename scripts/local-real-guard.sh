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
set -euo pipefail

die() { echo "local-real-guard: $*" >&2; exit 1; }

# A path that must never be committed. Lower-cased before matching.
#   .env and .env.<anything> but .env.example; secrets/ anywhere; the local-real/ output
#   directory anywhere; probe output; database dumps and their compressed forms.
FORBIDDEN='(^|/)\.env$|(^|/)\.env\.|(^|/)secrets(/|$)|(^|/)local-real(/|$)|(^|/)probe-output|\.(dump|pgdump|pg_dump|backup|bak)$|\.sql\.(gz|bz2|xz|zst|zip|7z)$|(^|/)[^/]*dump[^/]*\.sql$'
ALLOWED='(^|/)\.env\.example$'

check_paths() {
  local bad=0 path lower
  for path in "$@"; do
    lower="$(printf '%s' "$path" | tr '[:upper:]' '[:lower:]')"
    if [[ "$lower" =~ $ALLOWED ]]; then continue; fi
    if [[ "$lower" =~ $FORBIDDEN ]]; then
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
    # A checkout from before the guard has no script: the hook then lets git go on.
    # shellcheck disable=SC2016 # the hook expands these when it runs, not now
    printf '%s\n' '#!/usr/bin/env bash' '# installed by scripts/local-real-guard.sh install' \
      'guard="$(git rev-parse --show-toplevel)/scripts/local-real-guard.sh"' \
      '[ -f "$guard" ] || exit 0' \
      "exec bash \"\$guard\" $mode" > "$hooks/$name"
    chmod +x "$hooks/$name"
  done
  echo "installed pre-commit and pre-push in $hooks"
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
    *) die "usage: local-real-guard.sh staged|tracked|paths <p>...|pre-push|install" ;;
  esac
}

main "$@"
