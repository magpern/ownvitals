#!/usr/bin/env bash
# Stops the e2e stack and removes its volumes and generated certificates.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
docker compose -f compose.yaml -f compose.e2e.yaml down -v --remove-orphans
rm -rf e2e/.certs
