#!/usr/bin/env bash
# Decides which CI jobs a change needs. Writes backend/frontend/stack/all (true|false) to
# $GITHUB_OUTPUT, or to stdout when that is unset (local use). Ported from classbeat
# (d8060510) without the merge queue and shard handling.
#
#   EVENT_NAME  github.event_name; pull_request diffs, anything else means "run everything"
#   BASE_SHA    the pull request's base commit
#   HEAD_SHA    the pull request's head commit
#
# When the changed files cannot be determined (no SHA, an unknown commit), everything runs. Local
# try-out:
#   EVENT_NAME=pull_request BASE_SHA=origin/main HEAD_SHA=HEAD scripts/ci-changes.sh
set -euo pipefail

: "${EVENT_NAME:?EVENT_NAME is required}"
out="${GITHUB_OUTPUT:-/dev/stdout}"

# Path filters (extended regexes, matched against each whole path).
re_all='^(\.github/|\.gitattributes$|scripts/(ci-changes|ci-gate|test-ci-gate)\.sh$)'
re_backend='^(backend/|docker/postgres/)'
re_frontend='^frontend/'
re_stack='^((backend|frontend|e2e|docker)/|scripts/worktree\.sh$|Dockerfile$|compose[^/]*\.ya?ml$|\.dockerignore$|\.env\.example$|\.trivyignore$)'

backend=false
frontend=false
stack=false
all=false

if [ "$EVENT_NAME" != pull_request ]; then
  all=true
elif [ -z "${BASE_SHA:-}" ] || [ -z "${HEAD_SHA:-}" ]; then
  echo "::warning::no base or head commit for $EVENT_NAME; running everything" >&2
  all=true
else
  # Three dots: what the branch changed since it left the base, not what the base gained.
  # --no-renames: a moved file counts at its old and its new path. -z and quotePath=false: names
  # are NUL-separated and never quoted, whatever characters they hold.
  list="$(mktemp)"
  trap 'rm -f "$list"' EXIT
  if git -c core.quotePath=false diff --name-only --no-renames -z "${BASE_SHA}...${HEAD_SHA}" > "$list"; then
    echo "changed files:" >&2
    while IFS= read -r -d '' f; do
      printf '  %q\n' "$f" >&2
      [[ $f =~ $re_all ]] && all=true
      [[ $f =~ $re_backend ]] && backend=true
      [[ $f =~ $re_frontend ]] && frontend=true
      [[ $f =~ $re_stack ]] && stack=true
    done < "$list"
  else
    echo "::warning::could not diff ${BASE_SHA}...${HEAD_SHA}; running everything" >&2
    all=true
  fi
fi

if [ "$all" = true ]; then
  backend=true
  frontend=true
  stack=true
fi

{
  echo "backend=$backend"
  echo "frontend=$frontend"
  echo "stack=$stack"
  echo "all=$all"
} >> "$out"
