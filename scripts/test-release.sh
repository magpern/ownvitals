#!/usr/bin/env bash
# Tests the guards of .github/workflows/release.yml. Run by the `checks` job (#57).
#
#   scripts/test-release.sh [path/to/release.yml]
#
# Static: the trigger, the permissions of each job, `needs`, job and step conditions, action and
# image pins, and no `${{ }}` inside a `run:` script. Dynamic: the `run:` scripts of `verify`,
# `attest` and `release` run under `bash -e` (as Actions runs a step without `shell:`) against a
# scratch git repository (main A-B-C-..., a side branch D off B) with a fake `gh` that records
# its arguments. Nothing here touches the network, a registry or a real tag.
#
# Needs mikefarah yq v4 (read from stdin only), jq, git and python3 >= 3.11, all on the CI runner.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
wf="${1:-$here/../.github/workflows/release.yml}"

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

# ---- static policy ------------------------------------------------------------------------
# expect <name> <jq expression that must be exactly true>
expect() {
  local got
  got="$(jq -r "$2" <<< "$json" 2>&1)" || true
  if [ "$got" = true ]; then report ok "static: $1"; else report FAIL "static: $1 (got: ${got:0:200})"; fi
}
# shellcheck disable=SC2016  # jq programs and GitHub expressions, not shell
{
  expect "the only trigger is a push of v*.*.* tags" '.on == {"push": {"tags": ["v*.*.*"]}}'
  expect "workflow permissions are {}" '.permissions == {}'
  expect "the jobs are verify, image, attest and release" '.jobs | keys == ["attest", "image", "release", "verify"]'
  expect "verify: contents read only" '.jobs.verify.permissions == {"contents": "read"}'
  expect "image: contents read, packages write" '.jobs.image.permissions == {"contents": "read", "packages": "write"}'
  expect "attest: id-token, attestations and packages write" \
    '.jobs.attest.permissions == {"id-token": "write", "attestations": "write", "packages": "write"}'
  expect "release: contents write only" '.jobs.release.permissions == {"contents": "write"}'
  expect "id-token only in attest" '[.jobs | to_entries[] | select(.value.permissions | has("id-token")) | .key] == ["attest"]'
  expect "image needs verify" '[.jobs.image.needs] | flatten == ["verify"]'
  expect "attest needs image" '[.jobs.attest.needs] | flatten == ["image"]'
  expect "release needs image and attest" '[.jobs.release.needs] | flatten | sort == ["attest", "image"]'
  expect "verify runs only in magpern/ownvitals" ".jobs.verify.if == \"github.repository == 'magpern/ownvitals'\""
  expect "no other job has an if" '[.jobs | to_entries[] | select(.key != "verify" and (.value | has("if")))] == []'
  expect "no step has an if" '[.jobs[].steps[] | select(has("if"))] == []'
  expect "no continue-on-error anywhere" '[.. | objects | select(has("continue-on-error"))] == []'
  expect "every job has steps" '[.jobs[] | (.steps | type)] | all(. == "array")'
  expect 'no ${{ }} in any run: script' '[.jobs[].steps[] | .run // empty | select(contains("${{"))] == []'
  expect "every action is pinned by a full commit SHA" \
    '[.jobs[].steps[] | .uses // empty | select(test("^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$") | not)] == []'
  expect "IMAGE is ghcr.io/magpern/ownvitals" '.env == {"IMAGE": "ghcr.io/magpern/ownvitals"}'
  expect "QEMU image pinned by digest, no cache" \
    '[.jobs.image.steps[] | select(.uses // "" | startswith("docker/setup-qemu-action@")) | .with | (.image | test("@sha256:[0-9a-f]{64}$")) and .["cache-image"] == false] == [true]'
  expect "BuildKit image pinned by digest, no cache" \
    '[.jobs.image.steps[] | select(.uses // "" | startswith("docker/setup-buildx-action@")) | .with | (.["driver-opts"] | test("^image=[^ ]+@sha256:[0-9a-f]{64}$")) and .["cache-binary"] == false] == [true]'
  expect "build: no cache, SBOM generator pinned by digest" \
    '[.jobs.image.steps[] | select(.id == "build") | .with | (has("cache-from") or has("cache-to") | not) and (.sbom | test("^generator=[^ ]+@sha256:[0-9a-f]{64}$"))] == [true]'
  expect "metadata never tags latest" '[.jobs.image.steps[] | select(.id == "meta") | .with.flavor] == ["latest=false"]'
  expect "attest checks the digest before attesting it" \
    '.jobs.attest.steps as $s | ([$s[] | .name == "Digest is a sha256 digest"] | index(true)) as $d
     | ([$s[] | .uses // "" | startswith("actions/attest-build-provenance@")] | index(true)) as $a
     | ([$s[] | .name == "Attestation verifies as the deploy checks it"] | index(true)) as $v
     | $d != null and $a != null and $v != null and $d < $a and $a < $v'
  expect "the attestation's subject is the image job's digest, pushed to the registry" \
    '[.jobs.attest.steps[] | select(.uses // "" | startswith("actions/attest-build-provenance@")) | .with
      | .["subject-digest"] == "${{ needs.image.outputs.digest }}" and .["push-to-registry"] == true] == [true]'
}
uses_lines="$(grep -E '^[[:space:]]*-?[[:space:]]*uses:' "$wf" | tr -d '\r')"
check "static: every uses: line carries its version in a comment" \
  test -z "$(grep -Ev '@[0-9a-f]{40} # v[0-9]+\.[0-9]+\.[0-9]+$' <<< "$uses_lines")"
secrets_used="$(grep -Eo 'secrets\.[A-Za-z0-9_]+' "$wf" | sort -u | tr '\n' ' ')"
check "static: the only secret is GITHUB_TOKEN" test "$secrets_used" = "secrets.GITHUB_TOKEN "

# ---- the run: scripts ---------------------------------------------------------------------
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# step_script <job> <step name> <file>: the step's run: script, which must exist exactly once
step_script() {
  jq -r --arg j "$1" --arg n "$2" \
    '[.jobs[$j].steps[] | select(.name == $n) | .run] | if length == 1 then .[0] else error("not found once") end' \
    <<< "$json" > "$3" || { report FAIL "step '$2' of job '$1' not found"; exit 1; }
}
step_script verify "Tag is a SemVer version on main" "$tmp/verify.sh"
step_script attest "Digest is a sha256 digest" "$tmp/digest.sh"
step_script attest "Attestation verifies as the deploy checks it" "$tmp/attest.sh"
step_script release "Create the release" "$tmp/release.sh"
image="$(jq -r '.env.IMAGE' <<< "$json")"

# The fake gh: one NUL-separated argument file per call; exits 1 on call number GH_FAIL_CALL.
fake="$tmp/bin"
mkdir -p "$fake"
cat > "$fake/gh" <<'EOF'
#!/usr/bin/env bash
n=$(($(cat "$GH_LOG/count" 2> /dev/null || echo 0) + 1))
echo "$n" > "$GH_LOG/count"
printf '%s\0' "$@" > "$GH_LOG/call.$n"
[ "$n" != "${GH_FAIL_CALL:-}" ]
EOF
chmod +x "$fake/gh"
log="$tmp/gh-log"

# run_step <script> <dir> [VAR=value ...]: runs it as Actions does (bash -e) in a clean
# environment with the fake gh first on PATH; sets $out (stdout and stderr) and $rc.
base_env=(PATH="$fake:$PATH" HOME="$HOME" LANG=C.UTF-8 GH_LOG="$log")
[ -z "${SYSTEMROOT:-}" ] || base_env+=(SYSTEMROOT="$SYSTEMROOT")
run_step() {
  local script="$1" dir="$2"
  shift 2
  rm -rf "$log"
  mkdir -p "$log"
  rc=0
  out="$(cd "$dir" && env -i "${base_env[@]}" "$@" bash -e "$script" 2>&1)" || rc=$?
}
calls() { cat "$log/count" 2> /dev/null || echo 0; }
# argv_is <call number> <expected arguments...>
argv_is() {
  local n="$1" got=()
  shift
  [ -f "$log/call.$n" ] || return 1
  mapfile -d '' -t got < "$log/call.$n"
  [ "$(printf '%q ' "${got[@]}")" = "$(printf '%q ' "$@")" ]
}

# ---- verify: a scratch repository ---------------------------------------------------------
# Every commit carries a synthetic backend/pyproject.toml; the version check reads it from the
# tagged commit. The working tree ends at version 5.0.0, which no tagged case uses.
origin="$tmp/origin"
git init -q -b main "$origin"
git -C "$origin" config user.email t@example.com
git -C "$origin" config user.name t
git -C "$origin" config core.autocrlf false
proj() { printf '[project]\nname = "synthetic"\nversion = "%s"\n' "$1"; }
# commit_with <message> <pyproject.toml content, or - to delete it>: prints the new commit
commit_with() {
  if [ "$2" = - ]; then
    git -C "$origin" rm -q -f backend/pyproject.toml
  else
    mkdir -p "$origin/backend"
    printf '%s\n' "$2" > "$origin/backend/pyproject.toml"
    git -C "$origin" add backend/pyproject.toml
  fi
  git -C "$origin" commit -q --allow-empty -m "$1"
  git -C "$origin" rev-parse HEAD
}
A="$(commit_with A "$(proj 1.2.3)")"
commit_with B "$(proj 1.2.3)" > /dev/null # D branches off B
git -C "$origin" checkout -q -b side
D="$(commit_with D "$(proj 1.2.3)")"
git -C "$origin" checkout -q main
C="$(commit_with C "$(proj 1.2.3)")"
declare -A at
for v in 0.0.0 10.20.30 1.2.3-rc.1 1.2.3-alpha.0a 1.2.3-0 1.2.3-x-y.1; do
  at[$v]="$(commit_with "$v" "$(proj "$v")")"
done
pep440="$(commit_with pep440 "$(proj 1.2.3rc1)")"
literal="$(commit_with literal "$(printf "[project]\nname = 'synthetic'\nversion = '4.5.6'")")"
tooltable="$(commit_with tooltable "$(printf '[tool.synthetic]\nversion = "7.8.9"\n\n[project]\nname = "synthetic"\nversion = "9.9.9"')")"
dynamic="$(commit_with dynamic "$(printf '[project]\nname = "synthetic"\ndynamic = ["version"]')")"
badtoml="$(commit_with badtoml "$(printf '[project]\nversion = "1.2.3')")"
nofile="$(commit_with nofile -)"
commit_with tip "$(proj 5.0.0)" > /dev/null
clone="$tmp/clone"
nomain="$tmp/nomain"
git clone -q "$origin" "$clone"
git clone -q --single-branch -b side "$origin" "$nomain"

# Each refusal must come from the check meant to refuse it (its message), not a later one.
m_ref="is not a tag ref"
m_semver="is not vMAJOR.MINOR.PATCH"
m_nomain="origin/main is missing"
m_onmain="is not on main"
m_noversion="no project.version"
m_version="does not match version"

# verify_case <name> <ok | expected message> <tag> <commit> [ref] [checkout]
verify_case() {
  local name="$1" want="$2" tag="$3" commit="$4" ref="${5:-refs/tags/$3}" dir="${6:-$clone}"
  run_step "$tmp/verify.sh" "$dir" TAG="$tag" GITHUB_REF="$ref" GITHUB_SHA="$commit"
  if [ "$want" = ok ]; then
    if [ "$rc" -eq 0 ]; then report ok "verify: $name"; else report FAIL "verify: $name (exit $rc: ${out:0:300})"; fi
  elif [ "$rc" -ne 0 ] && grep -qF -- "$want" <<< "$out"; then
    report ok "verify: $name"
  else
    report FAIL "verify: $name (exit $rc, want a refusal saying '$want': ${out:0:300})"
  fi
}

verify_case "accepts v1.2.3 at main's C" ok v1.2.3 "$C"
verify_case "accepts v1.2.3 at an older main commit A" ok v1.2.3 "$A"
for v in "${!at[@]}"; do
  verify_case "accepts v$v" ok "v$v" "${at[$v]}"
done
# shellcheck disable=SC2016  # a literal $(id) in a tag, never expanded
for t in v1.2 v1.2.3.4 1.2.3 V1.2.3 v01.2.3 v1.02.3 v1.2.03 v1.2.3-01 v1.2.3-rc.01 v1.2.3- \
  v1.2.3-rc..1 v1.2.3-.rc v1.2.3-rc. v1.2.3+build v1.2.3-rc+b 'v1.2.3$(id)' 'v1.2.3 ' $'v1.2.3\n' \
  v1.2.3-r_c 'v1.2.3-ré' 'v١.2.3' '' v; do
  verify_case "refuses tag $(printf '%q' "$t")" "$m_semver" "$t" "$C"
done
verify_case "refuses a side-branch commit" "$m_onmain" v1.2.3 "$D"
verify_case "refuses an unknown commit" "$m_onmain" v1.2.3 0000000000000000000000000000000000000001
verify_case "refuses a branch ref" "$m_ref" v1.2.3 "$C" refs/heads/v1.2.3
verify_case "refuses a ref that is another tag" "$m_ref" v1.2.3 "$C" refs/tags/v9.9.9
verify_case "refuses a checkout without origin/main" "$m_nomain" v1.2.3 "$D" "" "$nomain"
# The version check (backend/pyproject.toml at the tagged commit).
verify_case "refuses v1.2.4 at version 1.2.3" "$m_version" v1.2.4 "$C"
verify_case "refuses v1.2.3-rc.1 at version 1.2.3" "$m_version" v1.2.3-rc.1 "$C"
verify_case "refuses v1.2.3 at version 1.2.3-rc.1" "$m_version" v1.2.3 "${at[1.2.3-rc.1]}"
verify_case "refuses v1.2.3-rc.1 at the PEP 440 spelling 1.2.3rc1" "$m_version" v1.2.3-rc.1 "$pep440"
verify_case "refuses v5.0.0 (the working tree, not commit C)" "$m_version" v5.0.0 "$C"
verify_case "accepts a TOML literal string version" ok v4.5.6 "$literal"
verify_case "reads [project], not another table's version" ok v9.9.9 "$tooltable"
verify_case "refuses another table's version" "$m_version" v7.8.9 "$tooltable"
verify_case "refuses a dynamic version" "$m_noversion" v1.2.3 "$dynamic"
verify_case "refuses a malformed pyproject.toml" "$m_noversion" v1.2.3 "$badtoml"
verify_case "refuses a commit without backend/pyproject.toml" "$m_noversion" v1.2.3 "$nofile"
# python3 -I: the step runs at the repository root, where `python3 -c` would put the current
# directory first on sys.path. A tomllib.py planted there (any commit can add one) must not be
# imported: it would answer the tag's version and leave a marker.
planted="$tmp/planted"
git clone -q "$origin" "$planted"
marker="$tmp/planted-imported"
py_marker="$(cygpath -m "$marker" 2> /dev/null || printf '%s' "$marker")" # Git Bash: C:/...
cat > "$planted/tomllib.py" << EOF
open("$py_marker", "w").close()
def load(_):
    return {"project": {"version": "6.6.6"}}
EOF
verify_case "ignores a planted tomllib.py: v1.2.3 at version 1.2.3" ok v1.2.3 "$C" "" "$planted"
verify_case "ignores a planted tomllib.py: v6.6.6 refused" "$m_version" v6.6.6 "$C" "" "$planted"
check "verify: the planted tomllib.py was never imported" test ! -e "$marker"

# ---- attest: the digest and the self-verification -----------------------------------------
good="sha256:$(printf '%064d' 0 | tr 0 a)"
run_step "$tmp/digest.sh" "$tmp" DIGEST="$good"
check "digest: accepts $good" test "$rc" -eq 0
for bad in '' sha256: "sha256:$(printf '%064d' 0 | tr 0 A)" "${good%a}" "sha512:${good#sha256:}" "$good"$'\n' \
  "${good}0" "sha256:$(printf '%064d' 0 | tr 0 z)" " $good" "$good " "SHA256:${good#sha256:}"; do
  run_step "$tmp/digest.sh" "$tmp" DIGEST="$bad"
  check "digest: refuses $(printf '%q' "$bad")" test "$rc" -ne 0
done

sha="$C"
policy=(--repo magpern/ownvitals
  --cert-identity "https://github.com/magpern/ownvitals/.github/workflows/release.yml@refs/tags/v1.2.3"
  --cert-oidc-issuer https://token.actions.githubusercontent.com
  --source-ref refs/tags/v1.2.3 --source-digest "$sha" --deny-self-hosted-runners)
attest_env=(IMAGE="$image" DIGEST="$good" TAG=v1.2.3 GITHUB_SHA="$sha" GH_TOKEN=synthetic)
run_step "$tmp/attest.sh" "$tmp" "${attest_env[@]}"
check "attest: verification passes with gh passing" test "$rc" -eq 0
check "attest: exactly two gh calls" test "$(calls)" -eq 2
check "attest: call 1 is the deploy's policy, from the API" \
  argv_is 1 attestation verify "oci://$image@$good" "${policy[@]}"
check "attest: call 2 is the same policy, from the registry" \
  argv_is 2 attestation verify "oci://$image@$good" "${policy[@]}" --bundle-from-oci
for n in 1 2; do
  run_step "$tmp/attest.sh" "$tmp" "${attest_env[@]}" GH_FAIL_CALL="$n"
  check "attest: a failing verification (call $n) fails the step" test "$rc" -ne 0
done

# ---- release ------------------------------------------------------------------------------
for tag in v1.2.3 v1.2.3-rc.1; do
  run_step "$tmp/release.sh" "$tmp" IMAGE="$image" DIGEST="$good" TAG="$tag" GITHUB_SHA="$sha" \
    GH_REPO=magpern/ownvitals GH_TOKEN=synthetic
  notes="Image: $image@$good

Check its build provenance before deploying it:

    gh attestation verify oci://$image@$good --repo magpern/ownvitals --cert-identity https://github.com/magpern/ownvitals/.github/workflows/release.yml@refs/tags/$tag --source-ref refs/tags/$tag --source-digest $sha --deny-self-hosted-runners
"
  want=(release create "$tag" --verify-tag --generate-notes --notes "$notes")
  [[ $tag == *-* ]] && want+=(--prerelease)
  check "release $tag: one gh call" test "$rc" -eq 0 -a "$(calls)" -eq 1
  check "release $tag: arguments and notes" argv_is 1 "${want[@]}"
done
run_step "$tmp/release.sh" "$tmp" IMAGE="$image" DIGEST="$good" TAG=v1.2.3 GITHUB_SHA="$sha" \
  GH_REPO=magpern/ownvitals GH_TOKEN=synthetic GH_FAIL_CALL=1
check "release: a failing gh fails the step" test "$rc" -ne 0

[ "$failures" -eq 0 ] || { echo "$failures test(s) failed"; exit 1; }
echo "all tests passed"
