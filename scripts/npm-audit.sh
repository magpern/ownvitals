#!/usr/bin/env bash
# npm audit with a dated allowlist (npm audit has no ignore flag). Usage: npm-audit.sh <dir>
# Runs `npm audit --json` in <dir>, fails on every high or critical advisory whose GHSA ID is not
# listed in .npm-audit-ignore, and on every listed entry whose exp:YYYY-MM-DD date has passed
# (an expired entry fails whether or not the advisory still appears). A listed ID excuses only
# that ID. Moderate and low advisories never fail. Tested by scripts/test-npm-audit.sh.
# Test hooks: NPM_AUDIT_JSON_FILE (audit JSON instead of running npm), NPM_AUDIT_TODAY (YYYY-MM-DD).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dir="${1:?usage: npm-audit.sh <dir>}"
ignore="${NPM_AUDIT_IGNORE_FILE:-$here/../.npm-audit-ignore}"
today="${NPM_AUDIT_TODAY:-$(date -u +%F)}"

[ -d "$dir" ] || { echo "npm-audit: directory $dir is missing" >&2; exit 1; }
[ -f "$ignore" ] || { echo "npm-audit: allowlist $ignore is missing" >&2; exit 1; }

# Allowlist: "<ID> exp:<date>" lines; comments and blank lines are skipped.
failed=0
listed=()
while IFS= read -r line; do
  line="${line%%#*}"
  [ -n "${line//[[:space:]]/}" ] || continue
  read -r id exp extra <<<"$line"
  if [[ ! "$id" =~ ^GHSA(-[0-9a-z]{4}){3}$ ]] || [[ ! "${exp:-}" =~ ^exp:[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || [ -n "${extra:-}" ]; then
    echo "npm-audit: malformed allowlist line (want 'GHSA-xxxx-xxxx-xxxx exp:YYYY-MM-DD'): $line" >&2
    failed=1
    continue
  fi
  exp="${exp#exp:}"
  if [[ "$today" > "$exp" ]]; then
    echo "npm-audit: allowlist entry $id expired on $exp; remove it or renew it with a reason" >&2
    failed=1
    continue
  fi
  listed+=("$id")
done <"$ignore"

if [ -n "${NPM_AUDIT_JSON_FILE:-}" ]; then
  json="$(cat "$NPM_AUDIT_JSON_FILE")"
else
  # npm audit exits non-zero when it finds anything; the verdict here comes from the JSON.
  json="$(cd "$dir" && npm audit --json || true)"
fi

# Every advisory object (via entries that are strings only name another vulnerable package).
if ! found="$(jq -r '
    if has("vulnerabilities") | not then error("no vulnerabilities key (npm audit failed?)") else . end
    | [.vulnerabilities[].via[]? | select(type == "object")
       | select(.severity == "high" or .severity == "critical")
       | {id: ((.url // "") | capture("(?<g>GHSA(-[0-9a-z]{4}){3})").g? // ("source-" + (.source | tostring))),
          pkg: .name, sev: .severity}]
    | unique | .[] | "\(.id) \(.pkg) \(.sev)"' <<<"$json")"; then
  echo "npm-audit: could not read the audit output for $dir" >&2
  exit 1
fi

while read -r id pkg sev; do
  [ -n "$id" ] || continue
  excused=0
  for l in "${listed[@]:-}"; do [ "$l" = "$id" ] && excused=1; done
  if [ "$excused" = 1 ]; then
    echo "npm-audit: $dir: $id ($pkg, $sev) excused by the allowlist"
  else
    echo "npm-audit: $dir: $id ($pkg, $sev) is not allowlisted" >&2
    failed=1
  fi
done <<<"$found"

[ "$failed" = 0 ] && echo "npm-audit: $dir ok"
exit "$failed"
