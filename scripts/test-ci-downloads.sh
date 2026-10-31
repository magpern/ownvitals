#!/usr/bin/env bash
# Tests that the release binaries ci.yml downloads (the gitleaks and Trivy install steps of the
# `checks` job) are checksum-verified before they are unpacked. Run by the `checks` job (#57).
#
#   scripts/test-ci-downloads.sh [path/to/ci.yml]
#
# Each step's `run:` script runs under `bash -e` (as Actions runs a step without `shell:`) with a
# fake curl that serves a synthetic archive, never the network. The right SHA-256 must unpack the
# binary; a wrong or empty one, or a failed download, must fail the step with nothing unpacked.
#
# Needs mikefarah yq v4 (read from stdin only), jq, sha256sum and tar, all on the CI runner.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
wf="${1:-$here/../.github/workflows/ci.yml}"

failures=0
report() { # report <ok|FAIL> <message>
  echo "$1 $2"
  [ "$1" = ok ] || failures=$((failures + 1))
}
check() { # check <name> <command...>: ok when the command succeeds
  local name="$1"
  shift
  if "$@"; then report ok "$name"; else report FAIL "$name"; fi
}

yqv="$(yq --version)"
grep -Eq 'mikefarah/yq.* v4\.' <<< "$yqv" || { echo "FAIL need mikefarah yq v4, got: $yqv"; exit 1; }
json="$(yq -o=json '.' < "$wf")"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# The fake curl: writes $FAKE_ARCHIVE to the -o file, logs the URL; FAKE_CURL_FAIL=1 fails it.
fake="$tmp/bin"
mkdir -p "$fake"
cat > "$fake/curl" <<'EOF'
#!/usr/bin/env bash
out=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    https://*) echo "$1" >> "$FAKE_CURL_LOG" ;;
  esac
  shift
done
[ -z "${FAKE_CURL_FAIL:-}" ] || exit 22
cp "$FAKE_ARCHIVE" "$out"
EOF
chmod +x "$fake/curl"

# download_case <tool> <step name> <release URL prefix>
download_case() {
  local tool="$1" name="$2" url="$3" script="$tmp/$1.sh" version digest archive right wrong
  local -a env_base
  # The step, exactly once, in the checks job.
  if ! jq -r --arg n "$name" \
    '[.jobs.checks.steps[] | select(.name == $n)] | if length == 1 then .[0].run else error("not found once") end' \
    <<< "$json" > "$script"; then
    report FAIL "$tool: step '$name' not found once in the checks job"
    return
  fi
  version="$(jq -r --arg n "$name" '.jobs.checks.steps[] | select(.name == $n) | .env.VERSION // ""' <<< "$json")"
  digest="$(jq -r --arg n "$name" '.jobs.checks.steps[] | select(.name == $n) | .env.SHA256 // ""' <<< "$json")"
  check "$tool: VERSION is X.Y.Z" grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+' <<< "$version"
  check "$tool: SHA256 is 64 lower-case hex" grep -Eqx '[0-9a-f]{64}' <<< "$digest"

  # A synthetic archive holding the binary; its own SHA-256 and one of other content.
  mkdir -p "$tmp/payload-$tool" "$tmp/other-$tool"
  echo "synthetic $tool" > "$tmp/payload-$tool/$tool"
  echo "other" > "$tmp/other-$tool/$tool"
  archive="$tmp/$tool.tgz"
  tar -czf "$archive" -C "$tmp/payload-$tool" "$tool"
  tar -czf "$tmp/$tool-other.tgz" -C "$tmp/other-$tool" "$tool"
  right="$(sha256sum "$archive" | cut -d' ' -f1)"
  wrong="$(sha256sum "$tmp/$tool-other.tgz" | cut -d' ' -f1)"

  env_base=(PATH="$fake:$PATH" HOME="$HOME" FAKE_ARCHIVE="$archive" FAKE_CURL_LOG="$tmp/curl.log" VERSION="$version")
  [ -z "${SYSTEMROOT:-}" ] || env_base+=(SYSTEMROOT="$SYSTEMROOT")
  # run <case name> <want: unpacked|refused> [VAR=value ...]
  run() {
    local cname="$1" want="$2" rt="$tmp/runner-temp" rc=0
    shift 2
    rm -rf "$rt" "$tmp/curl.log"
    mkdir -p "$rt"
    env -i "${env_base[@]}" RUNNER_TEMP="$rt" "$@" bash -e "$script" > /dev/null 2>&1 || rc=$?
    if [ "$want" = unpacked ]; then
      if [ "$rc" -eq 0 ] && [ "$(cat "$rt/$tool" 2> /dev/null)" = "synthetic $tool" ]; then
        report ok "$tool: $cname"
      else
        report FAIL "$tool: $cname (exit $rc, want the binary unpacked)"
      fi
    elif [ "$rc" -ne 0 ] && [ ! -e "$rt/$tool" ]; then
      report ok "$tool: $cname"
    else
      report FAIL "$tool: $cname (exit $rc, want a failure with nothing unpacked)"
    fi
  }
  run "the right SHA-256 unpacks it" unpacked SHA256="$right"
  check "$tool: downloads ${url}v$version/ over https" grep -q "^${url}v$version/" "$tmp/curl.log"
  run "a wrong SHA-256 fails the step" refused SHA256="$wrong"
  run "an empty SHA-256 fails the step" refused SHA256=
  run "the pinned SHA-256 (not this archive's) fails the step" refused SHA256="$digest"
  run "a failed download fails the step" refused SHA256="$right" FAKE_CURL_FAIL=1
}

download_case gitleaks "Install gitleaks" "https://github.com/gitleaks/gitleaks/releases/download/"
download_case trivy "Install Trivy" "https://github.com/aquasecurity/trivy/releases/download/"

[ "$failures" -eq 0 ] || { echo "$failures test(s) failed"; exit 1; }
echo "all tests passed"
