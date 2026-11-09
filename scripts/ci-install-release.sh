#!/usr/bin/env bash
# CI: install one checksum-verified release binary (gitleaks, Trivy) from a GitHub release tarball.
#   ci-install-release.sh <name> <sha256> <url> <cache-dir> <dest-dir>
# The tarball lives in <cache-dir>/<name>.tgz, the folder the workflow's actions/cache step
# restores and saves. A hit skips the download; either way the SHA-256 is checked before the
# tarball is unpacked, so a restored (possibly tampered) file is as verified as a fresh one and a
# mismatch fails the job. A cache is never trusted by its key alone. The download retries on any
# error (GitHub's release CDN answered 504 on 2026-11-09, which failed the checks job of PR #273). CURL_RETRY_DELAY (default 3 s)
# is for the test, scripts/test-ci-install-release.sh.
set -euo pipefail
[ "$#" -eq 5 ] || { echo "usage: $0 <name> <sha256> <url> <cache-dir> <dest-dir>" >&2; exit 2; }
name="$1" sha256="$2" url="$3" cache="$4" dest="$5"
tgz="$cache/$name.tgz"
mkdir -p "$cache" "$dest"

if [ -f "$tgz" ]; then
  echo "$name: using the cached tarball"
else
  echo "$name: downloading"
  curl -fSL --retry 5 --retry-all-errors --retry-delay "${CURL_RETRY_DELAY:-3}" -o "$tgz.part" "$url"
  mv "$tgz.part" "$tgz"
fi

# A failed check leaves the file in place but fails the job before anything is unpacked, so the
# post-job cache save does not run (a job that failed saves no cache).
if ! echo "$sha256  $tgz" | sha256sum --check --status; then
  echo "::error::$name: SHA-256 of $tgz does not match the pinned value"
  rm -f "$tgz"
  exit 1
fi
tar -xzf "$tgz" -C "$dest" "$name"
