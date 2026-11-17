#!/usr/bin/env bash
# Tests scripts/npm-audit.sh with synthetic audit JSON (no network, no npm): a listed ID passes,
# an unlisted high or critical fails, an expired entry fails, a moderate passes, a listed ID
# excuses only itself, a malformed allowlist or unreadable output fails. Run by the `checks` job.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/npm-audit.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/pkg"

failures=0
# expect <ok|fail> <name> : runs the script against $work/audit.json and $work/ignore.
expect() {
  local want="$1" name="$2" rc=0
  NPM_AUDIT_JSON_FILE="$work/audit.json" NPM_AUDIT_IGNORE_FILE="$work/ignore" \
    NPM_AUDIT_TODAY="${TODAY:-2026-11-17}" "$script" "$work/pkg" >"$work/out" 2>&1 || rc=$?
  if { [ "$want" = ok ] && [ "$rc" = 0 ]; } || { [ "$want" = fail ] && [ "$rc" != 0 ]; }; then
    echo "ok   $name"
  else
    echo "FAIL $name (exit $rc)"; sed 's/^/     /' "$work/out"; failures=$((failures + 1))
  fi
}

# audit <severity> <GHSA id>...: npm audit --json shape, one advisory per id plus a package that
# only names the advisory's package in via (as vite does for picomatch).
audit() {
  local sev="$1"; shift
  local objs="" id
  for id in "$@"; do
    objs="$objs{\"source\":1,\"name\":\"pkg-$id\",\"severity\":\"$sev\",\"url\":\"https://github.com/advisories/$id\"},"
  done
  printf '{"vulnerabilities":{"leaf":{"severity":"%s","via":[%s"other"]},"vite":{"severity":"%s","via":["leaf"]}}}' \
    "$sev" "$objs" "$sev" >"$work/audit.json"
}

A=GHSA-aaaa-bbbb-cccc
B=GHSA-dddd-eeee-ffff
printf '# reason\n%s exp:2027-01-31\n' "$A" >"$work/ignore"

audit high "$A";            expect ok   "listed high advisory passes"
audit high "$B";            expect fail "unlisted high advisory fails"
audit critical "$B";        expect fail "unlisted critical advisory fails"
audit high "$A" "$B";       expect fail "a listed ID does not excuse a second high advisory"
audit moderate "$B";        expect ok   "unlisted moderate advisory passes"
audit low "$B";             expect ok   "unlisted low advisory passes"
printf '{"vulnerabilities":{}}' >"$work/audit.json"; expect ok "no findings passes"

audit high "$A"
TODAY=2027-01-31 expect ok   "entry still valid on its expiry date"
TODAY=2027-02-01 expect fail "expired entry fails (advisory present)"
printf '{"vulnerabilities":{}}' >"$work/audit.json"
TODAY=2027-02-01 expect fail "expired entry fails (advisory gone)"

audit high "$A"
printf '%s\n' "$A" >"$work/ignore";                    expect fail "entry without expiry is malformed"
printf '%s exp:soon\n' "$A" >"$work/ignore";           expect fail "entry with a bad expiry is malformed"
printf '%s exp:2027-01-31\n' "$A" >"$work/ignore"
printf 'not json' >"$work/audit.json";                 expect fail "unreadable audit output fails"
printf '{"error":{"code":"ENOAUDIT"}}' >"$work/audit.json"; expect fail "npm audit error output fails"

# Third field on an allowlist line, impossible date, wrong URL shape.
audit high "$A"
printf '%s exp:2027-01-31 extra\n' "$A" >"$work/ignore"; expect fail "allowlist line with a third field fails"
printf '%s exp:2027-13-99\n' "$A" >"$work/ignore";       expect fail "impossible expiry date fails"
printf '%s exp:2027-01-31\n' "$A" >"$work/ignore"
printf '{"vulnerabilities":{"l":{"severity":"high","via":[{"source":1,"name":"p","severity":"high","url":"https://example.com/x/%s-evil"}]}}}' "$A" >"$work/audit.json"
expect fail "GHSA not at the end of an advisories URL is not matched"
printf '{"vulnerabilities":{"l":{"severity":"high","via":[{"source":1,"name":"p","severity":"high","url":"https://github.com/advisories/%s"}]}}}' "$A" >"$work/audit.json"
expect ok   "advisories URL path segment is matched"

# High package whose via entries are all strings (no advisory to match) fails; counts alone too.
printf '{"vulnerabilities":{"x":{"severity":"high","via":["y"]}},"metadata":{"vulnerabilities":{"high":1,"critical":0}}}' >"$work/audit.json"
expect fail "high package with only string via entries fails"
printf '{"vulnerabilities":{},"metadata":{"vulnerabilities":{"high":0,"critical":1}}}' >"$work/audit.json"
expect fail "metadata count above 0 with no advisory fails"

# Empty, whitespace and keyless output fail; so does a missing npm.
: >"$work/audit.json";                          expect fail "empty audit output fails"
printf ' \n\t\n' >"$work/audit.json";            expect fail "whitespace audit output fails"
printf '{"metadata":{}}' >"$work/audit.json";   expect fail "output without a vulnerabilities key fails"
printf '[]' >"$work/audit.json";                expect fail "non-object output fails"
mkdir "$work/nonpm"
for t in jq date dirname cat; do ln -s "$(command -v "$t")" "$work/nonpm/$t"; done
printf '%s exp:2027-01-31\n' "$A" >"$work/ignore"
rc=0; PATH="$work/nonpm" NPM_AUDIT_IGNORE_FILE="$work/ignore" "$(command -v bash)" "$script" "$work/pkg" >"$work/out" 2>&1 || rc=$?
grep -q "npm is not on PATH" "$work/out" || rc=0
if [ "$rc" != 0 ]; then echo "ok   npm missing from PATH fails"; else echo "FAIL npm missing from PATH"; failures=$((failures + 1)); fi

# Missing directory fails.
rc=0; NPM_AUDIT_JSON_FILE="$work/audit.json" "$script" "$work/nope" >/dev/null 2>&1 || rc=$?
if [ "$rc" != 0 ]; then echo "ok   missing directory fails"; else echo "FAIL missing directory"; failures=$((failures + 1)); fi

# The committed allowlist is well formed and unexpired (catches a bad edit before CI's real audit).
printf '{"vulnerabilities":{}}' >"$work/audit.json"
rc=0; NPM_AUDIT_JSON_FILE="$work/audit.json" "$script" "$work/pkg" >/dev/null 2>&1 || rc=$?
if [ "$rc" = 0 ]; then echo "ok   committed .npm-audit-ignore is well formed and unexpired"; else echo "FAIL committed .npm-audit-ignore"; failures=$((failures + 1)); fi

[ "$failures" = 0 ] || { echo "$failures failure(s)"; exit 1; }
