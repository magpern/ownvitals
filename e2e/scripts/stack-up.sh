#!/usr/bin/env bash
# Builds and starts the e2e stack (compose.yaml + compose.e2e.yaml) and waits until web is ready.
# Run from anywhere. Needs Docker, openssl and curl. Stop it with e2e/scripts/stack-down.sh.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

"$root/scripts/worktree.sh" init > /dev/null
compose=(docker compose -f compose.yaml -f compose.e2e.yaml)

# Compose loads every secret file at start-up; the real value is written by stack-prepare.sh.
[ -f secrets/oauth_google_client_secret ] || : > secrets/oauth_google_client_secret
"${compose[@]}" build web fake-oidc fake-withings
image="$(sed -n 's/^COMPOSE_PROJECT_NAME=//p' .env):local"
"$root/e2e/scripts/stack-prepare.sh" "$image"

"${compose[@]}" up -d
port="$(sed -n 's/^WEB_PORT=//p' .env)"
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${port:-8000}/api/ready" > /dev/null 2>&1; then
    echo "e2e stack ready on http://127.0.0.1:${port:-8000}"
    exit 0
  fi
  sleep 2
done
"${compose[@]}" ps
"${compose[@]}" logs --tail 40 web fake-oidc fake-withings migrate db-functions 2>&1 | sed 's/\(token\|secret\)=[^ ]*/\1=<redacted>/g' >&2
echo "stack-up: web did not become ready" >&2
exit 1
