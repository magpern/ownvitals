#!/usr/bin/env bash
# The decision of the `ci` gate job (the one required check besides `pr-title`), kept here so it
# can be tested (scripts/test-ci-gate.sh). Ported from classbeat (d8060510).
#
#   NEEDS    toJSON(needs) of the gate job
#   RESULTS  join(needs.*.result, ' ')
#
# `changes` and `checks` must have succeeded. Every other job must have succeeded, or have
# been skipped while the `changes` output that gates it is "false". A job that is skipped,
# cancelled or failed while its filter is true fails the gate, and so does a missing job.
#
# A new job is added in three places together: `needs` of the `ci` job, JOBS and FILTERS below,
# and the tests.
#
# Fails closed. jq validates the values, not only the shape, before it decides anything: exactly
# one JSON document; exactly the expected jobs; every result one of success, failure, cancelled
# or skipped; every filter output the string "true" or "false". Only then does it emit one
# verdict line per job, built from those validated values. Bash never splits or interprets the
# raw input.
set -euo pipefail

: "${NEEDS:?NEEDS is required}"
RESULTS="${RESULTS:-}"
echo "results: $RESULTS"

# The jobs of `needs` (sorted) and the `changes` output that gates each optional one.
JOBS='["backend", "changes", "checks", "frontend"]'
FILTERS='{"backend": "backend", "frontend": "frontend"}'

die() { echo "::error::$*"; exit 1; }

# toJSON(needs) is an object that starts with a brace; anything before it (a BOM, blanks) is
# not what the workflow produces.
[[ $NEEDS == "{"* ]] || die "NEEDS does not start with a JSON object"

# shellcheck disable=SC2016  # jq program, not shell
program='
  def fail($m): error($m);
  [inputs] as $in
  | if ($in | length) != 1 then fail("NEEDS must be exactly one JSON document") else $in[0] end
  | . as $n
  | if type != "object" then fail("NEEDS must be a JSON object") else . end
  | if (keys | map(select(test("^[A-Za-z0-9_-]+$") | not)) | length) > 0
      then fail("a job name has unexpected characters") else . end
  | if keys != $jobs then fail("NEEDS must hold exactly the jobs \($jobs | join(", "))") else . end
  | if all(.[]; type == "object"
        and (.result | type) == "string"
        and (.result | IN("success", "failure", "cancelled", "skipped")))
      then . else fail("every job needs a result of success, failure, cancelled or skipped") end
  | if (.changes.outputs | type) != "object" then fail("changes has no outputs object") else . end
  | if ($filter | [.[]] | unique
        | all(. as $f | $n.changes.outputs[$f] | type == "string" and IN("true", "false")))
      then . else fail("every filter output must be the string true or false") end
  | to_entries[]
  | .key as $j
  | .value.result as $r
  | if $r == "success" then "ok \($j) succeeded"
    elif $j == "changes" or $j == "checks" then "err \($j) finished as \($r)"
    elif $r == "skipped" and $n.changes.outputs[$filter[$j]] == "false"
      then "ok \($j) skipped: its filter (\($filter[$j])) is false"
    else "err \($j) finished as \($r) (filter \($filter[$j]) is \($n.changes.outputs[$filter[$j]]))"
    end'

# A failing or erroring jq fails the gate (plain assignment, so set -e sees its exit status).
if ! verdict="$(jq -r -n --argjson jobs "$JOBS" --argjson filter "$FILTERS" "$program" <<< "$NEEDS" 2>&1)"; then
  echo "$verdict" | tr -d '\r'
  die "NEEDS is malformed"
fi
verdict="$(tr -d '\r' <<< "$verdict")"

# An empty or short result list must not pass vacuously.
want="$(jq -r 'length' <<< "$JOBS")"
got="$(wc -w <<< "$RESULTS")"
[ "$got" -eq "$want" ] || die "expected $want results, got $got"

fail=0
while IFS= read -r line; do
  case "$line" in
    "ok "*) echo "${line#ok }" ;;
    "err "*) echo "::error::${line#err }"; fail=1 ;;
    *) echo "::error::unexpected verdict line"; fail=1 ;;
  esac
done <<< "$verdict"

exit "$fail"
