#!/usr/bin/env bash
# Tests scripts/ci-gate.sh and scripts/ci-changes.sh with sample inputs, and scripts/ci-uv-version.sh
# on scratch trees. Run by the `checks` job.
# Ported from classbeat (d8060510) for the jobs changes, checks, backend and frontend.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

failures=0
report() { # report <ok|FAIL> <message>
  echo "$1 $2"
  [ "$1" = ok ] || failures=$((failures + 1))
}

# needs <changes> <checks> <backend> <frontend> <backend filter> <frontend filter> <stack filter>
needs() {
  jq -nc --arg ch "$1" --arg ck "$2" --arg b "$3" --arg f "$4" \
    --arg fb "$5" --arg ff "$6" --arg fs "$7" '{
      changes: {result: $ch, outputs: {backend: $fb, frontend: $ff, stack: $fs, all: "false"}},
      checks: {result: $ck, outputs: {}},
      backend: {result: $b, outputs: {}},
      frontend: {result: $f, outputs: {}}}'
}

# gate <name> <expected exit 0|1> <needs json> [results override]
gate() {
  local name="$1" want="$2" json="$3" results got=0
  results="${4-$(jq -r '[.[].result] | join(" ")' <<< "$json")}"
  NEEDS="$json" RESULTS="$results" "$here/ci-gate.sh" > /dev/null 2>&1 || got=$?
  if [ "$got" -eq "$want" ]; then report ok "gate: $name"; else report FAIL "gate: $name (exit $got, want $want)"; fi
}

#     name                                  want  needs: changes checks backend frontend  filters: backend frontend stack
gate "docs only, all skipped"                 0 "$(needs success success skipped skipped false false false)"
gate "everything ran and passed"              0 "$(needs success success success success true true true)"
gate "backend only, frontend skipped"         0 "$(needs success success success skipped true false true)"
gate "frontend only, backend skipped"         0 "$(needs success success skipped success false true true)"
gate "ran although its filter was false"      0 "$(needs success success success success false false false)"
gate "backend skipped, filter true"           1 "$(needs success success skipped skipped true false false)"
gate "frontend skipped, filter true"          1 "$(needs success success skipped skipped false true false)"
gate "backend failed"                         1 "$(needs success success failure skipped true false false)"
gate "backend cancelled"                      1 "$(needs success success cancelled skipped true false false)"
gate "frontend failed"                        1 "$(needs success success skipped failure false true false)"
gate "failed although its filter was false"   1 "$(needs success success failure skipped false false false)"
gate "checks failed"                          1 "$(needs success failure skipped skipped false false false)"
gate "checks skipped"                         1 "$(needs success skipped skipped skipped false false false)"
gate "checks cancelled"                       1 "$(needs success cancelled skipped skipped false false false)"
gate "changes failed"                         1 "$(needs failure success skipped skipped '' '' '')"
gate "changes skipped"                        1 "$(needs skipped success skipped skipped '' '' '')"
gate "filter output missing"                  1 "$(needs success success skipped skipped '' '' '')"
gate "job missing from needs"                 1 "$(needs success success skipped skipped false false false | jq -c 'del(.frontend)')"
gate "empty needs"                            1 "{}"
gate 'NEEDS is a string'                      1 '"abcd"' "a b c d"
gate 'NEEDS is a number'                      1 '4' "a b c d"
gate 'NEEDS is an array'                      1 '[]' ""
gate 'NEEDS is null'                          1 'null' ""
gate 'NEEDS is an object of strings'          1 '{"backend":"x","changes":"x","checks":"x","frontend":"x"}' "a b c d"
gate 'NEEDS is malformed JSON'                1 '{' "a"
# Value-level mutations of a valid docs-only input: each must fail the gate. The results list
# is passed as four words so the count check cannot be what rejects them.
docs="$(needs success success skipped skipped false false false)"
four="success success skipped skipped"
mutated() { jq -c "$1" <<< "$docs"; }
gate "filter is boolean false"                1 "$(mutated '.changes.outputs.backend = false')" "$four"
gate "filter is number 0"                     1 "$(mutated '.changes.outputs.backend = 0')" "$four"
gate "filter is False"                        1 "$(mutated '.changes.outputs.backend = "False"')" "$four"
gate "filter has a trailing space"            1 "$(mutated '.changes.outputs.frontend = "false "')" "$four"
gate "filter is false then a fake job line"   1 "$(mutated '.changes.outputs.frontend = "false\nx success"')" "$four"
gate "filter is two words"                    1 "$(mutated '.changes.outputs.backend = "false true"')" "$four"
gate "filter is an array"                     1 "$(mutated '.changes.outputs.backend = ["false"]')" "$four"
gate "outputs is a string"                    1 "$(mutated '.changes.outputs = "x"')" "$four"
gate "outputs is missing"                     1 "$(mutated 'del(.changes.outputs)')" "$four"
gate "result is null"                         1 "$(mutated '.checks.result = null')" "$four"
gate "result is missing"                      1 "$(mutated 'del(.checks.result)')" "$four"
gate "result is Success"                      1 "$(mutated '.checks.result = "Success"')" "$four"
gate "result is a boolean"                    1 "$(mutated '.checks.result = true')" "$four"
gate "result is success x"                    1 "$(mutated '.checks.result = "success x"')" "$four"
gate "backend missing, crafted changes result" 1 "$(mutated 'del(.backend) | .changes.result = "success\nbackend success"')" "$four"
gate "job value is a string"                  1 "$(mutated '.checks = "success"')" "$four"
gate "job value is null"                      1 "$(mutated '.checks = null')" "$four"
gate "job value is an array"                  1 "$(mutated '.checks = []')" "$four"
gate "extra job"                              1 "$(mutated '.extra = {"result": "success", "outputs": {}}')" "$four success"
gate "renamed job Frontend"                   1 "$(mutated 'del(.frontend) | .Frontend = {"result": "success"}')" "$four"
gate "job name with a space"                  1 "$(mutated 'del(.backend) | .["backend x"] = {"result": "success"}')" "$four"
gate "job name with a newline"                1 "$(mutated 'del(.backend) | .["zz\nbackend"] = {"result": "success"}')" "$four"
gate "two JSON documents"                     1 "$docs $docs" "$four"
gate "NEEDS is whitespace"                    1 "   " "$four"
gate "valid input still passes"               0 "$docs" "$four"

# The filter-value validation must be what rejects a bad filter, not only a later step: each
# case fails with that check's message. (Without the check the gate still fails closed, so the
# exit status alone can't show the check exists.)
# gate_says <name> <message> <needs json> <results>
gate_says() {
  local name="$1" msg="$2" json="$3" results="$4" out got=0
  out="$(NEEDS="$json" RESULTS="$results" "$here/ci-gate.sh" 2>&1)" || got=$?
  if [ "$got" -eq 1 ] && grep -qF "$msg" <<< "$out"; then report ok "gate says: $name"; else report FAIL "gate says: $name (exit $got)"; fi
}
bad_filter="every filter output must be the string true or false"
gate_says "filter is boolean false"   "$bad_filter" "$(mutated '.changes.outputs.backend = false')" "$four"
gate_says "filter is False"           "$bad_filter" "$(mutated '.changes.outputs.frontend = "False"')" "$four"
gate_says "filter is missing"         "$bad_filter" "$(mutated 'del(.changes.outputs.backend)')" "$four"
gate_says "filter is an array"        "$bad_filter" "$(mutated '.changes.outputs.frontend = ["false"]')" "$four"
gate "result count mismatch"                  1 "$docs" "success success"
gate "three results for four jobs"            1 "$docs" "success success skipped"

# changes <name> <event> <"file file ..."> <expected "backend frontend stack all">
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git -C "$tmp" init -q
git -C "$tmp" config user.email t@example.com
git -C "$tmp" config user.name t
git -C "$tmp" commit -q --allow-empty -m base
base="$(git -C "$tmp" rev-parse HEAD)"
changes() {
  local name="$1" event="$2" files="$3" want="$4" f head out
  git -C "$tmp" reset -q --hard "$base"
  for f in $files; do
    mkdir -p "$tmp/$(dirname "$f")"
    echo "$name" > "$tmp/$f"
  done
  git -C "$tmp" add -A
  git -C "$tmp" commit -q --allow-empty -m change
  head="$(git -C "$tmp" rev-parse HEAD)"
  out="$(cd "$tmp" && env -u GITHUB_OUTPUT EVENT_NAME="$event" BASE_SHA="$base" HEAD_SHA="$head" "$here/ci-changes.sh" 2> /dev/null \
    | cut -d= -f2 | tr '\n' ' ')"
  if [ "$out" = "$want " ]; then report ok "changes: $name"; else report FAIL "changes: $name (got '$out', want '$want')"; fi
}

#       name                event             files                                 backend frontend stack all
changes "docs only"         pull_request      "README.md docs/x.md AGENTS.md"       "false false false false"
changes "backend only"      pull_request      "backend/app/x.py"                    "true false true false"
changes "frontend only"     pull_request      "frontend/src/a.ts"                   "false true true false"
changes "e2e only"          pull_request      "e2e/tests/a.ts"                      "false false true false"
changes "Dockerfile"        pull_request      "Dockerfile"                          "false false true false"
changes "compose file"      pull_request      "compose.e2e.yaml"                    "false false true false"
changes "docker files"      pull_request      "docker/nginx/x.conf"                 "false false true false"
changes "db init script"    pull_request      "docker/postgres/init/10-roles.sql"   "true false true false"
changes "deploy script"     pull_request      "deploy/ownvitals-deploy"             "true false false false"
changes "worktree script"   pull_request      "scripts/worktree.sh"                 "false false true false"
changes "ci-gate script"    pull_request      "scripts/ci-gate.sh"                  "true true true true"
changes "ci-changes script" pull_request      "scripts/ci-changes.sh"               "true true true true"
changes "test script"       pull_request      "scripts/test-ci-gate.sh"             "true true true true"
changes ".gitattributes"    pull_request      ".gitattributes"                      "true true true true"
changes "unrelated script"  pull_request      "scripts/other.sh"                    "false false false false"
changes ".env.example"      pull_request      ".env.example"                        "false false true false"
changes ".trivyignore"      pull_request      ".trivyignore"                        "false false true false"
changes ".github changes"   pull_request      ".github/workflows/ci.yml"            "true true true true"
changes "schedule"          schedule          "README.md"                           "true true true true"
changes "manual dispatch"   workflow_dispatch "README.md"                           "true true true true"
changes "unknown event"     push              "README.md"                           "true true true true"

# Paths that need care. changes_git <name> <expected> <setup function>: the function runs in the
# scratch repo on top of a base commit that holds backend/app/x.py and docs/y.md.
changes_git() {
  local name="$1" want="$2" setup="$3" head out
  git -C "$tmp" reset -q --hard "$base"
  git -C "$tmp" clean -qfd
  mkdir -p "$tmp/backend/app" "$tmp/docs"
  echo base > "$tmp/backend/app/x.py"
  echo base > "$tmp/docs/y.md"
  git -C "$tmp" add -A
  git -C "$tmp" commit -q -m seed
  local seed
  seed="$(git -C "$tmp" rev-parse HEAD)"
  (cd "$tmp" && "$setup")
  git -C "$tmp" add -A
  git -C "$tmp" commit -q --allow-empty -m change
  head="$(git -C "$tmp" rev-parse HEAD)"
  out="$(cd "$tmp" && env -u GITHUB_OUTPUT EVENT_NAME=pull_request BASE_SHA="$seed" HEAD_SHA="$head" \
    "$here/ci-changes.sh" 2> /dev/null | cut -d= -f2 | tr '\n' ' ')"
  if [ "$out" = "$want " ]; then report ok "changes: $name"; else report FAIL "changes: $name (got '$out', want '$want')"; fi
}
mv_out_of_backend() { git mv backend/app/x.py docs/x.py; }
mv_backend_to_frontend() { mkdir -p frontend/src && git mv backend/app/x.py frontend/src/x.py; }
add_non_ascii() { echo a > "backend/app/$(printf '\303\245').py"; }
add_quote_name() { echo a > 'backend/app/q"x.py'; }
add_newline_name() { echo a > "backend/app/a"$'\n'"b.py"; }
add_backslash_name() { mkdir -p frontend && echo a > 'frontend/a\b.ts'; }
add_docs_non_ascii() { echo a > "docs/$(printf '\303\245').md"; }
changes_git "move out of backend/"          "true false true false" mv_out_of_backend
changes_git "move backend/ to frontend/"    "true true true false"  mv_backend_to_frontend
changes_git "non-ASCII name in backend/"    "true false true false" add_non_ascii
# Windows file systems refuse double quotes, newlines and backslashes in names.
case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN*) ;;
  *)
    changes_git "double quote in backend/ name" "true false true false" add_quote_name
    changes_git "newline in backend/ name"      "true false true false" add_newline_name
    changes_git "backslash in frontend/ name"   "false true true false" add_backslash_name
    ;;
esac
changes_git "non-ASCII name in docs/"       "false false false false" add_docs_non_ascii

# The diff cannot be computed (unknown or missing SHA): everything runs, never "nothing changed".
fail_closed() { # fail_closed <name> <env assignments...>
  local name="$1" out
  shift
  out="$(cd "$tmp" && env -u GITHUB_OUTPUT -u BASE_SHA -u HEAD_SHA "$@" "$here/ci-changes.sh" 2> /dev/null | cut -d= -f2 | tr '\n' ' ')"
  if [ "$out" = "true true true true " ]; then report ok "changes: $name"; else report FAIL "changes: $name (got '$out')"; fi
}
fail_closed "unknown base runs everything" EVENT_NAME=pull_request BASE_SHA=0000000000000000000000000000000000000001 HEAD_SHA=HEAD
fail_closed "unknown head runs everything" EVENT_NAME=pull_request BASE_SHA=HEAD HEAD_SHA=0000000000000000000000000000000000000002
fail_closed "no base runs everything"      EVENT_NAME=pull_request HEAD_SHA=HEAD
fail_closed "no head runs everything"      EVENT_NAME=pull_request BASE_SHA=HEAD
fail_closed "empty SHAs run everything"    EVENT_NAME=pull_request BASE_SHA= HEAD_SHA=

# The gate's job list matches `needs` of the `ci` job in the workflow. Needs yq (on the CI
# runners; skipped locally without it).
if command -v yq > /dev/null; then
  wf="$(yq -r '.jobs.ci.needs | sort | join(" ")' "$here/../.github/workflows/ci.yml")"
  gs="$(sed -n "s/^JOBS='\(.*\)'$/\1/p" "$here/ci-gate.sh" | jq -r 'sort | join(" ")')"
  if [ -n "$wf" ] && [ "$wf" = "$gs" ]; then report ok "gate jobs match ci.yml needs ($wf)"; else report FAIL "gate jobs '$gs' differ from ci.yml needs '$wf'"; fi
elif [ -n "${CI:-}" ]; then
  report FAIL "yq is missing on the CI runner"
else
  echo "skip: no yq here (CI has it)"
fi

# scripts/ci-uv-version.sh on scratch trees: the Dockerfile's uv tag, the setup-uv steps and the
# uv commands in docs/dev/backend.md must agree. Needs yq (on the CI runners; skipped locally
# without it).
uvdir="$tmp/uv"
# uv_tree <Dockerfile tag> <setup-uv version> <docs version>: a consistent tree when all three match
uv_tree() {
  rm -rf "$uvdir"
  mkdir -p "$uvdir/.github/workflows" "$uvdir/docs/dev"
  printf 'FROM ghcr.io/astral-sh/uv:%s AS uv\nFROM python:3.12 AS runtime\n' "$1" > "$uvdir/Dockerfile"
  cat > "$uvdir/.github/workflows/ci.yml" <<EOF
name: CI
on: pull_request
jobs:
  backend:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa # v1.0.0
      - uses: astral-sh/setup-uv@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb # v1.0.0
        with:
          version: $2
EOF
  printf 'CI runs the commands below with uv %s.\n\n    uvx uv@%s sync --locked\n' "$3" "$3" > "$uvdir/docs/dev/backend.md"
}
# uv_case <name> <expected exit> <text the output must hold, or empty>
uv_case() {
  local name="$1" want="$2" msg="$3" out got=0
  out="$(cd "$uvdir" && "$here/ci-uv-version.sh" 2>&1)" || got=$?
  if [ "$got" -eq "$want" ] && { [ -z "$msg" ] || grep -qF -- "$msg" <<< "$out"; }; then
    report ok "uv version: $name"
  else
    report FAIL "uv version: $name (exit $got, want $want)"
  fi
}
if command -v yq > /dev/null; then
  uv_tree 0.13.6 0.13.6 0.13.6
  uv_case "all agree" 0 "uv 0.13.6 everywhere"

  uv_tree 0.13.6 0.13.7 0.13.6
  uv_case "setup-uv differs from the Dockerfile" 1 "setup-uv version '0.13.7' is not the Dockerfile uv tag 0.13.6"
  # shellcheck disable=SC2016  # a literal GitHub expression, not shell
  uv_tree 0.13.6 '${{ env.UV_VERSION }}' 0.13.6
  uv_case "setup-uv version is computed" 1 "is not the Dockerfile uv tag"
  uv_tree 0.13.6 0.13 0.13.6
  uv_case "setup-uv version is a number" 1 "is not the Dockerfile uv tag"
  uv_tree 0.13.6 0.13.6 0.13.6
  sed -i 's/^          version: 0.13.6$/          version-file: uv.toml/' "$uvdir/.github/workflows/ci.yml"
  uv_case "setup-uv uses version-file" 1 "must not set version-file"
  uv_tree 0.13.6 0.13.6 0.13.6
  sed -i '/setup-uv/,$d' "$uvdir/.github/workflows/ci.yml"
  uv_case "no setup-uv step" 1 "no setup-uv step found"
  uv_tree 0.13.6 0.13.6 0.13.6
  mkdir -p "$uvdir/.github/actions/x"
  printf 'name: x\nruns:\n  using: composite\n  steps:\n    - uses: astral-sh/setup-uv@cccccccccccccccccccccccccccccccccccccccc # v1.0.0\n      with:\n        version: 0.13.5\n' \
    > "$uvdir/.github/actions/x/action.yml"
  uv_case "a composite action differs" 1 "(composite action)"
  uv_tree 0.13.6 0.13.6 0.13.6
  printf '  frontend:\n    runs-on: ubuntu-24.04\n    steps:\n      - uses: astral-sh/setup-uv@dddddddddddddddddddddddddddddddddddddddd # v1.0.0\n        with:\n          version: 0.13.5\n' \
    >> "$uvdir/.github/workflows/ci.yml"
  uv_case "a second job differs" 1 "job 'frontend'"

  uv_tree 0.13.7 0.13.6 0.13.6
  uv_case "Dockerfile differs from both" 1 "is not the Dockerfile uv tag 0.13.7"
  uv_tree 0.13.6 0.13.6 0.13.6
  sed -i 's/uv:0.13.6/uv:latest/' "$uvdir/Dockerfile"
  uv_case "Dockerfile has no uv tag" 1 "no uv tag found in the Dockerfile"
  uv_tree 0.13.6 0.13.6 0.13.6
  echo 'FROM ghcr.io/astral-sh/uv:0.13.5 AS uv2' >> "$uvdir/Dockerfile"
  uv_case "Dockerfile has two uv tags" 1 "more than one uv tag"
  # Pinned by digest (uv:X.Y.Z@sha256:<64 hex>): the tag is still what is compared.
  d64=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  uv_tree "0.13.6@sha256:$d64" 0.13.6 0.13.6
  uv_case "Dockerfile pinned by digest, all agree" 0 "uv 0.13.6 everywhere"
  uv_tree "0.13.7@sha256:$d64" 0.13.6 0.13.6
  uv_case "Dockerfile pinned by digest, tag differs" 1 "is not the Dockerfile uv tag 0.13.7"
  uv_tree "0.13.6@sha256:${d64%a}" 0.13.6 0.13.6
  uv_case "Dockerfile digest too short" 1 "no uv tag found in the Dockerfile"
  uv_tree "0.13.6@sha256:${d64^^}" 0.13.6 0.13.6
  uv_case "Dockerfile digest in upper case" 1 "no uv tag found in the Dockerfile"
  uv_tree "0.13.6@sha512:$d64" 0.13.6 0.13.6
  uv_case "Dockerfile digest not sha256" 1 "no uv tag found in the Dockerfile"

  uv_tree 0.13.6 0.13.6 0.13.7
  uv_case "docs differ in both references" 1 "is not the Dockerfile uv tag 0.13.6"
  uv_tree 0.13.6 0.13.6 0.13.6
  sed -i 's/uvx uv@0.13.6/uvx uv@0.13.5/' "$uvdir/docs/dev/backend.md"
  uv_case "docs differ in a uvx command only" 1 "'uv@0.13.5' is not the Dockerfile uv tag"
  uv_tree 0.13.6 0.13.6 0.13.6
  sed -i 's/with uv 0.13.6/with uv 0.13.5/' "$uvdir/docs/dev/backend.md"
  uv_case "docs differ in the sentence only" 1 "'with uv 0.13.5' is not the Dockerfile uv tag"
  uv_tree 0.13.6 0.13.6 0.13.6
  echo 'No version here.' > "$uvdir/docs/dev/backend.md"
  uv_case "docs name no uv version" 1 "no uv version found"
  uv_tree 0.13.6 0.13.6 0.13.6
  rm "$uvdir/docs/dev/backend.md"
  uv_case "docs file is missing" 1 "docs/dev/backend.md is missing"
elif [ -n "${CI:-}" ]; then
  report FAIL "yq is missing on the CI runner"
else
  echo "skip: no yq here (CI has it)"
fi

[ "$failures" -eq 0 ] || { echo "$failures test(s) failed"; exit 1; }
echo "all tests passed"
