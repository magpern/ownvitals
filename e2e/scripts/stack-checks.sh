#!/usr/bin/env bash
# Checks on the running e2e stack (e2e/scripts/stack-up.sh), from the repository root:
#   - web is ready and serves the built single-page app, including a deep link
#   - an unknown /api path is a 404, not the single-page app
#   - web and the worker run as the non-root user
#   - no secret value appears in `docker inspect` of any container of the stack
# Prints what failed, never a secret.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

compose=(docker compose -f compose.yaml -f compose.e2e.yaml)
port="$(sed -n 's/^WEB_PORT=//p' .env)"
base="http://127.0.0.1:${port:-8000}"
failures=0
fail() { echo "FAIL $*"; failures=$((failures + 1)); }
ok() { echo "ok   $*"; }

if [ "$(curl -s -o /dev/null -w '%{http_code}' "$base/api/ready")" = 200 ]; then ok "/api/ready is 200"; else fail "/api/ready is not 200"; fi

for path in / /signin /settings/security; do
  if curl -fsS "$base$path" | grep -q '<div id="root"></div>'; then ok "$path serves the single-page app"; else fail "$path does not serve the single-page app"; fi
done
asset="$(curl -fsS "$base/" | sed -n 's/.*src="\(\/assets\/[^"]*\.js\)".*/\1/p' | head -n 1)"
if [ -n "$asset" ] && [ "$(curl -s -o /dev/null -w '%{http_code}' "$base$asset")" = 200 ]; then ok "the built script $asset is served"; else fail "no built script is served"; fi
if curl -fsS "$base/api/does-not-exist" > /dev/null 2>&1; then fail "an unknown /api path answered 200"; else ok "an unknown /api path is not the single-page app"; fi

for service in web worker; do
  uid="$("${compose[@]}" exec -T "$service" id -u | tr -d '\r')"
  if [ "$uid" != 0 ] && [ -n "$uid" ]; then ok "$service runs as uid $uid"; else fail "$service runs as root"; fi
done

# Every container of the stack, every file under secrets/: its content must not occur in the
# inspect output (environment, command, labels, mounts). Short values would match by accident.
inspect="$(mktemp)"
trap 'rm -f "$inspect"' EXIT
ids="$("${compose[@]}" ps -aq)"
[ -n "$ids" ] || { echo "FAIL no containers"; exit 1; }
# shellcheck disable=SC2086  # one id per word
docker inspect $ids > "$inspect"
checked=0
for file in secrets/*; do
  [ -f "$file" ] || continue
  value="$(cat "$file")"
  [ "${#value}" -ge 16 ] || continue
  checked=$((checked + 1))
  if grep -qF -- "$value" "$inspect"; then fail "the value of $file is in docker inspect"; fi
done
[ "$checked" -ge 6 ] && ok "$checked secret values are absent from docker inspect" || fail "only $checked secret files were checked"

[ "$failures" -eq 0 ] || { echo "$failures check(s) failed"; exit 1; }
echo "stack checks passed"
