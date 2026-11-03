#!/usr/bin/env bash
# Prepares what the e2e overlay (compose.e2e.yaml) mounts, from the repository root:
#
#   secrets/oauth_google_client_secret   a random value of this run, not a real Google secret
#   e2e/.certs/oidc.{key,pem}            the certificate fake-oidc serves for Google's two
#                                        back-channel host names, from a throwaway CA
#   e2e/.certs/cacert.pem                web's trust bundle (certifi's, read from the image
#                                        itself) plus that CA: web verifies TLS against certifi
#                                        only, so the CA has to be in its own bundle
#
# Usage: e2e/scripts/stack-prepare.sh <web image>   (for example ownvitals:local)
# Everything is regenerated on each run and git-ignored. Nothing here touches a real account.
set -euo pipefail

image="${1:?usage: stack-prepare.sh <web image>}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"
certs="e2e/.certs"
rm -rf "$certs"
mkdir -p "$certs" secrets

# Mode 0644 like the other secrets: the containers' users (not root) read the bind mounts.
printf '%s' "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" > secrets/oauth_google_client_secret
chmod 644 secrets/oauth_google_client_secret

openssl req -x509 -newkey rsa:2048 -nodes -days 2 -sha256 \
  -subj "/CN=ownvitals e2e CA (synthetic)" \
  -keyout "$certs/ca.key" -out "$certs/ca.pem" 2> /dev/null

openssl req -newkey rsa:2048 -nodes -sha256 \
  -subj "/CN=oauth2.googleapis.com" \
  -keyout "$certs/oidc.key" -out "$certs/oidc.csr" 2> /dev/null
printf 'subjectAltName=DNS:oauth2.googleapis.com,DNS:www.googleapis.com\nbasicConstraints=CA:FALSE\n' \
  > "$certs/oidc.ext"
openssl x509 -req -in "$certs/oidc.csr" -CA "$certs/ca.pem" -CAkey "$certs/ca.key" \
  -CAcreateserial -days 2 -sha256 -extfile "$certs/oidc.ext" -out "$certs/oidc.pem" 2> /dev/null
chmod 644 "$certs/oidc.key" "$certs/oidc.pem"

# certifi's bundle as the image has it, then the CA. The path is asked of the image, not guessed.
bundle="$(docker run --rm --entrypoint python "$image" -c 'import certifi; print(certifi.where())')"
docker run --rm --entrypoint cat "$image" "$bundle" > "$certs/cacert.pem"
printf '\n' >> "$certs/cacert.pem"
cat "$certs/ca.pem" >> "$certs/cacert.pem"
chmod 644 "$certs/cacert.pem"
rm -f "$certs/ca.key" "$certs/ca.srl" "$certs/oidc.csr" "$certs/oidc.ext"

# compose.e2e.yaml mounts the bundle over this path; fail early if the image moved it.
expected="/app/.venv/lib/python3.12/site-packages/certifi/cacert.pem"
[ "$bundle" = "$expected" ] || {
  echo "stack-prepare: certifi's bundle is at $bundle, compose.e2e.yaml mounts $expected" >&2
  exit 1
}
echo "e2e certificates ready"
