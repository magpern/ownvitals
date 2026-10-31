#!/usr/bin/env bash
# Lints one commit subject with commitlint (commitlint.config.cjs; the versions are pinned here).
# Used by .github/workflows/pr-title.yml for the PR title.
#
#   SUBJECT   the subject line; given through the environment, never as script text
#
# On a pull request the workflow passes the title with " (#<number>)" appended: that is the
# subject the squash commit gets, so a title that only fits
# `header-max-length` (100) without the suffix fails on the PR, not after the merge.
set -euo pipefail

: "${SUBJECT?SUBJECT is required}"
case "$SUBJECT" in
  *$'\n'* | *$'\r'*) echo "::error::the subject must be one line" >&2; exit 1 ;;
esac

printf '%s\n' "$SUBJECT" | npx --yes \
  --package @commitlint/cli@21.2.3 \
  --package @commitlint/config-conventional@21.2.3 \
  -- commitlint --verbose
