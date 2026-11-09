#!/usr/bin/env bash
# The one source of the backend test shard count: the `shard` list of the `backend-tests` matrix
# in the CI workflow. Prints N, the number of shards, after checking that the matrix is exactly
# `shard: [1, 2, ..., N]`: no other axis, no include or exclude, no repeated or missing shard. The
# shard check (`backend`) and every shard (`backend-tests`) take N from here, so they can't
# disagree. Tested by scripts/test-ci-gate.sh.
#
#   scripts/ci-shards.sh [workflow file]     # default .github/workflows/ci.yml
set -euo pipefail

workflow="${1:-.github/workflows/ci.yml}"
die() { echo "::error::$*" >&2; exit 1; }

[ -f "$workflow" ] || die "no workflow file $workflow"
matrix="$(yq -o=json -I=0 '.jobs["backend-tests"].strategy.matrix' "$workflow")" \
  || die "could not read the backend-tests matrix"

# shellcheck disable=SC2016  # jq program, not shell
total="$(jq -r '
  if type != "object" then error("the backend-tests matrix must be a mapping") else . end
  | if keys != ["shard"] then error("the backend-tests matrix must have the one axis shard, and no include or exclude") else . end
  | .shard
  | if type != "array" or length == 0 then error("shard must be a non-empty list") else . end
  | if . != [range(1; length + 1)] then error("shard must be the list 1 to N") else length end
' <<< "$matrix" 2>&1)" || die "${total#jq: error (at <stdin>:*): }"

[[ $total =~ ^[1-9][0-9]*$ ]] || die "bad shard count: $total"
echo "$total"
